# Travian 自動升級（GitHub Actions + Google Sheets）

定時登入自己的 Travian 村莊，依規則決定下一步要升級什麼、送出升級，
並把每次動作記在 Google Sheet。也附一支把人口／資源／英雄資料寫進 Sheet 的記錄腳本。

## 目前的實際狀態（先看這裡）

| 功能 | 狀態 |
|---|---|
| 自動升級（`.github/workflows/auto_build.yml` → `travian_ci_run.py`） | **正在排程執行**。UTC 0～16、23 點每 30 分鐘一次，正常建造；UTC 17～22 點（台灣 01:00～06:59）每小時一次，**只做 keep-alive（登入保活），不建造**，見第十節 |
| 村莊資料記錄（`Sheet1`） | **每輪自動記錄**（`runner.py` 的 `log_village_snapshot()`）。含「施工佇列」「Plus剩餘(小時)」「新手保護剩餘(小時)」三欄；舊表頭會自動補欄更新，舊資料不動。`Config` 的 `enabled=FALSE` 時整輪不執行；安靜時段只做 keep-alive，不記錄 |
| 村莊結構記錄（`Fields`／`Buildings`／`Buildable`／`Changes`） | **每輪自動同步**（`village_log.py`）。資源田、村莊建築位（含空地）、空地可蓋清單各一個分頁，有變動才更新並在 `Changes` 追加一筆；沒變動就完全不寫入。詳見第十二節 |
| 佇列長度、文化點 | 每輪從頁面內嵌資料讀種族／Plus，自動決定佇列長度；文化點缺口印在 Actions 執行紀錄（不寫 Sheet），見第四節 |
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
- **session 失效時也會自動變成停止狀態**：程式會把 `enabled` 改成 `FALSE`（見第七節）。
  修好 Cookie 之後，要**手動把 `enabled` 改回 `TRUE`**，排程才會恢復。

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

Cookie／session 會因為兩種原因失效，過期後腳本會抓到登入頁而失敗，
需要重新登入、複製新的 Cookie、更新 Secret：

- **有效期**：幾天到幾週，依伺服器而定。
- **閒置逾時**：session 太久沒有任何請求也會被伺服器端讓它過期。
  實測過：排程跳過 UTC 17～23 點、連續約 6～7 小時完全沒有請求，隔天 Actions 快取裡的 session
  和種子 Cookie 都失效了。所以**安靜時段不能整段停跑**，要維持每小時一次的 keep-alive（第十節）。
  實際的閒置上限沒有測過，只知道 6～7 小時會失效。

兩個 Cookie 的用途：

- `TRAVIAN_SEED_COOKIE`：Playwright 版（目前的排程）用。它只是「救援種子」：
  平常靠 Actions 快取裡的 `travian_state.json` 延續 session，快取沒命中或 session 失效時才會用它重建。
  伺服器通常會在後續請求換發新的 session 值，所以種子 Cookie 放久了自己也會失效，
  真正讓 session 活下去的是每一輪都存回快取的最新狀態。
- `TRAVIAN_COOKIE`：純 HTTP 版（`auto_build.py`）和 `scrape_and_log.py` 用。內容跟上面是同一串。

---

## 三、在 GitHub 倉庫裡設定

1. 新建一個 **Private（私有）** 倉庫。不要用公開倉庫：`travian_state.json` 含有登入 session，
   而且任何能推送 workflow 的人都讀得到快取內容。
2. 把專案檔案上傳：`travian_agent.py`、`runner.py`、`travian_ci_run.py`、`auto_build.py`、
   `sheets_writer.py`、`travian_parser.py`、`scrape_and_log.py`、`requirements.txt`、
   `.github/workflows/auto_build.yml`。
   **`runner.py` 一定要一起上傳**，否則兩支入口腳本都會 `ImportError`。
   **`runner.py` 和 `travian_agent.py` 必須是同一組版本一起更新**，只換其中一個會因為缺函式而
   `AttributeError`（例如 `runner.py` 用到 `travian_agent.py` 的 `parse_player_info`、`gid_name`）。
