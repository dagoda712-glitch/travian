#!/usr/bin/env python3
"""派兵計算器 v1.6：搶空綠洲讓兵避難（只做計算，不會自動操作遊戲）

v1.7 變更（v2.86）：內建 OASES／BLOCKED 只在家＝(-28|-72) 時有效；configure(home=其他座標) 會把它們清空（埃及人舊伺服器那邊
曾因退回內建清單而拿新伺服器的座標去驗證，全部「不安全」）。清空後沒有候選綠洲，hide_troops 會 skip（no_oasis），不派兵。

v1.6 變更（v2.85，族群通用）：兵種表（速度／攻擊力／載重）不再寫死埃及人，改成依族群的 TRIBES 表；
configure(tribe="auto") 會讀 Sheet「state」分頁的 tribe 欄（bot 每輪都會寫，例：高盧），自己判斷族群並把
UNIT_SPEED／UNIT_ATTACK／UNIT_CARRY 就地換成該族群的表（hide_troops 本來就是呼叫時才讀 dispatch.UNIT_*，所以立刻生效）。
讀不到、或族群不在 TRIBES 裡 → 三張表清空（hide_troops 會因「兵種速度未知」整輪不動，不會用錯表派兵）。
沒呼叫 configure 的場合（單元測試、單獨跑 CLI）維持舊預設（埃及人表），行為與 v1.5 相同。
CLI 新增 --tribe（族群名稱或 auto）。

v1.5 變更（v2.82，換伺服器）：家改成 (-28|-72)（doga 的村莊，✓ 區塊 JSON 該格 uid 2713）；HOME／MAP_SIZE 可由 configure() 在執行時覆寫
（travian_ci_run 從 Config home_x／home_y／map_size 讀）；plan 的 home 預設改成「呼叫時才取」；map_id 依 MAP_SIZE 推算半邊長（不再寫死 200）；
舊伺服器的 OASES／BLOCKED 搬到 OASES_OLD_SERVER／BLOCKED_OLD_SERVER（不再使用）；新的 OASES 先放區塊 JSON 快照裡「未佔領、無動物」的 4 個。
地圖大小（401）與伺服器倍速（1）依你的決定「先設定一樣」（✗ 未對新伺服器驗證）。

v1.4 變更：UNIT_SPEED t2 7→6（✓ v2.77：12:32 概況頁 outRaid 剩 3491 秒推出速度 ≤ 6.19，並與 v2.70 單程 3600 秒＝6.00 格÷6 合併）；
新增 UNIT_ATTACK（t1＝10 ✓ 戰報、t2≈30 △ 由 295 反推）與 UNIT_CARRY（t1＝15、t2＝50 ✓ 兩式聯立），供 hide_troops 的「攻擊力 >20」判斷用。

v1.3 變更：UNIT_SPEED 加 t4（探險者索普杜 16 ✓ 10-23 由偵察單程反推）；t2 註明「t2 或英雄是 6」（✓ (199|172) 單程 3600 秒、6.00 格）。

v1.2 變更：伺服器時區改用 Europe/London（夏令時間 10-25 結束後自動變 UTC+0，
不再寫死 UTC+1）；新增 --unit（依兵種取速度，多個兵種取最慢）；-s 改為可省略。

v1.1 變更：距離環繞（地圖 -200～200，寬 401）、候選明確依距離排序、
清單加入東邊候選（10-22 快照）、顯示每個綠洲能撐過幾波、輸出 map id 與預填網址。

用法範例：
    python dispatch.py -a 10:36:43 -a 11:21:33 -s 7 --now 09:43:44
    python dispatch.py -a 10:36:43 -a 11:21:33 --unit t1 --now 09:43:44
    python dispatch.py -a 09:45:10 -a 10:02:33 -s 7 --server-speed 2 --margin 60
    python dispatch.py -a 10:36:43 -a 11:21:33 -s 7 --include-unverified

參數：
    -a / --arrival   來襲到達時間（伺服器時間 HH:MM:SS），可重複填
    -s / --speed     部隊速度（格／小時，基礎值）；與 --unit 擇一
    --tribe          族群名稱（例：高盧）或 auto（讀 Sheet state 分頁）；兵種表依族群（TRIBES）
    --unit           兵種欄位 t1、t2…（可重複；多個取最慢），速度取自目前族群的表
    --server-speed   伺服器速度倍率（預設 1）
    --margin         安全緩衝秒數：兵要比最後一波晚幾秒回家（預設 30）
    --now            手動指定現在的伺服器時間 HH:MM:SS（預設用 Europe/London 的現在時間）
    --include-unverified  也把「未驗證」的綠洲列入建議
"""
import argparse
import math
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:          # Python < 3.9
    ZoneInfo = None

