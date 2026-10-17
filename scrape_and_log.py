#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Travian 村庄页面 -> 解析 -> 写入 Google Sheets 的辅助模块。

Actions 实际执行的入口是 travian_ci_run.py（Playwright）；这里只留 build_row()
（runner 在用），以及一个用本地 HTML 文件离线测试的命令行入口。
HTTP + Cookie 的即时抓取已移除（步骤 8）。

需要的环境变量（只有不加 --dry-run 时才需要）：
  GOOGLE_SERVICE_ACCOUNT_JSON  见 sheets_writer.py 顶部注释
  GOOGLE_SHEET_ID             见 sheets_writer.py 顶部注释

本地测试（不写入 Sheets，只打印结果）：
    python scrape_and_log.py --html sample_dorf2.html --dry-run

本地测试并写入 Sheets：
    python scrape_and_log.py --html sample_dorf2.html
"""

from __future__ import annotations

import argparse
import datetime as dt

from travian_parser import parse_village_page
from sheets_writer import append_row, HEADER_ROW


def build_row(overview, queue_text: str = "", plus_hours=None, protection_hours=None,
              attacks=None, attack_eta_min=None) -> list:
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
        attacks if attacks is not None else "",
        attack_eta_min if attack_eta_min is not None else "",
    ]
    assert len(row) == len(HEADER_ROW), "row 长度需和表头一致，检查是否漏填/多填字段"
    return row


def main():
    parser = argparse.ArgumentParser(description="抓取 Travian 村庄页面并记录到 Google Sheets")
    parser.add_argument("--html", required=True, help="本地 HTML 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写入 Google Sheets")
    parser.add_argument("--worksheet", default="Sheet1", help="要写入的分页名称，默认 Sheet1")
    args = parser.parse_args()

    with open(args.html, encoding="utf-8") as f:
        html = f.read()

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
