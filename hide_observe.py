"""躲兵 v0.4（v2.96）：dispatch.HOME 是 None（頁面與 Config 都沒給家）→ 直接略過，不躲兵；其餘不變。

躲兵 v0.2（檔名沿用 hide_observe）：hide_mode＝observe 時只讀、只記錄，絕不派兵；dry／live 見下。

v0.3（v2.83）：確認有來襲之後、算 plan 之前，讀 Sheet「Oases」分頁換掉 dispatch.OASES／BLOCKED（oasis_store.refresh，
  allow_scan=False：有來襲時只讀、不掃）；Sheet 沒資料、沒有 gspread 或任何失敗都只印 log、清單維持原狀（沒有內建清單）。其餘行為不變。

v0.2（v2.80）：
  - hide_mode 新增 dry（走到確認頁、不按確認）與 live（核對通過才按確認）；observe 行為不變（仍是 plan、動作「躲兵觀察」）。
    dry／live 的 Actions 列：動作＝「躲兵」、模式＝DRY／LIVE、結果＝派兵結果。
    **DRY_RUN 環境（測試分支）下 live 一律降成 dry**（travian_ci_run 傳 dry_run）。
  - 新增 min_attack（Config hide_min_attack，嚴格大於）與 wait_max_s（Config hide_wait_max_s，歸來窗口最多等幾秒；observe 不等）。
  - 沒有候選（例如村裡沒兵 → skip）時，固定探測 dispatch 清單裡離家最近的 1 個綠洲的 tile-details，
    回答「page.request.post 在 Actions 上通不通」（v2.77 ✗，之前因 skip 一直沒走到驗證）。
  - 說明欄多：攻擊力、歸來窗口、等待秒數、派兵結果。

以下是 v0.1 說明（observe 模式）：

做什麼（每輪建城前跑一次；沒有來襲就只印一行、不多打任何請求）：
  1. 用已經載入的 dorf1（d1）數來襲：viewData 的 incomingAttacksAmount，和 travian_pages 的 attacks 交叉對照；
  2. 有來襲才開集結點概況（唯讀 GET），原始 HTML 存進 debug_html/（workflow 已上傳，保留 3 天）；
  3. 用 hide_troops 的 plan 模式算「如果要躲，會怎麼派」，並唯讀驗證前 2 個候選綠洲（tile-details，
     順便實測 page.request.post 這條路在 Actions 上通不通）；
  4. 寫一列到 Actions：動作＝「躲兵觀察」、模式＝PLAN，說明欄含來襲提前量、在家兵力、決策、候選、各 in* 表的 class 與標題。
任何例外都只印 log，不往外拋，不影響後面的建城。
開關：Sheet「Config」分頁的 hide_mode（off／observe；預設 observe，空白或看不懂一律當 off），由 travian_ci_run 傳進 mode。
不會做的事：不開派遣頁、不填表、不按任何確認。
"""
import os
from datetime import datetime, timezone

import dispatch
import hide_troops as HT
import oasis_check


def _utc():
    return datetime.now(timezone.utc).strftime("%F %T")


def _default_log(row):
    from sheets_writer import log_action
    log_action(row)


def _probe_tile(page, base, pos):
    """唯讀：tile-details。回傳一小段文字（含 HTTP 狀態），失敗也只是文字。"""
    tag = f"{pos[0]}|{pos[1]}"
    try:
        r = page.request.post(f"{base}/api/v1/map/tile-details", data={"x": pos[0], "y": pos[1]})
        if not r.ok:
            return f"{tag}=HTTP{getattr(r, 'status', '?')}"
        info = oasis_check.parse_tile_details(r.json()["html"])
        return f"{tag}=" + ("安全" if oasis_check.is_safe(info, pos) else "不安全")
    except Exception as e:                                      # noqa: BLE001
        return f"{tag}=例外{type(e).__name__}"


_MODES = {"observe": "plan", "dry": "dry", "live": "live"}


def _fallback_positions(n=1):
    """沒有候選時的探測對象：dispatch 清單裡（排除 BLOCKED）離家最近的 n 個。"""
    rows = [(dispatch.distance(dispatch.HOME, p), p) for p, _b, _v in dispatch.OASES if p not in dispatch.BLOCKED]
    return [p for _d, p in sorted(rows)[:n]]


