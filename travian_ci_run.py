#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 用的「Playwright 版」自動升級資源田（v2.6：躲兵接 dry／live）。

v2.1：修正 title 被前端 JS 清空導致成本解析失敗的問題。get_html() 會在「這一頁抓到的
所有田成本都是空的」時，改用 context.request.get() 發一個不經過瀏覽器渲染、不執行任何
JS 的純 HTTP 請求去拿同一頁的原始 HTML（沿用同一組 session/cookie）。Travian 的某個 JS
tooltip 套件會在頁面載入後把 <a> 標籤的 title 讀走再清空，純 HTTP 請求拿得到伺服器端
原始吐出、title 還完整的 HTML。

v2.2：安靜時段（QUIET_UTC）不再直接離開。原本整段時間完全沒有請求，session 會因為閒置
太久被伺服器端過期（實測：跳過 UTC 17~23 之後 cache 與種子 Cookie 都失效）。現在安靜時段
仍然會載入一次 dorf1 確認 session 活著、把最新狀態存回 travian_state.json，但不建造、
不寫 Sheet。session 真的失效時照舊走 disable()（sheets_writer）。

v2.3：修正 NameError: name 'context' is not defined。transfer_io 的 make_io() 原本被貼在
檔案最上層（context / dry / cfg 都還不存在）。現在改在 main() 裡、session 驗證完成且確定
不會再替換 context 之後才建立 send / read_state，並以 transfer_io=(send, read_state)
傳給 R.run_build_cycle。DRY 模式下 send 換成只印 log 的 make_dry_send()。

v2.4：接上英雄產量加成（hero_focus）。和 transfer_io 同一個時機建立 read_focus / set_focus，
以 focus_io=(read_focus, set_focus) 傳給 R.run_build_cycle；DRY 時 set_focus 換成只印 log 的版本
（read_focus 是唯讀 GET，照常送）。是否真的切換由 Config 的 hero_focus_mode（預設 off）決定。

v2.5：躲兵「只觀察」。session 確認有效、確定不是安靜時段之後、建城之前，呼叫 hide_observe.observe：
有來襲才開集結點概況（唯讀 GET）、存 debug_html/、算「如果要躲會怎麼派」、寫一列 Actions（動作＝躲兵觀察、
模式＝PLAN）。**絕不派兵**。模組載入失敗（例如缺 beautifulsoup4）或執行出錯都只印 log，不影響建城。
開關在 Sheet 的 Config 分頁：hide_mode（off／observe，預設 observe）。

v2.7（v2.82，換伺服器）：家座標與地圖大小改由 Config 的 home_x／home_y／map_size 提供（預設 -28／-72／401），
取得 cfg 後立刻 dispatch.configure；Config 填壞會用預設，並印出實際採用的值。

v2.6：躲兵接上 dry／live（hide_mode＝observe／dry／live；預設仍是 observe）。HO.observe 多傳 min_attack（Config hide_min_attack，
預設 20）、wait_max_s（hide_wait_max_s，預設 180）、dry_run（DRY_RUN 測試環境下 live 會降成 dry）。observe 行為不變、仍絕不派兵。

這支只負責「Playwright 的 session（cache / 種子 Cookie）、怎麼抓頁面、怎麼送出網址」；
決策、build_order、送出與驗證、Config / Actions 分頁都在 runner.py。

