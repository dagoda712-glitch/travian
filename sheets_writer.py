#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Sheets 写入模块

通过 Google Service Account（服务账号）向指定表格追加一行数据。
在 GitHub Actions 里运行时，服务账号的 JSON 凭据通过环境变量
GOOGLE_SERVICE_ACCOUNT_JSON（整段 JSON 文本）传入。

首次设置步骤（一次性）：
1. 打开 https://console.cloud.google.com/ 新建一个项目（或用现有的）
2. 左侧菜单 "APIs & Services" -> "Library"，搜索并启用：
   - Google Sheets API
3. "APIs & Services" -> "Credentials" -> "Create Credentials" -> "Service account"
   随便起个名字，创建完成后点进去，"Keys" 标签 -> "Add Key" -> "Create new key"
   -> 选 JSON -> 下载。这个 JSON 文件里有个 client_email 字段，形如
   xxx@xxx.iam.gserviceaccount.com
4. 打开你要写入的 Google Sheet，点右上角"共享"，把上面那个 client_email
   添加为"编辑者"（这一步很关键，不然服务账号没权限写入）
5. 把下载的整个 JSON 文件内容，复制粘贴进 GitHub 仓库的
   Settings -> Secrets and variables -> Actions -> New repository secret，
   名字设为 GOOGLE_SERVICE_ACCOUNT_JSON
6. 表格的 ID 是网址里 /d/ 和 /edit 之间那一串字符，例如：
   https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit
                                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^ 这段
   同样存成 secret，名字设为 GOOGLE_SHEET_ID
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from notify import notify
from travian_const import DEFAULT_TARGET_BUILDING_LEVEL


HEADER_ROW = [
    "抓取時間(UTC)", "伺服器時間", "村莊名", "人口", "忠誠度%",
    "金幣", "銀幣",
    "木材_存量", "木材_產量", "磚塊_存量", "磚塊_產量",
    "鋼鐵_存量", "鋼鐵_產量", "穀物_存量", "穀物_產量", "自由穀物",
    "英雄血量%", "英雄經驗%", "英雄可用冒險數",
    "施工佇列", "Plus剩餘(小時)", "新手保護剩餘(小時)",
    "來襲波數", "最快來襲剩餘(分)",
]


def _get_credentials(service_account_json: Optional[str] = None):
    """
    从环境变量或传入字符串获取服务账号凭据，返回 gspread 可用的 Client。
    service_account_json: 整段 JSON 文本；若为 None 则从环境变量
    GOOGLE_SERVICE_ACCOUNT_JSON 读取。
    """
    import gspread
    from google.oauth2.service_account import Credentials

    raw = service_account_json or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError(
            "缺少服务账号凭据：请设置环境变量 GOOGLE_SERVICE_ACCOUNT_JSON "
            "（整段 JSON 文本），或在调用时传入 service_account_json 参数。"
        )

    info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def append_row(
    row: list,
    sheet_id: Optional[str] = None,
    worksheet_name: str = "Sheet1",
    service_account_json: Optional[str] = None,
    ensure_header: bool = True,
):
    """
    向指定 Google Sheet 的某个分页追加一行数据。
    如果分页是空的且 ensure_header=True，会先写入表头行。

    sheet_id: 表格 ID；若为 None 则从环境变量 GOOGLE_SHEET_ID 读取
    """
    client = _get_credentials(service_account_json)

    sid = sheet_id or os.environ.get("GOOGLE_SHEET_ID")
    if not sid:
        raise RuntimeError(
            "缺少表格 ID：请设置环境变量 GOOGLE_SHEET_ID，"
            "或在调用时传入 sheet_id 参数。"
        )

    spreadsheet = client.open_by_key(sid)

    try:
        ws = spreadsheet.worksheet(worksheet_name)
    except Exception:
        # 分页不存在则新建
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(HEADER_ROW) + 2)

    if ensure_header:
        first = ws.row_values(1)
        if not first:
            ws.append_row(HEADER_ROW, value_input_option="USER_ENTERED")
        elif first != HEADER_ROW:
            # 舊版表頭（欄位比較少）：補欄並把第 1 列更新成最新表頭，舊資料列不動
            if ws.col_count < len(HEADER_ROW):
                ws.add_cols(len(HEADER_ROW) - ws.col_count)
            ws.update(range_name="A1", values=[HEADER_ROW])

    ws.append_row(row, value_input_option="USER_ENTERED")
    return True


