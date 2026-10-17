"""版本：hero-v1.3（2026-10-17，地點比對改以 JSON 為準、不再用寫死的白名單，修正 lake 等新地形被誤判不一致；hero-v1.2（2026-10-16，體力 > hero_hard_min_health 才可選 hard，可選時優先 hard；2026-10-13 hero-v1.1，BIDI 同時支援正規表示式與 list；版本歷史見 project-notes 的「版本對照」）
英雄冒險：解析（階段 A）與決策（階段 B）。純函式，不碰網路、不碰 Sheet。

依據：project-notes §13.0（英雄冒險自動化統整）。
- 狀態碼：100 在家／50 去程／9 返回；其他一律不出發。
- 冒險資料在 Travian.React.HeroAdventure.render({... viewData: {...}, activePerspective ...}) 的 JSON；
  列表格只在 Playwright 渲染後的 page.content() 才有。
- 難度圖示只有 hard／normal，直接讀每列圖示，不用 JSON 數字推。

放置位置：本檔獨立（只依賴標準庫＋bs4；BIDI 優先從 travian_const 匯入）。
若要併入 travian_agent.py，plan_adventure 可直接搬過去；check_layout.py 要同步登記。
"""
from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass, field
from typing import Optional

try:  # 與專案共用的雙向控制字元表
    from travian_const import BIDI  # type: ignore
except Exception:  # 離線測試或尚未部署時的備援
    BIDI = ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u200e", "\u200f"]

from bs4 import BeautifulSoup

STATUS_HOME = 100
STATUS_GOING = 50
STATUS_RETURNING = 9

_VIEWDATA_RE = re.compile(r"viewData:\s*(\{.*\}),\s*activePerspective")


def _clean(s: str) -> str:
    # 專案的 travian_const.BIDI 是編譯好的正規表示式（有 .sub）；備援表是字元 list，兩種都支援
    if hasattr(BIDI, "sub"):
        return BIDI.sub("", s).strip()
    for ch in BIDI:
        s = s.replace(ch, "")
    return s.strip()


def _unescape_twice(s: str) -> str:
    # 經驗標題是雙重跳脫（&amp;#x202d;）；體力是單重。多做一次無害。
    return _html.unescape(_html.unescape(s))


# ---------------------------------------------------------------- 資料結構
@dataclass
class HeroHud:
    health: Optional[int] = None        # 讀不到 = None
    experience: Optional[int] = None
    adventures_count: Optional[int] = None
    at_home: Optional[bool] = None      # i.heroHome=True / i.heroRunning=False；都沒有 = None


@dataclass
class Adventure:
    number: int
    distance: float
    place: str
    difficulty: int                     # JSON 數字；0 對應 hard 圖示，勿當危險程度
    duration: int                       # 去程秒數（travelingDuration）


@dataclass
class HeroState:
    status: Optional[int] = None
    in_village: bool = False
    adventure_number: Optional[int] = None   # 去程中的冒險編號
    arrival_in: Optional[int] = None
    regenerating: bool = False
    difficulty_bonus: bool = False
    duration_bonus: bool = False
    adventures: list[Adventure] = field(default_factory=list)


@dataclass
class AdventureRow:
    place: str
    distance: float                     # 已正規化為 3 位有效數字
    duration: Optional[int]             # 秒；讀不到 None
    icon: str                           # "hard" | "normal" | ""
    disabled: bool                      # 按鈕有 disabled class
    has_button: bool
    classes: tuple = ()                 # 地點圖片的全部 class；比對時看 JSON 的地點在不在裡面


