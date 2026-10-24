"""躲兵自動化 v0.5：讀集結點概況 → 判斷要不要躲 → 即時驗證綠洲 → 派兵 → 事後核對。

v0.5（2026-10-24，v2.80）：依 v2.79 的決定補 dry／live 需要的邏輯（離線測，✗ 未對真實遊戲跑）：
  - decide 新增 min_attack（嚴格大於；None＝不檢查，行為同 v0.4）：派出兵力的攻擊力合計（dispatch.UNIT_ATTACK，未知兵種算 0）要 > 門檻，否則 skip；
  - Plan 新增 code（no_units／weak／…），方便呼叫端分辨為什麼 skip；
  - 新增 return_windows：自己的兵（inReturn）歸來後、下一波來襲前的空檔；run_once 新增 wait_return_max_s／sleep_fn／clock：
    dry／live 時若現在沒兵可派、但歸來夠快（≤ 上限秒）且歸來後到下一波還來得及，就等到歸來後重讀一次再決策；plan 模式只回報（res['windows']）；
  - 取消「概況已有 outRaid 就略過」的手動避讓：程式本來就沒有這條，不新增（決定 c）；英雄永遠不帶（confirm_matches 的 t11＝0）。

v0.4（2026-10-23）：觀察用（不改行為）：Movement 多 headline；run_once 多 on_overview 回呼，結果多 now_ts／arrivals／in_moves／top。
v0.3（2026-10-23）：對照真實確認頁與第一步表單修正（✓ 讀原始碼）：
  - parse_confirm 讀真實確認頁（action／span#at／troops[0][tN]／x／y／eventType／標題文字），
    新增 confirm_matches：按確認前核對「兵力、目標、類型、英雄為 0」都跟預期一致；
  - run_once：外層 try/except（失敗只回報、不重試；按確認後出錯標「結果不明」）、
    候選上限改成「最多開 4 次確認頁」＋「最多即時驗證 12 個綠洲」（便宜的驗證不再吃掉確認頁額度）。
v0.2：依真實概況頁修 incoming（只認 inRaid／inAttack）與 link_d（出擊取目的地）。

分兩層：
  純函式（離線可測，test_hide_troops_real.py）：parse_overview / decide / parse_confirm / confirm_matches / confirm_ok
  Playwright 流程（✗ 未對真實遊戲跑過，只用假 page 離線演練）：run_once(page, base, mode)

mode：
  plan  只讀概況＋算，不開派遣頁（不會動遊戲）
  dry   一路走到「確認頁」並核對單程時間，但不按確認（離開確認頁＝不出兵 ✓ 筆記 §10）
  live  核對通過才按 #confirmSendTroops

安全原則（全部沿用專案慣例）：英雄永遠不帶；失敗只回報、不重試；
一律以「確認頁的單程秒數」為準，dispatch.py 的速度表只用來排候選。
"""
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

import dispatch
import oasis_check


# ───────────────────────── 純函式：解析 ─────────────────────────

