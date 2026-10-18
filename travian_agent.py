"""
Travian (ts11.x1.international) 資源田 / 建城決策（不含網路、不含 Sheet）
- 頁面解析在 travian_pages.py、常數在 travian_const.py、資料結構在 travian_models.py（不再重新匯出，呼叫端請直接 import）
- plan():            舊流程，資源田 + 倉庫/糧倉的規則決策（不用 LLM）
- plan_with_order(): 依 build_order 建城的規則決策
- 可變設定（TARGET_LEVEL、QUEUE_SLOTS、IS_CAPITAL）由 runner.py 每輪賦值，必須留在這個模組
- 抓頁面與送出升級在 travian_ci_run.py / auto_build.py；流程在 runner.py
"""
import re

from travian_const import (
    BUILDING_GIDS, DEFAULT_TARGET_BUILDING_LEVEL, FREE_SLOT_RANGE, MAX_NON_CAPITAL_FIELD_LEVEL,
    RESOURCE_FIELD_PRODUCTION, ROMAN_TRIBE_ID, TRIBE_NAMES,
)
from travian_models import Field, State
from travian_pages import fetch_build_options, parse_building_cost, parse_dorf2_slots

# ---------------- 設定 ----------------
TARGET_LEVEL = 2          # 資源田先升到這個等級
QUEUE_SLOTS = 2           # 預設值；runner._setup_player() 每輪依種族 / Plus 自動偵測後賦值（Config 可手動覆蓋）

# 0 = 停用村莊中心建築自動升級（預設，因為只是剛驗證過解析，還沒實際跑過）。
# 要啟用時設一個 >0 的目標等級即可；Sheet 的 Config 分頁可用
# target_building_level 這個 key 覆蓋（見 runner.py 的 load_config()）。
TARGET_BUILDING_LEVEL = DEFAULT_TARGET_BUILDING_LEVEL


IS_CAPITAL = False   # Config 可用 is_capital 覆蓋（見 runner.py）；不確定就留 False


def _roi_key(f):
    """排序鍵：(每花費一單位資源能換到的每小時新增產量的倒數, 工期)，越小越優先。
    也就是「花費 ÷ 每小時新增產量」，比值越小代表 CP/資源投報率越好，愈早升愈划算。
    查不到這個等級的產量資料（超出上面那張表的範圍）時，退回舊規則（總花費、工期）,
    確保排序永遠有結果，不會因為缺資料而卡住或報錯。"""
    gain = RESOURCE_FIELD_PRODUCTION.get(f.level + 1)
    base = RESOURCE_FIELD_PRODUCTION.get(f.level)
    cost = sum(f.cost)
    if f.gid not in (1, 2, 3, 4) or gain is None or base is None or gain <= base or cost <= 0:
        return (2, cost, f.secs)        # 排在「有 ROI 資料」的後面，但仍是穩定排序
    return (1, cost / (gain - base), f.secs)


def queue_limit(tribe_id: int, plus_active: bool) -> int:
    """施工佇列長度的推估規則：羅馬族基本 2 條（資源田、建築各一）、其他種族 1 條；
    Plus 有效再多 1 格「等待佇列」（排隊等前一項完成，不是並行）。
    依據：官方說明只把同時施工兩項列為羅馬族的種族加成；Plus 的多排一格對非羅馬族的效果官方頁沒寫清楚，
    只有你這個帳號 Plus 有效時 dorf2 曾同時列出兩項施工可以佐證。所以這是推估，Config 的 queue_slots
    填數字就會蓋過它。羅馬族的兩條是「各自一條線」，這裡只算總數，沒有分線（見 README）。"""
    return (2 if tribe_id == ROMAN_TRIBE_ID else 1) + (1 if plus_active else 0)


def resolve_queue_slots(manual, info):
    """回傳 (佇列長度, 說明文字)。優先順序：Config 手動數字 > 自動偵測 > 保守值 1。"""
    if manual is not None:
        return manual, "Config 手動指定"
    if info and info.get("tribe_id") is not None and info.get("plus_active") is not None:
        tid, plus = info["tribe_id"], info["plus_active"]
        return queue_limit(tid, plus), f"自動偵測：{TRIBE_NAMES.get(tid, f'種族 {tid}')}、Plus {'有效' if plus else '未啟用'}"
    return 1, "偵測不到種族/Plus，保守用 1"


