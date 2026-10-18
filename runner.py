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
  QUIET_UTC   不運作的 UTC 小時區間 "起,迄"，預設 "17,23"（台灣 01:00~06:59；迄不含）

Sheet 的「Config」分頁：A 欄名稱、B 欄值，例如
  enabled       TRUE       <- 改成 FALSE 就是緊急停止開關
  target_level  2
  queue_slots   （留空/auto）<- 自動偵測：羅馬族 2、其他 1，Plus 有效再 +1；填數字就是手動指定（優先）
  is_capital    FALSE      <- 首都無資源田等級上限；非首都留 FALSE 或不填，會卡在 10 級（見 travian_agent.py）
  max_builds    2          <- 每輪最多送出幾個升級
  target_building_level 0  <- 舊流程：倉庫/糧倉升到幾級（0 = 停用）
  build_order   res:2,15:3 <- 建城順序（有填就用這個，舊流程的兩個目標等級不再使用）
  hero_adventure        FALSE <- TRUE = 英雄在家且條件符合時自動派冒險（空白／看不懂 = FALSE）
  hero_min_health       45    <- 體力 <= 這個數字不出發（讀不到也不出發）
  hero_allowed          normal <- 允許的難度圖示：normal 或 normal,hard
  hero_skip_on_incoming TRUE  <- 有來襲時不派英雄
  hero_focus_mode       off   <- auto = 建城「等待資源」時自動切換英雄產量加成（其餘一律當 off）
  hero_focus_min_hours  1     <- 兩次切換至少間隔幾小時
  hero_guard_mode       off   <- off／dry／incoming／always：庫存防搶＋防溢出守衛（dry 只記錄不送出；要 hero_focus_mode=auto 才會真的切）
  hero_guard_hidden     2000  <- 山洞每種資源隱藏量（守衛不看 hero_focus_min_hours，每輪都可切；插隊的來襲放寬也用它）
  fill_in_mode          off   <- off／dry／on：有 build_order 且主目標「等待資源」時，插隊做後面付得起、且不會拖慢主目標的項目（dry 只記錄「插隊」列、不送出）
  fill_in_slack_min     0     <- 插隊允許把主目標開工時間拖慢幾分鐘（整數，預設 0＝不准拖慢）
