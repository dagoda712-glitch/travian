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
from typing import Optional


HEADER_ROW = [
    "抓取時間(UTC)", "伺服器時間", "村莊名", "人口", "忠誠度%",
    "金幣", "銀幣",
    "木材_存量", "木材_產量", "磚塊_存量", "磚塊_產量",
    "鋼鐵_存量", "鋼鐵_產量", "穀物_存量", "穀物_產量", "自由穀物",
    "英雄血量%", "英雄經驗%", "英雄可用冒險數",
    "施工佇列", "Plus剩餘(小時)", "新手保護剩餘(小時)",
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
