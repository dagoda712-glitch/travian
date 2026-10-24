"""綠洲詳情解析（純函式，不連遊戲）。輸入 POST /api/v1/map/tile-details 回傳 JSON 的 html 字串。
v0.2：三種樣本都對照過（✓ 10-23）：未佔領無動物 (-200|171)、未佔領有動物 (198|173)、
已被佔領 (198|171)。判定仍保守：只有「未佔領＋軍隊表剛好是『無』」才算安全。"""
import html as _h
import re

_STRIP = dict.fromkeys(map(ord, "\u202d\u202c"), None)


def _text(s):
    s = re.sub(r"<script.*?</script>", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _h.unescape(s).translate(_STRIP).replace("\u2212", "-")
    return re.sub(r"\s+", " ", s).strip()


def parse_tile_details(page):
    out = dict(title="", pos=None, unoccupied=False, occupied=False, owner=None,
               animals=[], troops_none=False,
               troops_text="", distance=None, map_id=None,
               bonus=[], occupy_events=[])
    m = re.search(r'<h1[^>]*titleInHeader[^>]*>(.*?)</h1>', page, re.S)
    if m:
        out["title"] = _text(m.group(1))
        c = re.search(r"\(\s*(-?\d+)\s*\|\s*(-?\d+)\s*\)", out["title"])
        if c:
            out["pos"] = (int(c.group(1)), int(c.group(2)))
        out["unoccupied"] = "未被佔領" in out["title"]
        out["occupied"] = "已被佔領" in out["title"]
    m = re.search(r"targetMapId=(\d+)", page)
    if m:
        out["map_id"] = int(m.group(1))
    m = re.search(r"<h4>\s*加成\s*</h4>(.*?)</table>", page, re.S)
    if m:
        out["bonus"] = [_text(r) for r in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S)]
    # 注意：頁面有兩個 id="troop_info"（軍隊表、報告表），要抓「軍隊：」後面那個
    m = re.search(r"<h4>\s*軍隊：\s*</h4>\s*<table.*?<tbody>\s*<tr>(.*?)</tr>", page, re.S)
    if m:
        out["troops_text"] = _text(m.group(1))
        out["troops_none"] = out["troops_text"] == "無"
    m = re.search(r"<h4>\s*軍隊：\s*</h4>\s*(<table.*?</table>)", page, re.S)
    if m:
        out["animals"] = [(int(u), int(n), _text(d)) for u, n, d in re.findall(
            r'class="unit u(\d+)".*?<td class="val">\s*(\d+)\s*</td>\s*<td class="desc">(.*?)</td>',
            m.group(1), re.S)]
    m = re.search(r"持有人：</th>\s*<td>(.*?)</td>", page, re.S)
    if m:
        out["owner"] = _text(m.group(1))
    m = re.search(r'id="distance".*?class="bold">\s*([\d.]+)', page, re.S)
    if m:
        out["distance"] = float(m.group(1))
    m = re.search(r'id="surrounding_info".*?</table>', page, re.S)
    if m:
        out["occupy_events"] = [_text(r) for r in re.findall(r"<tr>(.*?)</tr>", m.group(0), re.S)
                                if "oasisOccupy" in r]
    return out


def is_safe(info, expect_pos=None):
    """保守判定：標題是「未被佔領」、座標符合、軍隊表剛好是「無」才算安全。"""
    if not info["unoccupied"] or not info["troops_none"] or info["animals"]:
        return False
    return expect_pos is None or info["pos"] == tuple(expect_pos)


def recent_occupy_warning(info, markers=("今天", "昨天", "分鐘", "小時")):
    """附近有最近的佔領事件（相對時間字樣）→ True。格式只看過「2 日前, HH:MM」與日期（△）。"""
    return any(any(k in e for k in markers) for e in info["occupy_events"])
