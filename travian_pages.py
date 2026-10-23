#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Travian 頁面解析（決策用）：HTML 進、資料出，不碰網路、不碰 Sheet。

  parse_dorf1()           dorf1.php  → State（資源量、資源田、施工佇列、來襲、保護/Plus）
  parse_movements()       dorf1 的「來村部隊」表格
  parse_dorf2()           dorf2.php  → {gid: (aid, level)}（只含 BUILDING_GIDS 裡的建築）
  parse_dorf2_slots()     dorf2.php  → 全部建築位（含空地）
  parse_building_cost()   村莊中心建築的 build.php → (cost, secs)
  parse_player_info()     頁面內嵌 viewData → 種族 / Plus / 文化點
  parse_build_options()   空地 build.php 的「建造新的建築物」頁面
  fetch_build_options()   抓空地的三個分頁並合併（get 由呼叫端傳入）
  find_upgrade_url()      只認綠色 build 按鈕，且網址必須通過白名單

注意：travian_parser.py 是另一套「村莊總覽」解析器（給 Sheet1 用），兩者的 _to_int 行為不同，
這裡的版本改名為 _to_int0（讀不到數字回 0、不處理負數）。
"""
import html as _html
import json
import re

from bs4 import BeautifulSoup

from travian_const import BIDI, BUILD_CATEGORIES, BUILDING_GIDS
from travian_models import Field, Slot, State


# ---------------- 解析 ----------------
def _js_dict(block: str, key: str) -> dict:
    m = re.search(key + r":\s*\{([^}]*)\}", block)
    return {k: int(v) for k, v in re.findall(r'"(l\d)":\s*(-?\d+)', m.group(1))} if m else {}


def _hms(s: str) -> int:
    h, m, sec = (int(x) for x in s.split(":"))
    return h * 3600 + m * 60 + sec


def _queue_label(li, secs: int) -> str:
    """施工佇列一項的文字：'農場 等級 1 剩0:01:46'。抓不到名稱時顯示 '（未知建築）'。"""
    n = li.select_one("div.name")
    name, lvl = "", ""
    if n is not None:
        lv = n.select_one("span.lvl")
        if lv is not None:
            lvl = BIDI.sub("", lv.get_text(" ", strip=True))
            lv.extract()
        name = BIDI.sub("", n.get_text(" ", strip=True))
    left = f"剩{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
    return " ".join(x for x in (name or "（未知建築）", lvl, left) if x)


def parse_dorf1(html: str) -> State:
    # 1) 資源量/容量/產量：直接讀頁面內的 var resources（比讀畫面數字可靠）
    blk = re.search(r"var resources = \{(.*?)\};", html, re.S)
    blk = blk.group(1) if blk else ""
    prod, stock, cap = (_js_dict(blk, k) for k in ("production", "storage", "maxStorage"))
    st = State(
        stock=[stock.get(f"l{i}", 0) for i in range(1, 5)],
        cap=[cap.get(f"l{i}", 0) for i in range(1, 5)],
        prod=[prod.get(f"l{i}", 0) for i in range(1, 5)],
        free_crop=prod.get("l5", 0),
    )

    soup = BeautifulSoup(html, "html.parser")

    # 2) 資源田：等級在 class，升級成本/時間在 title（HTML 實體，bs4 會解碼）
    for a in soup.select("#resourceFieldContainer a.resourceField"):
        cls = " ".join(a.get("class", []))
        lvl = re.search(r"\blevel(\d+)\b", cls)
        tip = a.get("title", "")
        cost = [int(v) for _, v in sorted(re.findall(r'class="r(\d)Big"></i><span class="value ">(\d+)<', tip))]
        dur = re.search(r'clock_medium"></i><span class="value ">(\d+:\d+:\d+)<', tip)
        st.fields.append(Field(
            aid=int(a["data-aid"]), gid=int(a["data-gid"]),
            level=int(lvl.group(1)) if lvl else 0,
            cost=cost, secs=_hms(dur.group(1)) if dur else 0,
            building="underConstruction" in cls,
        ))

    # 3) 施工佇列（選擇器未驗證：需要一份「正在施工」的 dorf1 來確認）
    for li in soup.select("div.buildingList li"):
        t = li.select_one("span.timer")
        if t and t.get("value", "").isdigit():
            st.queue.append(int(t["value"]))
            st.queue_info.append(_queue_label(li, int(t["value"])))

    # 4) 來襲：注意 .incomingTroops 圖示在沒有來襲時也存在，不能用「元素存在」判斷
    st.attacks = sum(int(x) for x in re.findall(r'incomingAttacksAmount":(\d+)', html))
    st.movements = parse_movements(soup)
    etas = [m["seconds"] for m in st.movements if m["kind"] == "attack" and m["seconds"] is not None]
    st.attack_eta_s = min(etas) if etas else None

    # 5) 新手保護 / Plus 剩餘秒數（用中文文字比對）
    for li in soup.select("#sidebarBoxInfobox li"):
        txt = BIDI.sub("", li.get_text(" ", strip=True))
        t = li.select_one("span.timer")
        if not (t and t.get("value", "").isdigit()):
            continue
        if "新手保護" in txt:
            st.protection_s = int(t["value"])
        elif "Plus" in txt:
            st.plus_s = int(t["value"])
    return st


# ---------------- 來村部隊（dorf1 的 #movements 表格） ----------------
# 已用真實頁面確認：img.att1 = 即將抵達的敵人軍隊（攻擊）、img.def1 = 即將抵達的援軍。
# 其他 class（自己出發的部隊、掠奪等）還沒看過樣本，原樣保留在 kind 裡，不猜意思。
_MOVEMENT_KINDS = {"att1": "attack", "def1": "reinforcement"}


def parse_movements(soup) -> list:
    """回傳 [{"kind": "attack"|"reinforcement"|<原 class>, "count": int|None, "seconds": int|None, "label": str}]。
    soup 可以是 BeautifulSoup 物件或 HTML 字串。沒有 #movements 表格 = 空 list。"""
    if isinstance(soup, str):
        soup = BeautifulSoup(soup, "html.parser")
    tbl = soup.find(id="movements")
    out = []
    if tbl is None:
        return out
    for tr in tbl.find_all("tr"):
        img = tr.select_one("td.typ img")
        if img is None:
            continue
        cls = next((c for c in img.get("class", []) if re.fullmatch(r"(att|def|rai|rei)\d+", c)), "")
        label = BIDI.sub("", (tr.select_one("div.mov") or tr).get_text(" ", strip=True))
        n = re.match(r"\s*(\d+)", label)
        t = tr.select_one("span.timer")
        secs = int(t["value"]) if t is not None and t.get("value", "").isdigit() else None
        out.append({"kind": _MOVEMENT_KINDS.get(cls, cls or "unknown"),
                    "count": int(n.group(1)) if n else None, "seconds": secs, "label": label})
    return out


