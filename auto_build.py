#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 用的「純 HTTP 版」自動升級（不需要瀏覽器）。

這支只負責「怎麼抓頁面、怎麼送出網址」（requests + Cookie）；
決策、build_order、送出與驗證、Config / Actions 分頁都在 runner.py。

環境變數：
  TRAVIAN_BASE_URL, TRAVIAN_COOKIE              必填
  GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_ID   選填（有的話讀 Config、寫 Actions）
  DRY_RUN     預設 1：只印出「會送出什麼」，不真的送出
  QUIET_UTC   不運作的 UTC 小時區間 "起,迄"，預設 "17,23"（台灣 01:00~07:00）
"""
import os
import sys

import requests

import runner as R
from travian_agent import find_upgrade_url  # noqa: F401  舊名稱轉發：debug_village_buildings.py 用 AB.find_upgrade_url

BASE = os.getenv("TRAVIAN_BASE_URL", "").rstrip("/")
COOKIE = os.getenv("TRAVIAN_COOKIE", "")
UA = os.getenv("TRAVIAN_UA", "Mozilla/5.0 (compatible; PersonalVillageLogger/1.0)")
_session = requests.Session()


def get(path: str) -> str:
    r = _session.get(f"{BASE}/{path.lstrip('/')}",
                     headers={"User-Agent": UA, "Cookie": COOKIE}, timeout=20)
    r.raise_for_status()
    return r.text


def main() -> int:
    if R.in_quiet_hours():
        print("安靜時段，本輪不動作")
        return 0
    if not BASE or not COOKIE:
        print("缺少 TRAVIAN_BASE_URL / TRAVIAN_COOKIE")
        return 1
    cfg, code = R.get_cfg()
    if cfg is None:
        return code
    dry = os.getenv("DRY_RUN", "1") != "0"
    return R.run_build_cycle(cfg, get=get, get_dorf1=lambda: get("dorf1.php"), submit=get, dry=dry)


if __name__ == "__main__":
    sys.exit(main())