C 欄是說明，程式不讀。分頁不存在會自動建立；缺哪一列就補哪一列（只補缺的，已填的值不動）。
"""
import copy
import os
import random
import time
from datetime import datetime, timezone

import travian_agent as T   # 決策與可變設定：QUEUE_SLOTS / TARGET_LEVEL / IS_CAPITAL 由這裡賦值，plan / plan_with_order
import travian_pages as P   # 頁面解析
import travian_hero as H    # 英雄冒險：解析與決策（純函式）
import travian_transfer as TR   # 英雄道具庫轉資源：決策與執行（純函式＋注入 send/read_state）
import hero_focus as HF         # 英雄產量加成切換：決策（純函式）＋注入 read_focus/set_focus
import fill_in as FI          # 插隊：build_order 主目標等資源時，做後面付得起且不拖慢主目標的項目（純函式）
import stock_guard as SG        # 庫存防搶＋防溢出守衛（純函式；重用 focus_io 的 read_focus/set_focus）
from notify import notify
from travian_const import BUILDING_GIDS, RES_NAMES, TRIBE_NAMES, gid_name
from travian_models import Field


# Sheet 讀寫在 sheets_writer.py
from sheets_writer import disable, has_sheet, last_action_age_hours, load_config, log_action, open_sheet, write_state


def _hours(sec):
    return round(sec / 3600, 1) if sec is not None else None


def log_village_snapshot(d1: str, get, st):
    """每輪把村莊數據記到 Sheet1（唯讀、失敗不影響升級）。
    數字以 dorf1 的 var resources（st）為準補齊：Playwright 抓到的 stockBar title 可能被前端 JS 清空，
    產量會變 0；只在解析結果是空／0 時才用 st 補，其他維持原本 scrape_and_log 的格式。"""
    if not has_sheet():
        return None
    d2 = None     # 結尾回傳給 log_village_structure 用（dorf2 只抓一次）
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
                r = ov.resources.get(key) or ResourceInfo(key=key, name=RES_NAMES[i])
                r.amount = r.amount or st.stock[i]
                r.production = r.production or st.prod[i]
                r.capacity = r.capacity or st.cap[i]
                ov.resources[key] = r
            if ov.free_crop is None:
                ov.free_crop = st.free_crop
        eta_min = round(st.attack_eta_s / 60, 1) if st.attack_eta_s is not None else None
        row = build_row(ov, "；".join(st.queue_info), _hours(st.plus_s), _hours(st.protection_s),
                        attacks=st.attacks, attack_eta_min=eta_min)
        append_row(row, worksheet_name="Sheet1")
        print("已記錄村莊數據到 Sheet1")
    except Exception as e:
        print(f"寫入 Sheet1 失敗（不影響升級）: {e!r}")
    return d2


def log_village_structure(d1: str, d2, get, st) -> None:
    """每輪把資源田 / 村莊建築 / 空地可蓋清單同步到 Fields / Buildings / Buildable / Changes（見 village_log.py）。
    唯讀、失敗不影響升級。"""
    if not has_sheet():
        return
    try:
        import village_log as V
        V.log_structure(open_sheet(), d2, get, st)
    except Exception as e:
        print(f"記錄村莊結構失敗（不影響升級）: {e!r}")


# ---------------- 決策 → 人看得懂的說明 ----------------
def _fname(f) -> str:
    n = gid_name(f.gid)
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
        short = [f"{RES_NAMES[i]}缺{c - s}" for i, (c, s) in enumerate(zip(f.cost, st.stock)) if c > s]
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
        over = [f"{RES_NAMES[i]}成本{c}>容量{m}" for i, (c, m) in enumerate(zip(f.cost, st.cap)) if c > m]
        return "倉容不足", f, f"{_fname(f)} {_flv(f)} 的成本超過倉庫/糧倉容量（{'、'.join(over)}），需先升倉庫/糧倉"
    if kind == "blocked":
        return "計畫卡住", None, f"需要人處理：{action[1]}"
    if kind == "no_slot":
        return "無空地", None, f"要蓋新建築 {gid_name(action[1])}，但村莊沒有空地了"
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
    for gid, (aid, level) in P.parse_dorf2(get("dorf2.php")).items():
        if level >= target_level or aid in touched:
            continue
        cost, secs = P.parse_building_cost(get(f"build.php?id={aid}"))
        if not cost:
            print(f"警告：{BUILDING_GIDS.get(gid, gid)} (aid={aid}) 的 build.php 解析不到成本，跳過")
            continue
        out.append(Field(aid=aid, gid=gid, level=level, cost=cost, secs=secs, building=False))
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%F %T")


# ---------------- 玩家資訊：佇列長度 + 文化點 ----------------
def cp_summary(cp: dict) -> str:
    """把文化點缺口整理成一行 log。"""
    have, need, per_day = cp["have"], cp["need"], cp["per_day"]
    slots = f"村莊名額 {cp['used']}/{cp['max']}"
    gap = need - have
    if gap <= 0:
        return f"[文化點] {have}/{need}，已達標；{slots}"
    if per_day > 0:
        eta = f"，照目前每日 +{per_day} 約 {-(-gap // per_day)} 天（不含慶典）"
    else:
        eta = "，目前每日產量是 0"
    return f"[文化點] {have}/{need}，缺 {gap}{eta}；{slots}"


def build_state_rows(info, st, queue_slots) -> list:
    """把玩家／村莊狀態整理成 [(key, 值, 說明)]，給 write_state 寫進 Sheet 的「state」分頁（Gemini 會讀）。
    純函式，不碰網路／Sheet。info 可能是 None（頁面沒有內嵌資料），cp 也可能缺：讀不到的欄位就不列，不要猜。"""
    rows = []

    def put(key, value, note):
        if value is not None:
            rows.append((key, value, note))

    if info:
        tid = info.get("tribe_id")
        if tid is not None:
            put("tribe", TRIBE_NAMES.get(tid, f"種族 {tid}"), "種族")
        plus = info.get("plus_active")
        if plus is not None:
            put("plus_active", "TRUE" if plus else "FALSE", "Plus 是否有效")
        cp = info.get("cp")
        if cp:
            have, need, per_day = cp.get("have"), cp.get("need"), cp.get("per_day")
            put("cp_have", have, "目前文化點")
            put("cp_need", need, "下一個村莊名額需要的文化點")
            if have is not None and need is not None:
                gap = max(0, need - have)
                put("cp_gap", gap, "還缺多少文化點（0 = 已達標）")
                if gap == 0:
                    put("cp_eta_days", 0, "照目前每日產量估計還要幾天達標（不含慶典）")
                elif per_day and per_day > 0:
                    put("cp_eta_days", -(-gap // per_day), "照目前每日產量估計還要幾天達標（不含慶典）")
            put("cp_per_day", per_day, "文化點每日增加量（不含慶典）")
            put("village_slots_used", cp.get("used"), "已用村莊名額")
            put("village_slots_max", cp.get("max"), "目前文化點可擁有的村莊名額上限")
    put("queue_slots", queue_slots, "施工佇列長度（自動偵測或 Config 指定）")
    if st is not None and any(st.cap):
        for i, key in enumerate(("wood", "clay", "iron", "crop")):
            put(f"cap_{key}", st.cap[i], f"{RES_NAMES[i]}倉容量")
    return rows


def log_player_state(info, st) -> None:
    """每輪把文化點、倉庫容量等寫進 state 分頁（覆蓋式、只寫 Sheet、失敗不影響升級）。"""
    if not has_sheet():
        return
    try:
        write_state(build_state_rows(info, st, T.QUEUE_SLOTS), _now())
    except Exception as e:
        print(f"記錄 state 失敗（不影響升級）: {e!r}")


def _setup_player(cfg: dict, d1: str, get):
    """讀種族 / Plus / 文化點：決定 T.QUEUE_SLOTS，並印出文化點缺口。
    這一步只讀資料，任何一步失敗都只會退回保守值，不會影響升級流程。
    回傳讀到的 info（讀不到是 None），給 log_player_state 用。"""
    info = None
    try:
        info = P.parse_player_info(d1)
        if info is None:             # dorf1 沒有內嵌資料時改讀 dorf2（真實 dorf2 頁面已確認有）
            try:
                info = P.parse_player_info(get("dorf2.php"))
            except Exception as e:
                print(f"讀 dorf2 取得玩家資訊失敗（不影響升級）: {e!r}")
        n, why = T.resolve_queue_slots(cfg.get("queue_slots"), info)
        T.QUEUE_SLOTS = n
        print(f"佇列長度 = {n}（{why}）")
        if info and info.get("cp"):
            print(cp_summary(info["cp"]))
        else:
            print("[文化點] 讀不到（頁面沒有內嵌資料）")
    except Exception as e:
        print(f"讀取玩家資訊失敗，佇列長度沿用 {T.QUEUE_SLOTS}（不影響升級）: {e!r}")
    return info


# ---------------- 主迴圈 ----------------
def _try_transfer(tcfg, action, st, transfer_io, dry: bool, mode: str):
    """action 是 wait_res 時，判斷要不要從英雄道具庫轉資源。回傳 (attempted, ok)：
    attempted = 真的送出過 use-item（不管成敗，用來擋「每輪最多一次」）；ok = 全部送出且村莊現量驗證到位。
    任何例外只通知、不往外拋、不 disable。"""
    send, read_state = transfer_io
    f = action[2]
    target = dict(zip(TR.RES, f.cost))             # 目標＝這個升級的成本；缺口由 decide_transfer 算
    eta = st.attack_eta_s if st.attacks else None  # 佇列沒滿才會有 wait_res，所以 queue_busy 固定 False
    try:
        state = read_state()
        plan = TR.decide_transfer(tcfg, eta, False, target, state)
    except Exception as e:
        print(f"[{mode}][轉移] 讀取狀態／決策失敗，略過本輪轉移: {e!r}")
        notify(f"英雄道具庫轉移：讀取狀態失敗，略過本輪（不影響建城）: {e!r}")
        return False, False

    desc = f"{_fname(f)} {_flv(f)}｜{plan.decision}：{plan.reason}｜缺口={plan.gaps}｜來襲ETA={eta}"
    print(f"[{mode}][轉移] {desc}")

    def log(result, extra=""):
        log_action([_now(), "轉移", f.aid, f.gid, f.level, "/".join(map(str, f.cost)),
                    mode, result, desc + extra])

    if plan.decision != "transfer":
        log(plan.decision)
        return False, False
    if dry:
        log("未送出", f"｜items={plan.items}（乾跑，沒有真的送 use-item）")
        return False, False

    try:
        res = TR.execute_transfer(plan, target, send, read_state, phases=TR.FRONTEND_PHASES)
    except Exception as e:
        log("例外", f"｜{e!r}")
        notify(f"英雄道具庫轉移出錯（已送出的不會重送）: {e!r}")
        return True, False
    log("成功" if res.ok else "失敗", f"｜{res.detail}｜sent={res.sent}")
    notify(("已從英雄道具庫轉資源：" if res.ok else "英雄道具庫轉移沒有確認成功（不重試）：") + f"{res.detail}")
    return True, res.ok
  
def _try_focus(cfg, action, st, focus_io, get_dorf1, dry: bool, mode: str) -> bool:
    """action 是 wait_res 時，看要不要切換英雄產量加成，讓缺的資源補得更快。
    只有「要切換」才寫 Actions；不用切換只印 log（避免每 40 分鐘一列）。
    任何例外只通知、不往外拋、不 disable。DRY 只記錄「會改成什麼」，不送。
    回傳 True＝本輪加成已處理（決定要切、切換失敗、或讀取失敗），守衛本輪不再跑；False＝沒動作。"""
    read_focus, set_focus = focus_io
    f = action[2]
    gaps = {i + 1: c - s for i, (c, s) in enumerate(zip(f.cost, st.stock)) if c > s}
    prod = {i + 1: st.prod[i] for i in range(4)}
    # v2.31：穀改用 var resources 的 l4（st.prod[3]，含英雄加成）。實測 l5（free_crop）不含加成：押穀時 l4=1,505、l5=227；押磚時 l4=225、l5=227
    prod[4] = st.prod[3]
    # 說明欄一律帶「穀總產量」（st.prod[3]）與淨產量並列：兩者差距若不等於人口耗糧，代表淨產量沒算進英雄加成
    desc = (f"{_fname(f)} {_flv(f)}｜缺口={gaps}｜產量={[prod[k] for k in (1, 2, 3, 4)]}"
            f"｜穀自由產量l5={st.free_crop}")

    def log(result, extra=""):
        # desc 會在決策完成後更新（閉包讀的是呼叫當下的值），所以「不動」「讀取失敗」「切換」都用同一個函式寫 Actions
        log_action([_now(), "加成", f.aid, f.gid, f.level, "/".join(map(str, f.cost)),
                    mode, result, desc + extra])

    try:
        cur, types = read_focus()
        age = last_action_age_hours("加成")     # 只認結果＝「成功」的列，所以下面寫的「不動」「讀取失敗」不會影響最短間隔
        dec = HF.decide_focus(gaps, prod, cur, types, age,
                              min_hours=float(cfg.get("hero_focus_min_hours", 1)),
                              back_to_balanced=False)
    except Exception as e:
        print(f"[{mode}][加成] 讀取狀態／決策失敗，略過: {e!r}")
        notify(f"英雄產量加成：讀取狀態失敗，略過本輪（不影響建城）: {e!r}")
        log("讀取失敗", f"｜{e!r}")
        return True
    kind, target, why = dec
    age_txt = "無紀錄" if age is None else f"{age:.1f}h"
    desc = (f"{_fname(f)} {_flv(f)}｜目前 {HF.RES_NAME[cur]}｜缺口={gaps}｜"
            f"產量={[prod[k] for k in (1, 2, 3, 4)]}｜穀自由產量l5={st.free_crop}｜加成量={types}｜"
            f"距上次切換={age_txt}｜{kind}：{why}")
    print(f"[{mode}][加成] {desc}")
    if kind != "switch":
        log("不動")          # 以前這裡只印 log、不寫 Actions；「等待資源」一輪才一列，量很小
        return False

    expect = {r: prod[r] - HF.hero_bonus(cur, types, r) + HF.hero_bonus(target, types, r) for r in (1, 2, 3, 4)}
    if dry:
        log("未送出", f"｜改成 {HF.RES_NAME[target]}，預期產量={[expect[k] for k in (1, 2, 3, 4)]}（乾跑，沒有真的送出）")
        return True
    try:
        sent = set_focus(target)
    except Exception as e:
        log("失敗", f"｜{e!r}")
        notify(f"英雄產量加成切換失敗（不重試）: {e!r}")
        return True
    extra = f"｜{sent}"
    try:
        after = P.parse_dorf1(get_dorf1())
        got = {1: after.prod[0], 2: after.prod[1], 3: after.prod[2], 4: after.prod[3]}
        bad = [HF.RES_NAME[r] for r in (1, 2, 3, 4) if abs(got[r] - expect[r]) > 2]
        extra += f"｜產量 預期={[expect[k] for k in (1, 2, 3, 4)]} 實際={[got[k] for k in (1, 2, 3, 4)]}"
        extra += "｜驗證符合" if not bad else f"｜驗證不符：{'、'.join(bad)}"
        if bad:
            notify(f"英雄產量加成已切到 {HF.RES_NAME[target]}，但產量與預期不符：{'、'.join(bad)}")
    except Exception as e:
        extra += f"｜切換後重讀產量失敗：{e!r}"
    log("成功", extra)
    notify(f"英雄產量加成：{HF.RES_NAME[cur]} → {HF.RES_NAME[target]}（{why}）")
    return True


def _guard_mode(cfg) -> str:
    """off／dry／incoming／always；空白或看不懂一律當 off。"""
    m = str(cfg.get("hero_guard_mode", "off")).strip().lower()
    return m if m in ("dry", "incoming", "always") else "off"


def _fill_mode(cfg) -> str:
    """off／dry／on；空白或看不懂一律當 off。"""
    m = str(cfg.get("fill_in_mode", "off")).strip().lower()
    return m if m in ("dry", "on") else "off"


def _try_fill_in(cfg, st, order, get, touched, mode: str, fmode: str):
    """build_order 主目標是 wait_res 時，看後面的目標有沒有「付得起、而且不會拖慢主目標開工」的可以先做。
    回傳 ("build", Field)＝插隊（fmode=on）；None＝不動（fmode=dry 永遠回 None，只記錄）。
    判斷在 fill_in.should_fill；候選由 T.plan_fill_ins 依 build_order 順序給，取第一個符合的。
    任何例外只印 log、回 None，不影響原本流程。Actions 動作名稱「插隊」（dry 每輪都記，on 只記有插隊的）。"""
    try:
        main, cands = T.plan_fill_ins(st, order, get, touched)
    except Exception as e:
        print(f"[{mode}][插隊] 取候選失敗，略過: {e!r}")
        return None
    if main[0] != "wait_res":
        return None
    mf = main[2]
    hidden = float(cfg.get("hero_guard_hidden", 2000))
    slack_h = float(cfg.get("fill_in_slack_min", 0)) / 60.0
    head = f"主目標 {_fname(mf)} {_flv(mf)}（成本 {'/'.join(map(str, mf.cost))}）"
    picked, notes = None, []
    for f in cands:
        ok, info = FI.should_fill(st.stock, st.prod, mf.cost, f.cost, f.secs, st.queue, T.QUEUE_SLOTS,
                                  hidden=hidden, attacks=st.attacks, attack_eta_s=st.attack_eta_s, slack_h=slack_h)
        notes.append(f"{_fname(f)} {_flv(f)}：{info['why']}")
        if ok and picked is None:
            picked = (f, info)
            if fmode == "on":
                break
    if not cands:
        notes.append("後面沒有現在付得起的目標")
    desc = f"{head}｜存量={[int(x) for x in st.stock]}｜來襲={st.attacks}｜" + "；".join(notes)
    print(f"[{mode}][插隊] {desc}")
    if picked is None:
        if fmode == "dry":
            log_action([_now(), "插隊", mf.aid, mf.gid, mf.level, "/".join(map(str, mf.cost)), mode, "不動", desc])
        return None
    f, info = picked
    log_action([_now(), "插隊", f.aid, f.gid, f.level, "/".join(map(str, f.cost)), mode,
                "未送出" if fmode == "dry" else "採用", desc])
    if fmode == "dry":
        return None
    notify(f"插隊：先升 {_fname(f)} {_flv(f)}（{info['why']}）")
    return ("build", f)


def _next_target(st, order, get, touched, action):
    """佇列忙（wait_queue）時，算「下一個目標」的缺口，給守衛當補齊時間的下限依據。
    做法：複製 State、清空佇列，用同一份 build_order 再跑 plan_with_order，結果交給 SG.gaps_from_plan 轉換。
    回傳 (gaps, ready_in_h, 說明) 或 None（沒有 build_order、下一目標正在施工、讀頁失敗…一律退回 gaps=None＝純拉平）。
    ready_in_h＝wait_queue 的等待秒數（施工結束、可以買的時間）。
    會多抓 dorf2／build.php（只有順序裡有資源田以外的目標才抓），所以守衛只在「真的想切換」時才呼叫。任何例外都回 None。"""
    if not order or action[0] != "wait_queue":
        return None
    try:
        st2 = copy.deepcopy(st)
        st2.queue = []
        if hasattr(st2, "queue_info"):
            st2.queue_info = []
        nxt = T.plan_with_order(st2, order, get, touched)
        ready_h = float(action[1]) / 3600.0
        got = SG.gaps_from_plan(nxt, st.stock, ready_h)
    except Exception as e:
        print(f"[守衛] 取下一目標缺口失敗，退回無缺口: {e!r}")
        return None
    if got is None:
        return None
    gaps, ready_h = got
    f = nxt[2] if nxt[0] == "wait_res" else nxt[1]
    return gaps, ready_h, f"｜下一目標 {_fname(f)} {_flv(f)}｜{ready_h:.2f}h 後可買｜下一目標缺口={gaps}"


def _try_guard(cfg, action, st, focus_io, get_dorf1, dry: bool, mode: str, gmode: str, lookup=None) -> bool:
    """庫存防搶＋防溢出守衛：action 是 wait_res 或 wait_queue 時，預估下一輪的溢出與超過山洞隱藏量的暴露，
    必要時把英雄產量加成移到成本最低的資源。和 _try_focus 分開記錄（Actions 動作名稱「防搶加成」）。
    gmode：dry＝只記錄不送出；incoming＝沒有來襲時暴露不計（只防溢出）；always＝一律計。
    任何例外只通知、不往外拋、不 disable。回傳 True＝LIVE 已切換成功。"""
    read_focus, set_focus = focus_io
    f = action[2] if action[0] == "wait_res" else None          # wait_queue 沒有目標，缺口不明（第一階段傳 None）
    stock = {i + 1: st.stock[i] for i in range(4)}
    prod = {i + 1: st.prod[i] for i in range(4)}                   # 穀＝l4（含加成），和 _try_focus 同一來源
    cap = {i + 1: st.cap[i] for i in range(4)} if any(st.cap) else None
    gaps = {i + 1: c - s for i, (c, s) in enumerate(zip(f.cost, st.stock)) if c > s} if f is not None else None
    hidden = float(cfg.get("hero_guard_hidden", 2000))
    lam = 0.5 if (gmode in ("always", "dry") or st.attacks) else 0.0
    head = (f"{_fname(f)} {_flv(f)}｜" if f is not None else "施工佇列忙｜")
    desc = (f"{head}存量={[stock[k] for k in (1, 2, 3, 4)]}｜產量={[prod[k] for k in (1, 2, 3, 4)]}"
            f"｜缺口={gaps}｜來襲={st.attacks}｜模式={gmode}")

    def log(result, extra=""):
        log_action([_now(), "防搶加成", f.aid if f else "", f.gid if f else "", f.level if f else "",
                    "/".join(map(str, f.cost)) if f else "", mode, result, desc + extra])

    try:
        cur, types = read_focus()
        dec = SG.guard_focus(stock, prod, cur, types, hidden=hidden, capacity=cap, gaps=gaps,
                             lambda_exposure=lam)
        # 佇列忙：先不帶缺口跑一次；只有「真的想切換」才去查下一目標缺口，再用缺口重算一次
        # （缺口只會縮小候選範圍，所以第一次說「不動」時，帶缺口也一定是不動，不用多抓頁面）
        if dec[0] == "switch" and f is None and lookup is not None:
            nt = lookup()
            if nt is not None:
                gaps, ready_h, note = nt
                desc += note
                dec = SG.guard_focus(stock, prod, cur, types, hidden=hidden, capacity=cap, gaps=gaps,
                                     lambda_exposure=lam, ready_in_h=ready_h)
            else:
                desc += "｜下一目標缺口算不出來（正在施工／無 build_order／讀頁失敗），只看溢出與暴露"
    except Exception as e:
        print(f"[{mode}][守衛] 讀取狀態／決策失敗，略過: {e!r}")
        notify(f"庫存守衛：讀取狀態失敗，略過本輪（不影響建城）: {e!r}")
        log("讀取失敗", f"｜{e!r}")
        return False
    kind, target, why = dec
    desc += f"｜目前 {HF.RES_NAME[cur]}｜{kind}：{why}"
    print(f"[{mode}][守衛] {desc}")
    if kind != "switch":
        if gmode == "dry":
            log("不動")            # dry 階段每輪都記，方便對照實際存量；其他模式「不動」只印 log
        return False
    if dry or gmode == "dry":
        log("未送出", f"｜改成 {HF.RES_NAME[target]}（{'乾跑' if dry else '守衛 dry'}，沒有真的送出）")
        return False
    try:
        # 2026-10-18：守衛不設冷卻，每一輪都可以切（使用者決定；decide_focus 的 hero_focus_min_hours 不受影響）
        sent = set_focus(target)
    except Exception as e:
        log("失敗", f"｜{e!r}")
        notify(f"庫存守衛切換加成失敗（不重試）: {e!r}")
        return False
    extra = f"｜{sent}"
    try:
        cur2, _t = read_focus()
        extra += "｜重讀加成符合" if cur2 == target else f"｜重讀加成不符：{HF.RES_NAME.get(cur2, cur2)}"
        if cur2 != target:
            notify(f"庫存守衛已送出 {HF.RES_NAME[target]}，但重讀加成是 {HF.RES_NAME.get(cur2, cur2)}")
    except Exception as e:
        extra += f"｜重讀加成失敗：{e!r}"
    log("成功", extra)
    notify(f"庫存守衛：加成 {HF.RES_NAME[cur]} → {HF.RES_NAME[target]}（{why}）")
    return True



def run_build_cycle(cfg: dict, *, get, get_dorf1, submit, dry: bool,
                    first_dorf1: str = None, sleep=time.sleep, transfer_io=None, focus_io=None) -> int:
    """跑一輪：最多送出 cfg["max_builds"] 個升級。回傳結束碼（0 正常、1 需要人看）。"""
    T.TARGET_LEVEL = cfg["target_level"]
    T.IS_CAPITAL = bool(cfg.get("is_capital"))
    T.QUEUE_SLOTS = cfg["queue_slots"] if cfg.get("queue_slots") is not None else 1   # 偵測前的保守值

    order = []
    if cfg.get("build_order"):
        try:
            order = T.parse_build_order(cfg["build_order"])
        except ValueError as e:
            notify(f"Config 的 build_order 格式錯誤，本輪不動作：{e}")
            return 1
        print(f"使用建城順序（{len(order)} 個目標）：{cfg['build_order']}")

    mode = "DRY" if dry else "LIVE"
    touched = set()
    info = None
    tcfg = TR.parse_config(cfg)
    transferred = False        # 每輪最多嘗試一次轉移（max_builds > 1 也一樣）
    focus_on = focus_io is not None and str(cfg.get("hero_focus_mode", "off")).strip().lower() == "auto"
    focused = False            # 每輪最多評估一次加成切換
    guard_mode = _guard_mode(cfg)
    fill_mode = _fill_mode(cfg)
    guarded = False            # 每輪最多評估一次守衛
    focus_switched = False     # 本輪 _try_focus 是否已處理加成（是的話守衛不跑）

    for i in range(cfg["max_builds"]):
        d1 = first_dorf1 if (i == 0 and first_dorf1 is not None) else get_dorf1()
        if 'id="stockBar"' not in d1:
            disable("抓到的不是村莊頁：session / Cookie 可能已失效，請重新複製 Cookie 更新 Secret")
            return 1
        if i == 0:
            info = _setup_player(cfg, d1, get)    # 決定佇列長度、印文化點（只讀）
        st = P.parse_dorf1(d1)
        if i == 0:
            d2 = log_village_snapshot(d1, get, st)
            log_village_structure(d1, d2, get, st)
            log_player_state(info, st)

        def _plan(st):
            for f in st.fields:                       # 本輪已送出的田，不要重複選
                if f.aid in touched:
                    f.building = True
            if dry and touched:                       # 乾跑沒真的送出：把「已送出」的也算進佇列長度
                st.queue = st.queue + [1800] * len(touched)
                st.queue_info = st.queue_info + ["（乾跑模擬：本輪已排入的升級）"] * len(touched)
            if order:
                return T.plan_with_order(st, order, get, touched)
            st.fields += _legacy_candidates(get, cfg["target_building_level"], touched)
            return T.plan(st)

        action = _plan(st)

        # 付不起 → 來襲時從英雄道具庫補缺口（mode=off 或沒傳 transfer_io 時完全不動）
        if (action[0] == "wait_res" and transfer_io is not None
                and tcfg.mode != "off" and not transferred):
            attempted, ok = _try_transfer(tcfg, action, st, transfer_io, dry, mode)
            transferred = transferred or attempted
            if ok:                                    # 資源到位：重讀 dorf1，同一輪重新決策、開工
                d1 = get_dorf1()
                st = P.parse_dorf1(d1)
                action = _plan(st)
              

        # 缺口太大／庫存不足／轉移失敗，且有來襲：改做「現有資源就付得起」的項目，趁資源被搶之前用掉。
        # 只在轉移功能開著（mode≠off）、舊流程（沒有 build_order，有 build_order 就是嚴格照順序）時啟用；
        # 直接用這一輪已算好候選的 st，不重抓頁面、不重複加候選。
        if (action[0] == "wait_res" and transfer_io is not None and tcfg.mode != "off"
                and st.attacks and not order):
            alt = T.plan(st, affordable_only=True)
            if alt[0] == "build":
                print(f"[{mode}][轉移] 資源補不到，有來襲：改做付得起的 {_fname(alt[1])} {_flv(alt[1])}"
                      f"（原本要等 {_fname(action[2])} {_flv(action[2])}）")
                action = alt

        # 有 build_order、主目標付不起（轉移沒做、沒補到或沒開）→ 看後面有沒有付得起、而且不拖慢主目標的項目可以先做
        # 插隊成功時 action 變成 build，下面的加成與守衛本輪不跑；fill_in_mode＝dry 只記錄、action 不變
        if action[0] == "wait_res" and order and fill_mode != "off":
            alt = _try_fill_in(cfg, st, order, get, touched, mode, fill_mode)
            if alt is not None:
                action = alt

        # 還是付不起（轉移沒做、沒補到或沒開）→ 看要不要把英雄產量加成切到缺的資源
        if action[0] == "wait_res" and focus_on and not focused:
            focused = True
            focus_switched = _try_focus(cfg, action, st, focus_io, get_dorf1, dry, mode)

        # 庫存守衛：付不起或佇列忙時，預估下一輪的溢出／暴露，必要時移動加成（dry 模式只記錄；其餘模式要 hero_focus_mode=auto）
        if (action[0] in ("wait_res", "wait_queue") and focus_io is not None and not guarded
                and guard_mode != "off" and (focus_on or guard_mode == "dry")):
            guarded = True
            if not focus_switched:
                lookup = ((lambda: _next_target(st, order, get, touched, action))
                          if action[0] == "wait_queue" else None)
                _try_guard(cfg, action, st, focus_io, get_dorf1, dry, mode, guard_mode, lookup)

        print(f"[{mode}] 決策: {action[0]}  佇列={st.queue}  存量={st.stock}  來襲={st.attacks}")
        if st.attacks:
            eta = f"，最快 {_mins(st.attack_eta_s)} 後抵達" if st.attack_eta_s is not None else ""
            notify(f"偵測到來襲 {st.attacks} 波{eta}！請手動處理")   # 目前只寫 log，見 README
        if action[0] == "need_storage":
            notify(f"slot {action[1].aid} 的成本超過倉庫容量 {st.cap}，請先升倉庫/糧倉")
        elif action[0] == "blocked":
            notify(f"建城計畫卡住了：{action[1]}")
        elif action[0] == "no_slot":
            notify(f"要蓋新建築（gid {action[1]}）但村莊沒有空地了")
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
        url = f.url or P.find_upgrade_url(get(f"build.php?id={f.aid}"))
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
        after = P.parse_dorf1(get("dorf1.php"))
        ok = len(after.queue) > len(st.queue) or sum(after.stock) < sum(st.stock) - 0.5 * sum(f.cost)
        log_action([_now(), "升級", f.aid, f.gid, f.level, cost, mode, "成功" if ok else "沒看到變化",
                    _with_queue(what if ok else f"{what}，但送出後沒看到資源被扣或佇列增加", st)])
        if not ok:
            notify(f"送出 slot {f.aid} 的升級後沒看到資源被扣或佇列增加，已停止")
            return 1
        notify(f"已升級 slot {f.aid} (gid {f.gid}) L{f.level}->L{f.level + 1}，成本 {cost}")
        sleep(random.uniform(3, 8))
    return 0


# ---------------- 英雄冒險（獨立於建城；失敗只通知，不 disable、不改結束碼）----------------
def _hero_log(mode: str, result: str, desc: str) -> None:
    log_action([_now(), "冒險", "", "", "", "", mode, result, desc])


def run_hero_cycle(cfg: dict, *, get, get_dorf1, click_adventure, dry: bool) -> None:
    """英雄在家、體力 > hero_min_health、有符合難度的冒險時，派出一趟。其他情況記錄原因、不動作。

      get(path)           -> str  抓頁面（Playwright 渲染後的 HTML，例如 "hero/adventures"）
      get_dorf1()         -> str  抓 dorf1（頂端列有英雄圖示；也用來數來襲）
      click_adventure(number, row_index, row) -> (bool, 說明)  由 travian_ci_run 提供，負責點擊與驗證

    任何例外都只通知，不往外拋；hero_adventure 預設 FALSE。"""
    if not cfg.get("hero_adventure"):
        print("[冒險] hero_adventure 不是 TRUE，略過")
        return
    mode = "DRY" if dry else "LIVE"
    try:
        d1 = get_dorf1()
        if 'id="stockBar"' not in d1:
            print("[冒險] dorf1 不是村莊頁（session 問題由建城流程處理），略過")
            return
        hud1 = H.parse_hero_hud(d1)
        if hud1.at_home is False:
            print(f"[冒險] 英雄不在家（頂端列圖示），略過（體力 {hud1.health}%）")
            return
        incoming = P.parse_dorf1(d1).attacks if cfg.get("hero_skip_on_incoming", True) else 0

        html = get("hero/adventures")
        hero = H.parse_hero_json(html)
        rows = H.parse_adventure_rows(html)
        hud = H.parse_hero_hud(html)
        health = hud1.health if hud1.health is not None else hud.health   # 渲染頁的 title 常被清空
        action = H.plan_adventure(hero, rows, health, cfg, incoming)
        print(f"[{mode}][冒險] 決策: {action[0]}  體力={health}  冒險數={len(rows)}  來襲={incoming}")

        if action[0] == "skip":
            reason = action[1]
            if hero is not None and hero.status in (50, 9):      # 去程／返回中：正常等待，只印 log
                print(f"[冒險] 略過：{reason}")
                return
            _hero_log(mode, "未出發", reason)
            if hero is None or "未知狀態" in reason or "不一致" in reason:   # 資料結構不對勁：要人看
                notify(f"英雄冒險略過：{reason}")
            else:
                print(f"[冒險] 略過：{reason}")
            return

        _, number, idx = action
        row = rows[idx]
        adv = hero.adventures[idx]
        what = (f"冒險 #{number}（第 {idx + 1} 列，{adv.place}，{row.icon}，距離 {adv.distance}，"
                f"去程 {adv.duration // 60} 分，體力 {health}%）")
        if dry:
            print(f"[DRY][冒險] 會派出 {what}")
            _hero_log(mode, "未送出", f"{what}（乾跑，沒有真的點擊）")
            return

        time.sleep(random.uniform(2, 6))
        ok, msg = click_adventure(number, idx, row)
        if ok:
            _hero_log(mode, "成功", f"{what}；{msg}")
            notify(f"英雄已出發：{what}")
        else:
            _hero_log(mode, "沒看到變化", f"{what}；{msg}")
            notify(f"英雄冒險沒有確認成功（不重試）：{msg}")
    except Exception as e:                      # 英雄流程出錯不能影響建城
        print(f"[冒險] 英雄流程失敗（不影響建城）: {e!r}")
        notify(f"英雄流程出錯（不影響建城）: {e!r}")