3. Settings → Secrets and variables → Actions → New repository secret，新增：

   | Secret 名稱 | 內容 |
   |---|---|
   | `TRAVIAN_BASE_URL` | 伺服器網址，如 `https://ts11.x1.international.travian.com` |
   | `TRAVIAN_SEED_COOKIE` | 上面複製的整串 Cookie（Playwright 版用） |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | 服務帳號 JSON 檔的**全部內容** |
   | `GOOGLE_SHEET_ID` | 表格 ID |

   （只用純 HTTP 版或 `scrape_and_log.py` 的話，另外加 `TRAVIAN_COOKIE`。）
4. Actions 分頁會看到「Travian 記錄與自動升級」這個 workflow，可用右側「Run workflow」手動先跑一次。
   注意：如果手動觸發的時間剛好落在安靜時段（UTC 17～22），這一輪只會做 keep-alive、不會建造。
5. 每一輪都會把最新的 session 存回快取，下一輪接續使用（安靜時段的 keep-alive 也會存）。
   workflow 最後還會把 `debug_html/` 上傳成 artifact（沒有這個資料夾就略過），保留 3 天。

---

## 四、Sheet 的 `Config` 分頁（選填，程式會自動建立、補齊）

A 欄是名稱、B 欄是值；C 欄是說明，程式不讀（多的欄位不影響）：

| A | B | 說明 |
|---|---|---|
| enabled | TRUE | 改成 `FALSE` 就是緊急停止；session 失效時程式也會自動改成 `FALSE` |
| target_level | 2 | 資源田升到這個等級（沒設 `build_order` 時使用）。非首都超過 10 會自動當成 10 |
| queue_slots | （留空） | 施工佇列長度。**留空 = 自動偵測**（羅馬族 2、其他種族 1，Plus 有效再 +1）；填數字 = 手動指定，優先於自動偵測 |
| is_capital | FALSE | 這個村莊是首都才填 `TRUE`。非首都資源田上限是 10 級，沒設 `TRUE` 會停在 10 級 |
| max_builds | 2 | 每輪最多送出幾個升級 |
| target_building_level | 0 | 倉庫／糧倉升到幾級，0 = 停用（沒設 `build_order` 時使用） |
| build_order | （空） | 建城順序，見第五節。有填就用它，上面兩個目標等級就不再使用 |

重點：

- **自動建立與補齊**：每輪讀 Config 時，分頁不存在就建立（帶上面 7 列預設值與 C 欄說明）；
  分頁已存在但缺哪一列，就只補哪一列。
  - **只補缺的，不覆蓋**：已經有的列（不論你填什麼值，包括故意留空）完全不動。
  - **不多花 API 呼叫**：用的是本來就讀進來的資料，只有真的缺列時才多一次寫入，平常沒有額外請求。
  - **純文字寫入**：`build_order` 這類值不會被 Google Sheets 誤當成時間。
  - **失敗只印 log**：例如權限問題，log 會出現「補齊 Config 預設列失敗（不影響本輪）」，不影響升級。
  - 兩個入口都是每輪讀一次 Config，補齊只發生在那一次。安靜時段的差別：Playwright 版仍會讀 Config（所以缺列照樣補），
    純 HTTP 版（`auto_build.py`）在讀 Config 之前就結束，那段時間不讀也不補。
  - 補上去的值就是當下的程式預設值，所以行為不變，只是把隱含的設定攤開讓你看到。
    **之後程式改了預設值，已經補上去的舊列不會跟著變**，要自己改。
  - 想刪掉某一列不會被接受：下一輪會再把它補回預設值（要停用功能請改值，例如 `target_building_level` 填 0）。
- **讀不到 Config 時**（憑證或網路錯誤）本輪不動作；**沒有 `Config` 分頁**則使用預設值
  （`queue_slots` 自動偵測、`is_capital` 為 FALSE），並順手建立分頁。