# ---------------- Config / Actions 分頁（原本在 runner.py）----------------
def has_sheet() -> bool:
    return bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") and os.getenv("GOOGLE_SHEET_ID"))


def open_sheet():
    """開啟 GOOGLE_SHEET_ID 指定的試算表（原 runner._sheet）。"""
    return _get_credentials().open_by_key(os.environ["GOOGLE_SHEET_ID"])


_FALSE_WORDS = ("false", "0", "no", "off", "否", "停")
_TRUE_WORDS = ("true", "1", "yes", "on", "是", "啟用")   # hero_adventure 專用：只有明確的 TRUE 才算開


# Config 分頁的預設列（順序 = 補上去時的順序）。C 欄是給人看的說明，程式只讀 A、B 欄。
_CONFIG_ORDER = ("enabled", "target_level", "queue_slots", "is_capital",
                 "max_builds", "target_building_level", "build_order",
                 "hero_adventure", "hero_min_health", "hero_allowed", "hero_skip_on_incoming", "hero_transfer_mode", "hero_transfer_min", "hero_transfer_max_gap", "hero_transfer_eta_min",
                 "hero_focus_mode", "hero_focus_min_hours", "hero_hard_min_health",
                 "hero_guard_mode", "hero_guard_hidden", "fill_in_mode", "fill_in_slack_min",
                 "hide_mode", "hide_min_attack", "hide_wait_max_s")
_CONFIG_NOTES = {
    "enabled": "FALSE = 緊急停止；session 失效時程式也會改成 FALSE",
    "target_level": "資源田升到幾級（沒設 build_order 時使用）",
    "queue_slots": "留空 = 自動偵測；填數字 = 手動指定",
    "is_capital": "首都才填 TRUE",
    "max_builds": "每輪最多送出幾個升級",
    "target_building_level": "倉庫／糧倉升到幾級，0 = 停用",
    "build_order": "建城順序，例如 res:2,10:3,15:5",
    "hero_adventure": "TRUE = 英雄在家時自動派冒險；留空或看不懂一律當 FALSE",
    "hero_min_health": "英雄體力 ≤ 這個數字就不出發（讀不到體力也不出發）",
    "hero_allowed": "允許的冒險難度圖示：normal 或 normal,hard（預設 normal）",
    "hero_skip_on_incoming": "TRUE = 村莊有來襲時不派英雄（預設 TRUE）",
    "hero_transfer_mode": "off／incoming_only／always（看不懂一律當 off）",
    "hero_transfer_min": "需要轉的每一種資源，道具庫存量都要 ≥ 此值才轉",
    "hero_transfer_max_gap": "各資源缺口加總 > 此值就不轉",
    "hero_transfer_eta_min": "最快來襲 ETA ≤ 此分鐘數才處理",
    "hero_focus_mode": "off／auto：auto = 建城「等待資源」時自動切換英雄產量加成（看不懂一律當 off；DRY 只記錄不送出）",
    "hero_hard_min_health": "體力 > 此數字才可選困難冒險（hero_allowed 要含 hard 才有作用，預設 60）",
    "hero_focus_min_hours": "「加成」（等待資源時的切換）兩次之間至少間隔幾小時（整數，預設 1）；庫存守衛「防搶加成」不看這個值，每輪都可切",
    "hero_guard_mode": "off／dry／incoming／always：庫存防搶＋防溢出守衛。dry = 只記錄「防搶加成」列、不送出；incoming = 有來襲才算暴露（溢出一律算）；always = 一律；要 hero_focus_mode=auto 才會真的切（看不懂一律當 off）",
    "hero_guard_hidden": "山洞每種資源的隱藏量（整數，預設 2000；本村埃及 10 級實測 2000）",
    "fill_in_mode": "off／dry／on：有 build_order 且主目標「等待資源」時，插隊做後面付得起、而且不會拖慢主目標開工的項目。dry = 只記錄「插隊」列、不送出；on = 真的先升；來襲時超過 hero_guard_hidden 的存量視為已被搶（看不懂一律當 off）",
    "fill_in_slack_min": "插隊允許把主目標開工時間拖慢幾分鐘（整數，預設 0＝不准拖慢）",
    "hide_mode": "躲兵：off／observe／dry／live。observe = 有來襲時只讀、只記錄（動作「躲兵觀察」），絕不派兵；dry = 走到確認頁、核對後不按確認；live = 核對通過才按確認派兵（DRY_RUN 測試環境下 live 會降成 dry）；off = 完全不做（看不懂一律當 off）",
    "hide_min_attack": "躲兵派出兵力的攻擊力合計必須「大於」這個數字（整數，預設 20；t1＝10、t2≈30）；沒列攻擊力的兵種算 0",
    "hide_wait_max_s": "躲兵 dry／live：自己的兵在幾秒內歸來、而且歸來後下一波還來得及，就等到歸來再派（整數秒，預設 180；0＝不等；observe 不等）",
}


