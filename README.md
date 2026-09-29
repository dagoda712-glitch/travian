# Travian 自動升級（GitHub Actions + Google Sheets）

定時登入自己的 Travian 村莊，依規則決定下一步要升級什麼、送出升級，
並把每次動作記在 Google Sheet。也附一支把人口／資源／英雄資料寫進 Sheet 的記錄腳本。

## 目前的實際狀態（先看這裡）

| 功能 | 狀態 |
|---|---|
| 自動升級（`.github/workflows/auto_build.yml` → `travian_ci_run.py`） | **正在排程執行**，每 30 分鐘一次 |
| 村莊資料記錄（`Sheet1`） | **每輪自動記錄**（`runner.py` 的 `log_village_snapshot()`）。新增「施工佇列」「Plus剩餘(小時)」「新手保護剩餘(小時)」三欄；舊表頭會自動補欄更新，舊資料不動。`Config` 的 `enabled=FALSE` 或安靜時段時整輪不執行，也不會記錄 |
| 來襲通知 | **沒有主動通知**，只寫在 Actions 的執行紀錄（見第七節） |

---

## ⚠️ 使用前必讀

- 這個工具用你已登入的 Cookie 去操作遊戲，**而且會自動點擊升級**，不只是讀取資料。
  多數 Travian 官方伺服器的規則不允許機器人或自動化操作，被偵測到可能導致帳號被停權。
  請先確認你所在伺服器的規則，前期建議用小號測試。
- **先用乾跑模式（`DRY_RUN=1`）跑幾輪**：乾跑只會印出「會送出什麼」，不會真的點。
  workflow 檔裡有設定 `DRY_RUN`（`"1"` = 乾跑、`"0"` = 正式送出），**上線前請確認它現在是哪個值**。
  沒有設定這個環境變數時，程式預設是乾跑。
- 這個工具**不會花金幣**：升級網址必須完全符合白名單格式，
  帶有金幣（`buildmaster`）或看廣告加速（`as=`）等額外參數的網址一律不點。
- 緊急停止：把 Sheet 的 `Config` 分頁裡 `enabled` 改成 `FALSE`，下一輪就不會動作，不用改程式或 Secret。

---

## 一、準備 Google Sheets

1. 新建一個 Google Sheet，記下網址中的表格 ID：
   ```
   https://docs.google.com/spreadsheets/d/【這一串就是 ID】/edit
   ```