# ---------------- 決策 ----------------
def plan(st: State, affordable_only: bool = False):
    """回傳 (動作, 參數...)。動作: build / wait_res / wait_queue / need_storage / done
    affordable_only=True：只在「現有資源就付得起」的候選裡挑 ROI 最好的；沒有付得起的就回 (\"no_affordable\",)。
    （來襲時趁資源被搶前用掉，由 runner 在轉移補不到資源時呼叫；預設 False＝原行為完全不變）"""
    if len(st.queue) >= QUEUE_SLOTS:
        return ("wait_queue", min(st.queue) + 5)

    # 一般村莊資源田上限 10 級（首都無上限，見 MAX_NON_CAPITAL_FIELD_LEVEL 的說明）。
    eff_target = TARGET_LEVEL if IS_CAPITAL else min(TARGET_LEVEL, MAX_NON_CAPITAL_FIELD_LEVEL)
    # 村莊中心建築（gid 在 BUILDING_GIDS 裡）由呼叫端先用它們自己的
    # target_building_level 篩過等級，這裡不再套用資源田的 TARGET_LEVEL，
    # 只需要成本非空、沒有正在施工即可。
    cands = [f for f in st.fields if f.cost and not f.building
             and (f.gid in BUILDING_GIDS or f.level < eff_target)]
    if not cands:
        return ("done",)
    if affordable_only:
        cands = [f for f in cands if all(c <= s for c, s in zip(f.cost, st.stock))]
        if not cands:
            return ("no_affordable",)

    # 糧食淨產出太低時優先升農田
    pool = [f for f in cands if f.gid == 4] if st.free_crop < 5 else cands
    pool = pool or cands
    f = min(pool, key=_roi_key)     # ROI（花費÷每小時新增產量）最好的先；建築沒有產量表，退回花費排序

    if any(c > m for c, m in zip(f.cost, st.cap)):
        return ("need_storage", f)                          # 成本超過倉庫容量，要先升倉庫/糧倉

    short = [max(0, c - s) for c, s in zip(f.cost, st.stock)]
    if any(short):
        secs = max(sh / max(p, 1) * 3600 for sh, p in zip(short, st.prod))
        return ("wait_res", int(secs) + 30, f)
    return ("build", f)


# ---------------- 建城順序 ----------------


def parse_build_order(text: str) -> list:
    """把 'res:2,15:3,10:3,17:1' 轉成 [('res', 2), (15, 3), (10, 3), (17, 1)]。
      res:N   所有資源田都升到 N 級
      1~4:N   只有木/磚/鐵/糧其中一種的所有田升到 N 級
      其他數字:N  該 gid 的建築升到 N 級（沒有就蓋一棟）。gid 見 README：
              15 村莊大樓、10 倉庫、11 糧倉、17 市場、18 大使館、25 行宮、22 研究院 ...
    分隔符號可以是逗號、空白、換行。格式不對會丟 ValueError（呼叫端要接住、不要亂猜）。"""
    goals = []
    for tok in re.split(r"[,\s，;；]+", (text or "").strip()):
        if not tok:
            continue
        m = re.fullmatch(r"(res|資源田|\d+)\s*[:：]\s*(\d+)", tok, re.I)
        if not m:
            raise ValueError(f"看不懂 {tok!r}，格式應該像 res:2 或 15:3")
        k = m.group(1)
        goals.append((int(k) if k.isdigit() else "res", int(m.group(2))))
    return goals


