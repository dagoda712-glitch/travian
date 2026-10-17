#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知。目前只印 log（Actions 的執行紀錄）；之後要加 Telegram / Discord / 寄信都放這裡。"""
from datetime import datetime


# ---------------- 通知 ----------------
def notify(msg: str):
    """目前只印 log（Actions 的執行紀錄）。來襲寄信尚未實作，見 README。"""
    print(f"[{datetime.now():%H:%M:%S}] {msg}")
