#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Travian Legends 村庄页面 (dorf1.php / dorf2.php) 解析器

用法一：解析本地/已保存的 HTML
    from travian_parser import parse_village_page
    with open("dorf2.html", encoding="utf-8") as f:
        data = parse_village_page(f.read())

用法二：配合登录后的 requests.Session 直接抓取
    from travian_parser import TravianClient
    client = TravianClient("https://xxx.travian.com")
    client.login("your_username", "your_password")
    data = client.get_village_overview()

注意：
- Travian 的登录、验证码、Cloudflare 等反爬机制可能变化，登录部分仅作为示例框架，
  请遵守 Travian 的服务条款（多数官服禁止未授权自动化操作，仅建议用于自己账号的
  只读数据查看/个人小工具，不要用于外挂或自动化点击等违反 ToS 的行为）。
- 页面结构（class 名、data-* 属性）可能随游戏版本更新而变化，若解析失败请检查
  对应 HTML 中的 class/id 是否仍然一致。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from bs4 import BeautifulSoup


# --------------------------------------------------------------------------
# 数据结构定义
# --------------------------------------------------------------------------

@dataclass
class ResourceInfo:
    """单类资源（木材/砖块/钢铁/穀物）"""
    key: str            # l1 / l2 / l3 / l4
    name: str            # 中文名
    amount: int = 0       # 当前存量
    capacity: int = 0     # 仓库/谷仓容量
    production: int = 0   # 每小时产量（含/不含扣除，取决于资源类型）
    bar_percent: float = 0.0  # 存量条百分比


@dataclass
class HeroInfo:
    health_percent: Optional[int] = None
    experience_percent: Optional[int] = None
    status_title: Optional[str] = None     # 例如 "英雄簡介||你的英雄正在前往冒險..."
    adventures_available: Optional[int] = None


@dataclass
class BuildingSlot:
    aid: str               # 地块编号 a19, a26 ...
    gid: str                # 建筑类型 id（0 表示空地）
    building_id: Optional[str] = None
    name: Optional[str] = None
    level: Optional[int] = None
    is_empty: bool = True


@dataclass
class QueueItem:
    """建造队列中的项目"""
    name: str
    level: Optional[int] = None
    remaining_seconds: Optional[int] = None
    finish_time: Optional[str] = None       # 页面上显示的完成时刻，如 "12:01"


@dataclass
class VillageOverview:
    player_name: Optional[str] = None
    village_name: Optional[str] = None
    village_id: Optional[str] = None
    coordinates: Optional[tuple] = None      # (x, y)
    population: Optional[int] = None
    loyalty_percent: Optional[int] = None

    gold: Optional[int] = None
    silver: Optional[int] = None

    server_time: Optional[str] = None
    timezone: Optional[str] = None

    resources: dict = field(default_factory=dict)   # key -> ResourceInfo
    free_crop: Optional[int] = None
    warehouse_capacity: Optional[int] = None
    granary_capacity: Optional[int] = None

    hero: HeroInfo = field(default_factory=HeroInfo)

    building_slots: list = field(default_factory=list)     # list[BuildingSlot]
    build_queue: list = field(default_factory=list)          # list[QueueItem]

    infobox_messages: list = field(default_factory=list)     # 资讯板文字列表

    def to_dict(self):
        d = asdict(self)
        d["resources"] = {k: asdict(v) for k, v in self.resources.items()}
        return d


# --------------------------------------------------------------------------
# 解析工具函数
# --------------------------------------------------------------------------

_RESOURCE_NAMES = {
    "l1": "木材",
    "l2": "磚塊",
    "l3": "鋼鐵",
    "l4": "穀物",
}


def _to_int(text: str) -> Optional[int]:
    """从形如 '&#x202d;551&#x202c;' 或含 &minus; 的坐标文本中提取整数（支持负数）。"""
    if text is None:
        return None
    # 去掉方向控制符 (LRO/PDF 等不可见 unicode 字符)、千分位逗号
    cleaned = re.sub(r"[\u202a-\u202e\u2066-\u2069,\s]", "", text)
    # 统一负号：− (U+2212) 或 HTML 实体解码后的 − 都当作负号
    cleaned = cleaned.replace("\u2212", "-").replace("−", "-")
    m = re.search(r"-?\d+", cleaned)
    return int(m.group()) if m else None


def _timer_seconds(tag) -> Optional[int]:
    """从 <span class="timer" value="106">...</span> 中取秒数"""
    if tag is None:
        return None
    v = tag.get("value")
    return int(v) if v and v.lstrip("-").isdigit() else None


