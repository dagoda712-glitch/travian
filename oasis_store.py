"""綠洲掃描 v0.1：抓區塊 → 解析（oasis_scan）→ 存 Sheet「Oases」分頁 → 套用到 dispatch.OASES／BLOCKED。

抓區塊的請求（OASIS_ENDPOINT／payload）是 ✗ 未對你的伺服器驗證的：請先照說明用 DevTools 對一次。
任何失敗都只印 log、回 False，不影響建城／躲兵（躲兵會退回 dispatch.py 裡寫死的清單）。
"""
import json
import math
import os
import time
from datetime import datetime, timezone

import dispatch
import oasis_scan

ENDPOINT = os.getenv("OASIS_ENDPOINT", "/api/v1/map/position")   # ✗ 以 DevTools 實際看到的為準
ZOOM = int(os.getenv("OASIS_ZOOM", "1"))                           # ✗ 同上
SHEET_TAB = "Oases"
HEADER = ["x", "y", "kind", "加成", "動物(json)", "佔領者", "更新時間(UTC)"]
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


def save(oases, now=None):
    """整份覆蓋寫入（raw，避免被 Sheets 轉型）。"""
    ws = _ws(create=True)
    if ws is None:
        return False
    now = now or _utc()
    rows = [[o["pos"][0], o["pos"][1], o["kind"], o["bonus"],
             json.dumps(o["animals"]), o["owner"] or "", now] for o in sorted(oases, key=lambda o: o["pos"])]
    ws.clear()
    ws.update(range_name="A1", values=[HEADER] + rows, raw=True)
    return True


def load():
    """→ (oases, age_hours)；沒有分頁或空的回 ([], None)。"""
    ws = _ws()
    if ws is None:
        return [], None
    vals = ws.get_all_values()[1:]
    out, newest = [], None
    for r in vals:
        if len(r) < 7 or not r[0].lstrip("-").isdigit():
            continue
        out.append(dict(pos=(int(r[0]), int(r[1])), kind=r[2], bonus=r[3],
                        animals=[tuple(a) for a in json.loads(r[4] or "[]")], owner=r[5] or None))
        try:
            t = datetime.strptime(r[6], _FMT).replace(tzinfo=timezone.utc)
            newest = t if newest is None or t > newest else newest
        except ValueError:
            pass
    age = None if newest is None else (datetime.now(timezone.utc) - newest).total_seconds() / 3600
    return out, age


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
    """每輪呼叫一次：Sheet 的資料夠新就直接套用；太舊（或沒有）且 allow_scan 才去掃。永遠不拋例外。"""
    try:
        oases, age = load()
        if oases and (age is None or age <= max_age_h or not allow_scan):
            return apply(oases)
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
