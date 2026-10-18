#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
檢查倉庫根目錄的 .py 是否符合 project-notes.md §2 的分類與依賴方向。
唯讀，不連網、不碰 Sheet、不碰 Secret。在倉庫根目錄執行：  python check_layout.py
有任何 FAIL 時結束碼為 1。需要更嚴格的未定義／未使用名稱檢查時，先 pip install pyflakes。

檢查項目：
 1. 該有的檔案都在、已刪除的檔案（auto_build.py）不在
 2. 每個檔案該有的頂層定義都在
 3. 「住址唯一」：某個名稱的家在 A 檔，其他檔案就不能自己定義一份（例如 agent 不能再定義 parse_dorf1）
 4. 依賴方向：每個檔案只能 import 允許的專案模組（不可反向）
 5. 低層模組（const / models / pages / agent / notify / parser）不能 import 網路或 Sheet 套件
 6. 可變設定（TARGET_LEVEL / QUEUE_SLOTS / IS_CAPITAL）只能在 travian_agent 被賦值
 7. 沒有 import *；沒有 import 已刪除的 auto_build
 8. （有裝 pyflakes 才做）未定義名稱、未使用的 import
"""
import ast
import sys
from pathlib import Path

ROOT = Path(".")

# 每個檔案「該有的頂層名稱」（家）。來源：project-notes.md §2、§10
MUST_DEFINE = {
    "travian_const.py": ["BUILDING_GIDS", "BIDI", "RES_NAMES", "GID_NAMES", "gid_name",
                         "RESOURCE_FIELD_PRODUCTION", "MAX_NON_CAPITAL_FIELD_LEVEL", "TRIBE_NAMES",
                         "ROMAN_TRIBE_ID", "BUILD_CATEGORIES", "FREE_SLOT_RANGE",
                         "DEFAULT_TARGET_BUILDING_LEVEL"],
    "travian_models.py": ["Field", "State", "Slot"],
    "travian_pages.py": ["parse_dorf1", "parse_movements", "parse_dorf2", "parse_building_cost",
                         "parse_player_info", "parse_dorf2_slots", "parse_build_options",
                         "find_upgrade_url", "_safe_build_url", "fetch_build_options"],
    "travian_agent.py": ["TARGET_LEVEL", "QUEUE_SLOTS", "IS_CAPITAL", "_roi_key", "queue_limit",
                         "resolve_queue_slots", "plan", "parse_build_order", "plan_with_order"],
    "notify.py": ["notify"],
    "sheets_writer.py": ["open_sheet", "has_sheet", "load_config", "log_action", "disable",
                         "append_row", "HEADER_ROW"],
    "runner.py": ["run_build_cycle", "in_quiet_hours", "get_cfg", "cp_summary",
                  "log_village_snapshot", "log_village_structure"],
    "travian_ci_run.py": ["main", "parse_cookie_header"],
    "scrape_and_log.py": ["build_row", "main"],
    "travian_parser.py": ["parse_village_page"],
    "village_log.py": [],
}
# 這些檔案（非家）不會被拿來比對「住址唯一」的名稱太泛用者
GENERIC = {"main"}

# 依賴方向：每個檔案允許 import 的專案模組（模組名，不含 .py）
ALLOWED = {
    "travian_const":     set(),
    "travian_models":    {"travian_const"},
    "travian_pages":     {"travian_const", "travian_models"},
    "notify":            set(),
    "travian_parser":    set(),
    "travian_agent":     {"travian_const", "travian_models", "travian_pages", "notify"},
    "sheets_writer":     {"travian_const", "notify"},
    "village_log":       {"travian_const", "travian_models", "travian_pages"},
    "scrape_and_log":    {"travian_parser", "sheets_writer"},
    "runner":            {"travian_const", "travian_models", "travian_pages", "travian_agent", "notify",
                          "sheets_writer", "village_log", "scrape_and_log", "travian_parser"},
    "travian_ci_run":    {"runner", "travian_pages", "notify", "sheets_writer"},
}
# 低層模組不可 import 的第三方（網路／瀏覽器／Sheet）
LOW_LEVEL = {"travian_const", "travian_models", "travian_pages", "travian_agent", "notify", "travian_parser"}
FORBIDDEN_3RD = {"requests", "httpx", "playwright", "gspread", "google"}
MUTABLE = {"TARGET_LEVEL", "QUEUE_SLOTS", "IS_CAPITAL"}
SHOULD_NOT_EXIST = ["auto_build.py"]

fails, warns = [], []


def fail(msg):
    fails.append(msg)
    print("  FAIL", msg)


def warn(msg):
    warns.append(msg)
    print("  警告", msg)


def parse(path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        fail(f"{path.name} 語法錯誤：{e}")
        return None


def top_defs(tree):
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        names.add(x.id)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names


def imports(tree):
    """回傳 [(頂層模組名, 別名或 None, 是否 import *)]，含函式內的延遲 import。"""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.append((a.name.split(".")[0], a.asname or a.name.split(".")[0], False))
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            out.append((n.module.split(".")[0], None, any(a.name == "*" for a in n.names)))
    return out


def main() -> int:
    py = {p.stem: p for p in ROOT.glob("*.py")}
    project = set(py) | {Path(f).stem for f in MUST_DEFINE}
    trees, defs = {}, {}

    print("[1] 檔案存在與否")
    for f in MUST_DEFINE:
        if not (ROOT / f).exists():
            fail(f"缺檔：{f}")
    for f in SHOULD_NOT_EXIST:
        if (ROOT / f).exists():
            fail(f"應已刪除但還在：{f}")

    for stem, p in py.items():
        t = parse(p)
        if t is not None:
            trees[stem] = t
            defs[stem] = top_defs(t)

    print("[2] 該有的頂層定義")
    home = {}
    for f, names in MUST_DEFINE.items():
        stem = Path(f).stem
        if stem not in defs:
            continue
        for nm in names:
            if nm not in defs[stem]:
                fail(f"{f} 缺少 {nm}")
            elif nm not in GENERIC:
                home[nm] = stem

    print("[3] 住址唯一（名稱只能定義在自己的家）")
    for nm, h in home.items():
        for stem, d in defs.items():
            if stem != h and nm in d and not stem.startswith(("debug_", "test_")):
                fail(f"{stem}.py 自己定義了 {nm}，它的家是 {h}.py")

    print("[4] 依賴方向 / [5] 低層不碰網路與 Sheet / [7] import * 與 auto_build")
    for stem, tree in trees.items():
        if stem.startswith(("debug_", "test_")):
            allowed = None  # 工具與測試不限制方向，只查 auto_build 與 import *
        else:
            allowed = ALLOWED.get(stem)
        for mod, alias, star in imports(tree):
            if star:
                fail(f"{stem}.py 使用 from {mod} import *")
            if mod == "auto_build":
                fail(f"{stem}.py 還在 import 已刪除的 auto_build")
            if mod in project and mod != stem and allowed is not None and mod not in allowed:
                fail(f"{stem}.py import {mod}，不在允許清單（允許：{', '.join(sorted(allowed)) or '無'}）")
            if stem in LOW_LEVEL and mod in FORBIDDEN_3RD:
                fail(f"{stem}.py 是低層模組，不該 import {mod}")
    # 還沒列入規劃、也不是 debug/test 的檔案：同時列出誰 import 了它，判斷能不能刪或移走
    for stem in sorted(py):
        if stem not in ALLOWED and not stem.startswith(("debug_", "test_", "check_")):
            users = sorted(o for o, t in trees.items()
                           if o != stem and any(m == stem for m, _, _ in imports(t)))
            warn(f"{stem}.py 不在規劃的檔案分類裡；被這些檔案 import：{', '.join(users) or '沒有人'}")

    print("[6] 可變設定只在 travian_agent 賦值")
    for stem, tree in trees.items():
        agent_alias = {a for m, a, _ in imports(tree) if m == "travian_agent" and a}
        for n in ast.walk(tree):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign)) else []
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr in MUTABLE:
                    base = t.value.id if isinstance(t.value, ast.Name) else "?"
                    if base not in agent_alias:
                        fail(f"{stem}.py 第 {n.lineno} 行對 {base}.{t.attr} 賦值，但 {base} 不是 travian_agent")
        if stem != "travian_agent":
            for nm in MUTABLE:
                if nm in defs.get(stem, set()) and not stem.startswith(("debug_", "test_")):
                    fail(f"{stem}.py 自己定義了 {nm}（應只在 travian_agent）")

    print("[8] pyflakes（未定義名稱／未使用 import）")
    try:
        from pyflakes.api import check
        from pyflakes.reporter import Reporter
        rep = Reporter(sys.stdout, sys.stdout)
        n_bad = 0
        for stem, p in sorted(py.items()):
            n_bad += check(p.read_text(encoding="utf-8"), p.name, rep)
        if n_bad:
            warn(f"pyflakes 回報 {n_bad} 項（上面列出；未使用的 import 通常是搬家後沒清乾淨）")
    except ImportError:
        warn("沒有安裝 pyflakes，略過。可用：pip install pyflakes")

    print("\n行數（供參考；重構後 travian_agent 約 270 行，之前是 769 行）：")
    for stem, p in sorted(py.items()):
        print(f"  {p.name:28s}{sum(1 for _ in p.open(encoding='utf-8')):5d}")

    print(f"\n結果：{len(fails)} 項 FAIL，{len(warns)} 項警告")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
