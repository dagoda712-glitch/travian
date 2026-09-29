#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
唯讀診斷腳本：完全不會點擊任何東西，只是把 dorf1.php 解析出來的每塊田
資訊印出來，特別針對「成本解析成空陣列」的田，額外印出它 title 屬性
的原始 HTML，方便判斷是不是 tooltip 格式跟預期的不一樣（例如倉容不足
時 Travian 顯示的是警告文字，不是正常成本數字）。

跟 travian_ci_run.py 共用同一套環境變數/cache，直接在 workflow 裡把
「run: python travian_ci_run.py」暫時換成「run: python debug_fields.py」
跑一次即可，跑完記得換回去。

環境變數：
  TRAVIAN_BASE_URL      必填
  TRAVIAN_STATE_PATH    選填，預設 travian_state.json（沿用 cache 裡的）
"""
import os
import sys
from pathlib import Path

from bs4 import BeautifulSoup
import travian_agent as T

BASE = os.getenv("TRAVIAN_BASE_URL", "").rstrip("/")
STATE_PATH = Path(os.getenv("TRAVIAN_STATE_PATH", "travian_state.json"))


def main() -> int:
    if not BASE:
        print("缺少 TRAVIAN_BASE_URL")
        return 1
    if not STATE_PATH.exists():
        print(f"找不到 {STATE_PATH}，代表 cache 沒還原到，無法用既有 session")
        return 1

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE_PATH))
        page = context.new_page()
        page.goto(f"{BASE}/dorf1.php", wait_until="networkidle")
        html = page.content()
        browser.close()

    if 'id="stockBar"' not in html:
        print("session 可能已失效，抓到的不是村莊頁")
        return 1

    st = T.parse_dorf1(html)
    print(f"目標等級 TARGET_LEVEL = {T.TARGET_LEVEL}")
    print(f"佇列 queue = {st.queue}")
    print(f"存量 stock = {st.stock}")
    print(f"倉容 cap   = {st.cap}")
    print(f"產量 prod  = {st.prod}")
    print(f"來襲 attacks = {st.attacks}")
    print()
    print(f"{'aid':>4} {'gid':>4} {'level':>5} {'cost':<20} {'secs':>6} {'building':>8}  是否為候選")
    print("-" * 80)

    soup = BeautifulSoup(html, "html.parser")
    tag_by_aid = {}
    for a in soup.select("#resourceFieldContainer a.resourceField"):
        try:
            tag_by_aid[int(a["data-aid"])] = a
        except (KeyError, ValueError):
            pass

    for f in sorted(st.fields, key=lambda x: x.aid):
        is_cand = (f.level < T.TARGET_LEVEL) and bool(f.cost) and not f.building
        cost_str = "/".join(map(str, f.cost)) if f.cost else "（空！）"
        print(f"{f.aid:>4} {f.gid:>4} {f.level:>5} {cost_str:<20} {f.secs:>6} "
              f"{str(f.building):>8}  {'✅ 候選' if is_cand else ''}")

        # 成本是空的、但等級明明還沒到目標 → 這就是問題所在，印出原始 title
        if not f.cost and f.level < T.TARGET_LEVEL and not f.building:
            tag = tag_by_aid.get(f.aid)
            title = tag.get("title", "") if tag else "（找不到對應的 tag）"
            print(f"      >>> slot {f.aid} 疑似解析失敗，原始 title 屬性如下：")
            print(f"      >>> {title!r}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