# ---------------------------------------------------------------- 階段 A：解析
def parse_hero_hud(html: str) -> HeroHud:
    soup = BeautifulSoup(html, "lxml")
    hud = HeroHud()

    t = soup.select_one("#topBarHero svg.health path.title > title")
    if t is not None:
        m = re.search(r"(\d+)\s*%", _clean(_unescape_twice(t.get_text())))
        if m:
            hud.health = int(m.group(1))

    t = soup.select_one("#topBarHero svg.experience path.title > title")
    if t is not None:
        # 標題含「經驗 97%||還需要…」，只取第一個百分比
        m = re.search(r"(\d+)\s*%", _clean(_unescape_twice(t.get_text())))
        if m:
            hud.experience = int(m.group(1))

    c = soup.select_one("#topBarHero a.adventure .content")
    if c is not None:
        m = re.search(r"\d+", _clean(c.get_text()))
        if m:
            hud.adventures_count = int(m.group(0))

    # 圖示只能分在家／在外；去程與返回要看 JSON 的 status
    if soup.select_one("#topBarHero .heroStatus i.heroHome"):
        hud.at_home = True
    elif soup.select_one("#topBarHero .heroStatus i.heroRunning"):
        hud.at_home = False
    return hud


def _parse_duration(text: str) -> Optional[int]:
    m = re.search(r"(\d+):(\d{2}):(\d{2})", _clean(text))
    if not m:
        return None
    h, mi, s = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s


def _parse_distance(text: str) -> Optional[float]:
    m = re.search(r"\d+(?:\.\d+)?", _clean(text))
    return float(f"{float(m.group(0)):.3g}") if m else None


def parse_hero_json(html: str) -> Optional[HeroState]:
    """取出冒險頁 JSON；找不到或格式不符回 None（呼叫端應 skip 並通知）。"""
    m = _VIEWDATA_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        hero = data["data"]["ownPlayer"]["hero"]
        st = hero["status"]
    except (ValueError, KeyError, TypeError):
        return None
    adv = st.get("adventure") or {}
    state = HeroState(
        status=st.get("status"),
        in_village=bool(st.get("inVillage")),
        adventure_number=adv.get("number"),
        arrival_in=st.get("arrivalIn"),
        regenerating=bool(hero.get("isRegenerating")),
        difficulty_bonus=bool(hero.get("adventureDifficultyBonus")),
        duration_bonus=hero.get("adventureDurationBonus") is not None,
    )
    for a in hero.get("adventures") or []:
        state.adventures.append(
            Adventure(
                number=a["number"],
                distance=float(f"{float(a['distance']):.3g}"),
                place=a["place"],
                difficulty=a["difficulty"],
                duration=a["travelingDuration"],
            )
        )
    return state


def parse_adventure_rows(html: str) -> list[AdventureRow]:
    """渲染後表格；去程中（表格被文字取代）回空 list。"""
    soup = BeautifulSoup(html, "lxml")
    rows: list[AdventureRow] = []
    for tr in soup.select("table.adventureList tbody tr"):
        img = tr.select_one("td.place img")
        place_classes = tuple(img.get("class", [])) if img is not None else ()
        # 真實頁面（10-17 實測）：<img class="lake">，class 本身就是地形名。place 只給 log 看、不用白名單；
        # 是否一致由 rows_match_json 拿 JSON 的地點去比對 place_classes
        place = place_classes[0] if place_classes else ""
        dist_td = tr.select_one("td.distance")
        dur = tr.select_one("td.duration div.duration")
        icon_el = tr.select_one("td.difficulty i")
        icon = ""
        if icon_el is not None:
            classes = icon_el.get("class", [])
            icon = "hard" if "difficulty_hard" in classes else (
                "normal" if "difficulty_normal" in classes else "")
        btn = tr.select_one("td.button button")
        rows.append(
            AdventureRow(
                place=place,
                distance=_parse_distance(dist_td.get_text()) if dist_td else None,
                duration=_parse_duration(dur.get_text()) if dur else None,
                icon=icon,
                disabled=bool(btn is not None and "disabled" in btn.get("class", [])),
                has_button=btn is not None,
                classes=place_classes,
            )
        )
    return rows


