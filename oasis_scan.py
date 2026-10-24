"""區塊 JSON 解析（純函式，不連遊戲、不連 Sheet）v0.1。輸入你抓到的區塊回傳 JSON（{"tiles": [...]}，字串或 dict）。

格子規則（從 10-24 貼的 99 格樣本推出，✓ 對得上該樣本；✗ 沒對過家附近以外的區塊）：
  - title 含 {k.fo}、did＝-1、沒有 uid → 未佔領綠洲；title 含 {k.bt}（有 uid）→ 已被佔領的綠洲；{k.dt} → 村莊。
  - text 的 {a:rN} … P% → 加成；N＝1 木、2 磚、3 鐵、4 穀（Travian 慣例 △，樣本沒有直接證明）。
  - text 含 {k.animals} → 有動物；後面的 <i class="unit uNN"> 與 <span class="value">數量 是動物種類與數量。
這個模組只做解析與篩選；怎麼抓區塊（請求網址、參數）還沒有（✗ 要你提供）。
"""
import html as _h
import json
import re

import dispatch

RES = {1: "木", 2: "磚", 3: "鐵", 4: "穀"}
_STRIP = dict.fromkeys(map(ord, "\u202d\u202c"), None)


def _pos(tile):
    p = tile["position"]
    return (int(p["x"]), int(p["y"]))


def bonus_label(text):
    """'{a:r3} {a.r3} 25%<br />{a:r4} …' → '鐵＋穀 25%'；沒有加成回 ''。百分比不同時各自標。"""
    found = [(int(n), int(p)) for n, p in re.findall(r"\{a:r(\d)\}\s*\{a\.r\d\}\s*(\d+)%", text)]
    if not found:
        return ""
    pcts = {p for _n, p in found}
    names = "＋".join(RES.get(n, f"r{n}") for n, _p in found)
    if len(pcts) == 1:
        return f"{names} {found[0][1]}%"
    return "＋".join(f"{RES.get(n, f'r{n}')} {p}%" for n, p in found)


def animals_of(text):
    """回傳 [(單位編號, 數量)]；沒有動物回 []。"""
    if "{k.animals}" not in text:
        return []
    part = text.split("{k.animals}", 1)[1]
    return [(int(u), int(n)) for u, n in re.findall(r'class="unit u(\d+)"></i><span class="value\s*">(\d+)</span>', part)]


def parse_tiles(data):
    """→ list of dict(pos, kind, bonus, animals, owner)。kind：free_oasis／taken_oasis／village／other。
    只回傳綠洲（free_oasis、taken_oasis）；其餘格子忽略。"""
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    out = []
    for t in data.get("tiles", []):
        title, text = t.get("title", ""), t.get("text", "")
        if "{a:r" not in text:
            continue
        taken = "{k.bt}" in title or "uid" in t
        owner = None
        m = re.search(r"\{k\.spieler\}\s*([^<]*)", text)
        if m:
            owner = _h.unescape(m.group(1)).translate(_STRIP).strip()
        out.append(dict(pos=_pos(t), kind="taken_oasis" if taken else "free_oasis",
                        bonus=bonus_label(text), animals=animals_of(text), owner=owner))
    return out


def free_candidates(tiles, home=None, max_dist=None):
    """未佔領且沒動物的綠洲 → dispatch.OASES 格式 [(pos, bonus, True)]，依距離排序（環繞距離）。"""
    home = home or dispatch.HOME
    rows = [(dispatch.distance(home, o["pos"]), o["pos"], o["bonus"]) for o in tiles
            if o["kind"] == "free_oasis" and not o["animals"]]
    if max_dist is not None:
        rows = [r for r in rows if r[0] <= max_dist]
    return [(pos, bonus, True) for _d, pos, bonus in sorted(rows)]


def blocked_set(tiles):
    """有動物或已被佔領的綠洲座標（給 BLOCKED 用）。"""
    return {o["pos"] for o in tiles if o["kind"] == "taken_oasis" or o["animals"]}