- **`queue_slots` 的意思是「施工佇列最多幾項」，不是同時施工幾項。**
  官方說明只把「資源田和建築同時各蓋一項」列為羅馬族的種族加成；
  其他種族預設只有一條施工線，Travian Plus 的「等待佇列」是多排一項，等前一項完成後才接著做，不是並行。
  多排一項時資源是否會提前預扣，我沒有查到官方說明，也請留意。
- **自動偵測是推估，請確認一次。** 程式從頁面內嵌的玩家資料（dorf1，沒有就讀 dorf2）讀種族和 Plus 狀態，
  再套上面的規則。每輪 log 開頭會印一行，例如：
  `佇列長度 = 3（自動偵測：羅馬、Plus 有效）`。
  跟你在遊戲內實際能排的數量不一樣時，直接在 `queue_slots` 填數字蓋過它。
  讀不到種族／Plus 時保守用 1，並在 log 印出原因。
- **文化點缺口**會印在 log，例如：`[文化點] 3200/5000，缺 1800，照目前每日 +240 約 8 天（不含慶典）；村莊名額 1/2`。
  只是資訊，不會影響升級流程，也沒有寫進 Sheet。
- 每個升級成功或失敗都會寫進 `Actions` 分頁：
  時間(UTC)、動作、slot、gid、目前等級、成本、模式（DRY / LIVE）、結果、**升級項目 / 說明**。
  **每一輪（建造時段）都會寫一列**，不只是升級的時候：
  - 有升級：說明是「升級 伐木場(slot 1) L1→L2」。
  - 沒升級：「動作」欄會是 等待資源／等待佇列／倉容不足／計畫卡住／無空地／已完成，
    說明寫清楚在等什麼、缺什麼，例如「下一個要升級 伐木場(slot 1) L1→L2，資源不夠：木材缺50，預計約 50分 後足夠」、
    「施工佇列已滿（2/2），等有空位再升級」。
  - 每列結尾都附「施工中：農場 等級 1 剩0:01:46、…」，沒有施工時寫「目前沒有施工中的項目」。
  - 舊的 `Actions` 分頁（8 欄）會在第一次寫入時自動補上第 9 欄與表頭。
  - 安靜時段的 keep-alive 不會寫 `Actions`。

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

**資源田等級上限**：非首都村莊的資源田最多 10 級（官方規則）。`res:N`、`1:N`～`4:N` 的 N 超過 10 時，
程式會當成 10，不會去嘗試不可能的升級；首都要在 `Config` 設 `is_capital = TRUE` 才會解除。
村莊中心建築（`gid:N`）不受這個上限影響。

**缺前置條件時會自動先補**：例如寫了 `25:1`（行宮），但村莊大樓只有 3 級（要 5 級），
程式會先升村莊大樓。被劃掉的條件（例如蓋行宮前不能有皇宮）補不了，
會在 log 印出「建城計畫卡住了」。

**倉庫／糧倉容量不夠時**（只在有設 `build_order` 時）：
目標的成本超過容量，會自動先處理儲存建築，而不是通知後就卡住。
木／磚／鐵超過 → 先升倉庫（gid 10）；只有穀物超過 → 先升糧倉（gid 11），每次升一級，
下一輪容量變大再重新評估。村莊裡還沒有就先蓋一棟；已經 20 級，或補倉庫本身的成本就超過容量時，
會標成「計畫卡住了」。沒設 `build_order` 時維持舊行為（只寫 log）。

**資源田怎麼排先後（ROI）**：同時有好幾塊資源田可以升（`res:N` 或沒設 `build_order` 的預設流程）時，
程式挑「花費 ÷ 每小時新增產量」最小的那一塊，也就是最划算的先升；
糧食淨產出太低（< 5）時，改成只從農田裡挑。

- 產量用內建的等級表（標準速度、無綠洲／工廠／英雄加成，等級 1～10 每小時總產量：
  5、9、15、22、33、50、70、100、145、200）。這是社群長年沿用的數字，不是官方逐級公布的，
  而且各塊田通常是等比例受加成，所以只影響「先升哪一塊」，不影響能不能升。精確數字請以遊戲內為準。