MAP_SIZE = 401                      # 座標 -200..200（新伺服器先設定一樣 ✗）
HOME = (-28, -72)                   # 新伺服器（2026-10-24）。Config home_x／home_y 會覆寫


def configure(home=None, map_size=None, tribe=None, read_state=None):
    """執行時覆寫家座標、地圖大小（奇數；座標範圍 -(n-1)/2..(n-1)/2）與族群。None＝不改。
    tribe："auto"＝讀 state 分頁的 tribe 欄；其他字串＝直接指定名稱（例：高盧、gauls）。
    讀不到或不認得 → 兵種表清空（保守：躲兵會因兵種速度未知而不動），並印出原因。回傳目前族群鍵（可能是 None）。"""
    global HOME, MAP_SIZE
    if map_size is not None:
        if int(map_size) < 3 or int(map_size) % 2 == 0:
            raise ValueError(f"map_size 必須是奇數且 >= 3：{map_size!r}")
        MAP_SIZE = int(map_size)
    if home is not None:
        HOME = (int(home[0]), int(home[1]))
        if HOME == BUILTIN_HOME:
            OASES[:] = _BUILTIN_OASES
            BLOCKED.clear()
            BLOCKED.update(_BUILTIN_BLOCKED)
        else:
            if OASES:
                print(f"[設定] 家 {HOME} 不是內建清單的家 {BUILTIN_HOME}：內建綠洲清單作廢，只用掃描結果")
            OASES.clear()
            BLOCKED.clear()
    if tribe is not None:
        raw = tribe_from_state(read_state) if str(tribe).strip().lower() == "auto" else tribe
        key = resolve_tribe(raw)
        src = "state" if str(tribe).strip().lower() == "auto" else "指定"
        _apply_tribe(key, src)
        if key:
            print(f"[族群] {raw}（{src}）→ {key}，兵種表 {sorted(UNIT_SPEED)}")
        else:
            print(f"[族群] 無法判斷族群（{raw!r}）；兵種表已清空，躲兵不會動")
    return TRIBE


def _half():
    return (MAP_SIZE - 1) // 2


def _server_tz():
    """伺服器時區。頁面設定 timeZone = Europe/London（✓ 10-22 概況頁）：
    夏令時間 UTC+1，10-25 結束後 UTC+0。沒有 tzdata 時退回固定 UTC+1 並警告。"""
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Europe/London")
        except Exception:
            pass
    print("⚠ 找不到 Europe/London 時區資料，暫用固定 UTC+1；"
          "10-25 之後請務必加 --now。")
    return timezone(timedelta(hours=1))


SERVER_TZ = _server_tz()

