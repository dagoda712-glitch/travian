#!/usr/bin/env python3
"""派兵計算器 v1.5：搶空綠洲讓兵避難（只做計算，不會自動操作遊戲）

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
    --unit           兵種欄位 t1～t10（可重複；多個取最慢）。t1 奴隸民兵＝7 ✓（10-23
                     確認頁單程 0:35:20 = 4.12 格÷7）；t2＝6（✓ 上限推算）；t3 為 7（△ 未實測）
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


def configure(home=None, map_size=None):
    """執行時覆寫家座標與地圖大小（奇數；座標範圍 -(n-1)/2..(n-1)/2）。None＝不改。"""
    global HOME, MAP_SIZE
    if map_size is not None:
        if int(map_size) < 3 or int(map_size) % 2 == 0:
            raise ValueError(f"map_size 必須是奇數且 >= 3：{map_size!r}")
        MAP_SIZE = int(map_size)
    if home is not None:
        HOME = (int(home[0]), int(home[1]))


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

# 兵種速度（格／小時，基礎值，伺服器 1 倍）。埃及人欄位 t1～t10。
# 只收已知的步兵；t1 ✓ 實測，其餘 △。其他兵種請用 -s。
UNIT_SPEED = {
    "t1": 7,   # 奴隸民兵 ✓（10-23：4.12 格、單程 0:35:20）
    "t2": 6,   # 看守人艾什 ✓（v2.77：12:32 outRaid t1×2＋t2×9、英雄 0，剩 3491 秒 → 速度 ≤ 6.19；與 3600 秒＝6.00 格÷6 合併，t1＝7 所以只能是 t2）
    "t3": 7,   # 斧刀勇士 △
    "t4": 16,  # 探險者索普杜 ✓（偵察 Village 003：3.1623 格÷16×3600＝711.5 秒）
}

# 兵種攻擊力（單兵）。只收已知的；沒列＝未知，hide_troops 的攻擊力合計把它當 0（保守）。
UNIT_ATTACK = {
    "t1": 10,  # ✓ 04:24 戰報：1 隻＝攻 10
    "t2": 30,  # △ 295 反推約 30.6（2×10＋9×30＝290，差 5 未解）
}

# 兵種載重（單兵，✓ v2.78 兩式聯立：a＋b＝65、2a＋9b＝480）
UNIT_CARRY = {"t1": 15, "t2": 50}

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
    ap = argparse.ArgumentParser(description="派兵計算器 v1.5")
    ap.add_argument("-a", "--arrival", action="append", required=True,
                    help="來襲到達時間 HH:MM:SS（可重複）")
    ap.add_argument("-s", "--speed", type=float,
                    help="部隊速度（格／小時，基礎值）；與 --unit 擇一")
    ap.add_argument("--unit", action="append", choices=sorted(UNIT_SPEED),
                    help="兵種欄位（t1～t4，可重複；多個取最慢）")
    ap.add_argument("--server-speed", type=float, default=1.0)
    ap.add_argument("--margin", type=int, default=30)
    ap.add_argument("--now", help="現在伺服器時間 HH:MM:SS")
    ap.add_argument("--include-unverified", action="store_true")
    args = ap.parse_args()
    if args.speed is None and not args.unit:
        ap.error("請用 -s 指定速度，或用 --unit 指定兵種")
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