- 沒有表的等級（例如首都 11 級以上）和倉庫／糧倉，改用舊規則「總花費最低、工期最短」，
  並排在有 ROI 資料的田後面。
- 補前置條件時（例如為了蓋行宮先升資源田），挑的是「最接近達標」的那一塊，不用 ROI。

注意事項：

1. 格式寫錯（例如 `abc`）本輪整個不動作並寫進 log，不會亂猜。
   如果 `build_order` 儲存格只寫單一目標（例如只有 `10:3`），Google Sheets 可能把它當成時間；
   請把儲存格格式設成「純文字」，或前面加一個單引號 `'`。
2. 新建築蓋在編號最小的空地（19～38；39 是集結點、40 是城牆，不會動）。**蓋下去不能退**，
   請先用乾跑看 log 裡選的位置和建築對不對，再切成正式。
3. 「建築是否施工中」是看 dorf2 的 `underConstruction` class，這點還沒有用真實的施工中頁面驗證。
   佇列長度是 1 時，只要有任何施工佇列就不會動作，所以不受影響；
   佇列長度 ≥ 2 時，最壞情況是多排一次同一棟的下一級，或 log 出現「找不到按鈕」。
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
- 讀 Config 失敗、`build_order` 格式錯誤時，一律本輪不動作。
  Cookie／session 失效時同樣不動作，並自動把 `enabled` 設成 `FALSE`（見第七節）。
- 安靜時段（見第十節）只做 keep-alive，不會建造。

---

## 七、通知現況

目前**沒有任何主動通知**。所有提醒都只會印在 GitHub Actions 的執行紀錄裡：

- 偵測到來襲：印一行「偵測到來襲 N 波，最快 X 後抵達」，**來襲寄信尚未實作**。
- 建城計畫卡住、沒空地可蓋、成本超過容量、送出後沒看到變化。
- 安靜時段只做 keep-alive，那段時間不會偵測來襲。
- **session／Cookie 失效**：這一輪會失敗（run 顯示失敗），GitHub 會依你帳號的通知設定寄「workflow 失敗」的信，
  這是目前唯一會主動通知到你的情況。
  同時程式會把 `Config` 的 `enabled` 自動設成 `FALSE`，之後每一輪都直接跳過、**顯示成功**，
  所以**只有第一次失敗會收到信**，之後不會再有任何提醒。
  請更新 `TRAVIAN_SEED_COOKIE` 之後，再把 `enabled` 手動改回 `TRUE`。
  沒有連 Google Sheet 時無法自動停用，每一輪都會照樣重試並失敗。