# ── 兵種表（依族群）──
# 每個兵種：(速度 格/時 伺服器 1 倍, 單兵攻擊力, 單兵載重)；None＝未知（不收進表，hide_troops 會當「未知」處理）。
# 只收步兵／騎兵；攻城器、拓荒者、首領不收（不該拿去避難）。標記：✓ 有實測或官方表，△ 推定／第三方。
# 新族群請在這裡加一筆（名稱別名在 TRIBE_ALIASES）；沒加＝未知族群＝全部兵種視為速度未知、躲兵不動。
# 目前沒有：匈奴、斯巴達、維京（v2.85 查不到完整可靠的表：各來源速度互相矛盾、兵種欄位編號不確定，寧可不填）。
TRIBES = {
    "gauls": {          # 高盧。Travian Kingdoms 說明中心的一般世界兵種表（support.kingdoms.com；注意那是 Kingdoms 的說明頁，
                        # 不是你玩的 Legends——t1～t5 數值與我記憶中的 Legends 一致，△）。新伺服器沒實測，確認頁單程仍是最後核對。
        "t1": (7, 15, 35),      # 方陣兵 Phalanx
        "t2": (6, 65, 45),      # 劍士 Swordsman
        "t3": (17, 0, 0),       # 偵察兵 Pathfinder
        "t4": (19, 90, 75),     # 雷法師 Theutates Thunder
        "t5": (16, 45, 35),     # 德魯伊騎兵 Druidrider
        "t6": (13, 140, 65),    # 海頓聖騎士 Haeduan（一般世界攻擊 140；Summer Mayhem 2026 活動世界是 95，不採用）
    },
    "romans": {         # 羅馬。Travian Kingdoms 說明中心的一般世界兵種表（△ 與 Legends 基礎值一致是我的記憶，新伺服器沒實測）
        "t1": (6, 40, 50),      # 近衛兵 Legionnaire
        "t2": (5, 30, 20),      # 禁衛兵 Praetorian
        "t3": (7, 70, 50),      # 帝國兵 Imperian
        "t4": (16, 0, 0),       # 偵察騎兵 Equites Legati
        "t5": (14, 120, 100),   # 帝國騎兵 Equites Imperatoris
        "t6": (10, 180, 70),    # 皇帝騎兵 Equites Caesaris
    },
    "teutons": {        # 條頓。同上來源與保留
        "t1": (7, 40, 60),      # 棍棒兵 Clubswinger
        "t2": (7, 10, 40),      # 矛兵 Spearfighter
        "t3": (6, 60, 50),      # 斧頭兵 Axefighter
        "t4": (9, 0, 0),        # 偵察兵 Scout
        "t5": (10, 55, 110),    # 騎士 Paladin
        "t6": (9, 150, 80),     # 日耳曼騎士 Teutonic Knight
    },
    "egyptians": {      # 埃及人。舊伺服器實測（換伺服器後沒重驗；新伺服器若是埃及人要再核對）
        "t1": (7, 10, 15),      # 奴隸民兵 ✓（速度 10-23 確認頁；攻擊 04:24 戰報；載重 v2.78 兩式聯立）
        "t2": (6, 30, 50),      # 看守人艾什：速度 ✓ v2.77 上限推算；攻擊 △ 295 反推約 30.6；載重 ✓
        "t3": (7, None, None),  # 斧刀勇士：速度 △
        "t4": (16, None, None), # 探險者索普杜：速度 ✓（偵察 Village 003 反推）
    },
}

# state 分頁 tribe 欄可能的寫法 → TRIBES 的鍵（比對時去空白、轉小寫；「人」「族」結尾會先去掉）
TRIBE_ALIASES = {
    "gauls": ("高盧", "高卢", "gaul", "gauls"),
    "egyptians": ("埃及", "egyptian", "egyptians"),
    "romans": ("羅馬", "罗马", "roman", "romans"),
    "teutons": ("條頓", "条顿", "日耳曼", "teuton", "teutons"),
}

UNIT_SPEED = {}      # 目前族群的表；用 _apply_tribe 就地更新（不要重新指定，hide_troops 等是呼叫時才讀）
UNIT_ATTACK = {}
UNIT_CARRY = {}
TRIBE = None         # 目前採用的族群鍵（TRIBES 的鍵）；None＝未知（表是空的）
TRIBE_SOURCE = "default"


