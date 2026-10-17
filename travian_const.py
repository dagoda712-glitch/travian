#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Travian 常數（不會在執行時被改動的資料）。只放常數與純查表函式，不放邏輯、不 import 專案其他模組。
會被改動的設定（TARGET_LEVEL、QUEUE_SLOTS、IS_CAPITAL）留在 travian_agent.py，
runner.py 是對 travian_agent 的屬性賦值，搬到這裡 import 會讓賦值傳不到 plan()。
"""
import re


# 村莊中心建築（非資源田）。目前只驗證過倉庫/糧倉：debug_village_buildings.py
# 已確認這兩者的 build.php 頁面結構跟資源田不同（成本/時間寫在正文而非 title），
# 見 parse_dorf2() / parse_building_cost()。之後要加其他建築（例如主建築）
# 可以直接擴充這個 dict，但要先用 debug_village_buildings.py 驗證格式一致。
BUILDING_GIDS = {10: "倉庫", 11: "糧倉"}

# 村莊中心建築升級目標等級的預設值（0 = 停用）。travian_agent.TARGET_BUILDING_LEVEL 以此為初值。
DEFAULT_TARGET_BUILDING_LEVEL = 0

BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")   # 頁面數字外面包的方向控制字元


# 給 Sheet / log 顯示用的中文名稱。沒列到的 gid 會顯示成「建築(gid N)」。
RES_NAMES = ["木材", "磚塊", "鋼鐵", "穀物"]
GID_NAMES = {1: "伐木場", 2: "泥坑", 3: "鐵礦場", 4: "農場",
             10: "倉庫", 11: "糧倉", 15: "村莊大樓", 16: "集結點", 17: "市場", 18: "大使館",
             20: "馬廄", 22: "研究院", 23: "山洞", 24: "城鎮廳", 25: "行宮", 26: "皇宮",
             27: "寶物庫", 28: "交易所", 34: "石匠鋪", 37: "英雄宅", 45: "供水系統",
             5: "鋸木廠", 6: "磚廠", 7: "鋼鐵鑄造廠", 8: "麵粉廠", 9: "麵包店", 13: "盔甲廠",
             14: "競技場", 19: "兵營", 21: "工場", 38: "大倉庫", 39: "大穀倉", 42: "石墻", 46: "醫院"}


def gid_name(gid) -> str:
    return GID_NAMES.get(gid, f"建築(gid {gid})")


# ---------------- 資源田產量表（用於 ROI 排序）----------------
# 標準速度、無任何加成（無綠洲、無工廠、無英雄）下，各等級的每小時總產量。
# 來源：Travian 官方「Guide: Resources Explained」只說明「等級越高產量越高」但未列出
# 逐級數字；逐級產量來自社群長年沿用、多個第三方工具互相印證的標準表
# （travian-strategy-guide.com「The Law of Leveling Up」ROI 概念一致；
# cs.cmu.edu 的舊版試算頁列出同一組「等級 1→10 累積產量 5/9/15/22/33/50/70/100/145/200」）。
# 這張表只到 10 級（一般村莊上限，見 MAX_NON_CAPITAL_FIELD_LEVEL），且不含伺服器倍速、
# 綠洲、工廠（鋸木廠等）加成——這些加成通常對所有田等比例套用，不太會影響「先升哪塊」
# 的相對排序，但精確數字請以遊戲內畫面為準。11 級以上（僅首都適用）目前沒有可靠來源，
# 缺表時 ROI 排序會自動退回舊的「花費最低」規則，不會用錯誤數字硬套。
RESOURCE_FIELD_PRODUCTION = {0: 0, 1: 5, 2: 9, 3: 15, 4: 22, 5: 33,
                              6: 50, 7: 70, 8: 100, 9: 145, 10: 200}

# 一般（非首都）村莊資源田上限是 10 級，首都可以無限往上；來源：Travian 官方
# 「Guide: Resources Explained」（support.travian.com/en/articles/214）：
# 「In regular villages the maximum level of resource fields is 10.」
# 是否為首都無法從目前解析的頁面可靠判斷，預設一律視為非首都（較保險，見 README）。
MAX_NON_CAPITAL_FIELD_LEVEL = 10


# 種族 id（來源：dorf2 頁面內嵌 viewData 的 ownPlayer.tribeId）
TRIBE_NAMES = {1: "羅馬", 2: "條頓", 3: "高盧", 6: "埃及", 7: "匈奴"}
ROMAN_TRIBE_ID = 1


# 空地 build.php 的 &category=1/2/3 三個分頁（樣本 build1~3 已確認）
BUILD_CATEGORIES = {1: "基礎", 2: "軍事", 3: "資源"}


# 村莊中心可以蓋新建築的空地範圍。39 固定是集結點、40 固定是城牆，不拿來蓋別的。
FREE_SLOT_RANGE = (19, 38)
