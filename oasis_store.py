"""綠洲掃描 v0.2：抓區塊 → 解析（oasis_scan）→ 存 Sheet「Oases」分頁 → 套用到 dispatch.OASES／BLOCKED。

v0.2（v2.86）：Oases 分頁多存「中心x、中心y」（這次掃描的中心＝當時的家）。載入時核對：中心不等於現在的家、或舊格式沒有中心欄
→ 不採用；有來襲那輪（allow_scan=False）只放棄、不掃，建城後那次（allow_scan=True）才重掃。避免換伺服器／換帳號後
拿別的世界或別的家的資料來套用（埃及人 bot 曾因此用新伺服器座標驗證，全部「不安全」）。load() 回傳 (oases, age_hours, center)。

v0.1（v2.83）：
抓區塊的請求（✓ v2.84 新伺服器 Actions 跑通）：POST /api/v1/map/position，body 為 {"data": {x, y, zoomLevel, ignorePositions}}；
一次回傳 11×9 格；OASIS_ENDPOINT／OASIS_ZOOM 環境變數可覆寫。
任何失敗都只印 log、回 False，不影響建城／躲兵（家等於 dispatch.BUILTIN_HOME 時躲兵退回 dispatch.py 的內建清單，否則沒有候選、不派兵）。
"""
import json
import math
import os
import time
from datetime import datetime, timezone

import dispatch
import oasis_scan

ENDPOINT = os.getenv("OASIS_ENDPOINT", "/api/v1/map/position")   # ✓ v2.84
ZOOM = int(os.getenv("OASIS_ZOOM", "1"))                           # ✓ v2.84
SHEET_TAB = "Oases"
HEADER = ["x", "y", "kind", "加成", "動物(json)", "佔領者", "更新時間(UTC)", "中心x", "中心y"]
_FMT = "%Y-%m-%d %H:%M:%S"


def _utc():
    return datetime.now(timezone.utc).strftime(_FMT)


def _wrap(v):
    h = dispatch._half()
    return (v + h) % dispatch.MAP_SIZE - h


# ───────── 抓 ─────────
def fetch_block(page, base, x, y):
    """POST 一次，回傳 {"tiles": [...]}。HTTP 不 ok 就拋例外。"""
    r = page.request.post(
        f"{base}{ENDPOINT}",
        data={"data": {"x": _wrap(x), "y": _wrap(y), "zoomLevel": ZOOM, "ignorePositions": []}},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{base}/karte.php"})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status}")
    return r.json()


def _span(tiles):
    xs = [int(t["position"]["x"]) for t in tiles]
    ys = [int(t["position"]["y"]) for t in tiles]
    return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) if tiles else (0, 0)


def scan(page, base, radius=15, max_requests=30, fetch=fetch_block, pause=0.5):
    """以家為中心掃 radius 格內的綠洲。第一次請求量出一次回傳涵蓋多大，再依此排格子（不寫死 11×9）。
    回傳 oasis_scan.parse_tiles 的 list（依座標去重）。"""
    hx, hy = dispatch.HOME
    first = fetch(page, base, hx, hy)
    w, h = _span(first.get("tiles", []))
    if w < 3 or h < 3:
        raise RuntimeError(f"回傳格子範圍太小（{w}×{h}），請求參數可能不對")
    sx, sy = max(1, w - 1), max(1, h - 1)               # 步距＝涵蓋寬高 -1（留 1 格重疊）
    nx, ny = math.ceil(radius / sx), math.ceil(radius / sy)
    centers = [(hx + i * sx, hy + j * sy) for j in range(-ny, ny + 1) for i in range(-nx, nx + 1)]
    centers.sort(key=lambda c: abs(c[0] - hx) + abs(c[1] - hy))
    if len(centers) > max_requests:
        raise RuntimeError(f"radius={radius} 需要 {len(centers)} 次請求，超過上限 {max_requests}")
    seen, out = {}, []
    for k, (cx, cy) in enumerate(centers):
        if k:
            time.sleep(pause)                               # 別連打
        data = first if k == 0 else fetch(page, base, cx, cy)
        for o in oasis_scan.parse_tiles(data):
            seen[o["pos"]] = o
    print(f"[綠洲掃描] {len(centers)} 次請求（每次 {w}×{h}），找到綠洲 {len(seen)} 個")
    return list(seen.values())