def _default_cfg() -> dict:
    return {"enabled": True, "target_level": 2, "queue_slots": None, "max_builds": 2,
            "target_building_level": DEFAULT_TARGET_BUILDING_LEVEL, "build_order": "", "is_capital": False,
            "hero_adventure": False, "hero_min_health": 45, "hero_allowed": "normal",
            "hero_skip_on_incoming": True,"hero_transfer_mode": "off", "hero_transfer_min": 20000,
            "hero_transfer_max_gap": 1000, "hero_transfer_eta_min": 45,
            "hero_focus_mode": "off", "hero_focus_min_hours": 1, "hero_hard_min_health": 60,
            "hero_guard_mode": "off", "hero_guard_hidden": 2000,
            "fill_in_mode": "off", "fill_in_slack_min": 0,
            "hide_mode": "observe", "hide_min_attack": 20, "hide_wait_max_s": 180}


def _cfg_cell(v) -> str:
    """預設值轉成要寫進儲存格的純文字。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return str(v)


def _ensure_config_rows(ws, existing_rows: list) -> None:
    """Config 缺哪一列就補哪一列（只補缺的，已有的列不覆蓋）。
    existing_rows 是 load_config() 本來就讀進來的資料，不多打請求；
    沒有缺列時完全不寫入。用 raw=True 寫純文字，避免 build_order 之類的值被 Sheets 誤判成時間。"""
    have = {r[0].strip().lower() for r in existing_rows if r}
    defaults = _default_cfg()
    missing = [[k, _cfg_cell(defaults[k]), _CONFIG_NOTES[k]] for k in _CONFIG_ORDER if k not in have]
    if not missing:
        return
    start = len(existing_rows) + 1                 # 寫在既有資料的最後一列之後
    need_rows = start + len(missing) - 1
    if ws.row_count < need_rows:
        ws.add_rows(need_rows - ws.row_count)
    if ws.col_count < 3:                           # disable() 建的 Config 只有 2 欄，C 欄要先補
        ws.add_cols(3 - ws.col_count)
    ws.update(range_name=f"A{start}", values=missing, raw=True)
    print(f"Config 補上缺少的 {len(missing)} 列：{', '.join(r[0] for r in missing)}")


def load_config() -> dict:
    """讀 Sheet 的 Config 分頁；沒有這個分頁就用預設值（不是錯誤），並順手建立分頁、補齊預設列。
    分頁存在但缺列時，只補缺的列，不覆蓋已填的值。補齊失敗（例如權限問題）只印 log，不影響本輪。
    真正的異常（憑證、網路等）會往外拋，讓呼叫端中止本輪。"""
    from gspread.exceptions import WorksheetNotFound

    cfg = _default_cfg()
    if not has_sheet():
        return cfg
    sh = open_sheet()
    try:
        ws = sh.worksheet("Config")
    except WorksheetNotFound:
        print("Sheet 裡沒有 Config 分頁，使用預設值，並建立分頁")
        try:
            _ensure_config_rows(sh.add_worksheet("Config", 20, 3), [])
        except Exception as e:
            print(f"建立 Config 分頁失敗（不影響本輪，使用預設值）: {e!r}")
        return cfg
    rows = ws.get_all_values()
    for row in rows:
        if len(row) < 2:
            continue
        k, v = row[0].strip().lower(), row[1].strip()
        if k in ("enabled", "is_capital"):
            cfg[k] = v.lower() not in _FALSE_WORDS
        elif k == "hero_adventure":
            cfg[k] = v.lower() in _TRUE_WORDS            # 空白／看不懂 = False（不能沿用上面「空白 = TRUE」的規則）
        elif k == "hero_skip_on_incoming":
            cfg[k] = v.lower() not in _FALSE_WORDS       # 空白 = True（保守：有來襲就不派）
        elif k == "hero_allowed":
            if v:
                cfg[k] = v
        elif k == "build_order":
            cfg[k] = v
        elif k == "hero_transfer_mode":
            cfg[k] = v.lower()          # 只是存字串；空白／看不懂由 parse_config 當成 off
        elif k == "hero_focus_mode":
            cfg[k] = v.lower()          # 只有 "auto" 算開，其餘一律當 off（由 runner 判斷）
        elif k == "hero_guard_mode":
            cfg[k] = v.lower()          # off／dry／incoming／always，其餘由 runner 當 off
        elif k == "fill_in_mode":
            cfg[k] = v.lower()          # off／dry／on，其餘由 runner 當 off
        elif k == "hide_mode":
            cfg[k] = v.lower()          # off／observe／dry／live，其餘由 hide_observe 當 off（空白也是 off）
        elif k in ("target_level", "queue_slots", "max_builds", "target_building_level",
                   "hero_min_health", "hero_transfer_min", "hero_transfer_max_gap",
                   "hero_transfer_eta_min", "hero_focus_min_hours", "hero_hard_min_health",
                   "hero_guard_hidden", "fill_in_slack_min", "hide_min_attack", "hide_wait_max_s") and v.isdigit():
            cfg[k] = int(v)
    try:
        _ensure_config_rows(ws, rows)
    except Exception as e:
        print(f"補齊 Config 預設列失敗（不影響本輪）: {e!r}")
    return cfg


ACTIONS_HEADER = ["時間(UTC)", "動作", "slot", "gid", "目前等級", "成本", "模式", "結果", "升級項目 / 說明"]
_actions_header_ok = False


def log_action(row: list):
    global _actions_header_ok
    if not has_sheet():
        return
    try:
        sh = open_sheet()
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


def last_action_age_hours(action: str, result: str = "成功") -> Optional[float]:
    """Actions 分頁裡最近一筆（動作＝action、結果＝result）距現在幾小時。沒有這種列回 None。
    有這種列但時間讀不懂（儲存格被 Sheets 轉成別的日期格式）→ 回 0.0（保守：擋住切換），並印 log。
    讀取失敗回 0.0（同樣保守）。給「最短切換間隔」用，因為排程每輪都是全新的行程，記不住上次。"""
    if not has_sheet():
        return None
    try:
        ws = open_sheet().worksheet("Actions")
        acts, res = ws.col_values(2), ws.col_values(8)
        idx = next((i for i in range(min(len(acts), len(res)) - 1, 0, -1)
                    if acts[i] == action and res[i] == result), None)
        if idx is None:
            return None
        raw = ws.cell(idx + 1, 1).value or ""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                t = datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
                return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 3600)
            except ValueError:
                continue
        print(f"Actions 最近一筆「{action}」的時間讀不懂（{raw!r}），保守當成剛剛才發生")
        return 0.0
    except Exception as e:
        print(f"讀取 Actions 最近一筆「{action}」失敗，保守當成剛剛才發生: {e!r}")
        return 0.0


STATE_HEADER = ["項目(key)", "值", "說明", "更新時間(UTC)"]


def write_state(rows: list, updated: str) -> bool:
    """把玩家／村莊狀態（文化點、倉庫容量…）整份覆蓋寫到「state」分頁，給 Apps Script / Gemini 讀。
    rows 是 [(key, 值, 說明), ...]；覆蓋式（不是追加），所以分頁永遠只有最新一份。
    用 raw 寫入純值，避免日期字串被 Sheets 轉型。失敗只印 log、不影響升級；回傳是否成功。"""
    if not has_sheet():
        return False
    try:
        from gspread.exceptions import WorksheetNotFound
        sh = open_sheet()
        try:
            ws = sh.worksheet("state")
        except WorksheetNotFound:
            ws = sh.add_worksheet("state", 60, len(STATE_HEADER))
        values = [STATE_HEADER] + [[k, v, note, updated] for k, v, note in rows]
        ws.clear()
        ws.update(range_name="A1", values=values, raw=True)
        return True
    except Exception as e:
        print(f"寫入 state 分頁失敗（不影響升級）: {e!r}")
        return False


def disable(reason: str):
    """偵測到 session / Cookie 失效等需要人工介入的狀況時呼叫：
    把 Config 的 enabled 設成 FALSE，讓之後每一輪一讀到 cfg 就直接跳過，
    不會再嘗試登入、也不會每 30 分鐘重複失敗。要恢復請手動把 enabled 改回 TRUE。
    沒有連 Sheet 時沒辦法持久化，下一輪還是會照樣重試。"""
    notify(reason)
    if not has_sheet():
        print("沒有連 Google Sheet，無法自動停用，下一輪還會再試一次")
        return
    from gspread.exceptions import WorksheetNotFound
    try:
        sh = open_sheet()
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
