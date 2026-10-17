"""hero_focus.py v0.3.1（未部署、未實測）英雄資源產量加成切換。

v0.3.1 使用者確認：改完產量「立刻變」✓、3＝鐵 ✓。
v0.3  ✓ 依 10-16 回應內容改寫：
      - 讀目前加成：GET /api/v1/hero/v2/screen/attributes 的 hero.productionType（0 平均、1 木、2 磚、3 鐵、4 穀（皆 ✓））
        與 hero.productionTypes（長度 5：[平均時每種資源各加, 木, 磚, 鐵, 穀 單押時加]）。
        10-16 實測：[336, 1120, 1120, 1120, 1120]（28 點 × 12／40）。
      - 改加成：POST /api/v1/hero/v2/attributes，body {"resource":N}，回 JSON
        {"productionType":N,"tooltip":...}（成功時 productionType 等於送出的 N，程式會核對）。
      - 冒險中可以改 ✓（10-16 英雄 status=50 時改成功，使用者回報）。
      - 平均＝四種資源各 +336（共 1344）；單押＝一種 +1120（共 1120）。單押只有在「補某一種」時才划算。
      - 決策改用精確產量：切換後產量＝現產量 − 現加成 + 新加成，不再估算。
v0.2  加入 resource 0。v0.1 初版。
沒有 x-nonce、沒有 PUT 預備步驟。
產量改完立刻變 ✓，所以切換後可以馬上重讀產量當驗證。
"""
import json
from typing import Optional

ATTR_PATH = "/api/v1/hero/v2/attributes"
SCREEN_PATH = "/api/v1/hero/v2/screen/attributes"
RES_CODE = {"balanced": 0, "wood": 1, "clay": 2, "iron": 3, "crop": 4}
RES_NAME = {v: k for k, v in RES_CODE.items()}


def parse_screen(data: dict):
    """從屬性頁 API 的 JSON 取出 (目前加成類型, productionTypes)。格式不符就丟例外。"""
    hero = data["hero"]
    cur = int(hero["productionType"])
    types = [int(x) for x in hero["productionTypes"]]
    if len(types) != 5 or cur not in range(5):
        raise ValueError(f"屬性頁格式不符: productionType={cur} productionTypes={types}")
    return cur, types


def make_focus_io(context, base_url: str, referer: str = ""):
    base = base_url.rstrip("/")
    headers = {"content-type": "application/json; charset=UTF-8", "accept": "*/*", "origin": base}
    headers["referer"] = referer or base + "/hero/attributes"

    def read_focus():
        r = context.request.get(base + SCREEN_PATH, headers=headers)
        if not r.ok:
            raise RuntimeError(f"GET screen/attributes HTTP {r.status}")
        return parse_screen(r.json())

    def set_focus(resource: int) -> str:
        r = context.request.post(base + ATTR_PATH, data=json.dumps({"resource": resource}), headers=headers)
        try:
            body = r.text()[:300]
        except Exception as e:  # noqa: BLE001
            body = f"(讀不到回應內容: {e!r})"
        if not r.ok:
            raise RuntimeError(f"POST attributes HTTP {r.status} 回應={body!r}")
        try:
            got = r.json().get("productionType")
        except Exception:  # noqa: BLE001
            got = None
        if got != resource:
            raise RuntimeError(f"POST attributes {r.status} 但回應 productionType={got!r} ≠ 送出 {resource}")
        return f"POST {r.status} productionType={got}"

    return read_focus, set_focus


def make_dry_set_focus(log=print):
    def set_focus(resource: int) -> str:
        log(f"[DRY] 不送出 POST attributes resource={resource}（{RES_NAME.get(resource)}）")
        return "DRY"
    return set_focus


def hero_bonus(ptype: int, types: list, res: int) -> int:
    """加成類型 ptype 下，英雄對資源 res（1..4）貢獻的每小時產量。"""
    if ptype == 0:
        return types[0]
    return types[ptype] if res == ptype else 0


def decide_focus(
    gaps: dict,            # {1..4: 缺口}，只放 >0 的（缺木、磚、鐵、穀）
    production: dict,      # {1..4: 目前每小時產量}（穀請給扣除人口後的淨產量）
    current: Optional[int],
    types: Optional[list],
    hours_since_change: Optional[float] = None,
    min_hours: float = 3.0,
    switch_ratio: float = 1.3,
    crop_floor: float = 0.0,
    back_to_balanced: bool = True,
):
    """回傳 ("keep", None, 原因) 或 ("switch", 類型, 原因)。純函式。
    對每個候選類型 t（0..4）算「所有缺口補齊所需時間」＝ max(缺口 ÷ 切換後產量)；
    切換後穀淨產量 ≤ crop_floor 的候選不考慮（避免餓死）。取時間最短者。"""
    if current is None or not types:
        return ("keep", None, "讀不到目前加成，不動")
    if hours_since_change is not None and hours_since_change < min_hours:
        return ("keep", None, f"距上次切換 {hours_since_change:.1f}h < {min_hours}h")

    def after(t: int, r: int) -> float:
        return production[r] - hero_bonus(current, types, r) + hero_bonus(t, types, r)

    if not gaps:
        if back_to_balanced and current != 0 and after(0, 4) > crop_floor:
            return ("switch", 0, "沒有缺口，切回平均（四種資源合計產量較高）")
        return ("keep", None, "沒有缺口")

    def fill_hours(t: int) -> float:
        worst = 0.0
        for r, g in gaps.items():
            p = after(t, r)
            if p <= 0:
                return float("inf")
            worst = max(worst, g / p)
        return worst

    ok = [t for t in range(5) if after(t, 4) > crop_floor]
    if not ok:
        return ("keep", None, "所有選項穀淨產量都 ≤ 下限，不動")
    best = min(ok, key=fill_hours)
    hb, hc = fill_hours(best), fill_hours(current)
    if best == current:
        return ("keep", None, f"目前 {RES_NAME[current]} 已是最快（補齊約 {hc:.1f} 小時）")
    if hc <= hb * switch_ratio:
        return ("keep", None, f"{RES_NAME[best]} 只快一點（{hb:.1f}h vs 目前 {hc:.1f}h），不切")
    return ("switch", best, f"改 {RES_NAME[best]}：補齊缺口約 {hb:.1f} 小時，目前 {RES_NAME[current]} 要 {hc:.1f} 小時")
