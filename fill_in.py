"""fill_in.py v0.1（新、未部署、未實測）build_order 嚴格照順序時，主目標等資源的空檔「插隊」做後面付得起的項目。

標準：插隊之後，主目標「可以開工」的時間不能比不插隊更晚（可用 slack_h 放寬一點）。
  主目標開工時間＝max(湊齊成本的時間, 施工佇列出現空位的時間)。
  插隊項目 F 會：① 扣掉 F 的成本（主目標可能要多等某種資源）；② 佔一格佇列直到 F 完工（佇列只有 1 格時最明顯）。
來襲放寬：有來襲、且來襲會在主目標湊齊之前抵達（或不知道 ETA）時，超過山洞隱藏量（hidden）的存量反正會被搶，
  所以主目標的存量只算 min(存量, hidden)；拿超出的部分付 F 沒有損失。
  △ 假設超出的部分會「全部」被搶（實際有載重上限，被搶量可能較少）→ 放寬會偏樂觀。
純函式、不連網路、不 import 專案其他模組。stock／prod／cost 順序都是 [木, 磚, 鐵, 穀]；prod 的穀請用 l4（和 State.prod 一致）。
"""
INF = float("inf")


def start_hours(stock, prod, cost, hidden=None) -> float:
    """湊齊 cost 需要幾小時（0＝現在就夠）；某種缺的資源產量 ≤ 0 → inf。
    hidden 不是 None：存量只算 min(存量, hidden)（來襲後倖存的部分）。"""
    worst = 0.0
    for s, p, c in zip(stock, prod, cost):
        if hidden is not None:
            s = min(s, hidden)
        short = c - s
        if short <= 0:
            continue
        if p <= 0:
            return INF
        worst = max(worst, short / p)
    return worst


def queue_free_hours(queue_secs, slots: int, f_secs: float) -> float:
    """放進 F 之後，施工佇列要幾小時才有空位（0＝還有空位）。queue_secs＝目前各項剩餘秒數。"""
    items = list(queue_secs) + [f_secs]
    if len(items) < slots:
        return 0.0
    return min(items) / 3600.0


def should_fill(stock, prod, main_cost, f_cost, f_secs, queue_secs, slots: int,
                hidden=None, attacks: int = 0, attack_eta_s=None, slack_h: float = 0.0):
    """回傳 (可以插隊?, 說明 dict)。說明裡有 t0／t1／tq（小時）、relaxed（是否套用來襲放寬）、why。"""
    if any(c > s for c, s in zip(f_cost, stock)):
        return False, {"why": "插隊項目現在付不起"}
    t_plain = start_hours(stock, prod, main_cost)
    relaxed = bool(attacks) and hidden is not None and (attack_eta_s is None or attack_eta_s / 3600.0 <= t_plain)
    h = hidden if relaxed else None
    t0 = start_hours(stock, prod, main_cost, h)
    if t0 == INF:
        return False, {"why": "主目標有資源產量 ≤ 0，算不出開工時間", "relaxed": relaxed}
    after = [s - c for s, c in zip(stock, f_cost)]
    t_res = start_hours(after, prod, main_cost, h)
    tq = queue_free_hours(queue_secs, slots, f_secs)
    t1 = max(t_res, tq)
    ok = t1 <= t0 + slack_h + 1e-9
    why = (f"主目標開工 {t0:.2f}h → {t1:.2f}h（湊資源 {t_res:.2f}h、佇列 {tq:.2f}h）"
           + ("，來襲放寬（超出隱藏量的存量視為已被搶）" if relaxed else "")
           + ("" if ok else "：會拖慢主目標，不插隊"))
    return ok, {"t0": t0, "t1": t1, "tq": tq, "relaxed": relaxed, "why": why}
