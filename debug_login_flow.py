#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
唯讀診斷腳本：不會登入、不會送出任何表單，只是觀察「沒有有效 session 時」
造訪 dorf1.php 會被導去哪裡、實際的登入頁面長什麼樣子。

用途：在考慮整合帳密自動登入之前，先確認：
  1. 是先到 lobby（www.travian.com / id.travian.com）還是直接在遊戲伺服器本身登入
  2. 登入表單的欄位 name／id 實際是什麼（不要用猜的）
  3. 有沒有出現 CAPTCHA 或其他機器人驗證
  4. 表單送出後的按鈕 selector 是什麼

跑法：故意不使用 travian_state.json、不設 TRAVIAN_SEED_COOKIE，
直接用乾淨的瀏覽器 context 造訪，模擬「session 完全失效」時的狀況。

環境變數：
  TRAVIAN_BASE_URL   必填
"""
import os
import sys
from pathlib import Path

BASE = os.getenv("TRAVIAN_BASE_URL", "").rstrip("/")
OUT_DIR = Path("debug_html")


def main() -> int:
    if not BASE:
        print("缺少 TRAVIAN_BASE_URL")
        return 1

    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()  # 全新、沒有任何 cookie
        page = context.new_page()

        print(f"造訪 {BASE}/dorf1.php（乾淨 session，預期會被導去登入）...")
        page.goto(f"{BASE}/dorf1.php", wait_until="networkidle", timeout=60000)

        print(f"\n最終停在的網址：{page.url}")
        print(f"（如果網域跟 TRAVIAN_BASE_URL 不同，代表登入是走 lobby／SSO，"
              f"不是遊戲伺服器自己的 login.php）")

        html = page.content()
        out = OUT_DIR / "login_page.html"
        out.write_text(html, encoding="utf-8")
        print(f"\n已存檔完整 HTML：{out}")

        # 列出所有 <input>，看真實的 name/id/type，不要用猜的
        print("\n=== 頁面上所有 <input> ===")
        inputs = page.locator("input")
        for i in range(inputs.count()):
            el = inputs.nth(i)
            print(f"  name={el.get_attribute('name')!r:20} "
                  f"id={el.get_attribute('id')!r:20} "
                  f"type={el.get_attribute('type')!r:12} "
                  f"placeholder={el.get_attribute('placeholder')!r}")

        # 列出所有可能的送出按鈕
        print("\n=== 頁面上所有 <button> / input[type=submit] ===")
        buttons = page.locator("button, input[type='submit']")
        for i in range(buttons.count()):
            el = buttons.nth(i)
            print(f"  tag/text={el.inner_text()!r:30} "
                  f"type={el.get_attribute('type')!r:12} "
                  f"class={el.get_attribute('class')!r}")

        # 粗略檢查是否有驗證碼／機器人偵測套件
        lowered = html.lower()
        flags = []
        if "captcha" in lowered:
            flags.append("captcha")
        if "recaptcha" in lowered:
            flags.append("recaptcha")
        if "cloudflare" in lowered or "cf-challenge" in lowered:
            flags.append("cloudflare challenge")
        if "hcaptcha" in lowered:
            flags.append("hcaptcha")
        print(f"\n=== 疑似機器人驗證關鍵字 ===\n{flags or '（沒偵測到，但不保證絕對沒有）'}")

        browser.close()

    print("\n請把上面的 <input>／<button> 清單，跟 debug_html/login_page.html 一起貼回去，"
          "才能確認要用哪個 selector，或是判斷需不需要走 lobby 轉址。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