def observe(page, base, d1, *, mode="observe", log=None, save_dir="debug_html", probe=True, now_fn=_utc,
            min_attack=None, wait_max_s=0, dry_run=True):
    """回傳 run_once 的結果 dict；沒有來襲或失敗回 None。永遠不拋例外。
    mode：observe／dry／live，其餘（off、空白、看不懂）一律略過。dry_run＝True 時 live 降成 dry。"""
    m = str(mode or "").strip().lower()
    run_mode = _MODES.get(m)
    if run_mode is None:
        print(f"[躲兵] Config hide_mode={mode!r}，不是 observe／dry／live，略過")
        return None
    if run_mode == "live" and dry_run:
        print("[躲兵] DRY_RUN 環境：live 降成 dry，不會按確認")
        run_mode = "dry"
    if dispatch.HOME is None:
        print("[躲兵] 家座標未知（頁面讀不到、Config 也沒填 home_x／home_y），略過")
        return None
    try:
        n_sym = HT.incoming_symbols(d1)["amount"]
        n_pages = None
        try:
            import travian_pages as P
            n_pages = P.parse_dorf1(d1).attacks
        except Exception:                                       # noqa: BLE001
            pass
        if max(n_sym, n_pages or 0) == 0:
            print("[躲兵觀察] 沒有來襲")
            return None

        try:                                                    # 有來襲才讀 Sheet「Oases」換掉 dispatch.OASES/BLOCKED；不在這時候掃（省時間）
            import oasis_store
            oasis_store.refresh(page, base, allow_scan=False)
        except Exception as e:                                  # noqa: BLE001
            print(f"[躲兵] 綠洲清單載入失敗，清單維持原狀：{e!r}")

        saved = {}

        def on_overview(html):
            os.makedirs(save_dir, exist_ok=True)
            name = os.path.join(save_dir, f"overview_{now_fn().replace(' ', '_').replace(':', '')}.html")
            with open(name, "w", encoding="utf-8") as f:
                f.write(html)
            saved["path"] = name

        kw = {} if min_attack is None else {"min_attack": int(min_attack)}
        res = HT.run_once(page, base, run_mode, on_overview=on_overview,
                          wait_return_max_s=(int(wait_max_s) if run_mode != "plan" else 0), **kw)

        now_ts, arr = res.get("now_ts"), res.get("arrivals", [])
        leads = ",".join(f"{(a - now_ts) / 60:.1f}" for a in arr) if now_ts else ""
        top = res.get("top", [])
        probe_pos = [t["pos"] for t in top[:2]] or _fallback_positions(1)
        probes = [_probe_tile(page, base, p) for p in probe_pos] if probe else []
        desc = " ｜ ".join(x for x in [
            f"dorf1來襲={n_sym}/{n_pages}",
            f"概況敵方={len(arr)}波 提前(分)={leads}",
            f"在家={res.get('units')}",
            f"決策={res.get('action')}:{res.get('reason')}",
            f"攻擊力={HT.attack_total(res.get('units') or {})}（門檻>{min_attack}）" if min_attack is not None else "",
            "窗口=" + ";".join(res["windows"]) if res.get("windows") else "",
            f"等待={res['waited_s']}s" if res.get("waited_s") else "",
            f"窗口備註={res['window_note']}" if res.get("window_note") else "",
            f"結果={res['result']}" if res.get("result") else "",
            "首選=" + ";".join(f"({t['pos'][0]}|{t['pos'][1]}) 單程{t['one_way']} 回{t['back']}"
                              f"{'' if t['ok'] else ' 缺波'}" for t in top) if top else "",
            "即時驗證=" + ",".join(probes) if probes else "",
            "in表=" + ";".join(res.get("in_moves", [])),
            f"未知class={res.get('unknown_in')}" if res.get("unknown_in") else "",
            f"now={now_ts} arr={arr}",
            f"存檔={saved.get('path', '無')}",
            f"錯誤={res['error']}" if res.get("error") else "",
        ] if x)
        name = "躲兵觀察" if run_mode == "plan" else "躲兵"
        outcome = res.get("action", "error") if run_mode == "plan" else (res.get("result") or res.get("action", "error"))
        (log or _default_log)([now_fn(), name, "", "", "", "", run_mode.upper(), outcome, desc])
        print(f"[{name}] {res.get('action')}：{res.get('reason')}；提前(分)={leads}；{', '.join(probes)}"
              + (f"；{res['result']}" if res.get("result") else ""))
        return res
    except Exception as e:                                      # noqa: BLE001
        print(f"[躲兵] 失敗（不影響建城）: {e!r}")
        return None
