#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
唯讀診斷腳本：不會做任何升級動作，只是把 dorf2.php（村莊中心，倉庫/糧倉/
主建築等都在這一頁）的建築資料印出來，特別針對「倉庫」「糧倉」這兩項，
額外去對應的 build.php?id=<aid> 頁面測試：
  1. 能不能用跟資源田一樣的正則表達式解析出升級成本
  2. 能不能用既有的 auto_build.find_upgrade_url() 找到綠色升級按鈕

這兩件事都確認可行，才代表可以安全地把倉庫/糧倉納入自動升級範圍，
不用再改動任何解析邏輯（因為兩者共用同一套 build.php 頁面結構）。

跟 debug_fields.py 一樣，共用同一套環境變數/cache，把 workflow 裡的
「run: python travian_ci_run.py」暫時換成「run: python debug_village_buildings.py」
跑一次即可，跑完記得換回去。

環境變數：
  TRAVIAN_BASE_URL      必填
  TRAVIAN_STATE_PATH    選填，預設 travian_state.json（沿用 cache 裡的）
"""
import os
import sys
from pathlib import Path

from bs4 import BeautifulSoup
import auto_build as AB  # 重用 find_upgrade_url()
import travian_agent as T  # 重用 parse_building_cost()：跟正式流程用同一份解析邏輯，
                             # 避免這支 debug 腳本自己另外維護一份 regex 兜不起來

BASE = os.getenv("TRAVIAN_BASE_URL", "").rstrip("/")
STATE_PATH = Path(os.getenv("TRAVIAN_STATE_PATH", "travian_state.json"))
HTML_DUMP_DIR = Path("debug_html")  # 存原始 HTML 供人工檢查，會由 workflow 上傳成 artifact

# 常見會想自動化的村莊中心建築（目標 gid，官方 Travian 固定編號）：
# 10 = 倉庫 Warehouse, 11 = 糧倉 Granary
TARGET_GIDS = {10: "倉庫", 11: "糧倉"}


def fetch(context, page, path: str, force_http: bool = False) -> str:
    """跟 travian_ci_run.py 一樣的邏輯：先用瀏覽器載入，
    如果懷疑 title 被前端 JS 清空，改用純 HTTP 請求重抓一次。"""
    url = f"{BASE}/{path.lstrip('/')}"
    if force_http:
        return context.request.get(url).text()
    page.goto(url, wait_until="networkidle")
    return page.content()


def main() -> int:
    if not BASE:
        print("缺少 TRAVIAN_BASE_URL")
        return 1
    if not STATE_PATH.exists():
        print(f"找不到 {STATE_PATH}，代表 cache 沒還原到，無法用既有 session")
        return 1

    HTML_DUMP_DIR.mkdir(exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE_PATH))
        page = context.new_page()

        html = fetch(context, page, "dorf2.php")
        if 'id="stockBar"' not in html:
            print("session 可能已失效，抓到的不是村莊頁")
            browser.close()
            return 1

        soup = BeautifulSoup(html, "html.parser")
        slots = soup.select("#villageContent .buildingSlot")

        print(f"共找到 {len(slots)} 個村莊中心建築位（villageContent）\n")
        print(f"{'aid':>4} {'gid':>4} {'level':>5}  name")
        print("-" * 50)

        found = {}  # gid -> aid，記錄倉庫/糧倉實際對應的 aid
        for slot in slots:
            try:
                aid = int(slot["data-aid"])
                gid = int(slot["data-gid"])
            except (KeyError, ValueError):
                continue
            name = slot.get("data-name", "")
            a = slot.select_one("a.level")
            level = a.get("data-level", "?") if a else "（未建造）"
            print(f"{aid:>4} {gid:>4} {str(level):>5}  {name}")
            if gid in TARGET_GIDS:
                found[gid] = aid

        print()
        if not found:
            print("⚠️ 沒有在 gid=10（倉庫）或 gid=11（糧倉）找到任何已建造的建築。")
            print("   可能這個伺服器的 gid 編號跟預期不同，或倉庫/糧倉還沒建造。")
            print("   請把上面完整清單貼回去，人工確認正確的 gid。")
            browser.close()
            return 0

        for gid, aid in found.items():
            label = TARGET_GIDS[gid]
            print(f"=== 測試 {label}（aid={aid}, gid={gid}）的 build.php 頁面 ===")

            build_html = fetch(context, page, f"build.php?id={aid}")
            cost, secs = T.parse_building_cost(build_html)
            print(f"  用瀏覽器直接載入 → 解析到的成本：{cost or '（空）'}  工期：{secs}秒"
                  f"{'（正常，非空）' if cost else '（懷疑 title 被 JS 清空）'}")

            if not cost:
                build_html_http = fetch(context, page, f"build.php?id={aid}", force_http=True)
                cost_http, secs_http = T.parse_building_cost(build_html_http)
                print(f"  改用純 HTTP 請求重抓 → 解析到的成本：{cost_http or '（空）'}  工期：{secs_http}秒"
                      f"{'（純 HTTP 有效！）' if cost_http else '（純 HTTP 也是空的，需要人工看原始 HTML）'}")
                build_html = build_html_http if cost_http else build_html

            url = AB.find_upgrade_url(build_html)
            print(f"  find_upgrade_url() 找到的升級網址：{url or '（找不到，可能已達最高等級或前置條件未滿足）'}")

            out_path = HTML_DUMP_DIR / f"aid{aid}_gid{gid}_{label}.html"
            out_path.write_text(build_html, encoding="utf-8")
            print(f"  已存檔：{out_path}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