def parse_resources(soup: BeautifulSoup) -> dict:
    """解析顶部资源栏 (#stockBar) 的木/砖/铁/粮存量、产量、仓储上限。"""
    result = {}
    stockbar = soup.find(id="stockBar")
    if not stockbar:
        return result, None, None

    for key, cn_name in _RESOURCE_NAMES.items():
        link = stockbar.find("a", class_=f"stockBarButton resource{key[-1]}")
        if not link:
            continue

        amount_tag = link.find(id=f"l{key[-1]}")
        amount = _to_int(amount_tag.get_text()) if amount_tag else None

        bar = link.find(id=f"lbar{key[-1]}")
        bar_pct = None
        if bar and bar.get("style"):
            m = re.search(r"width:\s*([\d.]+)%", bar["style"])
            bar_pct = float(m.group(1)) if m else None

        # title 属性里含 "成果: 74<br />充滿在: ..." 或（穀物专属）
        # "扣除建築物所需之後的生產力: 69<br />充滿在: ..."
        title = link.get("title", "")
        prod_match = re.search(r"(?:成果|扣除建築物所需之後的生產力)[:：]\s*(-?\d+)", title)
        production = int(prod_match.group(1)) if prod_match else None

        result[key] = ResourceInfo(
            key=key,
            name=cn_name,
            amount=amount or 0,
            production=production or 0,
            bar_percent=bar_pct or 0.0,
        )

    # 仓库/谷仓容量
    warehouse = stockbar.find(class_="warehouse")
    granary = stockbar.find(class_="granary")

    def _capacity(box):
        if not box:
            return None
        cap_tag = box.find(class_="capacity")
        if not cap_tag:
            return None
        val_tag = cap_tag.find(class_="value")
        return _to_int(val_tag.get_text()) if val_tag else None

    wh_cap = _capacity(warehouse)
    gr_cap = _capacity(granary)
    for k in ("l1", "l2", "l3"):
        if k in result:
            result[k].capacity = wh_cap or 0
    if "l4" in result:
        result["l4"].capacity = gr_cap or 0

    return result, wh_cap, gr_cap


def parse_free_crop(soup: BeautifulSoup) -> Optional[int]:
    tag = soup.find(id="stockBarFreeCrop")
    return _to_int(tag.get_text()) if tag else None


def parse_gold_silver(soup: BeautifulSoup):
    gold_tag = soup.find(class_="ajaxReplaceableGoldAmount")
    silver_tag = soup.find(class_="ajaxReplaceableSilverAmount")
    return _to_int(gold_tag.get_text()) if gold_tag else None, \
           _to_int(silver_tag.get_text()) if silver_tag else None


def parse_server_time(soup: BeautifulSoup):
    box = soup.find(id="servertime")
    if not box:
        return None, None
    timer = box.find(class_="timer")
    time_str = timer.get_text(strip=True) if timer else None
    tz_match = re.search(r"UTC\s*[+\-]\s*[\d:]+", box.get_text())
    tz = tz_match.group(0) if tz_match else None
    return time_str, tz


def parse_player_and_village(soup: BeautifulSoup) -> dict:
    info = {}
    player_tag = soup.find(class_="playerName")
    info["player_name"] = player_tag.get_text(strip=True) if player_tag else None

    village_input = soup.select_one("#villageName input.villageInput")
    if village_input:
        info["village_name"] = village_input.get("value")
        info["village_id"] = village_input.get("data-did")

    pop_tag = soup.find(class_="population")
    if pop_tag:
        span = pop_tag.find("span")
        info["population"] = _to_int(span.get_text()) if span else None

    loyalty_tag = soup.find(class_="loyalty")
    if loyalty_tag:
        span = loyalty_tag.find("span")
        info["loyalty_percent"] = _to_int(span.get_text()) if span else None

    # 坐标：来自侧栏村庄列表 coordinatesGrid，或 embedded JSON
    coord_tag = soup.select_one(".listEntry.village.active .coordinatesGrid")
    if coord_tag:
        x_tag = coord_tag.find(class_="coordinateX")
        y_tag = coord_tag.find(class_="coordinateY")
        x = _to_int(x_tag.get_text()) if x_tag else None
        y = _to_int(y_tag.get_text()) if y_tag else None
        info["coordinates"] = (x, y)

    return info


def parse_hero(soup: BeautifulSoup) -> HeroInfo:
    hero = HeroInfo()
    hero_box = soup.find(id="topBarHero")
    if not hero_box:
        return hero

    # 血量/经验的 <title> 文字里含百分比，例如 "體力: 100%"
    for svg in hero_box.select("svg.health title, svg.experience title"):
        text = svg.get_text()
        pct = re.search(r"(\d+)\s*%", text.replace("&#37;", "%"))
        if "體力" in text or "health" in svg.parent.get("class", []):
            pass

    health_title = hero_box.select_one("svg.health path.title")
    exp_title = hero_box.select_one("svg.experience path.title")
    if health_title:
        m = re.search(r"(\d+)", health_title.get_text())
        hero.health_percent = int(m.group(1)) if m else None
    if exp_title:
        m = re.search(r"(\d+)", exp_title.get_text())
        hero.experience_percent = int(m.group(1)) if m else None

    status_tag = hero_box.find(class_="heroStatus")
    hero.status_title = status_tag.get("title") if status_tag else None

    adv_btn = hero_box.find(class_="adventure")
    if adv_btn:
        content = adv_btn.find(class_="content")
        hero.adventures_available = _to_int(content.get_text()) if content else None

    return hero