def coords_from_map_id(mid):
    """map id → (x, y)（dispatch.map_id 的反函數，✓ 11630→(-200,171)）。"""
    n = mid - 1
    m, half = dispatch.MAP_SIZE, dispatch._half()          # 呼叫時才讀，Config 覆寫後才對
    return (n % m - half, half - n // m)


# 真實概況頁（✓ 10-23 05:47）：同一個「來村軍團」區塊同時有 inReturn（自己的兵歸來）與 inRaid（敵方搶奪），
# 所以不能用區塊標題判斷來襲，只看 table 的 class。真攻擊（會拆建築）class＝inAttack（✓ v2.79：標題「X 攻擊 Y的村莊」）。
HOSTILE_KINDS = {"inRaid", "inAttack"}
KNOWN_IN_KINDS = HOSTILE_KINDS | {"inReturn"}


@dataclass
class Movement:
    section: str            # h4.spacer 標題（來村軍團／出擊軍團／在本村…）
    kinds: list             # table 的 class（inRaid／outRaid／outHero…）
    arrival_ts: int = None  # 絕對到達時間（epoch）
    counts: list = field(default_factory=list)   # t1..t11；None＝顯示 ?
    link_d: int = None      # 「對方的格子」：來襲／歸來＝td.role 連結（來源）；出擊＝troopHeadline 連結（目的地，✓ 10-23）
    role_d: int = None      # td.role 連結的 d（來襲＝來源村；出擊／歸來＝自己的村）
    head_d: int = None      # td.troopHeadline 連結的 d（出擊＝目的地；歸來＝綠洲；來襲＝自己的村）
    own_home: bool = False  # 本村自己的在家部隊表
    recall_url: str = None  # 取消鈕（送出後約 90 秒內才有，✓ v2.65）
    headline: str = ""      # td.troopHeadline 的文字（✓ 例：「JerseyDevil 搶奪 WoodViking的村莊」）；觀察真攻擊／增援用

    @property
    def incoming(self):
        """只有敵方搶奪／攻擊才算來襲；自己歸來（inReturn）、其他 in* 一律不算。"""
        return any(k in HOSTILE_KINDS for k in self.kinds)


def parse_overview(page):
    """集結點概況頁 `build.php?gid=16&tt=1` → (server_ts, [Movement])。
    到達時間＝頁面 Travian.Game.timestamp＋span.timer[value]（不碰時區，✓ 1792725952+3246＝05:19:58）。
    真攻擊的 class 名稱未知（✗）→ 以「h4 標題含『來村』」判定來襲，class 只當輔助。"""
    m = re.search(r"Travian\.Game\.timestamp\s*=\s*(\d+)", page)
    if not m:
        raise ValueError("找不到 Travian.Game.timestamp")
    ts = int(m.group(1))
    soup = BeautifulSoup(page, "html.parser")
    out, section = [], ""
    for el in soup.select("h4.spacer, table.troop_details"):
        if el.name == "h4":
            section = oasis_check._text(str(el))
            continue
        kinds = [c for c in el.get("class", []) if c != "troop_details"]
        mv = Movement(section=section, kinds=kinds)
        mv.own_home = bool(el.get("data-did")) and el.get("data-did") == el.get("data-ort")
        tm = el.select_one("tbody.infos span.timer[value]")
        if tm:
            mv.arrival_ts = ts + int(tm["value"])
        row = el.select_one("tbody.units.last tr")
        if row:
            for td in row.select("td.unit"):
                t = td.get_text(strip=True)
                mv.counts.append(int(t) if t.isdigit() else None)
        ab = el.select_one("div.abort button[onclick*='recall=']")
        if ab:
            r = re.search(r"recall=(\d+)", ab["onclick"])
            mv.recall_url = f"/build.php?gid=16&tt=1&recall={r.group(1)}" if r else None
        def _d(sel):
            a = el.select_one(sel)
            m2 = re.search(r"[?&]d=(\d+)", a["href"]) if a else None
            return int(m2.group(1)) if m2 else None
        head = el.select_one("td.troopHeadline")
        mv.headline = oasis_check._text(str(head)) if head else ""
        mv.role_d = _d("td.role a[href*='d=']")
        mv.head_d = _d("td.troopHeadline a[href*='d=']")
        mv.link_d = mv.head_d if any(k.startswith("out") for k in mv.kinds) else mv.role_d
        out.append(mv)
    return ts, out


def unknown_in_kinds(moves):
    """沒看過的 in* class（可能是真攻擊／增援／偵察），回報用；程式不會把它們當來襲。"""
    return sorted({k for m in moves for k in m.kinds if k.startswith("in") and k not in KNOWN_IN_KINDS})


def incoming_symbols(page):
    """任何一頁都內嵌的 viewData（側欄村莊列表）→ 來襲數與四色符號加總。
    ✓ 04:40:39 全 0、05:07:38 amount=1／gray=1（顏色意義 ✗）。不必載入集結點就能做輕量輪詢。"""
    tot = dict(amount=0, gray=0, green=0, red=0, yellow=0)
    for m in re.finditer(r'"incomingAttacksAmount":(\d+),"incomingAttacksSymbols":'
                         r'\{"gray":(\d+),"green":(\d+),"red":(\d+),"yellow":(\d+)\}', page):
        for k, v in zip(tot, m.groups()):
            tot[k] += int(v)
    return tot


def home_units(moves):
    """本村在家兵力 {'t1': n, ...}（不含英雄 t11、不含 ?）。"""
    for m in moves:
        if m.own_home:
            return {f"t{i+1}": n for i, n in enumerate(m.counts[:10]) if n}
    return {}


def incoming_arrivals(moves):
    return sorted(m.arrival_ts for m in moves if m.incoming and m.arrival_ts)


# ───────────────────────── 純函式：決策 ─────────────────────────

def attack_total(units, attacks=None):
    """派出兵力的攻擊力合計。attacks 預設 dispatch.UNIT_ATTACK；沒列的兵種算 0（保守）。"""
    attacks = attacks if attacks is not None else dispatch.UNIT_ATTACK
    return sum(attacks.get(u, 0) * n for u, n in units.items() if n)


RETURN_BUFFER_S = 3     # 歸來時間之後再等幾秒才重讀（讓兵確實進村）


def return_windows(moves, now_ts):
    """自己的兵（inReturn）歸來後、下一波來襲前的空檔。回傳依歸來時間排序的 list：
    dict(return_ts, units, attack, next_attack_ts, gap_s)。
    只列：歸來還沒到（> now）、歸來後還有來襲、兵力欄位都讀得到（不含英雄 t11）。
    用途：兵歸來的那一刻要有一次 run 把它們再派出去，否則歸來剛好被下一波打（v2.79 ③ 的 12 分 24 秒空檔）。"""
    hostile = incoming_arrivals(moves)
    out = []
    for m in moves:
        if "inReturn" not in m.kinds or not m.arrival_ts or m.arrival_ts <= now_ts:
            continue
        cnt = m.counts[:10]
        if not cnt or any(c is None for c in cnt):
            continue
        units = {f"t{i+1}": n for i, n in enumerate(cnt) if n}
        nxt = [a for a in hostile if a > m.arrival_ts]
        if not units or not nxt:
            continue
        out.append(dict(return_ts=m.arrival_ts, units=units, attack=attack_total(units),
                        next_attack_ts=nxt[0], gap_s=nxt[0] - m.arrival_ts))
    return sorted(out, key=lambda w: w["return_ts"])


def fmt_window(w, now_ts):
    """給 log 用的一行說明（伺服器時區）。"""
    f = lambda t: datetime.fromtimestamp(t, dispatch.SERVER_TZ).strftime("%H:%M:%S")
    return (f"歸來 {f(w['return_ts'])}（{w['return_ts'] - now_ts}秒後，攻{w['attack']}）"
            f"→ 下一波 {f(w['next_attack_ts'])}（空檔 {w['gap_s']}秒）")



@dataclass
class Plan:
    action: str                    # skip／wait／go
    reason: str
    units: dict = field(default_factory=dict)
    scope: list = field(default_factory=list)       # 要撐過的來襲（datetime）
    candidates: list = field(default_factory=list)  # dispatch.plan 的列，依「建議順序」
    code: str = ""                 # skip／wait 的原因代碼：no_units／unknown_speed／weak／no_future／too_close／no_oasis；wait＝too_far


def decide(now, arrivals_ts, units, *, lookahead_min=90, cluster_min=120, margin=30,
           min_lead_s=120, speeds=None, home=None, oases=None, blocked=None,
           include_unverified=True, min_attack=None, attacks=None):
    """now：datetime（伺服器時區）。arrivals_ts：來襲到達 epoch 清單。units：在家兵力。
    include_unverified 預設 True：候選一律會在發兵前即時驗證，清單的「已驗證」旗標不再當門檻。
    候選順序：先挑「撐過範圍內全部來襲」的（近→遠），再挑「只撐過前幾波」的（撐得多→近）；
    沒撐過第一波的直接丟掉。
    min_attack：派出兵力的攻擊力合計必須「嚴格大於」它（v2.79 決定 b：20）；None＝不檢查。
    attacks：單兵攻擊力表，預設 dispatch.UNIT_ATTACK；沒列的兵種算 0（保守）。"""
    speeds = speeds or dispatch.UNIT_SPEED
    if not units:
        return Plan("skip", "村裡沒有可派的兵", code="no_units")
    unknown = [u for u in units if u not in speeds]
    if unknown:
        return Plan("skip", f"兵種速度未知：{','.join(sorted(unknown))}（請先量測或加進 speeds）", code="unknown_speed")
    if min_attack is not None:
        total = attack_total(units, attacks)
        if total <= min_attack:
            return Plan("skip", f"攻擊力合計 {total} 不超過 {min_attack}（動物底值 10 的兩倍，派出去可能白送）",
                        units, code="weak")
    fut = sorted(datetime.fromtimestamp(t, now.tzinfo) for t in arrivals_ts)
    fut = [a for a in fut if a > now]
    if not fut:
        return Plan("skip", "沒有未來的來襲", code="no_future")
    lead = (fut[0] - now).total_seconds()
    if lead > lookahead_min * 60:
        return Plan("wait", f"第一波還有 {lead/60:.0f} 分鐘（> {lookahead_min}）", code="too_far")
    if lead < min_lead_s:
        return Plan("skip", f"第一波只剩 {lead:.0f} 秒，來不及兩步驟派兵", code="too_close")
    scope = [a for a in fut if a <= fut[0] + timedelta(minutes=cluster_min)]
    speed = min(speeds[u] for u in units)
    rows = dispatch.plan(now, scope, speed, margin, home if home is not None else dispatch.HOME,
                         oases if oases is not None else dispatch.OASES,
                         blocked if blocked is not None else dispatch.BLOCKED)
    if not include_unverified:
        rows = [r for r in rows if r["verified"]]
    full = [r for r in rows if r["ok"]]
    part = sorted((r for r in rows if not r["ok"] and r["covered"] and r["covered"][0] == scope[0]),
                  key=lambda r: (-len(r["covered"]), r["dist"]))
    cands = full + part
    if not cands:
        return Plan("skip", "沒有任何綠洲能撐過第一波", units, scope, code="no_oasis")
    kind = "可一趟撐過範圍內全部來襲" if full else "只能撐過前幾波，返回後需再發第二趟"
    return Plan("go", kind, units, scope, cands)


def parse_confirm(page):
    """確認頁 → dict，或 None（找不到關鍵欄位時，呼叫端不得派兵）。
    ✓ 真實樣本（10-23）：隱藏欄位 action＝troopsSend/<vid>/<epoch>（epoch＝頁面產生時的伺服器時間戳）；
    span#at[value]＝若此刻按確認的到達時間戳；單程＝兩者之差（✓ 1792736965−1792734845＝2120＝0:35:20）；
    兵力在隱藏欄位 troops[0][t1]～[t11]（t11＝英雄）；目標 x／y、eventType 也是隱藏欄位。"""
    soup = BeautifulSoup(page, "html.parser")
    act = soup.select_one("input[name=action]")
    at = soup.select_one("span#at[value]")
    if not act or not at:
        return None
    m = re.search(r"troopsSend/(\d+)/(\d+)", act.get("value", ""))
    if not m:
        return None
    try:
        at_ts = int(at["value"])
    except ValueError:
        return None
    epoch = int(m.group(2))

    def val(name):
        el = soup.select_one(f'input[name="{name}"]')
        return el.get("value") if el else None

    def num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    head = soup.select_one("td.troopHeadline")
    return dict(
        epoch=epoch, trip_s=at_ts - epoch, village_id=int(m.group(1)),
        event_type=val("eventType"), x=num(val("x")), y=num(val("y")),
        troops={f"t{i}": num(val(f"troops[0][t{i}]")) for i in range(1, 12)},
        kind_text=head.get_text(strip=True) if head else "",
        has_confirm=soup.select_one("#confirmSendTroops") is not None,
    )


def confirm_matches(info, units, pos):
    """按確認前的核對：兵力、目標、類型都要和預期一致，英雄必須是 0。回傳 (ok, 說明)。
    units：預期派出的 {'t1': n, ...}（不含英雄）；pos：預期目標 (x, y)。"""
    if not info:
        return False, "確認頁解析失敗"
    if not info.get("has_confirm"):
        return False, "確認頁沒有確認鈕"
    t = info["troops"]
    if t.get("t11") != 0:
        return False, f"確認頁的英雄欄不是 0：{t.get('t11')}（英雄永遠不帶）"
    for i in range(1, 11):
        k = f"t{i}"
        want = units.get(k, 0)
        if t.get(k) != want:
            return False, f"兵力不符：{k} 確認頁 {t.get(k)}、預期 {want}"
    if info.get("event_type") != "4":
        return False, f"類型不是搶奪（eventType={info.get('event_type')}）"
    if (info.get("x"), info.get("y")) != tuple(pos):
        return False, f"目標不符：確認頁 ({info.get('x')}|{info.get('y')})、預期 ({pos[0]}|{pos[1]})"
    if info.get("kind_text") and "掠奪" not in info["kind_text"]:
        return False, f"標題文字不是掠奪：{info['kind_text']}"
    return True, "ok"


def confirm_ok(info, scope_ts, *, margin=30, clicks_s=45):
    """以確認頁的單程秒數做最後核對。
    ① 現在（epoch＋按確認的耗時 clicks_s）要在第一波之前；
    ② 返回時間（現在＋2×單程）要晚於「範圍內第一波」（至少撐過第一波）。
    回傳 (ok, 撐過幾波, 說明)。是否撐過全部由呼叫端比較。"""
    if not info or info["trip_s"] <= 0:
        return False, 0, "確認頁解析失敗"
    send = info["epoch"] + clicks_s
    back = send + 2 * info["trip_s"]
    scope = sorted(scope_ts)
    if send >= scope[0]:
        return False, 0, "來不及：按確認時第一波已到"
    n = sum(1 for a in scope if send < a < back - margin)
    if n == 0 or not (send < scope[0] < back - margin):
        return False, n, "返回前第一波就到了"
    return True, n, f"單程 {info['trip_s']}s、返回 epoch {back}、撐過 {n}/{len(scope)} 波"


# ───────────────────────── Playwright 流程（✗ 未對真實遊戲測） ─────────────────────────

def _fetch_tile(page, base, x, y):
    """tile-details：靠瀏覽器 context 的 Cookie（含 JWT），不另外處理認證。"""
    r = page.request.post(f"{base}/api/v1/map/tile-details", data={"x": x, "y": y})
    if not r.ok:
        return None
    return oasis_check.parse_tile_details(r.json()["html"])


def _live_safe(page, base, pos):
    info = _fetch_tile(page, base, *pos)
    if not info or not oasis_check.is_safe(info, pos):
        return False, "即時驗證不通過（有動物／被佔領／解析失敗）"
    if oasis_check.recent_occupy_warning(info):
        return False, "附近剛有佔領事件"
    return True, "ok"


def _open_confirm(page, base, row, units):
    """填第一步表單 → 進確認頁。英雄 t11 不填。回確認頁 HTML 或 None。"""
    page.goto(f"{base}/build.php?gid=16&tt=2&eventType=4&targetMapId={row['map_id']}")
    for t, n in units.items():
        page.fill(f'input[name="troop[{t}]"]', str(n))
    radio = page.query_selector('input[name="eventType"][value="4"]')
    if radio:
        radio.check()
    page.click("#ok")
    page.wait_for_selector("#troopSendForm", timeout=15000)
    return page.content()


def run_once(page, base, mode="dry", *, speeds=None, max_confirm=4, max_checks=12, on_overview=None,
             wait_return_max_s=0, sleep_fn=time.sleep, clock=time.monotonic, **kw):
    """一輪：回傳 dict（給 Actions「躲兵」列用）。失敗只回報、不重試。
    max_confirm：最多開幾次確認頁；max_checks：最多即時驗證幾個綠洲（便宜，不吃確認頁額度）。
    wait_return_max_s：dry／live 時，現在沒兵可派但自己的兵在這麼多秒內歸來、而且歸來後下一波還來得及 → 等到歸來後重讀一次
    （0＝不等；plan 模式永遠不等，只在 res['windows'] 回報）。min_attack 等 decide 參數走 **kw。"""
    assert mode in ("plan", "dry", "live")
    res = dict(mode=mode, action="error", reason="", units={}, scope=[], tried=[])
    try:
        return _run_once(page, base, mode, res, speeds, max_confirm, max_checks, kw, on_overview,
                         wait_return_max_s, sleep_fn, clock)
    except Exception as e:                                  # noqa: BLE001  任何例外都只回報
        res["error"] = repr(e)
        res["result"] = ("已按確認後發生例外，兵可能已送出，請人工確認（不重試）" if res.get("clicked")
                         else f"例外，未按確認、不重試：{e!r}")
        return res


def _read_overview(page, base, on_overview, clock):
    page.goto(f"{base}/build.php?gid=16&tt=1")
    html = page.content()
    if on_overview:
        try:
            on_overview(html)                               # 觀察用：例如把原始 HTML 存檔；出錯不影響流程
        except Exception:                                   # noqa: BLE001
            pass
    ts, moves = parse_overview(html)
    return ts, moves, clock()


def _run_once(page, base, mode, res, speeds, max_confirm, max_checks, kw, on_overview=None,
              wait_return_max_s=0, sleep_fn=time.sleep, clock=time.monotonic):
    ts, moves, t_load = _read_overview(page, base, on_overview, clock)
    now = datetime.fromtimestamp(ts, dispatch.SERVER_TZ)
    units = home_units(moves)
    pl = decide(now, incoming_arrivals(moves), units, speeds=speeds, **kw)
    wins = return_windows(moves, ts)
    if wins:
        res["windows"] = [fmt_window(w, ts) for w in wins]
    # 歸來窗口：現在沒兵（或不夠強）、但兵很快回來、回來後到下一波還來得及 → dry／live 等一下再讀
    if mode != "plan" and wait_return_max_s and pl.action == "skip" and pl.code in ("no_units", "weak") and wins:
        w = wins[0]
        wait = w["return_ts"] + RETURN_BUFFER_S - ts - (clock() - t_load)
        enough = w["next_attack_ts"] - (w["return_ts"] + RETURN_BUFFER_S) > kw.get("min_lead_s", 120)
        if 0 < wait <= wait_return_max_s and enough:
            sleep_fn(wait)
            res["waited_s"] = round(wait)
            ts, moves, t_load = _read_overview(page, base, on_overview, clock)
            now = datetime.fromtimestamp(ts, dispatch.SERVER_TZ)
            units = home_units(moves)
            pl = decide(now, incoming_arrivals(moves), units, speeds=speeds, **kw)
        else:
            res["window_note"] = (f"有歸來窗口但沒等：等待 {wait:.0f}s（上限 {wait_return_max_s}s）、"
                                  f"{'空檔夠' if enough else '空檔不夠'}")
    res.update(action=pl.action, reason=pl.reason, units=units, code=pl.code,
               scope=[a.strftime("%H:%M:%S") for a in pl.scope],
               unknown_in=unknown_in_kinds(moves), now_ts=ts, arrivals=incoming_arrivals(moves),
               in_moves=[f"{m.kinds[0] if m.kinds else '?'}|{m.headline}" for m in moves
                         if any(k.startswith("in") for k in m.kinds)],
               top=[dict(pos=r["pos"], one_way=str(r["one_way"]), back=r["back"].strftime("%H:%M:%S"),
                         ok=r["ok"]) for r in pl.candidates[:3]])
    if pl.action != "go" or mode == "plan":
        return res
    scope_ts = [int(a.timestamp()) for a in pl.scope]
    full_n = len(scope_ts)
    confirms = 0
    for row in pl.candidates:
        if len(res["tried"]) >= max_checks or confirms >= max_confirm:
            break
        pos = row["pos"]
        ok, why = _live_safe(page, base, pos)
        res["tried"].append(dict(pos=pos, check=why))
        if not ok:
            continue
        confirms += 1
        info = parse_confirm(_open_confirm(page, base, row, units))
        same, why_m = confirm_matches(info, units, pos)
        if not same:
            res["tried"][-1]["confirm"] = why_m
            continue                                        # 核對不符：不按確認，換下一個
        good, n, msg = confirm_ok(info, scope_ts, margin=kw.get("margin", 30))
        res["tried"][-1]["confirm"] = msg
        if not good or (n < full_n and pl.reason.startswith("可一趟")):
            continue                                        # 單程比預估短→換下一個（通常是更遠的）
        if mode == "dry":
            res.update(result="dry：確認頁核對通過，未按確認", chosen=pos)
            return res
        res["clicked"] = True                               # 之後若出例外，要標「結果不明」
        page.click("#confirmSendTroops")
        page.wait_for_url(re.compile(r"tt=1"), timeout=20000)
        _, after = parse_overview(page.content())
        sent = [m for m in after if "outRaid" in m.kinds and m.link_d == row["map_id"]]
        res.update(result="已送出" if sent else "按了確認但概況頁沒看到出擊（請人工確認）",
                   chosen=pos, back_epoch=info["epoch"] + 2 * info["trip_s"])
        return res
    res["result"] = "候選全部未通過，沒有派兵"
    return res


if __name__ == "__main__":
    print(__doc__)
