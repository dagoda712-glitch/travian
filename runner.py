#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共用主流程：讀 Config、決策（build_order 或舊流程）、送出升級、驗證、記錄到 Sheet。

auto_build.py（純 requests）和 travian_ci_run.py（Playwright）只負責「怎麼抓頁面、怎麼送出網址」，
把這三個小函式傳進 run_build_cycle()：

  get(path)      -> str   抓一個頁面的 HTML（例如 "dorf2.php"、"build.php?id=26"）
  get_dorf1()    -> str   抓 dorf1 的 HTML（Playwright 版要在這裡處理 title 被 JS 清空的備援）
  submit(url)    -> None  送出升級（url 已通過白名單，例如 "/dorf2.php?id=31&gid=11&action=build&checksum=76c9cd"）

環境變數：
  GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_ID   選填（有的話讀 Config、寫 Actions）
  QUIET_UTC   不運作的 UTC 小時區間 "起,迄"，預設 "17,23"（台灣 01:00~07:00）

Sheet 的「Config」分頁：A 欄名稱、B 欄值，例如
  enabled       TRUE       <- 改成 FALSE 就是緊急停止開關
  target_level  2
  queue_slots   2          <- Plus 到期後改 1
  max_builds    2          <- 每輪最多送出幾個升級
  target_building_level 0  <- 舊流程：倉庫/糧倉升到幾級（0 = 停用）
  build_order   res:2,15:3 <- 建城順序（有填就用這個，舊流程的兩個目標等級不再使用）