def rows_match_json(state: HeroState, rows: list[AdventureRow]) -> Optional[str]:
    """列與 JSON 逐筆核對（地點、距離、時間）。一致回 None，否則回不一致原因。
    注意（距離、時間）會重複，所以一定要含地點；難度圖示來自列、JSON 數字不可比。"""
    if len(rows) != len(state.adventures):
        return f"列數 {len(rows)} ≠ JSON 筆數 {len(state.adventures)}"
    for i, (r, a) in enumerate(zip(rows, state.adventures)):
        # 地點以 JSON 為準：表格圖片 class 裡要含有 JSON 的地點（不用白名單，新地形也能對上）
        place_ok = (r.place == a.place) or (a.place in r.classes)
        if not place_ok or r.distance != a.distance or r.duration != a.duration:
            return f"第 {i + 1} 列與 JSON 不一致（{r.place}/{r.distance}/{r.duration} vs {a.place}/{a.distance}/{a.duration}）"
    return None


# ---------------------------------------------------------------- 階段 B：決策
def plan_adventure(
    hero: Optional[HeroState],
    rows: list[AdventureRow],
    health: Optional[int],
    cfg: dict,
    incoming: Optional[int] = None,
):
    """回傳 ("send", number, row_index) 或 ("skip", 原因)。

    cfg 讀取：hero_min_health（預設 45）、hero_allowed（預設 "normal"，逗號分隔）、
    hero_skip_on_incoming（預設 True）。任何沒見過的狀態一律 skip。
    row_index 是 0 起算的列索引（點擊時用 +1 或 nth(index)，由呼叫端決定）。
    """
    if hero is None:
        return ("skip", "讀不到冒險頁 JSON")
    if hero.status == STATUS_GOING:
        return ("skip", f"已出發（冒險 #{hero.adventure_number}，剩 {hero.arrival_in} 秒抵達）")
    if hero.status == STATUS_RETURNING:
        return ("skip", f"返回中，還剩 {hero.arrival_in} 秒")
    if hero.status != STATUS_HOME:
        return ("skip", f"未知狀態碼 {hero.status}")
    if not hero.in_village:
        return ("skip", "狀態 100 但 inVillage 為空")
    if hero.adventure_number is not None:
        return ("skip", f"已有進行中的冒險 #{hero.adventure_number}")
    if hero.regenerating:
        return ("skip", "英雄復活中")

    min_health = int(cfg.get("hero_min_health", 45))
    if health is None:
        return ("skip", "讀不到體力")
    if health <= min_health:
        return ("skip", f"體力 {health}% ≤ {min_health}")

    if hero.difficulty_bonus:
        return ("skip", "下一次普通冒險會提高為困難（difficulty bonus）")

    if cfg.get("hero_skip_on_incoming", True):
        if incoming is None:
            return ("skip", "來襲數未知")
        if incoming > 0:
            return ("skip", f"有來襲 {incoming} 筆")

    if not hero.adventures:
        return ("skip", "沒有可選的冒險")
    mismatch = rows_match_json(hero, rows)
    if mismatch:
        return ("skip", mismatch)

    allowed = {x.strip() for x in str(cfg.get("hero_allowed", "normal")).split(",") if x.strip()}
    if health <= int(cfg.get("hero_hard_min_health", 60)):
        allowed.discard("hard")                       # 體力不夠高時，即使 hero_allowed 有 hard 也不選困難
    candidates = []
    for i, (r, a) in enumerate(zip(rows, hero.adventures)):
        if r.icon not in allowed:
            continue
        if not r.has_button or r.disabled:
            continue
        candidates.append((0 if r.icon == "hard" else 1, a.duration, a.number, i))   # 能選 hard（體力夠高且允許）就優先 hard
    if not candidates:
        return ("skip", f"沒有符合難度 {sorted(allowed)} 且可按的冒險")
    _, _, number, idx = min(candidates)  # hard 優先（體力不夠時 hard 已被排除）；同類取去程最短；同時間取編號小
    return ("send", number, idx)