def parse_dorf2(html: str) -> dict:
    """解析 dorf2.php（村莊中心），回傳 {gid: (aid, level)}，
    只保留 BUILDING_GIDS 裡關心的建築（目前是倉庫/糧倉）。
    尚未建造的位置沒有 a.level，level 視為 0（跳過，不會被當成候選，
    因為蓋新建築跟升級既有建築的按鈕/流程不一樣，尚未驗證）。"""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for slot in soup.select("#villageContent .buildingSlot"):
        try:
            aid, gid = int(slot["data-aid"]), int(slot["data-gid"])
        except (KeyError, ValueError):
            continue
        if gid not in BUILDING_GIDS:
            continue
        a = slot.select_one("a.level")
        if not a or not a.get("data-level", "").isdigit():
            continue
        out[gid] = (aid, int(a["data-level"]))
    return out


def _div_block(html: str, open_pattern: str):
    """回傳第一個符合 open_pattern 的 <div ...> 元素的完整 HTML（含巢狀 div，用深度計數找對應的 </div>）；找不到回 None。"""
    m = re.search(open_pattern, html)
    if not m:
        return None
    depth = 0
    for t in re.finditer(r"<(/?)div\b", html[m.start():]):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return html[m.start(): m.start() + t.end()]
    return html[m.start():]    # 沒有對應的結尾（HTML 被截斷）：取到最後