def resolve_tribe(name):
    """state 的 tribe 值（例：高盧）→ TRIBES 的鍵；認不得回 None。"""
    t = str(name or "").strip().casefold().rstrip("人族")
    if not t:
        return None
    for key, names in TRIBE_ALIASES.items():
        if t in (n.casefold().rstrip("人族") for n in names):
            return key
    return None


def _apply_tribe(key, source="configure"):
    """把三張表就地換成 key 的內容；key 是 None 或不在 TRIBES → 三張表清空。"""
    global TRIBE, TRIBE_SOURCE
    units = TRIBES.get(key, {}) if key else {}
    for tbl in (UNIT_SPEED, UNIT_ATTACK, UNIT_CARRY):
        tbl.clear()
    for u, (spd, atk, carry) in units.items():
        if spd is not None:
            UNIT_SPEED[u] = spd
        if atk is not None:
            UNIT_ATTACK[u] = atk
        if carry is not None:
            UNIT_CARRY[u] = carry
    TRIBE = key if units else None
    TRIBE_SOURCE = source
    return TRIBE


def tribe_from_state(read_rows=None):
    """讀 Sheet「state」分頁的 tribe 欄（A 欄 key、B 欄值）→ 原字串（例：高盧）；讀不到回 None，不拋例外。
    read_rows：測試用，回傳 [[key, 值, ...], ...] 的 callable；預設用 sheets_writer 開試算表。"""
    try:
        if read_rows is None:
            import sheets_writer as SW
            if not SW.has_sheet():
                return None
            read_rows = SW.open_sheet().worksheet("state").get_all_values
        for row in read_rows():
            if row and str(row[0]).strip() == "tribe" and len(row) > 1:
                return str(row[1]).strip() or None
    except Exception as e:                                          # noqa: BLE001
        print(f"[族群] 讀 state 分頁失敗：{e!r}")
    return None


_apply_tribe("egyptians", "default")      # 沒人呼叫 configure 時的舊預設（與 v1.5 相同）

# ── 舊伺服器（家 (-196|172)）的快照，換伺服器後作廢，只留作格式參考，程式不再使用 ──
OASES_OLD_SERVER = [
    ((-200, 171), "磚 25%", True),
    ((-190, 174), "鐵 25%", True),
    ((-189, 170), "磚＋穀", True),
    ((-189, 178), "磚＋穀", True),
    ((-190, 180), "鐵 25%", True),
    ((-185, 172), "鐵 25%", True),
    ((-185, 173), "鐵＋穀", True),
    ((-184, 173), "鐵＋穀", True),
    ((-181, 170), "磚 25%", True),
    ((-180, 172), "穀 25%", True),
    ((-180, 173), "穀 25%", True),
    ((-180, 174), "穀 25%", True),
    # 10-22 區塊 JSON 快照（未佔領、無動物）
    ((-198, 180), "木 25%", True),
    ((-200, 180), "磚＋穀 25%", True),
    ((197, 179), "磚 25%（環繞）", True),
    ((195, 178), "木 25%（環繞）", True),
    # 你手動送過，但動物／佔領狀態沒有資料
    ((-196, 167), "荒廢綠洲", False),
]

BLOCKED_OLD_SERVER = {
    (-187, 175), (-191, 171), (-181, 173), (-179, 173), (-184, 172),
    (-183, 168), (-190, 167), (-179, 169), (-179, 167),            # 有動物
    (-181, 172), (-178, 167), (-176, 167), (-178, 165),            # 動物很多
    (-178, 175), (-176, 175), (-184, 166), (-186, 164), (-187, 164),
    (-179, 166), (-178, 166), (-179, 165),                         # 已被佔領
    (-199, 172), (-199, 170), (-199, 168), (-188, 177),            # 附近村莊
    (199, 174), (196, 179), (198, 178), (194, 176),                # 動物（10-22）
    (194, 179), (194, 172),                                        # 已被佔領（10-22）
}


