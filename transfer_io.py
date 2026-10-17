"""transfer_io.py v1.2（POST 帶 x-nonce）

v1.2  ✓ 對照瀏覽器手動轉移（10-15）：每個 PUT 的「回應標頭」會給一個 x-nonce，
      同一個 itemId 的 POST 要把它放進「請求標頭」x-nonce（每筆不同；PUT 本身不帶）。
      舊版 POST 沒帶 → 固定 400。
      做法：PUT 成功後記下回應標頭的 x-nonce（以 itemId 為鍵）；POST 前取出（只用一次）。
      PUT 沒給 nonce → 當作失敗（execute_transfer 會停、不送 POST）；
      POST 前找不到該 itemId 的 nonce → 不送出、直接失敗（不裸送一定會 400 的請求）。
      △ 未實測：nonce 是否一次性／有效多久；POST 回應是否也給新 nonce（目前忽略）。
v1.1  失敗時帶出回應前 300 字；成功時回傳狀態碼。
v1.0  travian_transfer 需要的網路函式（send／read_state），用 Playwright 的 context.request。

用法（在 travian_ci_run.py 有 context 的地方）：
    from transfer_io import make_io
    send, read_state = make_io(context, base_url)   # base_url 例：https://ts11.x1.international.travian.com
    DRY 時不要呼叫 send（用 make_dry_send 只印 log）。
"""
import json

import travian_transfer as TR


def make_io(context, base_url: str, referer: str = ""):
    base = base_url.rstrip("/")
    headers = {"content-type": "application/json; charset=UTF-8", "accept": "*/*", "origin": base}
    if referer:
        headers["referer"] = referer
    nonces = {}                            # itemId -> 最近一次 PUT 回應給的 x-nonce

    def send(payload: dict, method: str = "POST") -> str:
        fn = {"PUT": context.request.put, "POST": context.request.post}[method]
        item_id = payload.get("itemId")
        hdrs = dict(headers)
        if method == "POST":
            nonce = nonces.pop(item_id, None)          # 只用一次
            if not nonce:
                raise RuntimeError(f"POST 前沒有 itemId={item_id} 的 x-nonce（PUT 沒給或沒送），不送出")
            hdrs["x-nonce"] = nonce
        r = fn(base + TR.USE_ITEM_PATH, data=json.dumps(payload), headers=hdrs)
        if not r.ok:                       # PUT 預期 204，POST 預期 200
            try:
                body = r.text()[:300]
            except Exception as e:  # noqa: BLE001
                body = f"(讀不到回應內容: {e!r})"
            raise RuntimeError(f"{method} use-item HTTP {r.status} 回應={body!r}")
        if method == "PUT":
            n = (r.headers or {}).get("x-nonce")       # Playwright 的 headers 鍵是小寫
            if not n:
                raise RuntimeError(f"PUT HTTP {r.status} 但回應標頭沒有 x-nonce，不送 POST")
            nonces[item_id] = n
            return f"PUT {r.status} nonce有"
        return f"POST {r.status}"

    def read_state() -> "TR.State":
        r = context.request.post(base + TR.GRAPHQL_PATH, data=json.dumps(TR.graphql_body()), headers=headers)
        if not r.ok:
            raise RuntimeError(f"graphql HTTP {r.status}")
        return TR.parse_state(r.json())

    return send, read_state


def make_dry_send(log=print):
    def send(payload: dict, method: str = "POST") -> None:
        log(f"[DRY] 不送出 {method} use-item {payload}")
    return send