def parse_building_cost(html: str) -> tuple:
    """解析 build.php?id=<aid> 的村莊中心建築頁面（倉庫/糧倉），回傳 (cost, secs)。
    跟資源田的 tooltip 格式不同（debug_village_buildings.py 已驗證）：
      - 成本/時間直接寫在頁面正文，不是 <a> 的 title 屬性
      - 成本的 class 是 "value value"（資源田是單一個 "value "）
      - 數字外層包了雙向文字控制字元 \\u202d...\\u202c，跟 BIDI 常數要清的是同一種，
        用同一個 BIDI.sub 清掉即可（不影響 \\d+，只是保險起見先清乾淨）
      - 時間前面多一個 </div>："clock_medium"></i></div><span..."
    """
    # v2.37：成本數字 >= 1,000 時頁面有千分位逗號（例如 "2,550"），舊的 (\\d+)< 遇到逗號就整筆漏掉，
    # 剩下的數字往前擠（行宮 L5->L6 只解析出 [620]），之後 zip(cost, stock) 會把穀成本當成木成本。
    # 另外：requests 抓到的原始 HTML 把雙向控制字元寫成實體（&#x202d;），Playwright 的 page.content() 則是真字元；
    # 先 unescape 再 BIDI.sub，兩種來源都處理。
    clean = BIDI.sub("", _html.unescape(html))
    # v2.38：只讀「升級」區塊（<div class="upgradeBuilding">）。研究院的 build.php 在同一頁還列出每個兵種的研究成本
    # （<div class="research">），標記和升級成本完全一樣（r1Big…value value）；舊版掃整頁、同一種資源後面的值覆蓋前面，
    # 研究院升級成本會被最後一個兵種（族長 15,880/22,900/25,200/22,660）蓋掉，成本 > 倉庫容量 → 誤升倉庫。
    upgrade = _div_block(clean, r'<div\s+class="upgradeBuilding\b')
    scope = upgrade or _div_block(clean, r'<div\s+id="contract"')   # 成本區：升級外框 → 退一步只認 #contract
    if scope is None:
        # 找不到升級成本區。頁面若有兵種研究清單（<div class="research">）或訓練表單（buildActionOverview，兵營等），
        # 那些兵種成本也是 r1Big…value value，不能當升級成本 → 回空，呼叫端會當「讀不到升級成本」處理。
        if 'class="research"' in clean or "buildActionOverview" in clean:
            return [], 0
        scope = clean          # 其他建築頁面改版：退回舊行為，並在下面對「多組成本」發警告
    cost_re = r'class="r([1-4])Big"></i><span class="value value">([\d.,\s]*\d)\s*<'
    matches = re.findall(cost_re, scope)
    if len(matches) != 4:
        print(f"[警告] parse_building_cost 找到 {len(matches)} 筆成本欄位（預期 4）：{matches[:8]}")
    found = {int(i): int(re.sub(r"\D", "", v)) for i, v in matches}
    # 一定要四種資源都讀到；少任何一種就視為解析失敗（回空 list），不要回「縮水」的清單讓呼叫端錯位比較
    cost = [found[i] for i in (1, 2, 3, 4)] if set(found) == {1, 2, 3, 4} else []
    # 工期欄位跟成本欄位的 class 不一樣：這裡其實是 "value "（單一個，跟資源田同格式），
    # 不是成本用的 "value value"（兩個）。原本兩個都用 "value value" 找，導致 secs 永遠是 0。
    # 工期在 .upgradeButtonsContainer（可能不在 #contract 裡），所以用升級外框或按鈕區塊找，最後才退回整頁。
    dur_scope = upgrade or _div_block(clean, r'<div\s+class="upgradeButtonsContainer\b') or scope
    dur = re.search(r'clock_medium"></i></div><span class="value ">(\d+:\d+:\d+)<', dur_scope)
    return cost, (_hms(dur.group(1)) if dur else 0)