# ───────── Sheet ─────────
def _ws(create=False):
    import sheets_writer as SW
    if not SW.has_sheet():
        return None
    sh = SW.open_sheet()
    try:
        return sh.worksheet(SHEET_TAB)
    except Exception:                                   # noqa: BLE001
        return sh.add_worksheet(SHEET_TAB, 500, len(HEADER)) if create else None


def save(oases, now=None, center=None):
    """整份覆蓋寫入（raw，避免被 Sheets 轉型）。center＝這次掃描的中心（預設 dispatch.HOME），一起寫進每一列。"""
    ws = _ws(create=True)
    if ws is None:
        return False
    now = now or _utc()
    cx, cy = center or dispatch.HOME
    rows = [[o["pos"][0], o["pos"][1], o["kind"], o["bonus"],
             json.dumps(o["animals"]), o["owner"] or "", now, cx, cy] for o in sorted(oases, key=lambda o: o["pos"])]
    ws.clear()
    ws.update(range_name="A1", values=[HEADER] + rows, raw=True)
    return True


def load():
    """→ (oases, age_hours, center)；沒有分頁或空的回 ([], None, None)。center＝掃描中心 (x, y)；舊格式沒有這兩欄回 None。"""
    ws = _ws()
    if ws is None:
        return [], None, None
    vals = ws.get_all_values()[1:]
    out, newest, center = [], None, None
    for r in vals:
        if len(r) < 7 or not r[0].lstrip("-").isdigit():
            continue
        out.append(dict(pos=(int(r[0]), int(r[1])), kind=r[2], bonus=r[3],
                        animals=[tuple(a) for a in json.loads(r[4] or "[]")], owner=r[5] or None))
        if center is None and len(r) >= 9 and r[7].lstrip("-").isdigit() and r[8].lstrip("-").isdigit():
            center = (int(r[7]), int(r[8]))
        try:
            t = datetime.strptime(r[6], _FMT).replace(tzinfo=timezone.utc)
            newest = t if newest is None or t > newest else newest
        except ValueError:
            pass
    age = None if newest is None else (datetime.now(timezone.utc) - newest).total_seconds() / 3600
    return out, age, center


# ───────── 套用 ─────────
def apply(oases, max_dist=40):
    """把掃描結果換進 dispatch.OASES／BLOCKED。結果是空的就不動（保留 dispatch.py 內建清單）。"""
    cand = oasis_scan.free_candidates(oases, max_dist=max_dist)
    if not cand:
        print("[綠洲掃描] 沒有可用候選，沿用 dispatch.py 內建清單")
        return False
    dispatch.OASES[:] = cand
    dispatch.BLOCKED.clear()
    dispatch.BLOCKED.update(oasis_scan.blocked_set(oases))
    print(f"[綠洲掃描] 套用：候選 {len(cand)} 個、排除 {len(dispatch.BLOCKED)} 個")
    return True


def refresh(page, base, *, max_age_h=12, allow_scan=True, radius=15):
    """每輪呼叫一次：Sheet 的資料夠新、而且掃描中心就是現在的家，才直接套用；否則（太舊／沒有／中心不符／沒記錄中心）
    且 allow_scan 才去掃。中心不符或沒記錄時，allow_scan=False 一律不套用（避免用到別的伺服器或別的家的資料）。
    永遠不拋例外。"""
    try:
        oases, age, center = load()
        mine = center == tuple(dispatch.HOME)
        if oases and mine and (age is None or age <= max_age_h or not allow_scan):
            return apply(oases)
        if oases and not mine:
            print(f"[綠洲掃描] Sheet 的資料中心是 {center}，現在的家是 {tuple(dispatch.HOME)}：不採用" + ("，重掃" if allow_scan else ""))
        if not allow_scan:
            return False
        oases = scan(page, base, radius=radius)
        try:
            save(oases)
        except Exception as e:                          # noqa: BLE001
            print(f"[綠洲掃描] 寫 Sheet 失敗（仍套用本輪結果）：{e!r}")
        return apply(oases)
    except Exception as e:                              # noqa: BLE001
        print(f"[綠洲掃描] 失敗，沿用內建清單：{e!r}")
        return False