# 候選空綠洲：(座標, 加成, 已驗證)。已驗證＝區塊 JSON 快照「未佔領、無動物」（日期不明，你 10-24 貼的；
# 加成 r3＝鐵、r2＝磚是依 Travian 慣例 △）。人工快照，發兵前一定會即時驗證 tile-details。
# 之後由綠洲掃描（oasis_scan.py）取代。
OASES = [
    ((-29, -71), "鐵 25%", True),
    ((-23, -69), "鐵 25%", True),
    ((-22, -75), "磚 25%", True),
    ((-21, -75), "鐵 25%", True),
]

# 不要派去的地方（動物或已被佔領；同一份區塊 JSON）。程式會直接排除。
BLOCKED = {
    (-25, -70), (-23, -70), (-21, -74),                            # 有動物
    (-20, -70), (-21, -71), (-22, -72), (-25, -74), (-20, -75),    # 已被佔領
}

# 內建清單只對「這個家」有效（v1.7）：它們是新伺服器、家 (-28|-72) 附近的座標。家不是這個座標（例如另一個伺服器的
# 帳號）時，configure(home=...) 會把 OASES／BLOCKED 清空，不拿別的伺服器的座標去驗證或派兵；之後只有掃描結果（oasis_store.apply）會填。
BUILTIN_HOME = (-28, -72)
_BUILTIN_OASES = list(OASES)
_BUILTIN_BLOCKED = set(BLOCKED)


def wrap_delta(a, b):
    d = abs(a - b)
    return min(d, MAP_SIZE - d)


def distance(a, b):
    """環繞後的直線距離。"""
    return math.hypot(wrap_delta(a[0], b[0]), wrap_delta(a[1], b[1]))


def map_id(x, y):
    """格子 id（派遣預填網址的 targetMapId）。"""
    h = _half()
    return (h - y) * MAP_SIZE + (x + h) + 1


def parse_time(s, base):
    """HH:MM:SS -> datetime；若比 base 早就視為隔天。"""
    h, m, *rest = [int(x) for x in s.split(":")]
    sec = rest[0] if rest else 0
    t = base.replace(hour=h, minute=m, second=sec, microsecond=0)
    if t < base:
        t += timedelta(days=1)
    return t


def fmt(t):
    return t.strftime("%H:%M:%S")


def plan(now, arrivals, speed, margin=30, home=None, oases=None, blocked=None):
    """純函式。回傳依距離排序的列（dict）。
    現在發兵：離開 now～返回 back。a 被撐過＝ now < a < back - margin。"""
    home = HOME if home is None else home
    oases = OASES if oases is None else oases
    blocked = BLOCKED if blocked is None else blocked
    arrivals = sorted(arrivals)
    rows = []
    for pos, bonus, verified in oases:
        if pos in blocked:
            continue
        d = distance(home, pos)
        one_way = timedelta(seconds=round(d / speed * 3600))
        back = now + one_way * 2
        safe_until = back - timedelta(seconds=margin)
        covered = [a for a in arrivals if now < a < safe_until]
        missed = [a for a in arrivals if a >= safe_until]
        rows.append(dict(pos=pos, bonus=bonus, verified=verified, dist=d,
                         one_way=one_way, back=back, covered=covered,
                         missed=missed, ok=not missed, map_id=map_id(*pos)))
    rows.sort(key=lambda r: (r["dist"], r["pos"]))
    return rows


def recommend(rows, include_unverified=False):
    """最近的、能撐過全部來襲的綠洲；沒有則 None。"""
    for r in rows:
        if r["ok"] and (r["verified"] or include_unverified):
            return r
    return None