之後想做來襲寄信的方案（尚未實作）：用 SMTP 寄信（標準函式庫 `smtplib`，不需要新套件），
需要 `SMTP_HOST`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO` 四個 Secret（Gmail 的 `SMTP_PASS` 要用「應用程式密碼」）；
波數增加才寄、波數沒變時隔 12 小時再提醒、來襲解除就清掉紀錄；
去重狀態存成 `notify_state.json`，跟 `travian_state.json` 一起放進 Actions 快取。
來襲的抵達時間已可解析（dorf1 的「來村部隊」表格），會印在 Actions 紀錄並寫進 `Sheet1` 的「來襲波數」「最快來襲剩餘(分)」兩欄；主動通知仍未做。

---

## 八、檔案分工

| 檔案 | 負責 |
|---|---|
| `travian_agent.py` | 純解析 + 決策（看懂頁面、決定升哪個、ROI 排序、建城順序、玩家資訊、升級網址白名單），不含網路和 Sheet |
| `runner.py` | 共用主流程：Config（含自動建立／補齊預設列）／Actions 分頁、玩家資訊（佇列長度、文化點）、決策、送出升級、驗證、記錄、session 失效時停用 |
| `travian_ci_run.py` | Playwright 版入口（目前排程使用）：session（快取／種子 Cookie）、抓頁面、送出網址、安靜時段 keep-alive |
| `auto_build.py` | 純 HTTP 版入口：用 requests 抓頁面、送出網址。安靜時段直接結束，**沒有 keep-alive**，不適合單獨當排程 |
| `travian_parser.py`、`scrape_and_log.py`、`sheets_writer.py` | 村莊資料解析並寫入 `Sheet1`（`runner.py` 每輪會呼叫它們記錄） |
| `village_log.py` | 村莊結構記錄：`Fields`／`Buildings`／`Buildable`／`Changes` 四個分頁的比對與寫入（`runner.py` 每輪呼叫 `log_structure()`） |
| `test_village_log.py` | 離線測試 `village_log.py`（假 Sheet + 範例頁面，不需網路與憑據） |
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

## 十、排程與安靜時段（keep-alive）

`.github/workflows/auto_build.yml` 的 cron（UTC）目前是：

```yaml
- cron: "*/30 0-16,23 * * *"   # 白天與晚上：每 30 分鐘，正常建造
- cron: "0 17-22 * * *"        # 安靜時段：每小時一次，只做 keep-alive
```

同一個 workflow 的環境變數 `QUIET_UTC` 是 `"17,23"`，判斷方式是「起 ≤ UTC 小時 < 迄」，
也就是 UTC 17～22 點（台灣 01:00～06:59）。**cron 的小時範圍要跟 `QUIET_UTC` 一致**：
改其中一邊時，另一邊也要跟著改，否則安靜時段會跑到不該建造的時候，或該建造的時候只做 keep-alive。

安靜時段這一輪做的事：載入一次 `dorf1.php` 確認 session 還活著，把最新狀態存回 `travian_state.json`，
然後結束。**不建造、不寫 `Sheet1`／`Actions`、不偵測來襲。** session 真的失效時仍會走第七節的停用流程。

為什麼不整段停跑：第二節說過，整段約 6～7 小時完全沒有請求會讓 session 失效，
之前把排程改成跳過這段時間，就是因此而壞掉。

其他說明：

- **想省 Actions 分鐘數**：keep-alive 每次仍要安裝環境，成本接近一次正常執行。
  安靜時段共 6 次，比「全天每 30 分鐘」少 6 次。私有倉庫的免費分鐘數有限，請到 GitHub 帳號的 Billing 頁看實際用量。
  想再降低頻率，可以把 keep-alive 的間隔拉長（例如每 2 小時），但要先確認 session 撐得住，
  閒置上限沒有測過；改完要觀察幾天。
- **改完還是失效**：先把 keep-alive 的 cron 改成 `*/30 17-22 * * *` 確認保活有效，再逐步放寬。
- **臨時要在安靜時段測試建造**：把 `QUIET_UTC` 改成 `"0,0"`（起迄相同 = 永不安靜），測完改回 `"17,23"`。
- 純 HTTP 版（`auto_build.py`）沒有 keep-alive，安靜時段只會印「安靜時段，本輪不動作」。

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
| 來襲偵測 | 有實際偵測到過；抵達時間已能解析（依一份範例頁面，只確認過攻擊與援軍兩種類型），通知沒做 |
| 村莊結構記錄（第十二節） | 解析依真實範例頁面；比對與寫入只用假 Sheet 測過，**還沒在真實 Google Sheet 實測** |
| ROI 排序、非首都 10 級上限、`is_capital` | 只用模擬頁面測過，還沒在正式模式觀察實際選擇；產量表為社群數字 |
| 佇列長度自動偵測、文化點 log | 讀取邏輯依 dorf2 真實頁面寫成；dorf1 有沒有同一段資料尚未確認（沒有時會改讀 dorf2）；Plus 對非羅馬族的效果為推估 |
| Config 自動建立／補齊預設列 | 只用模擬的 Sheet 物件測過（新建、缺列補齊、不覆蓋已填值、寫入失敗只印 log），還沒在真實 Sheet 實測；已核對兩個入口都只透過 `get_cfg()` 讀 Config，gspread 5.x／6.x 的 `update(raw=True)` 等呼叫都支援 |
| 安靜時段 keep-alive | 新加入，還沒在正式排程觀察過；「閒置逾時」是依失效 log 推測，實際上限沒測過 |

其他限制：

- 只處理目前選中的單一村莊。
- 資源田的決策是 ROI 最好的先升（沒設 `build_order` 時也一樣），倉庫／糧倉則是「最便宜、最快」；
  沒有另外排優先序，工期長的項目可能佔住唯一的佇列。
- 不會使用金幣功能（Master Builder、立即完成、看廣告加速）。
- session 失效時一次就停用（`enabled=FALSE`）。偶發的網路問題或一次錯誤頁也會讓排程停下來，需要手動恢復。

---

## 十二、村莊結構記錄（`Fields` / `Buildings` / `Buildable` / `Changes`）

每輪建造輪（安靜時段不執行）由 `runner.py` 呼叫 `village_log.log_structure()`，把下面三份資料同步到 Sheet。
程式會自動建立分頁；每輪先讀回現有內容比對，**沒有差異就完全不寫入**。以 Sheet 為準，不依賴 Actions 快取。

| 分頁 | 內容 | 來源 | 額外請求 |
|---|---|---|---|
| `Fields` | 資源田 18 塊：位置、類型、等級、施工中、升級成本、升級工期、最後變動 | dorf1 | 0 |
| `Buildings` | 村莊建築位 19～40（22 個，空地也有一列）：建築、等級、施工中、最後變動 | dorf2 | 0 |
| `Buildable` | 空地可蓋的建築：分類、成本、工期、前置條件是否已達成、未達成條件、最後變動、最後確認 | 空地的 `build.php?id=<空地>&category=1/2/3` | 3，見下 |
| `Changes` | 變動紀錄，一件事一列：時間、類別、位置、項目、舊→新、備註（只追加） | 上面三者的差異 | 0 |

**空地的建築清單分三個分頁**（基礎／軍事／資源）。`travian_agent.fetch_build_options()` 三頁都抓並合併；
`build_order` 的新建築規劃也改用它（以前只抓預設分頁，會漏掉英雄宅、研究院、盔甲廠、鋸木廠等）。
代價是規劃新建築時多 2 次請求。

**Changes 會記的事件**：資源田／建築的等級變化、開始施工、新建築、建築移除或變更、可蓋清單新增／移除、
前置條件達成（否→是）或退回。第一次執行只記三筆「初始化」，不會產生幾十筆假變動。

**`Buildable` 只在需要時重抓**：第一次、`Buildings` 有變動、距離上次確認超過 24 小時；村莊沒有空地時跳過。
不記「資源夠不夠」（每輪都在變），只記「前置條件是否都已達成」。

**注意事項**

- 變動的時間精度是排程間隔（最長 30 分鐘），不是遊戲內的精確完成時間。安靜時段的變動會併在之後第一輪。
- dorf2 的石墻（slot 40）在頁面上出現兩次，解析時會去重。
- Playwright 有時會把資源田的成本欄清空；等級沒變時沿用舊成本，不會被清成空白，也不算變動。
- 這輪抓不到 dorf2 時只記資源田，不會動 `Buildings`／`Buildable`。
- 失敗只印 log（`[村莊結構] ...`），不影響升級。
- 離線測試：`python test_village_log.py`（需要同資料夾有 `dorf1.txt`、`dorf2.txt`、`build1.txt`～`build3.txt`）。


---

## 十三、後續可以做

- 來襲寄信通知（見第七節）。
- session 失效改成「連續失敗 N 次才停用」，避免偶發錯誤就讓排程停擺（需要在快取裡多存一個失敗計數）。
- 縮短施工完成到下次動手之間的空檔：cron 避開整點、佇列快完成時在同一輪裡等到完成再送出。
- 改用純 HTTP 版當主排程：目前 Playwright 版每次都要走「title 被前端 JS 清空 → 改用純 HTTP 重抓」的備援，
  瀏覽器只剩下保存 session 的用途。