def plan_with_order(st: State, order: list, get, touched=(), start=0, _trace=None):
    """依建城順序決定下一步。跟 plan() 回傳同樣的動作（但不會回傳 need_storage：
    成本超過容量時會自動改成先升倉庫/糧倉，補不了才回 blocked），另外多兩種：
      ("blocked", 說明)   計畫卡住了（例如被「不能已經有某棟」擋住），需要人看一下
      ("no_slot", gid)    要蓋新建築但村莊沒有空地了
    規則：由上到下，第一個「還沒達成」的目標就是現在要做的；它還在等資源/施工，
    後面的目標就不動（嚴格照順序）。要蓋的建築缺前置條件時，自動改成先補條件。
    get(path) -> html 由呼叫端提供（requests 或 Playwright），需要時才會呼叫：
    只有順序裡有「資源田以外」的目標才會多抓 dorf2 / build.php。
    start / _trace（v2.36 新增，plan_fill_ins 專用；預設值下行為和舊版完全相同）：
      start   從 order[start:] 開始找第一個還沒達成的目標（前面的目標當作已達成，不檢查）
      _trace  傳入 list 時，會 append「回傳動作對應的 order 索引」（全部達成＝len(order)；
              佇列滿而提早回傳 wait_queue 時不 append）"""
    touched = set(touched)
    if len(st.queue) >= QUEUE_SLOTS:
        return ("wait_queue", min(st.queue) + 5)
    cache = {}

    def q_wait():
        return ("wait_queue", (min(st.queue) + 5) if st.queue else 600)

    def slots():
        if "slots" not in cache:
            cache["slots"] = parse_dorf2_slots(get("dorf2.php"))
        return cache["slots"]

    def options():
        """抓一次空地的「建造新的建築物」頁面（每個空地看到的清單一樣）。"""
        if "opts" not in cache:
            lo, hi = FREE_SLOT_RANGE
            empty = sorted(s.aid for s in slots()
                           if s.gid == 0 and lo <= s.aid <= hi and s.aid not in touched)
            cache["empty"] = empty[0] if empty else None
            cache["opts"] = fetch_build_options(get, empty[0]) if empty else []
        return cache["opts"], cache["empty"]

    fixing = set()      # 正在補的儲存建築 gid，防止「補倉庫的成本又超過容量」無限遞迴

    def storage_fix(f):
        """成本超過容量：木/磚/鐵超過 → 先處理倉庫(gid 10)；只有穀物超過 → 先處理糧倉(gid 11)。
        做法是把該儲存建築升一級（沒有就蓋一棟），下一輪容量變大再重新評估。"""
        over_res = any(c > m for c, m in zip(f.cost[:3], st.cap[:3]))
        gid, name = (10, "倉庫") if over_res else (11, "糧倉")
        if gid in fixing:
            return ("blocked", f"{name}自己的升級成本就超過容量，無法自動補救")
        lvl = max((s.level for s in slots() if s.gid == gid), default=0)
        if lvl >= 20:
            return ("blocked", f"成本超過{name}容量，但{name}已滿級")
        fixing.add(gid)
        try:
            r = resolve(gid, lvl + 1)
        finally:
            fixing.discard(gid)
        return r if r is not None else ("blocked", f"成本超過{name}容量，但{name}已達目標等級（資料不一致）")

    def decide(f):
        if any(c > m for c, m in zip(f.cost, st.cap)):
            return storage_fix(f)
        short = [max(0, c - s) for c, s in zip(f.cost, st.stock)]
        if any(short):
            secs = max(sh / max(p, 1) * 3600 for sh, p in zip(short, st.prod))
            return ("wait_res", int(secs) + 30, f)
        return ("build", f)

    def fields_action(gids, level, need_all):
        # 資源田（gid 1~4）套用一般村莊 10 級上限；村莊中心建築（gid 不在 1~4）不受此限制。
        if gids == (1, 2, 3, 4) and not IS_CAPITAL:
            level = min(level, MAX_NON_CAPITAL_FIELD_LEVEL)
        fs = [f for f in st.fields if f.gid in gids]
        if not need_all and any(f.level >= level for f in fs):
            return None                       # 前置條件只要「有一塊」達標就算
        todo = [f for f in fs if f.level < level]
        if not todo:
            return None
        avail = [f for f in todo if f.cost and not f.building and f.aid not in touched]
        if not avail:
            if any(f.building or f.aid in touched for f in todo):
                return q_wait()
            return ("blocked", "資源田的升級成本讀不到（頁面 title 被前端 JS 清空？）")
        pool = [f for f in avail if f.gid == 4] if st.free_crop < 5 else avail
        pool = pool or avail
        if need_all:
            f = min(pool, key=_roi_key)   # res:N 這種「全部升到位」的目標：ROI 最好的先
        else:
            f = min(pool, key=lambda x: (-x.level, sum(x.cost), x.secs))   # 補條件：挑最接近的
        return decide(f)

    def resolve(gid, level, depth=0, need_all=False):
        """回傳 None = 這個目標已達成；否則回傳要做的動作。"""
        if depth > 3:
            return ("blocked", f"前置條件套了太多層（卡在 gid {gid}）")
        if gid in (1, 2, 3, 4):
            return fields_action((gid,), level, need_all)

        mine = [s for s in slots() if s.gid == gid]
        if mine:                                              # 已經有：升級
            best = max(mine, key=lambda s: s.level)
            if best.level >= level:
                return None
            if best.building or best.aid in touched:
                return q_wait()
            cost, secs = parse_building_cost(get(f"build.php?id={best.aid}"))
            if not cost:
                return q_wait() if st.queue else (
                    "blocked", f"{BUILDING_GIDS.get(gid, gid)} (aid={best.aid}) 讀不到升級成本（已達最高等級？）")
            return decide(Field(aid=best.aid, gid=gid, level=best.level, cost=cost, secs=secs, building=False))

        if any(s.aid in touched and s.gid == 0 for s in slots()):
            return q_wait()        # 本輪剛送出一個新建築、dorf2 還沒反映（乾跑模式就是這樣）：不要重複蓋
        opts, empty = options()                               # 還沒有：蓋新的
        if empty is None:
            return ("no_slot", gid)
        entry = next((o for o in opts if o["gid"] == gid), None)
        if entry is None:
            return ("blocked", f"gid {gid} 不在可蓋清單裡（種族不能蓋、已達數量上限，或空地頁面讀取失敗）")
        f = Field(aid=empty, gid=gid, level=0, cost=entry["cost"], secs=entry["secs"],
                  building=False, url=entry["url"] or "")
        if entry["buildable"]:
            return decide(f)
        unmet = [c for c in entry["conditions"] if not c["met"]]
        for c in unmet:
            if c["forbidden"]:
                return ("blocked", f"{entry['name']} 被 {c['name']} 擋住（村莊裡不能已經有它）")
        for c in unmet:                                       # 先補前置條件
            r = resolve(c["gid"], c["level"], depth + 1)
            if r is not None:
                return r
        if unmet:
            return q_wait() if st.queue else (
                "blocked", f"{entry['name']}：頁面說條件沒達成，但村莊資料看起來已達成")
        r = decide(f)                                         # 條件沒問題：可能只是資源不足
        if r[0] == "build":
            return ("blocked", f"{entry['name']} 沒有綠色按鈕、也沒有未達成的條件（原因不明）")
        return r

    for idx, (key, level) in enumerate(order):
        if idx < start:
            continue
        r = fields_action((1, 2, 3, 4), level, True) if key == "res" else resolve(key, level, need_all=True)
        if r is not None:
            if _trace is not None:
                _trace.append(idx)
            return r
    if _trace is not None:
        _trace.append(len(order))
    return ("done",)


