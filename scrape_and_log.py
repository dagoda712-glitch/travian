#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时抓取 Travian 村庄页面 -> 解析 -> 写入 Google Sheets

设计成可以直接被 GitHub Actions 的定时任务（cron）调用。

需要的环境变量（在 GitHub Secrets 里设置，Actions 里会自动读取）：
  TRAVIAN_BASE_URL           例如 https://ts11.x1.international.travian.com
  TRAVIAN_COOKIE             登录后浏览器 F12 -> Network -> 任意请求 -> 
                              Request Headers -> Cookie，整串复制过来
  GOOGLE_SERVICE_ACCOUNT_JSON  见 sheets_writer.py 顶部注释
  GOOGLE_SHEET_ID             见 sheets_writer.py 顶部注释

本地测试（不写入 Sheets，只打印结果）：
    python scrape_and_log.py --html sample_dorf2.html --dry-run

本地测试（真的抓取网页，但不写入 Sheets）：
    export TRAVIAN_BASE_URL="https://xxx.travian.com"
    export TRAVIAN_COOKIE="PHPSESSID=xxxx; other=yyy"
    python scrape_and_log.py --dry-run

正式在 GitHub Actions 里运行（写入 Sheets）：
    python scrape_and_log.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from travian_parser import parse_village_page
from sheets_writer import append_row, HEADER_ROW


def fetch_html_via_cookie(base_url: str, cookie: str, page: str = "dorf2.php") -> str:
    """用已登录的 Cookie 直接请求游戏页面，拿到 HTML。"""
    import requests

    url = f"{base_url.rstrip('/')}/{page}"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PersonalVillageLogger/1.0)",
        "Cookie": cookie,
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()

    # 简单校验：真实村庄页一定有 #stockBar（资源栏），登录页/过期页没有。
    # （原本用 "loginBox" 字符串判断，但登录页样式未经验证，改用已在真实
    #  页面确认过的 #stockBar 更可靠。）
    if 'id="stockBar"' not in resp.text:
        raise RuntimeError(
            "抓取到的页面没有 #stockBar，Cookie 可能已过期或页面结构变了，"
            "请重新从浏览器复制 Cookie。"
        )
    return resp.text


def build_row(overview, queue_text: str = "", plus_hours=None, protection_hours=None) -> list:
    """把 VillageOverview 对象拍平成一行，顺序需对应 sheets_writer.HEADER_ROW。"""
    res = overview.resources
    l1 = res.get("l1")
    l2 = res.get("l2")
    l3 = res.get("l3")
    l4 = res.get("l4")

    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    row = [
        now_utc,
        overview.server_time,
        overview.village_name,
        overview.population,
        overview.loyalty_percent,
        overview.gold,
        overview.silver,
        l1.amount if l1 else None,
        l1.production if l1 else None,
        l2.amount if l2 else None,
        l2.production if l2 else None,
        l3.amount if l3 else None,
        l3.production if l3 else None,
        l4.amount if l4 else None,
        l4.production if l4 else None,
        overview.free_crop,
        overview.hero.health_percent,
        overview.hero.experience_percent,
        overview.hero.adventures_available,
        queue_text,
        plus_hours if plus_hours is not None else "",
        protection_hours if protection_hours is not None else "",
    ]
    assert len(row) == len(HEADER_ROW), "row 长度需和表头一致，检查是否漏填/多填字段"
    return row


def main():
    parser = argparse.ArgumentParser(description="抓取 Travian 村庄页面并记录到 Google Sheets")
    parser.add_argument("--html", help="本地 HTML 文件路径（测试用，跳过真实抓取）")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写入 Google Sheets")
    parser.add_argument("--worksheet", default="Sheet1", help="要写入的分页名称，默认 Sheet1")
    args = parser.parse_args()

    if args.html:
        with open(args.html, encoding="utf-8") as f:
            html = f.read()
    else:
        base_url = os.environ.get("TRAVIAN_BASE_URL")
        cookie = os.environ.get("TRAVIAN_COOKIE")
        if not base_url or not cookie:
            print("缺少 TRAVIAN_BASE_URL / TRAVIAN_COOKIE 环境变量，"
                  "且未指定 --html 本地文件，无法继续。", file=sys.stderr)
            sys.exit(1)
        html = fetch_html_via_cookie(base_url, cookie)

    overview = parse_village_page(html)
    row = build_row(overview)

    print("=== 本次抓取结果 ===")
    for label, value in zip(HEADER_ROW, row):
        print(f"{label}: {value}")

    if args.dry_run:
        print("\n[dry-run] 未写入 Google Sheets。")
        return

    append_row(row, worksheet_name=args.worksheet)
    print("\n已写入 Google Sheets。")


if __name__ == "__main__":
    main()