def parse_building_slots(soup: BeautifulSoup) -> list:
    slots = []
    village_content = soup.find(id="villageContent")
    if not village_content:
        return slots

    for div in village_content.find_all("div", class_="buildingSlot"):
        aid = div.get("data-aid")
        gid = div.get("data-gid")
        bid = div.get("data-building-id")
        name = div.get("data-name") or None
        is_empty = (gid == "0")

        level = None
        level_link = div.find("a", class_="level")
        if level_link:
            level = level_link.get("data-level")
            level = int(level) if level and level.isdigit() else None

        slots.append(BuildingSlot(
            aid=aid, gid=gid, building_id=bid,
            name=name if name else None,
            level=level,
            is_empty=is_empty,
        ))
    return slots


def parse_build_queue(soup: BeautifulSoup) -> list:
    queue = []
    building_list = soup.find(class_="buildingList")
    if not building_list:
        return queue

    for li in building_list.select("ul > li"):
        name_div = li.find(class_="name")
        if not name_div:
            continue
        # name 文本里混了等级 span，先取等级再拿纯名字
        lvl_span = name_div.find(class_="lvl")
        level = None
        if lvl_span:
            m = re.search(r"(\d+)", lvl_span.get_text())
            level = int(m.group(1)) if m else None
            lvl_span.extract()  # 从 DOM 里移除，方便拿纯名字
        name = name_div.get_text(strip=True)

        duration_div = li.find(class_="buildDuration")
        remaining, finish_time = None, None
        if duration_div:
            timer = duration_div.find(class_="timer")
            remaining = _timer_seconds(timer)
            m = re.search(r"完成時間於\s*([\d:]+)", duration_div.get_text())
            finish_time = m.group(1) if m else None

        queue.append(QueueItem(
            name=name, level=level,
            remaining_seconds=remaining, finish_time=finish_time,
        ))
    return queue


def parse_infobox_messages(soup: BeautifulSoup) -> list:
    messages = []
    box = soup.find(id="sidebarBoxInfobox")
    if not box:
        return messages
    for li in box.select("ul > li"):
        text = li.get_text(" ", strip=True)
        # 去掉里面按钮文字重复的部分，只保留核心信息
        text = re.sub(r"\s+", " ", text)
        messages.append(text)
    return messages


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def parse_village_page(html: str) -> VillageOverview:
    soup = BeautifulSoup(html, "lxml")

    overview = VillageOverview()

    player_info = parse_player_and_village(soup)
    overview.player_name = player_info.get("player_name")
    overview.village_name = player_info.get("village_name")
    overview.village_id = player_info.get("village_id")
    overview.population = player_info.get("population")
    overview.loyalty_percent = player_info.get("loyalty_percent")
    overview.coordinates = player_info.get("coordinates")

    overview.gold, overview.silver = parse_gold_silver(soup)
    overview.server_time, overview.timezone = parse_server_time(soup)

    resources, wh_cap, gr_cap = parse_resources(soup)
    overview.resources = resources
    overview.warehouse_capacity = wh_cap
    overview.granary_capacity = gr_cap
    overview.free_crop = parse_free_crop(soup)

    overview.hero = parse_hero(soup)
    overview.building_slots = parse_building_slots(soup)
    overview.build_queue = parse_build_queue(soup)
    overview.infobox_messages = parse_infobox_messages(soup)

    return overview


# --------------------------------------------------------------------------
# 可选：登录 + 抓取客户端（框架示例，具体登录流程需按目标服务器实际接口调整）
# --------------------------------------------------------------------------

class TravianClient:
    """
    简单封装：用 requests.Session 登录并抓取 dorf2.php。
    仅作为个人账号只读查看用途的示例，请自行确认符合目标服务器的使用条款。
    """

    def __init__(self, base_url: str):
        import requests
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; PersonalVillageBot/1.0)"
        })

    def login(self, username: str, password: str):
        """
        注意：Travian 官服的登录接口可能是 POST /api/v1/auth/login 之类的 JSON 接口，
        并可能带 CSRF token / 验证码，需要你用浏览器开发者工具抓包确认真实的登录请求
        （Network 面板里找登录时发出的 POST 请求），再对应改写这里的实现。
        以下仅为占位示例。
        """
        raise NotImplementedError(
            "请根据实际登录接口（抓包获得）填写此方法，"
            "本框架仅提供页面解析部分（parse_village_page）。"
        )

    def get_village_overview(self, village_page: str = "dorf2.php") -> VillageOverview:
        resp = self.session.get(f"{self.base_url}/{village_page}")
        resp.raise_for_status()
        return parse_village_page(resp.text)


# --------------------------------------------------------------------------
# CLI：直接对本地 HTML 文件跑一遍，打印 JSON 结果
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python travian_parser.py <本地HTML文件路径>")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        html = f.read()

    data = parse_village_page(html)
    print(json.dumps(data.to_dict(), ensure_ascii=False, indent=2))