def plan_fill_ins(st: State, order: list, get, touched=(), max_scan: int = 4):
    """主目標（build_order 裡第一個還沒達成的）是 wait_res 時，列出後面目標中「現在就付得起」的候選，給 runner 判斷能不能插隊。
    回傳 (主目標動作, [Field...])；主目標不是 wait_res（或佇列滿）時候選是空 list。
    候選依 build_order 的順序排列，最多往後檢查 max_scan 個未達成的目標；同一輪內相同頁面只抓一次；
    和主目標同一格（aid）的候選會被排除。純規則、不改 st（plan_with_order 本來就不改 st）。"""
    cache = {}

    def cget(path):
        if path not in cache:
            cache[path] = get(path)
        return cache[path]

    trace = []
    main = plan_with_order(st, order, cget, touched, _trace=trace)
    if main[0] != "wait_res" or not trace:
        return main, []
    main_aid = main[2].aid
    cands = []
    j, scanned = trace[-1] + 1, 0
    while j < len(order) and scanned < max_scan:
        t = []
        r = plan_with_order(st, order, cget, touched, start=j, _trace=t)
        if r[0] == "done" or not t:
            break
        scanned += 1
        if r[0] == "build" and r[1].aid != main_aid and all(r[1].aid != c.aid for c in cands):
            cands.append(r[1])
        j = t[-1] + 1
    return main, cands