2. 到 [Google Cloud Console](https://console.cloud.google.com/) 建一個專案（或用現有的）。
3. 左側選單「APIs & Services」→「Library」，搜尋 **Google Sheets API** 並啟用。
4. 「APIs & Services」→「Credentials」→「Create Credentials」→「Service account」，名字隨意。
5. 建好後點進這個服務帳號，「Keys」→「Add Key」→「Create new key」→ 選 **JSON** 下載。
   JSON 裡有個 `client_email` 欄位，形如 `xxx@your-project.iam.gserviceaccount.com`。
6. 回到第 1 步的 Sheet，右上角「共用」，把 `client_email` 加為**編輯者**（沒做這步會寫入失敗）。

---

## 二、準備 Travian 的登入 Cookie

1. 用瀏覽器登入你的 Travian 帳號。
2. 按 F12 開啟開發者工具 → Network（網路）分頁。
3. 重新整理頁面，點任一個請求（例如 `dorf1.php`）。
4. 在 Headers → Request Headers 找到 `Cookie:` 那一整行，全部複製。
5. 記下伺服器網址，例如 `https://ts11.x1.international.travian.com`。

Cookie 有有效期（幾天到幾週，依伺服器而定）。過期後腳本會抓到登入頁而失敗，
需要重新登入、複製新的 Cookie、更新 Secret。

兩個 Cookie 的用途：

- `TRAVIAN_SEED_COOKIE`：Playwright 版（目前的排程）用。它只是「救援種子」：
  平常靠 Actions 快取裡的 `travian_state.json` 延續 session，快取沒命中或 session 失效時才會用它重建。
- `TRAVIAN_COOKIE`：純 HTTP 版（`auto_build.py`）和 `scrape_and_log.py` 用。內容跟上面是同一串。

---

## 三、在 GitHub 倉庫裡設定

1. 新建一個 **Private（私有）** 倉庫。不要用公開倉庫：`travian_state.json` 含有登入 session，
   而且任何能推送 workflow 的人都讀得到快取內容。
2. 把專案檔案上傳：`travian_agent.py`、`runner.py`、`travian_ci_run.py`、`auto_build.py`、
   `sheets_writer.py`、`travian_parser.py`、`scrape_and_log.py`、`requirements.txt`、
   `.github/workflows/auto_build.yml`。
   **`runner.py` 一定要一起上傳**，否則兩支入口腳本都會 `ImportError`。
3. Settings → Secrets and variables → Actions → New repository secret，新增：

   | Secret 名稱 | 內容 |
   |---|---|
   | `TRAVIAN_BASE_URL` | 伺服器網址，如 `https://ts11.x1.international.travian.com` |
   | `TRAVIAN_SEED_COOKIE` | 上面複製的整串 Cookie（Playwright 版用） |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | 服務帳號 JSON 檔的**全部內容** |
   | `GOOGLE_SHEET_ID` | 表格 ID |

   （只用純 HTTP 版或 `scrape_and_log.py` 的話，另外加 `TRAVIAN_COOKIE`。）
4. Actions 分頁會看到「Travian 記錄與自動升級」這個 workflow，可用右側「Run workflow」手動先跑一次。
5. 每一輪都會把最新的 session 存回快取，下一輪接續使用。
   workflow 最後還會把 `debug_html/` 上傳成 artifact（沒有這個資料夾就略過），保留 3 天。

---

## 四、Sheet 的 `Config` 分頁（選填，沒有就用預設值）

A 欄是名稱、B 欄是值：

| A | B | 說明 |
|---|---|---|
| enabled | TRUE | 改成 `FALSE` 就是緊急停止 |
| target_level | 2 | 資源田升到這個等級（沒設 `build_order` 時使用） |
| queue_slots | 2 | 施工佇列長度，**沒有 Plus 請改成 1**（見下） |
| max_builds | 2 | 每輪最多送出幾個升級 |
| target_building_level | 0 | 倉庫／糧倉升到幾級，0 = 停用（沒設 `build_order` 時使用） |
| build_order | （空） | 建城順序，見第五節。有填就用它，上面兩個目標等級就不再使用 |

重點：

- **讀不到 Config 時**（憑證或網路錯誤）本輪不動作；**只是沒有 `Config` 分頁**則使用預設值。
  注意預設的 `queue_slots` 是 2，如果你沒有 Plus，請務必建立 Config 並設成 1。
- **`queue_slots` 的意思是「施工佇列最多幾項」，不是同時施工幾項。**
  官方說明只把「資源田和建築同時各蓋一項」列為羅馬族的種族加成；
  其他種族預設只有一條施工線，Travian Plus 的「等待佇列」是多排一項，等前一項完成後才接著做，不是並行。
  Plus 對你的種族實際能多排幾項，請以遊戲內為準（Plus 有效時，先開一項施工，再試著排第二項看看）。
  多排一項時資源是否會提前預扣，我沒有查到官方說明，也請留意。
- 每個升級成功或失敗都會寫進 `Actions` 分頁：
  時間(UTC)、動作、slot、gid、目前等級、成本、模式（DRY / LIVE）、結果、**升級項目 / 說明**。
  **每一輪都會寫一列**，不只是升級的時候：
  - 有升級：說明是「升級 伐木場(slot 1) L1→L2」。
  - 沒升級：「動作」欄會是 等待資源／等待佇列／倉容不足／計畫卡住／無空地／已完成，
    說明寫清楚在等什麼、缺什麼，例如「下一個要升級 伐木場(slot 1) L1→L2，資源不夠：木材缺50，預計約 50分 後足夠」、
    「施工佇列已滿（2/2），等有空位再升級」。
  - 每列結尾都附「施工中：農場 等級 1 剩0:01:46、…」，沒有施工時寫「目前沒有施工中的項目」。
  - 舊的 `Actions` 分頁（8 欄）會在第一次寫入時自動補上第 9 欄與表頭。

---

## 五、建城順序（`build_order`）

在 `Config` 加一列 `build_order`，程式就照這個順序建城：
每次挑「順序裡第一個還沒達成的目標」來做；它還在等資源或施工時，後面的目標就不動（嚴格照順序）。
**沒有這一列（或留空）就是原本的流程**（資源田 `target_level` + 倉庫／糧倉 `target_building_level`）。

| A | B |
|---|---|
| build_order | res:2,10:3,11:3,15:5,17:1 |

格式：用逗號、空白或換行分隔，每項是 `目標:等級`。

- `res:N`：所有資源田都升到 N 級。
- `1:N`～`4:N`：只有木／磚／鐵／糧其中一種的所有田升到 N 級。
- `gid:N`：該建築升到 N 級；村莊裡還沒有就自動找空地蓋一棟。

已在頁面上看過的 gid：15 村莊大樓、10 倉庫、11 糧倉、16 集結點、17 市場、18 大使館、
20 馬廄、22 研究院、23 山洞、24 城鎮廳、25 行宮、26 皇宮、27 寶物庫、28 交易所、
34 石匠鋪、37 英雄宅、45 供水系統。

**缺前置條件時會自動先補**：例如寫了 `25:1`（行宮），但村莊大樓只有 3 級（要 5 級），
程式會先升村莊大樓。被劃掉的條件（例如蓋行宮前不能有皇宮）補不了，
會在 log 印出「建城計畫卡住了」。

**倉庫／糧倉容量不夠時**（只在有設 `build_order` 時）：
目標的成本超過容量，會自動先處理儲存建築，而不是通知後就卡住。
木／磚／鐵超過 → 先升倉庫（gid 10）；只有穀物超過 → 先升糧倉（gid 11），每次升一級，
下一輪容量變大再重新評估。村莊裡還沒有就先蓋一棟；已經 20 級，或補倉庫本身的成本就超過容量時，
會標成「計畫卡住了」。沒設 `build_order` 時維持舊行為（只寫 log）。

注意事項：

1. 格式寫錯（例如 `abc`）本輪整個不動作並寫進 log，不會亂猜。
   如果 `build_order` 儲存格只寫單一目標（例如只有 `10:3`），Google Sheets 可能把它當成時間；
   請把儲存格格式設成「純文字」，或前面加一個單引號 `'`。
2. 新建築蓋在編號最小的空地（19～38；39 是集結點、40 是城牆，不會動）。**蓋下去不能退**，
   請先用乾跑看 log 裡選的位置和建築對不對，再切成正式。
3. 「建築是否施工中」是看 dorf2 的 `underConstruction` class，這點還沒有用真實的施工中頁面驗證。
   `queue_slots=1` 時，只要有任何施工佇列就不會動作，所以不受影響；
   `queue_slots>=2` 時，最壞情況是多排一次同一棟的下一級，或 log 出現「找不到按鈕」。
4. 嚴格照順序的代價：前面的目標在等資源時，後面就算買得起也不會先做。

---

## 六、自動點擊的安全機制

- 只認綠色的「興建／擴建」按鈕。
- 升級網址只接受 `/dorf1.php` 或 `/dorf2.php?id=…&gid=…&action=build&checksum=…` 這種格式，
  多任何參數（金幣的 `buildmaster`、看廣告的 `as=`）都不會點。
- 網址如果不符合格式（而且不是上面兩種已知的金幣／廣告按鈕），log 會出現一行 `[警告]`，
  網址中的 checksum 只顯示長度。如果所有升級都變成「找不到按鈕」，先往上找這行警告：
  可能是遊戲改版、多加了參數，需要人工確認後再放寬白名單。
- 送出後會重抓 dorf1 檢查：佇列有增加，或資源明顯被扣，才算成功。沒看到變化就停止並回報失敗。
- 讀 Config 失敗、Cookie 失效、`build_order` 格式錯誤時，一律本輪不動作。
- 安靜時段（見第十節）不會動作。

---

## 七、通知現況

目前**沒有任何主動通知**。所有提醒都只會印在 GitHub Actions 的執行紀錄裡：

- 偵測到來襲：只印一行「偵測到來襲 N 波」，**來襲寄信尚未實作**。
- 建城計畫卡住、沒空地可蓋、成本超過容量（舊流程）、送出後沒看到變化。
- 夜間安靜時段 workflow 不會執行，那段時間不會偵測來襲。
- session／Cookie 失效時整個 run 會失敗，GitHub 會依你帳號的通知設定寄「workflow 失敗」的信，
  這是目前唯一會主動通知到你的情況。

之後想做來襲寄信的方案（尚未實作）：用 SMTP 寄信（標準函式庫 `smtplib`，不需要新套件），
需要 `SMTP_HOST`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO` 四個 Secret（Gmail 的 `SMTP_PASS` 要用「應用程式密碼」）；
波數增加才寄、波數沒變時隔 12 小時再提醒、來襲解除就清掉紀錄；
去重狀態存成 `notify_state.json`，跟 `travian_state.json` 一起放進 Actions 快取。
來襲的「抵達時間」目前沒辦法解析：需要遇到來襲時存一份 dorf1（或集結點）頁面當樣本才能寫。

---

## 八、檔案分工

| 檔案 | 負責 |
|---|---|
| `travian_agent.py` | 純解析 + 決策（看懂頁面、決定升哪個、建城順序、升級網址白名單），不含網路和 Sheet |
| `runner.py` | 共用主流程：Config／Actions 分頁、決策、送出升級、驗證、記錄 |
| `travian_ci_run.py` | Playwright 版入口（目前排程使用）：session（快取／種子 Cookie）、抓頁面、送出網址 |
| `auto_build.py` | 純 HTTP 版入口：用 requests 抓頁面、送出網址 |
| `travian_parser.py`、`scrape_and_log.py`、`sheets_writer.py` | 村莊資料解析並寫入 `Sheet1`（目前不在排程裡） |
| `debug_fields.py`、`debug_village_buildings.py` | 唯讀診斷，見第九節 |

---

## 九、本地測試與診斷

```bash
pip install -r requirements.txt
```

**升級流程（純 HTTP 版，乾跑）**

```bash
export TRAVIAN_BASE_URL="https://xxx.travian.com"
export TRAVIAN_COOKIE="貼上你的 Cookie"
export DRY_RUN=1
export QUIET_UTC="0,0"      # 本地測試時關掉安靜時段（起迄相同 = 永不安靜）
python auto_build.py
```

沒設 `GOOGLE_SERVICE_ACCOUNT_JSON`／`GOOGLE_SHEET_ID` 時，不會讀 Config 也不會寫 Actions 分頁，只用預設值。

**村莊資料記錄（`Sheet1`）**

```bash
# 用手動存下的 HTML 測試解析，不涉及網路和憑證
python scrape_and_log.py --html sample_dorf2.html --dry-run

# 真實抓取但只印不寫
python scrape_and_log.py --dry-run

# 連 Sheets 一起測試
export GOOGLE_SERVICE_ACCOUNT_JSON='{"type": "service_account", ...}'
export GOOGLE_SHEET_ID="你的表格ID"
python scrape_and_log.py
```

**診斷腳本**（唯讀，不會點任何東西）：把 workflow 裡的 `run: python travian_ci_run.py`
暫時換成下面其中一個，跑完記得換回去。

- `debug_fields.py`：印出 dorf1 每塊資源田的解析結果，特別針對成本解析成空的田印出原始 `title`。
- `debug_village_buildings.py`：印出村莊中心建築位，並測試倉庫／糧倉的升級成本解析與按鈕網址，
  原始 HTML 存進 `debug_html/`，會被 workflow 上傳成 artifact。

---

## 十、排程與安靜時段

`.github/workflows/auto_build.yml` 的 cron（UTC）目前是：

```yaml
- cron: "*/30 0-16,23 * * *"
```

每 30 分鐘一次，只在 UTC 0～16 點、23 點觸發，跳過 UTC 17～22 點
（= 台灣時間 01:00～07:00 左右）。程式本身也有安靜時段檢查（環境變數 `QUIET_UTC`，預設 `17,23`），
兩邊要一致。

GitHub Actions 的排程在負載高時可能延遲十幾分鐘，屬於正常現象。
你的 log 顯示實際執行時間常比 :00／:30 晚 12～19 分鐘，這也是施工完成到下次動手之間會有空檔的主因之一。

---

## 十一、驗證狀態與已知限制

| 項目 | 狀態 |
|---|---|
| 資源田升級 | 已在正式模式實際升級成功過 |
| 倉庫升級 | 已在正式模式實際升級成功過 |
| 糧倉、其他村莊建築 | 解析邏輯已驗證，但還沒有實際升級過 |
| 蓋新建築、自動補前置條件、倉容不足時補儲存建築 | 只用模擬頁面測過，還沒有真實頁面實測 |
| 建築「施工中」的判斷（`underConstruction`） | 未用真實頁面驗證 |
| 來襲偵測 | 有實際偵測到過；抵達時間、通知都沒做 |

其他限制：

- 只處理目前選中的單一村莊。
- 每個資源田要升級時，決策時是「最便宜、最快」先升（沒設 `build_order` 時），
  沒有另外排優先序；工期長的便宜項目可能佔住唯一的佇列。
- 不會使用金幣功能（Master Builder、立即完成、看廣告加速）。

---

## 十二、後續可以做

- 來襲寄信通知（見第七節）。
- 縮短施工完成到下次動手之間的空檔：cron 避開整點、佇列快完成時在同一輪裡等到完成再送出。
- 改用純 HTTP 版當主排程：目前 Playwright 版每次都要走「title 被前端 JS 清空 → 改用純 HTTP 重抓」的備援，
  瀏覽器只剩下保存 session 的用途。