環境變數：跟 v2 完全相同，見 SETUP.md
"""
import os
import sys
from pathlib import Path

import dispatch
import runner as R
import hero_focus as HF
import transfer_io as TIO
import travian_hero as H
import travian_pages as P
from notify import notify
from sheets_writer import disable

try:                                   # 躲兵觀察壞了（例如缺 beautifulsoup4）不能拖垮建城
    import hide_observe as HO
except Exception as _e:                # noqa: BLE001
    HO = None
    print(f"[躲兵觀察] 模組載入失敗，本輪略過：{_e!r}")

BASE = os.getenv("TRAVIAN_BASE_URL", "").rstrip("/")
STATE_PATH = Path(os.getenv("TRAVIAN_STATE_PATH", "travian_state.json"))


def parse_cookie_header(header: str, url: str) -> list:
    """把 'name1=value1; name2=value2' 這種原始 Cookie header 字串，
    轉成 Playwright context.add_cookies() 吃的格式。"""
    cookies = []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append({"name": name.strip(), "value": value.strip(), "url": url})
    return cookies


def main() -> int:
    quiet = R.in_quiet_hours()      # 安靜時段：只做 keep-alive，不建造（見下方）
    if not BASE:
        print("缺少 TRAVIAN_BASE_URL")
        return 1

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright，請確認 requirements.txt 與 workflow 有安裝")
        return 1

    cfg, code = R.get_cfg()
    if cfg is None:
        return code
    dry = os.getenv("DRY_RUN", "1") != "0"
    dispatch.configure(home=(cfg.get("home_x", dispatch.HOME[0]), cfg.get("home_y", dispatch.HOME[1])))
    try:
        dispatch.configure(map_size=cfg.get("map_size", dispatch.MAP_SIZE))
    except ValueError as e:
        print(f"[設定] Config 的 map_size 不合法，改用預設 {dispatch.MAP_SIZE}：{e}")
    print(f"[設定] 家 ({dispatch.HOME[0]}|{dispatch.HOME[1]})、地圖邊長 {dispatch.MAP_SIZE}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        seed_cookie = os.getenv("TRAVIAN_SEED_COOKIE", "")

        def new_context_from_seed():
            ctx = browser.new_context()
            ctx.add_cookies(parse_cookie_header(seed_cookie, BASE))
            return ctx

        used_cache = STATE_PATH.exists()
        if used_cache:
            print(f"找到既有 session 狀態：{STATE_PATH}（來自 actions/cache）")
            context = browser.new_context(storage_state=str(STATE_PATH))
        else:
            if not seed_cookie:
                print("找不到 cache，也沒有 TRAVIAN_SEED_COOKIE 可用救援。")
                print("請從瀏覽器複製登入後的 Cookie header，存進這個 Secret。")
                browser.close()
                return 1
            print("cache 沒中，改用 TRAVIAN_SEED_COOKIE 這個救援種子建立 session")
            context = new_context_from_seed()

        page = context.new_page()

        def get_html(path: str, need_cost: bool = False, raw: bool = False) -> str:
            """load 這一頁；need_cost=True 時，若解析出來所有田的成本
            都是空的（title 被前端 JS 拿掉），改發一次純 HTTP 請求
            （不執行 JS），沿用同一組 session 取得未被 JS 動過的原始 HTML。"""
            url = f"{BASE}/{path.lstrip('/')}"
            if raw:
                # 純 HTTP、不執行 JS：拿伺服器原始吐出的 HTML（title 完整）
                return context.request.get(url).text()
            page.goto(url, wait_until="networkidle")
            html = page.content()

            if need_cost and 'id="stockBar"' in html:
                probe = P.parse_dorf1(html)
                if probe.fields and not any(f.cost for f in probe.fields):
                    print(f"  [備援] {path} 的 title 被前端 JS 清空，"
                          f"改用純 HTTP 請求重抓一次（不執行 JS）")
                    resp = context.request.get(url)
                    html = resp.text()
            return html

        def submit(url: str) -> None:
            page.goto(url if url.startswith("http") else f"{BASE}/{url.lstrip('/')}",
                      wait_until="networkidle")

        def click_adventure(number: int, row_index: int, expected):
            """點冒險頁第 row_index 列的「探索」，並重讀頁面確認英雄真的出發。回傳 (成功與否, 說明)。
            安全規則：點擊前重讀並核對（狀態、列數、地點／距離／時間／難度圖示、按鈕沒有 disabled、沒有
            difficulty bonus）；只點 table.adventureList 裡的按鈕（不碰「觀看影片」）；成功只看重讀結果，
            不看網路請求；不重試。確認視窗（International 11 未驗證）若出現且沒人按，重讀會看到沒變化。"""
            page.goto(f"{BASE}/hero/adventures", wait_until="networkidle")
            html = page.content()
            hero, rows = H.parse_hero_json(html), H.parse_adventure_rows(html)
            if hero is None or hero.status != H.STATUS_HOME:
                return False, "點擊前重讀：英雄已不在家或讀不到狀態，不點"
            if hero.difficulty_bonus:
                return False, "點擊前重讀：difficulty bonus 為真，不點"
            bad = H.rows_match_json(hero, rows)
            if bad:
                return False, f"點擊前核對失敗：{bad}"
            if row_index >= len(rows) or hero.adventures[row_index].number != number:
                return False, f"點擊前核對失敗：第 {row_index + 1} 列已不是 #{number}（清單變了）"
            row = rows[row_index]
            if (row.place, row.distance, row.duration, row.icon) != (
                    expected.place, expected.distance, expected.duration, expected.icon):
                return False, "點擊前核對失敗：該列內容和決策時不同"
            if not row.has_button or row.disabled:
                return False, "點擊前核對失敗：按鈕不存在或是 disabled"
            btn = page.locator("table.adventureList tbody tr").nth(row_index).locator("td.button button")
            btn.click()
            page.wait_for_timeout(3000)
            page.goto(f"{BASE}/hero/adventures", wait_until="networkidle")
            after = H.parse_hero_json(page.content())
            if after is not None and after.status == H.STATUS_GOING and after.adventure_number == number:
                return True, f"已確認出發，約 {after.arrival_in} 秒後抵達"
            got = None if after is None else (after.status, after.adventure_number)
            return False, f"點擊後重讀沒看到出發（狀態/編號 = {got}）"

        # 安靜時段只是確認 session 還活著，不需要田的成本，省掉備援那次額外請求
        d1 = get_html("dorf1.php", need_cost=not quiet)

        # cache 裡的 session 失效了：丟掉它，改用種子 Cookie 重試一次。
        # （context/page 在這個函式作用域內重新指定，上面的 get_html() 等
        #  closure 會自動用到新的物件。）
        if 'id="stockBar"' not in d1 and used_cache and seed_cookie:
            print("cache 的 session 已失效，改用 TRAVIAN_SEED_COOKIE 重新建立 session")
            context.close()
            try:
                STATE_PATH.unlink()
            except OSError:
                pass
            context = new_context_from_seed()
            page = context.new_page()
            d1 = get_html("dorf1.php", need_cost=not quiet)

        if 'id="stockBar"' not in d1:
            if used_cache and not seed_cookie:
                msg = "cache 的 session 已失效，且沒有設定 TRAVIAN_SEED_COOKIE 可以救援，請設定後重跑"
            else:
                msg = "抓到的不是村莊頁：種子 Cookie 也已失效，請重新登入複製 Cookie 更新 TRAVIAN_SEED_COOKIE"
            if dry:
                # DRY（測試用）不動共用 Sheet 的 Config：否則分支上的測試失敗會把正式排程一起停掉
                notify(msg + "（DRY 模式：不修改 Config 的 enabled）")
            else:
                disable(msg)
            browser.close()
            return 1

        if quiet:
            # session 是活的：把最新狀態存回去讓下一輪 actions/cache 接續，然後離開，不建造
            print("安靜時段：session 正常，只做 keep-alive，不建造")
            context.storage_state(path=str(STATE_PATH))
            browser.close()
            return 0

        # 躲兵「只觀察」：只讀、只記錄、不派兵；沒有來襲時不多打任何請求；任何例外都不影響建城
        if HO is not None:
            HO.observe(page, BASE, d1, mode=cfg.get("hide_mode", "observe"),
                       min_attack=cfg.get("hide_min_attack", 20),
                       wait_max_s=cfg.get("hide_wait_max_s", 180), dry_run=dry)

        # session 已確認有效、context 之後不會再被替換，這時才建立 transfer_io
        # （make_io 會把「當下」的 context 綁進 closure，所以不能在 context 重建之前呼叫）
        send, read_state = TIO.make_io(context, BASE, referer=f"{BASE}/dorf1.php")
        if dry:
            send = TIO.make_dry_send()   # DRY：use-item 一律不送（read_state 是唯讀查詢，照常送）
        read_focus, set_focus = HF.make_focus_io(context, BASE)
        if dry:
            set_focus = HF.make_dry_set_focus()   # DRY：改加成一律不送（read_focus 是唯讀 GET）

        code = R.run_build_cycle(
            cfg, get=get_html, get_dorf1=lambda: get_html("dorf1.php", need_cost=True),
            submit=submit, dry=dry, first_dorf1=d1,
            transfer_io=(send, read_state), focus_io=(read_focus, set_focus))

        # 英雄冒險：獨立於建城，失敗只通知（run_hero_cycle 內部已接住例外）；hero_adventure 預設 FALSE
        R.run_hero_cycle(cfg, get=get_html, get_dorf1=lambda: get_html("dorf1.php", raw=True),
                         click_adventure=click_adventure, dry=dry)

        # 不管這輪做了什麼，只要一開始 session 是活的，就把最新狀態存回去，
        # 讓下一輪 actions/cache 可以接續使用
        context.storage_state(path=str(STATE_PATH))
        browser.close()

    return code


if __name__ == "__main__":
    sys.exit(main())
