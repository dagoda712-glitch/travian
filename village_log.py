#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
村莊結構記錄：把「資源田 / 村莊建築 / 空地可蓋清單」存進 Google Sheet，有變動才更新，並記一筆變動紀錄。

分頁（程式自動建立）：
  Fields     資源田 18 塊，每塊一列（來源 dorf1，不需要額外請求）
  Buildings  村莊中心建築位 19～40，每位一列，空地也有一列（來源 dorf2，不需要額外請求）
  Buildable  空地可蓋的建築清單，每種建築一列（來源空地的 build.php 三個分頁，共 3 次請求）
  Changes    變動紀錄，一件事一列（只追加，不覆蓋）

原則：
- 狀態分頁「原地更新」：每輪先讀回現有內容，跟剛解析的結果比對；沒有差異就完全不寫入。
- 以 Sheet 為準做比對，不依賴 Actions 快取。快取沒命中也不會把所有東西誤判成變動。
- 第一次（分頁是空的或表頭不同）只寫入並在 Changes 記一筆「初始化」，不會產生幾十筆假變動。
- 任何一步失敗都只印 log，不影響升級（呼叫端也有 try/except）。

Buildable 只在需要時重抓（省請求）：第一次、Buildings 有變動、距離上次確認超過 24 小時；
村莊沒有空地時直接跳過。「資源夠不夠」每輪都在變，所以不記，只記「前置條件是否都已達成」。
"""
from datetime import datetime, timezone

import travian_const as C
import travian_pages as P

TS_FMT = "%Y-%m-%d %H:%M:%S"
BUILDABLE_MAX_AGE_H = 24

FIELDS_TAB, BUILDINGS_TAB, BUILDABLE_TAB, CHANGES_TAB = "Fields", "Buildings", "Buildable", "Changes"

FIELDS_HEADER = ["位置(slot)", "類型", "等級", "施工中", "升級成本(木/磚/鐵/糧)", "升級工期"]
BUILDINGS_HEADER = ["位置(slot)", "建築gid", "建築", "等級", "施工中"]
BUILDABLE_HEADER = ["gid", "建築", "分類", "成本(木/磚/鐵/糧)", "工期", "前置條件已達成", "未達成條件"]
CHANGES_HEADER = ["時間(UTC)", "類別", "位置", "項目", "舊→新", "備註"]


# ---------------- 小工具 ----------------
def _s(x) -> str:
    return "" if x is None else str(x)


def _dur(secs) -> str:
    secs = int(secs or 0)
    return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}" if secs > 0 else ""


def _yes(b) -> str:
    return "是" if b else "否"


def _get_ws(sh, name: str, rows: int = 100, cols: int = 12):
    """取得分頁；不存在就建立。用類別名稱判斷，不必在這裡 import gspread。"""
    try:
        return sh.worksheet(name)
    except Exception as e:
        if type(e).__name__ != "WorksheetNotFound":
            raise
        return sh.add_worksheet(title=name, rows=rows, cols=cols)


def sync_tab(ws, values, header, new_rows, n, watch, now, refresh=False, stamp_checked=False):
    """把 new_rows 跟現有內容（values = ws.get_all_values()）比對，必要時整頁寫回。

    header       資料欄的表頭（不含後面的「最後變動」「最後確認」欄）
    new_rows     每列前 n 欄是資料，第 0 欄是 key
    watch        要比對的欄位編號；這些欄有差異才算「變動」
    refresh      True = 就算沒有差異也寫（更新「最後確認」時間）
    stamp_checked True = 多一欄「最後確認(UTC)」
    回傳 (events, init, wrote)：events 是 [(種類, 新列字串, 舊列字串)]，種類是 新增 / 變動 / 移除
    """
    meta = ["最後變動(UTC)"] + (["最後確認(UTC)"] if stamp_checked else [])
    full_header = header + meta
    init = not values or list(values[0][:len(full_header)]) != full_header

    old = {}
    if not init:
        for row in values[1:]:
            if row and row[0] != "":
                old[row[0]] = list(row) + [""] * (len(full_header) - len(row))

    events, out, seen = [], [], set()
    for r in new_rows:
        rs = [_s(c) for c in r[:n]]
        seen.add(rs[0])
        o = old.get(rs[0])
        if init:
            last = now
        elif o is None:
            events.append(("新增", rs, None))
            last = now
        elif any(o[c] != rs[c] for c in watch):
            events.append(("變動", rs, o))
            last = now
        else:
            last = o[n] or now
        out.append(list(r[:n]) + [last] + ([now] if stamp_checked else []))
    if not init:
        for k, o in old.items():
            if k not in seen:
                events.append(("移除", None, o))

    wrote = init or bool(events) or refresh
    if wrote:
        rows = [full_header] + out
        if ws.row_count < len(rows):
            ws.add_rows(len(rows) - ws.row_count)
        if ws.col_count < len(full_header):
            ws.add_cols(len(full_header) - ws.col_count)
        ws.clear()
        ws.update(range_name="A1", values=rows, raw=True)
    return events, init, wrote


# ---------------- 資源田 ----------------
def fields_rows(st, values) -> list:
    """Playwright 有時會把 title 清空，導致成本讀不到。等級沒變時沿用舊的成本/工期，不要清成空白。"""
    prev = {r[0]: r for r in (values[1:] if values else []) if r}
    rows = []
    for f in sorted(st.fields, key=lambda x: x.aid):
        cost = "/".join(map(str, f.cost)) if f.cost else ""
        dur = _dur(f.secs)
        o = prev.get(str(f.aid))
        if not cost and o and len(o) > 5 and o[2] == str(f.level):
            cost, dur = o[4], o[5]
        rows.append([f.aid, C.gid_name(f.gid), f.level, _yes(f.building), cost, dur])
    return rows


def fields_changes(events, now) -> list:
    out = []
    for kind, new, old in events:
        if kind != "變動":
            row = new or old
            out.append([now, f"資源田{kind}", f"資源田 {row[0]}", row[1], "", ""])
            continue
        pos = f"資源田 {new[0]}"
        if old[1] != new[1]:
            out.append([now, "資源田類型變更", pos, new[1], f"{old[1]}→{new[1]}", ""])
        if old[2] != new[2]:
            out.append([now, "資源田等級", pos, new[1], f"L{old[2]}→L{new[2]}", ""])
        if old[3] != "是" and new[3] == "是":
            out.append([now, "資源田開始施工", pos, new[1], f"L{new[2]}→L{_lv(new[2]) + 1}", ""])
    return out


def _lv(s) -> int:
    return int(s) if str(s).isdigit() else 0


# ---------------- 村莊建築 ----------------
def dedupe_slots(slots) -> list:
    """dorf2 的石墻（slot 40）會出現兩次，同一個位置只留一筆（優先留施工中、等級高的）。"""
    best = {}
    for s in slots:
        o = best.get(s.aid)
        if o is None or (s.building, s.level) > (o.building, o.level):
            best[s.aid] = s
    return [best[a] for a in sorted(best)]


def buildings_rows(slots) -> list:
    return [[s.aid, s.gid, (s.name or C.gid_name(s.gid)) if s.gid else "（空地）", s.level, _yes(s.building)]
            for s in slots]


def buildings_changes(events, now) -> list:
    out = []
    for kind, new, old in events:
        if kind != "變動":
            row = new or old
            out.append([now, f"建築位{kind}", f"建築位 {row[0]}", row[2], "", ""])
            continue
        pos = f"建築位 {new[0]}"
        og, ng = old[1], new[1]
        if og == "0" and ng != "0":
            out.append([now, "新建築", pos, new[2], f"空地→{new[2]} L{new[3]}", "施工中" if new[4] == "是" else ""])
        elif og != "0" and ng == "0":
            out.append([now, "建築移除", pos, old[2], f"{old[2]} L{old[3]}→空地", ""])
        elif og != ng:
            out.append([now, "建築變更", pos, new[2], f"{old[2]}→{new[2]}", ""])
        else:
            if old[3] != new[3]:
                out.append([now, "建築等級", pos, new[2], f"L{old[3]}→L{new[3]}", ""])
            if old[4] != "是" and new[4] == "是":
                out.append([now, "建築開始施工", pos, new[2], f"L{new[3]}→L{_lv(new[3]) + 1}", ""])
    return out


# ---------------- 空地可蓋清單 ----------------
def _cond_text(conds) -> str:
    parts = []
    for c in conds:
        if c["met"]:
            continue
        if c["forbidden"]:
            parts.append(f"不可已有{c['name']}")
        else:
            cur = f"（目前 L{c['current']}）" if c["current"] is not None else ""
            parts.append(f"{c['name']} L{c['level']}{cur}")
    return "、".join(parts)


def buildable_rows(opts) -> list:
    rows = []
    for o in opts:
        unmet = [c for c in o["conditions"] if not c["met"]]
        rows.append([o["gid"], o["name"] or C.gid_name(o["gid"]), o.get("category", ""),
                     "/".join(map(str, o["cost"])), _dur(o["secs"]),   # 不能蓋時頁面不給工期 → 空白
                     _yes(not unmet), _cond_text(o["conditions"])])
    return rows


def buildable_changes(events, now) -> list:
    out = []
    for kind, new, old in events:
        if kind == "新增":
            note = "前置條件已達成" if new[5] == "是" else f"未達成：{new[6]}"
            out.append([now, "可蓋清單新增", "空地", new[1], new[2], note])
        elif kind == "移除":
            out.append([now, "可蓋清單移除", "空地", old[1], old[2], "已蓋、已達數量上限或清單改變"])
        else:
            if old[5] != "是" and new[5] == "是":
                out.append([now, "前置條件達成", "空地", new[1], "否→是", "現在可以蓋（資源另計）"])
            elif old[5] == "是" and new[5] != "是":
                out.append([now, "前置條件退回", "空地", new[1], "是→否", f"未達成：{new[6]}"])
            # 只有「未達成條件」文字改變（例如前置建築升了一級）：更新分頁，但不記變動，Buildings 已經記過
    return out


def buildable_due(values) -> bool:
    """Buildable 需要重抓嗎？（沒資料、表頭不同、或最後確認超過 24 小時）"""
    full = BUILDABLE_HEADER + ["最後變動(UTC)", "最後確認(UTC)"]
    if not values or len(values) < 2 or list(values[0][:len(full)]) != full:
        return True
    stamps = []
    for r in values[1:]:
        try:
            stamps.append(datetime.strptime(r[len(BUILDABLE_HEADER) + 1], TS_FMT).replace(tzinfo=timezone.utc))
        except (IndexError, ValueError):
            pass
    if not stamps:
        return True
    return (datetime.now(timezone.utc) - min(stamps)).total_seconds() >= BUILDABLE_MAX_AGE_H * 3600


# ---------------- 主入口 ----------------
def _changes_ws(sh):
    ws = _get_ws(sh, CHANGES_TAB, 1000, len(CHANGES_HEADER))
    if not ws.row_values(1):
        ws.append_row(CHANGES_HEADER)
    return ws


def log_structure(sh, d2, get, st, now=None) -> list:
    """sh = gspread 的 Spreadsheet；d2 = dorf2 的 HTML（None = 這輪沒有）；get(path) 抓頁面；st = parse_dorf1() 的結果。
    回傳這輪新增到 Changes 的列（方便測試）。"""
    now = now or datetime.now(timezone.utc).strftime(TS_FMT)
    changes = []

    def step(label, fn):
        try:
            fn()
        except Exception as e:
            print(f"[村莊結構] 記錄{label}失敗（不影響升級）: {e!r}")

    def do_fields():
        if not st.fields:
            return
        ws = _get_ws(sh, FIELDS_TAB, 100, len(FIELDS_HEADER) + 1)
        values = ws.get_all_values()
        rows = fields_rows(st, values)
        ev, init, wrote = sync_tab(ws, values, FIELDS_HEADER, rows, 6, (1, 2, 3), now)
        if init:
            changes.append([now, "初始化", FIELDS_TAB, "", f"{len(rows)} 塊資源田", ""])
        changes.extend(fields_changes(ev, now))
        print(f"[村莊結構] 資源田：{'初始化' if init else f'{len(ev)} 項變動' if ev else '沒有變動'}")

    step("資源田", do_fields)

    slots = None
    if d2 and 'id="villageContent"' in d2:
        slots = dedupe_slots(P.parse_dorf2_slots(d2))
    if not slots:
        print("[村莊結構] 這輪沒有 dorf2，略過村莊建築與可蓋清單")
    else:
        b_changed = []

        def do_buildings():
            ws = _get_ws(sh, BUILDINGS_TAB, 100, len(BUILDINGS_HEADER) + 1)
            values = ws.get_all_values()
            rows = buildings_rows(slots)
            ev, init, wrote = sync_tab(ws, values, BUILDINGS_HEADER, rows, 5, (1, 3, 4), now)
            b_changed.append(bool(ev))
            if init:
                changes.append([now, "初始化", BUILDINGS_TAB, "", f"{len(rows)} 個建築位", ""])
            changes.extend(buildings_changes(ev, now))
            print(f"[村莊結構] 村莊建築：{'初始化' if init else f'{len(ev)} 項變動' if ev else '沒有變動'}")

        step("村莊建築", do_buildings)

        def do_buildable():
            lo, hi = C.FREE_SLOT_RANGE
            empty = sorted(s.aid for s in slots if s.gid == 0 and lo <= s.aid <= hi)
            if not empty:
                print("[村莊結構] 村莊沒有空地，略過可蓋清單")
                return
            ws = _get_ws(sh, BUILDABLE_TAB, 100, len(BUILDABLE_HEADER) + 3)
            values = ws.get_all_values()
            if not (any(b_changed) or buildable_due(values)):
                print("[村莊結構] 可蓋清單：沒有觸發條件，這輪不重抓")
                return
            opts = P.fetch_build_options(get, empty[0])
            if not opts:
                print("[村莊結構] 可蓋清單讀不到任何建築（頁面異常？），這輪不更新")
                return
            rows = buildable_rows(opts)
            ev, init, wrote = sync_tab(ws, values, BUILDABLE_HEADER, rows, 7, (5, 6), now,
                                       refresh=True, stamp_checked=True)
            if init:
                changes.append([now, "初始化", BUILDABLE_TAB, "", f"{len(rows)} 種可蓋建築", ""])
            changes.extend(buildable_changes(ev, now))
            print(f"[村莊結構] 可蓋清單：{len(rows)} 種，{'初始化' if init else f'{len(ev)} 項變動' if ev else '沒有變動'}")

        step("可蓋清單", do_buildable)

    if changes:
        step("變動紀錄", lambda: _changes_ws(sh).append_rows(changes, value_input_option="RAW"))
    return changes