"""
import os
import random
import time
from datetime import datetime, timezone

import travian_agent as T


# ---------------- Google Sheets ----------------
def _has_sheet() -> bool:
    return bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") and os.getenv("GOOGLE_SHEET_ID"))


def _sheet():
    from sheets_writer import _get_credentials
    return _get_credentials().open_by_key(os.environ["GOOGLE_SHEET_ID"])


def load_config() -> dict:
    """讀 Sheet 的 Config 分頁；沒有這個分頁就用預設值（不是錯誤）。
    真正的異常（憑證、網路等）會往外拋，讓呼叫端中止本輪。"""
    from gspread.exceptions import WorksheetNotFound

    cfg = {"enabled": True, "target_level": 2, "queue_slots": 2, "max_builds": 2,
           "target_building_level": T.TARGET_BUILDING_LEVEL, "build_order": ""}
    if not _has_sheet():
        return cfg
    try:
        rows = _sheet().worksheet("Config").get_all_values()
    except WorksheetNotFound:
        print("Sheet 裡沒有 Config 分頁，使用預設值")
        return cfg
    for row in rows:
        if len(row) < 2:
            continue
        k, v = row[0].strip().lower(), row[1].strip()
        if k == "enabled":
            cfg[k] = v.lower() not in ("false", "0", "no", "off", "否", "停")
        elif k == "build_order":
            cfg[k] = v
        elif k in ("target_level", "queue_slots", "max_builds", "target_building_level") and v.isdigit():
            cfg[k] = int(v)
    return cfg


ACTIONS_HEADER = ["時間(UTC)", "動作", "slot", "gid", "目前等級", "成本", "模式", "結果", "升級項目 / 說明"]
_actions_header_ok = False


def log_action(row: list):
    global _actions_header_ok
    if not _has_sheet():
        return
    try:
        sh = _sheet()
        try:
            ws = sh.worksheet("Actions")
        except Exception:
            ws = sh.add_worksheet("Actions", 1000, len(ACTIONS_HEADER) + 1)
            ws.append_row(ACTIONS_HEADER)
            _actions_header_ok = True
        if not _actions_header_ok:
            # 舊分頁只有 8 欄：補欄並更新表頭，舊資料列不動
            if ws.row_values(1) != ACTIONS_HEADER:
                if ws.col_count < len(ACTIONS_HEADER):
                    ws.add_cols(len(ACTIONS_HEADER) - ws.col_count)
                ws.update(range_name="A1", values=[ACTIONS_HEADER])
            _actions_header_ok = True
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"寫入 Actions 分頁失敗（不影響升級）: {e!r}")
      
def disable(reason: str):
    """偵測到 session / Cookie 失效等需要人工介入的狀況時呼叫：
    把 Config 的 enabled 設成 FALSE，讓之後每一輪一讀到 cfg 就直接跳過，
    不會再嘗試登入、也不會每 30 分鐘重複失敗。要恢復請手動把 enabled 改回 TRUE。
    沒有連 Sheet 時沒辦法持久化，下一輪還是會照樣重試。"""
    T.notify(reason)
    if not _has_sheet():
        print("沒有連 Google Sheet，無法自動停用，下一輪還會再試一次")
        return
    from gspread.exceptions import WorksheetNotFound
    try:
        sh = _sheet()
        try:
            ws = sh.worksheet("Config")
        except WorksheetNotFound:
            ws = sh.add_worksheet("Config", 10, 2)
        rows = ws.get_all_values()
        for i, row in enumerate(rows, start=1):
            if row and row[0].strip().lower() == "enabled":
                ws.update(range_name=f"B{i}", values=[["FALSE"]])
                break
        else:
            ws.append_row(["enabled", "FALSE"])
        print("已將 Config 的 enabled 設為 FALSE，排程會自動跳過，直到手動改回 TRUE")
    except Exception as e:
        print(f"嘗試寫入 Config 停用失敗（不影響本輪判定為失敗）: {e!r}")

def _hours(sec):
    return round(sec / 3600, 1) if sec is not None else None


def log_village_snapshot(d1: str, get, st) -> None:
    """每輪把村莊數據記到 Sheet1（唯讀、失敗不影響升級）。
    數字以 dorf1 的 var resources（st）為準補齊：Playwright 抓到的 stockBar title 可能被前端 JS 清空，
    產量會變 0；只在解析結果是空／0 時才用 st 補，其他維持原本 scrape_and_log 的格式。"""
    if not _has_sheet():
        return
    try:
        from travian_parser import parse_village_page, ResourceInfo
        from scrape_and_log import build_row
        from sheets_writer import append_row

        try:
            d2 = get("dorf2.php")
        except Exception as e:
            print(f"抓 dorf2 失敗，改用 dorf1 記錄：{e!r}")
            d2 = d1
        ov = parse_village_page(d2 if 'id="stockBar"' in d2 else d1)
        if any(st.cap):
            for i, key in enumerate(("l1", "l2", "l3", "l4")):
                r = ov.resources.get(key) or ResourceInfo(key=key, name=T.RES_NAMES[i])
                r.amount = r.amount or st.stock[i]
                r.production = r.production or st.prod[i]
                r.capacity = r.capacity or st.cap[i]
                ov.resources[key] = r
            if ov.free_crop is None:
                ov.free_crop = st.free_crop
        row = build_row(ov, "；".join(st.queue_info), _hours(st.plus_s), _hours(st.protection_s))
        append_row(row, worksheet_name="Sheet1")
        print("已記錄村莊數據到 Sheet1")
    except Exception as e:
        print(f"寫入 Sheet1 失敗（不影響升級）: {e!r}")


# ---------------- 決策 → 人看得懂的說明 ----------------
def _fname(f) -> str:
    n = T.gid_name(f.gid)
    return f"{n}(slot {f.aid})" if f.gid in (1, 2, 3, 4) else n


def _flv(f) -> str:
    return f"L{f.level}→L{f.level + 1}" if f.level > 0 or f.gid in (1, 2, 3, 4) else "新建→L1"


def _mins(secs) -> str:
    m = max(1, round(secs / 60))
    return f"{m // 60}小時{m % 60}分" if m >= 60 else f"{m}分"


def describe(action: tuple, st) -> tuple:
    """回傳 (動作標籤, Field 或 None, 說明)。說明要寫清楚「在等什麼、缺什麼」。"""
    kind = action[0]
    if kind == "wait_res":
        f = action[2]
        short = [f"{T.RES_NAMES[i]}缺{c - s}" for i, (c, s) in enumerate(zip(f.cost, st.stock)) if c > s]
        return "等待資源", f, (f"下一個要升級 {_fname(f)} {_flv(f)}，資源不夠：{'、'.join(short) or '（差一點）'}，"
                             f"預計約 {_mins(action[1])} 後足夠")
    if kind == "wait_queue":
        if len(st.queue) >= T.QUEUE_SLOTS:
            why = f"施工佇列已滿（{len(st.queue)}/{T.QUEUE_SLOTS}），等有空位再升級"
        else:
            why = "目標建築（或它的前置建築）正在施工，等施工完成"
        return "等待佇列", None, f"{why}，約 {_mins(action[1])} 後再試"
    if kind == "need_storage":
        f = action[1]
        over = [f"{T.RES_NAMES[i]}成本{c}>容量{m}" for i, (c, m) in enumerate(zip(f.cost, st.cap)) if c > m]
        return "倉容不足", f, f"{_fname(f)} {_flv(f)} 的成本超過倉庫/糧倉容量（{'、'.join(over)}），需先升倉庫/糧倉"
    if kind == "blocked":
        return "計畫卡住", None, f"需要人處理：{action[1]}"
    if kind == "no_slot":
        return "無空地", None, f"要蓋新建築 {T.gid_name(action[1])}，但村莊沒有空地了"
    if kind == "done":
        return "已完成", None, "設定的目標都已達成，沒有需要升級的項目"
    return kind, None, ""


def _with_queue(desc: str, st) -> str:
    return desc + (f"｜施工中：{'、'.join(st.queue_info)}" if st.queue_info else "｜目前沒有施工中的項目")


# ---------------- 開頭的共用檢查 ----------------
def in_quiet_hours() -> bool:
    q0, q1 = (int(x) for x in os.getenv("QUIET_UTC", "17,23").split(","))
    return q0 <= datetime.now(timezone.utc).hour < q1


def get_cfg():
    """回傳 (cfg, 結束碼)。cfg 是 None 代表本輪不動作，結束碼就是該回傳的值。"""
    try:
        cfg = load_config()
    except Exception as e:
        print(f"讀取 Config 失敗，為安全起見本輪不動作: {e!r}")
        return None, 1
    if not cfg["enabled"]:
        print("Config 的 enabled 是 FALSE，本輪不動作")
        return None, 0
    return cfg, 0


# ---------------- 舊流程：資源田 + 倉庫/糧倉 ----------------
def _legacy_candidates(get, target_level: int, touched: set) -> list:
    """倉庫/糧倉裡等級 < target_level 的候選 Field（成本已填好）。target_level <= 0 = 停用，
    不多打任何請求。每個候選要多抓一次 build.php，所以只在真的啟用時才呼叫。"""
    if target_level <= 0:
        return []
    out = []
    for gid, (aid, level) in T.parse_dorf2(get("dorf2.php")).items():
        if level >= target_level or aid in touched:
            continue
        cost, secs = T.parse_building_cost(get(f"build.php?id={aid}"))
        if not cost:
            print(f"警告：{T.BUILDING_GIDS.get(gid, gid)} (aid={aid}) 的 build.php 解析不到成本，跳過")
            continue
        out.append(T.Field(aid=aid, gid=gid, level=level, cost=cost, secs=secs, building=False))
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%F %T")


# ---------------- 主迴圈 ----------------
def run_build_cycle(cfg: dict, *, get, get_dorf1, submit, dry: bool,
                    first_dorf1: str = None, sleep=time.sleep) -> int:
    """跑一輪：最多送出 cfg["max_builds"] 個升級。回傳結束碼（0 正常、1 需要人看）。"""
    T.TARGET_LEVEL, T.QUEUE_SLOTS = cfg["target_level"], cfg["queue_slots"]

    order = []
    if cfg.get("build_order"):
        try:
            order = T.parse_build_order(cfg["build_order"])
        except ValueError as e:
            T.notify(f"Config 的 build_order 格式錯誤，本輪不動作：{e}")
            return 1
        print(f"使用建城順序（{len(order)} 個目標）：{cfg['build_order']}")

    mode = "DRY" if dry else "LIVE"
    touched = set()

    for i in range(cfg["max_builds"]):
        d1 = first_dorf1 if (i == 0 and first_dorf1 is not None) else get_dorf1()
        if 'id="stockBar"' not in d1:
            T.notify("抓到的不是村莊頁：session / Cookie 可能已失效，請重新複製 Cookie 更新 Secret")
            return 1
        st = T.parse_dorf1(d1)
        if i == 0:
            log_village_snapshot(d1, get, st)     # 每輪記一次村莊數據到 Sheet1
        for f in st.fields:                       # 本輪已送出的田，不要重複選
            if f.aid in touched:
                f.building = True
        if dry and touched:                       # 乾跑沒真的送出，佇列不會變：
            st.queue = st.queue + [1800] * len(touched)   # 把本輪「已送出」的也算進佇列長度
            st.queue_info = st.queue_info + ["（乾跑模擬：本輪已排入的升級）"] * len(touched)

        if order:                                 # 有設 build_order：照順序建城
            action = T.plan_with_order(st, order, get, touched)
        else:                                     # 沒設：資源田 + 倉庫/糧倉
            st.fields += _legacy_candidates(get, cfg["target_building_level"], touched)
            action = T.plan(st)

        print(f"[{mode}] 決策: {action[0]}  佇列={st.queue}  存量={st.stock}  來襲={st.attacks}")
        if st.attacks:
            T.notify(f"偵測到來襲 {st.attacks} 波！請手動處理")   # 目前只寫 log，見 README
        if action[0] == "need_storage":
            T.notify(f"slot {action[1].aid} 的成本超過倉庫容量 {st.cap}，請先升倉庫/糧倉")
        elif action[0] == "blocked":
            T.notify(f"建城計畫卡住了：{action[1]}")
        elif action[0] == "no_slot":
            T.notify(f"要蓋新建築（gid {action[1]}）但村莊沒有空地了")
        if action[0] != "build":
            label, nf, desc = describe(action, st)
            print(f"[{mode}] {label}：{desc}")
            log_action([_now(), label, nf.aid if nf else "", nf.gid if nf else "",
                        nf.level if nf else "", "/".join(map(str, nf.cost)) if nf else "",
                        mode, "未升級", _with_queue(desc, st)])
            break

        f = action[1]
        cost = "/".join(map(str, f.cost))
        what = f"升級 {_fname(f)} {_flv(f)}"
        url = f.url or T.find_upgrade_url(get(f"build.php?id={f.aid}"))
        if not url:
            print(f"slot {f.aid}: 找不到綠色擴建按鈕（資源不足、佇列滿、前置條件未達，"
                  f"或網址被白名單擋下：往上找有沒有 [警告] 那行）")
            log_action([_now(), "升級", f.aid, f.gid, f.level, cost, mode, "找不到按鈕",
                        _with_queue(f"想{what}，但找不到綠色按鈕（資源不足、佇列滿或前置條件未達）", st)])
            break
        touched.add(f.aid)

        if dry:
            print(f"[DRY] slot {f.aid} (gid {f.gid}) L{f.level}->L{f.level + 1}，會送出: {url}")
            log_action([_now(), "升級", f.aid, f.gid, f.level, cost, mode, "未送出",
                        _with_queue(f"{what}（乾跑，沒有真的送出）", st)])
            continue

        sleep(random.uniform(2, 6))
        submit(url)
        sleep(random.uniform(2, 5))
        after = T.parse_dorf1(get("dorf1.php"))
        ok = len(after.queue) > len(st.queue) or sum(after.stock) < sum(st.stock) - 0.5 * sum(f.cost)
        log_action([_now(), "升級", f.aid, f.gid, f.level, cost, mode, "成功" if ok else "沒看到變化",
                    _with_queue(what if ok else f"{what}，但送出後沒看到資源被扣或佇列增加", st)])
        if not ok:
            T.notify(f"送出 slot {f.aid} 的升級後沒看到資源被扣或佇列增加，已停止")
            return 1
        T.notify(f"已升級 slot {f.aid} (gid {f.gid}) L{f.level}->L{f.level + 1}，成本 {cost}")
        sleep(random.uniform(3, 8))
    return 0
