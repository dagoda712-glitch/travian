#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Travian 資料結構。只放資料（dataclass），不放邏輯、不 import 專案其他模組。"""
from dataclasses import dataclass, field


# ---------------- 資料結構 ----------------
@dataclass
class Field:
    aid: int          # 田位編號 (build.php?id=aid)
    gid: int          # 1 木 2 磚 3 鐵 4 糧
    level: int
    cost: list        # [木, 磚, 鐵, 糧]，升到下一級
    secs: int         # 升級所需秒數
    building: bool    # 是否正在施工（class 名稱未驗證）
    url: str = ""     # 已知的「興建/擴建」網址；空字串 = 由呼叫端自己去 build.php 找


@dataclass
class State:
    stock: list
    cap: list
    prod: list
    free_crop: int
    fields: list = field(default_factory=list)
    queue: list = field(default_factory=list)   # 剩餘秒數列表（選擇器未驗證）
    queue_info: list = field(default_factory=list)   # 跟 queue 一一對應，例如「農場 等級 1 剩0:01:46」
    attacks: int = 0
    movements: list = field(default_factory=list)   # parse_movements() 的結果
    attack_eta_s: int = None                         # 最快一批來襲剩餘秒數；沒有來襲 = None
    protection_s: int = None
    plus_s: int = None


@dataclass
class Slot:
    """村莊中心（dorf2）的一個建築位。gid=0 代表空地。"""
    aid: int
    gid: int
    level: int
    name: str
    building: bool    # 是否正在施工（class 名稱未驗證，見 parse_dorf2_slots）