# ---------------- 玩家資訊：種族 / Plus / 文化點（頁面內嵌的 viewData JSON） ----------------
# dorf2 頁面（已用真實頁面確認）的側邊欄有一段 `viewData: {...}`，裡面的 ownPlayer 有
# tribeId、goldFeatures.travianPlus.isActive、culturalPointsOverview。dorf1 有沒有同一段
# 還沒確認，所以讀不到時呼叫端要改讀 dorf2，兩邊都沒有就退回保守值，不要猜。
def parse_player_info(html: str):
    """回傳 {"tribe_id": int|None, "plus_active": bool|None, "cp": dict|None}；整段找不到回傳 None。
    cp = {"have": 已累積, "need": 下一個村莊需要, "per_day": 每日產量, "used": 已用名額, "max": 名額上限}。"""
    m = re.search(r"viewData:\s*(\{)", html)
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, m.start(1))
        p = data["ownPlayer"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(p, dict):
        return None

    plus = ((p.get("goldFeatures") or {}).get("travianPlus") or {}).get("isActive")
    tribe = p.get("tribeId")
    cp = None
    c = p.get("culturalPointsOverview")
    if isinstance(c, dict):
        try:
            cp = {"have": int(c["cpProducedForNextSlot"]), "need": int(c["cpNeededForNextSlot"]),
                  "per_day": int(c["cpProductionTotal"]),
                  "used": int(c["usedSlots"]), "max": int(c["maxControllableVillages"])}
        except (KeyError, TypeError, ValueError):
            cp = None
    return {"tribe_id": tribe if isinstance(tribe, int) else None,
            "plus_active": plus if isinstance(plus, bool) else None,
            "cp": cp}


def parse_dorf2_slots(html: str) -> list:
    """解析 dorf2.php，回傳村莊中心「所有」建築位（含空地）的 Slot 清單。
    選擇器（#villageContent .buildingSlot / data-aid / data-gid / a.level[data-level]）
    已用真實頁面驗證過（debug_village_buildings.py 抓到 22 個建築位）。
    「施工中」是看 class 有沒有 underConstruction（跟 dorf1 資源田同名），
    這點在 dorf2 上還沒用真實的「施工中」頁面驗證過。"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for slot in soup.select("#villageContent .buildingSlot"):
        try:
            aid, gid = int(slot["data-aid"]), int(slot["data-gid"])
        except (KeyError, ValueError):
            continue
        a = slot.select_one("a.level")
        lv = a.get("data-level", "") if a is not None else ""
        cls = " ".join(slot.get("class", [])) + " " + " ".join(a.get("class", []) if a is not None else [])
        out.append(Slot(aid=aid, gid=gid, level=int(lv) if lv.isdigit() else 0,
                        name=slot.get("data-name", ""), building="underConstruction" in cls))
    return out


# ---------------- 空地「建造新的建築物」頁面（gid=0） ----------------
def _clean_text(el) -> str:
    """取出元素文字，去掉雙向文字控制字元。"""
    return BIDI.sub("", el.get_text("", strip=True)) if el is not None else ""


def _to_int0(s: str) -> int:
    """'2,880' -> 2880。頁面上的數字可能有千分位逗號。"""
    d = re.sub(r"[^\d]", "", s or "")
    return int(d) if d else 0


# 「興建/擴建」按鈕的網址只認這一種格式（已在真實頁面看過的：
# /dorf1.php?id=7&gid=3&action=build&checksum=76c9cd、/dorf2.php?id=35&gid=17&action=build&checksum=89273d）。
# 多任何參數（例如金幣的 &buildmaster、看廣告的 &as=）一律不算，避免不小心花金幣。
_BUILD_URL_RE = re.compile(r"/dorf[12]\.php\?id=\d+&gid=\d+&action=build&checksum=[0-9a-fA-F]+")


_rejected_seen = set()   # 同一個被拒絕的網址在同一輪只警告一次，避免洗版


def _mask_checksum(u: str) -> str:
    """log 裡不要留下完整的 checksum，只留長度，方便判斷格式有沒有變。"""
    return re.sub(r"(checksum=)([^&]+)", lambda m: f"{m.group(1)}<{len(m.group(2))}字元>", u)


def _safe_build_url(url: str):
    u = _html.unescape(url or "")
    if _BUILD_URL_RE.fullmatch(u):
        return u
    # 被拒絕時留一行警告，這樣遊戲哪天多加參數、導致所有升級都變成「找不到按鈕」時，
    # 看 log 就知道是網址格式變了。金幣（buildmaster）、看廣告（&as=）是預期中要拒絕的，不記。
    if u and "buildmaster" not in u and not re.search(r"[?&]as=", u) and u not in _rejected_seen:
        _rejected_seen.add(u)
        print(f"[警告] 升級按鈕的網址不符合白名單格式，已忽略（遊戲改版了嗎？）：{_mask_checksum(u)}")
    return None


def find_upgrade_url(build_page_html: str):
    """只認綠色 build 按鈕，而且網址必須通過白名單；紫色的「建構25%更快」(看廣告加速)、
    金幣的按鈕一律不碰。"""
    soup = BeautifulSoup(build_page_html, "html.parser")
    for b in soup.select("button.green.build"):
        m = re.search(r"location\.href\s*=\s*'([^']+)'", b.get("onclick", ""))
        u = _safe_build_url(m.group(1)) if m else None
        if u:
            return u
    return None


def parse_build_options(html: str) -> list:
    """解析 build.php?id=<空地> 的「建造新的建築物」頁面（body 內 #build.gid0）。
    每個建築一個 dict：
      gid, name
      cost            [木, 磚, 鐵, 糧]
      secs            工期（秒）；不能蓋時頁面不顯示工期，為 0
      buildable       True = 頁面上有綠色「興建建築物」按鈕
      url             上述按鈕的網址（不含紫色看廣告加速、金幣的按鈕）；不能蓋時為 None
      resource_short  頁面出現「交換資源」按鈕 = 目前資源不足
      exceeds_storage 「交換資源」按鈕是 disabled = 成本超過倉庫/糧倉容量
      conditions      前置條件清單，每筆：
                        gid, name, level (要求的等級)
                        met      True = 已達成
                        current  目前等級（未達成時頁面才會給；達成時為 None）
                        forbidden True = 被劃掉的建築，代表「不能已經有這棟」（例如蓋行宮前不能有皇宮）
    注意：條件只會出現在「還不能蓋」的建築上；能蓋的建築沒有條件清單。
    """
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for w in soup.select('div.buildingWrapper[id^="contract_building"]'):
        m = re.search(r"contract_building(\d+)", w.get("id", ""))
        if not m:
            continue
        gid = int(m.group(1))

        # 成本：只認 r1Big~r4Big；cropConsumptionBig（穀物消耗）不算
        cost = {}
        for r in w.select("div.contractWrapper div.resource"):
            i = r.select_one("i")
            mm = re.search(r"\br([1-4])Big\b", " ".join(i.get("class", []))) if i is not None else None
            v = r.select_one("span.value")
            if mm and v is not None:
                cost[int(mm.group(1))] = _to_int0(_clean_text(v))

        box = w.select_one("div.upgradeButtonsContainer")
        url, secs = None, 0
        if box is not None:
            for b in box.select("button.green"):
                mm = re.search(r"location\.href\s*=\s*'([^']+)'", b.get("onclick", ""))
                url = _safe_build_url(mm.group(1)) if mm else None
                if url:
                    break
            # section1 才是正常工期；section2（看廣告加速）的工期比較短，不能拿來用
            d = box.select_one("div.section1 .duration span.value")
            dm = re.search(r"\d+:\d+:\d+", _clean_text(d))
            if dm:
                secs = _hms(dm.group(0))

        conds = []
        if box is not None:
            for c in box.select("span.buildingCondition"):
                a = c.find("a")
                om = re.search(r"open\('building',\s*(\d+)\)", a.get("onclick", "")) if a else None
                lvl_el = next((s for s in c.find_all("span", recursive=False)), None)
                lm = re.search(r"等級\s*(\d+)", _clean_text(lvl_el))
                cur = re.search(r"目前等級:\s*(\d+)", BIDI.sub("", lvl_el.get("title", ""))) if lvl_el is not None else None
                conds.append({
                    "gid": int(om.group(1)) if om else 0,
                    "name": _clean_text(a),
                    "level": int(lm.group(1)) if lm else 0,
                    "met": "error" not in c.get("class", []),
                    "current": int(cur.group(1)) if cur else None,
                    "forbidden": bool(a and a.find("span", style=re.compile("line-through"))),
                })

        short = w.select_one("div.upgradeBlocked button.exchange")
        h2 = w.select_one("div.build_desc h2")
        out.append({
            "gid": gid, "name": _clean_text(h2),
            "cost": [cost.get(k, 0) for k in (1, 2, 3, 4)], "secs": secs,
            "buildable": url is not None, "url": url,
            "resource_short": short is not None,
            "exceeds_storage": short is not None and "disabled" in short.get("class", []),
            "conditions": conds,
        })
    return out


def fetch_build_options(get, aid: int) -> list:
    """抓空地 aid 的三個分頁並合併成一份完整可蓋清單（每筆多一個 "category" 欄位）。
    只抓預設分頁會漏掉軍事與資源兩類（英雄宅、研究院、盔甲廠、鋸木廠…），所以一律三頁都抓。"""
    out, seen = [], set()
    for cat, label in BUILD_CATEGORIES.items():
        for o in parse_build_options(get(f"build.php?id={aid}&category={cat}")):
            if o["gid"] in seen:
                continue
            seen.add(o["gid"])
            o["category"] = label
            out.append(o)
    return out