def main():
    ap = argparse.ArgumentParser(description="派兵計算器 v1.6")
    ap.add_argument("-a", "--arrival", action="append", required=True,
                    help="來襲到達時間 HH:MM:SS（可重複）")
    ap.add_argument("-s", "--speed", type=float,
                    help="部隊速度（格／小時，基礎值）；與 --unit 擇一")
    ap.add_argument("--unit", action="append",
                    help="兵種欄位（t1、t2…，可重複；多個取最慢；要在目前族群的表裡）")
    ap.add_argument("--tribe", help="族群名稱（例：高盧）或 auto（讀 Sheet state 分頁）；省略＝舊預設（埃及人表）")
    ap.add_argument("--server-speed", type=float, default=1.0)
    ap.add_argument("--margin", type=int, default=30)
    ap.add_argument("--now", help="現在伺服器時間 HH:MM:SS")
    ap.add_argument("--include-unverified", action="store_true")
    args = ap.parse_args()
    if args.tribe:
        configure(tribe=args.tribe)
    if args.speed is None and not args.unit:
        ap.error("請用 -s 指定速度，或用 --unit 指定兵種")
    bad = [u for u in (args.unit or []) if u not in UNIT_SPEED]
    if bad:
        ap.error(f"目前族群（{TRIBE}）的表沒有 {','.join(bad)}；可用：{','.join(sorted(UNIT_SPEED)) or '（空）'}，或改用 -s")
    base_speed = args.speed if args.speed is not None \
        else min(UNIT_SPEED[u] for u in args.unit)

    real_now = datetime.now(SERVER_TZ).replace(microsecond=0)
    now = parse_time(args.now, real_now.replace(hour=0, minute=0, second=0)) \
        if args.now else real_now
    arrivals = sorted(parse_time(a, now) for a in args.arrival)
    first, last = arrivals[0], arrivals[-1]

    speed = base_speed * args.server_speed
    print(f"現在：{fmt(now)}　來襲：{', '.join(fmt(a) for a in arrivals)}"
          f"　速度：{speed:g} 格/時　緩衝：{args.margin}s\n")

    rows = plan(now, arrivals, speed, args.margin)
    print(f"{'座標':<11}{'加成':<14}{'距離':>6}  {'單程':>8}  {'回家':>9}  {'撐過':>5}  判定")
    for r in rows:
        tag = "" if r["verified"] else "（未驗證）"
        if r["ok"]:
            verdict = "OK 撐過全部" + tag
        else:
            verdict = ("缺 " + ",".join(fmt(a) for a in r["missed"])
                       + "；回家後再發" + tag)
        n = f"{len(r['covered'])}/{len(arrivals)}"
        print(f"({r['pos'][0]}|{r['pos'][1]})".ljust(11) + r["bonus"].ljust(14)
              + f"{r['dist']:>6.2f}  {str(r['one_way']):>8}  {fmt(r['back']):>9}  {n:>5}  {verdict}")

    print()
    if now >= first:
        print("⚠ 第一波已經到達或同時到達，請確認時間是否正確。")
    best = recommend(rows, args.include_unverified)
    if not best:
        print("沒有任何綠洲能一趟撐過最後一波；可改「回家後再發第二趟」"
              "（看上表『缺』欄的時間），或用其他方式避難。")
        return
    print(f"建議：({best['pos'][0]}|{best['pos'][1]}) {best['bonus']}，"
          f"距離 {best['dist']:.2f}（map id {best['map_id']}）")
    print(f"  現在發兵 → 到達 {fmt(now + best['one_way'])}，回家 {fmt(best['back'])}"
          f"（最後一波 {fmt(last)} 之後）")
    earliest_send = last + timedelta(seconds=args.margin) - best["one_way"] * 2
    print(f"  發兵時間要晚於 {fmt(earliest_send)}（否則太早回家），"
          f"且務必在第一波 {fmt(first)} 之前發出。")
    print(f"  預填網址：/build.php?gid=16&tt=2&eventType=4&targetMapId={best['map_id']}")
    print("\n提醒：發兵前一刻再確認該綠洲「未佔領、無動物」（清單是快照）；"
          "英雄不要帶。")


if __name__ == "__main__":
    main()
