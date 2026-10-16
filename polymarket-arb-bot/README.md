# polyarb：Polymarket 低至中風險結構性套利機械人

`polyarb` 針對 **2026 年 10 月現行的 Polymarket**（CLOB V2、pUSD 抵押品、按類別收取 taker 手續費、體育及加密市場的 taker 延遲撮合）設計。它只做「回報由合約或邏輯保證」的結構性套利，不做方向性押注。

它附有繁體中文的即時網頁控制台，可以一鍵部署到 Zeabur，預設以紙盤（模擬盤）運行。實盤代碼已經完整寫好，條件齊備後只需在 Zeabur 加幾個環境變數再重啟，就可以轉實盤；任何條件未通過，系統都會自動留在紙盤。

> **先講結論**：2026-10-16 對全市場的實測掃描中（16,751 個多結果事件、276,724 個市場），**沒有任何機會通過正式門檻**。市場在靜止狀態下是有效率的，利潤只會在短暫失衡時出現（突發新聞、大額市價單掃單、新事件上架）。這個機械人的價值在於：以低延遲全天候監察、在失衡出現的幾秒內安全地完成交易，並以嚴格的風險控制避免虧損。**它不是印鈔機。** 詳細數據及分析見 [`RESEARCH_REPORT_ZH.md`](RESEARCH_REPORT_ZH.md)。

---

## 策略一覽

| 策略 | 風險 | 原理 | 結算方式 | 主要風險 |
|---|---|---|---|---|
| `negrisk_convert`：NegRisk NO 籃子轉換 | **低** | 在互斥多結果事件中，買入子集 S（至少 2 個問題）的 NO，再呼叫 `NegRiskCtfCollateralAdapter.convertPositions`，即時換回 (\|S\|−1) 份 pUSD，另外免費獲得 S 以外所有問題的 YES | 即時（鏈上交易） | 腿風險（部分成交） |
| `negrisk_yes_basket`：窮舉 YES 籃子 | 中 | 買入一個「結果清單完整」事件中每個結果的 YES，結算時必有一個支付 $1 | 等待結算 | 結果清單不完整、結算爭議、資金鎖定 |
| `ladder`：單調蘊含階梯 | 中 | 若 A 成立必然推出 B 成立（例如「超過 $1T」⟹「超過 $900B」），買 B 的 YES 加 A 的 NO，任何情況下至少收回 $1 | 等待結算 | 規則細節不一致、資金鎖定 |

**為何沒有「YES + NO < $1」或 split/merge 套利？** Polymarket 每個二元市場只有一本訂單簿，NO 的賣價永遠等於 1 − YES 的買價，買入 NO 在撮合層面就等於賣出 YES。同一市場內的這類套利在結構上不可能出現，所以本專案不浪費資源去掃描。

---

## 控制台（`polyarb serve`）

`polyarb serve` 在同一個程序內運行套利引擎及網頁控制台，預設紙盤。控制台全繁體中文，深色極光玻璃風格，桌面及手機均適用；以原生 JavaScript、SVG 及 Canvas 繪圖，無需前端建置，亦不載入任何外部 CDN。

| 區塊 | 內容 |
|---|---|
| 累計利潤 | 已實現、今日已實現、已鎖定待結算、本金回報、執行次數，以及 1／6／24 小時利潤走勢 |
| 套利雷達 | 每點代表一個目標，越近中心越接近套利，顏色代表策略；滑鼠停留看詳情，點擊開啟 Polymarket 市場頁 |
| 即時機會、成交紀錄 | 通過門檻的機會；逐腿成交、費用、延遲及修復動作 |
| 最接近套利、邊際分佈 | 每個策略最接近正邊際的市場排行，以及全市場邊際分佈（每 20 秒重算） |
| 風險控制 | 資金、持倉及單日虧損儀表，未下單原因統計；「暫停交易」及「重置熔斷」（須輸入 RESET 確認） |
| 持倉待結算 | YES 籃子及階梯的成本、保證派彩、鎖定利潤及結算倒數 |
| 引擎狀態 | 全量掃描耗時、WebSocket 訊息率、評估率、新鮮訂單簿、CPU 及記憶體 |
| 轉實盤 | 七項實盤條件逐項顯示 ✓／✕，附可一鍵複製的環境變數及步驟；實盤時顯示交易錢包（入金地址）及餘額 |
| 系統日誌 | 即時串流，可只看交易或警告 |

- 數據經 Server-Sent Events 每 2 秒推送，斷線自動重連。重啟或重新部署後，由永久磁碟上的日誌還原 24 小時內的利潤走勢、機會及成交紀錄。
- 「暫停交易」會寫入 `<data>/KILL`，重啟後仍然有效，要按「恢復交易」才會解除。
- **安全**：單一密碼登入（`POLYARB_DASHBOARD_PASSWORD`）；HMAC 簽名、14 日到期的 Cookie（HttpOnly、SameSite=Strict，經 HTTPS 時加 Secure）；登入失敗按來源 IP 及全域限流；所有 POST 請求檢查 Origin；嚴格的 Content-Security-Policy。私鑰及控制台密碼在所有日誌（包括控制台的系統日誌）中一律顯示為 `[redacted]`。除登入頁外，只有 `/healthz`（只回傳運行階段、模式及版本）無需登入。

本地試用：

```bash
pip install -e ".[dev]"
POLYARB_DASHBOARD_PASSWORD=change-me polyarb serve     # 開啟 http://localhost:8080
```

---

## 雲端部署：Zeabur（先紙盤）

`deploy/zeabur.py` 只使用 Zeabur 官方 GraphQL API 及官方 CLI（經 `npx` 執行，需要 Node.js 18+），可以重複執行：

```bash
export ZEABUR_TOKEN=...                                # Zeabur → Settings → Developer 建立 API key
python deploy/zeabur.py --region hkg1                  # Zeabur 香港共享區（按用量計費）
python deploy/zeabur.py --server <專屬伺服器 ID>        # 或部署到你自己的 Zeabur 專屬伺服器
```

腳本依次會：

1. 按名稱沿用或建立專案 `polyarb`（同名但不同地區會另建一個）及服務 `polyarb`；
2. 只補上缺少的環境變數：`POLYARB_MODE=paper`、`POLYARB_DATA_DIR=/data`、`PORT=8080`，以及隨機產生的控制台密碼。**已存在的變數不會被覆蓋**，所以你在 Zeabur 改成 `live` 之後再執行腳本，也不會被改回紙盤；
3. 掛載永久磁碟 `/data`，設定資源上限（預設 800m CPU／1536 MiB，可用 `--cpu`、`--memory` 調整）；
4. 上傳目前的程式碼（不含 `tests`、`research`、`data`），以 `Dockerfile` 建置映像（約 2–3 分鐘），等待部署完成；
5. 設定 HTTP 健康檢查 `/healthz`，建立 `*.zeabur.app` 網域；
6. 等待引擎載入全市場（約 100 秒），以密碼登入，列印地區檢查及轉實盤清單。

`--env KEY=VALUE`（可重複）用來設定或覆蓋變數，`--no-upload` 只更新設定而不上傳程式碼。腳本會拒絕任何 `POLYMARKET_*` 變數：錢包機密必須直接在 Zeabur 控制台輸入，不經指令列。

### 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `POLYARB_MODE` | `paper` | 設為 `live` 時，只有全部必要條件通過才會實盤；否則繼續紙盤，並在控制台顯示原因 |
| `POLYARB_DASHBOARD_PASSWORD` | 隨機 | 控制台密碼。未設定時每次啟動隨機產生並寫入日誌；實盤必須設定 |
| `POLYARB_DATA_DIR` | `/data` | 紙盤及實盤分別寫入 `<data>/paper/`、`<data>/live/`（日誌及狀態檔）；暫停檔為 `<data>/KILL` |
| `PORT` | `8080` | 控制台連接埠 |
| `POLYARB_LIVE_CONFIRM` | — | 實盤必須設為 `yes` |
| `POLYMARKET_PRIVATE_KEY` | — | 實盤簽名私鑰，只放在 Zeabur 環境變數 |
| `POLYMARKET_WALLET` | — | 只在沿用現有 Polymarket 代理錢包時需要；不設則使用此私鑰的 Deposit Wallet |
| `POLYARB__<區段>__<參數>` | — | 覆蓋 `config/default.yaml` 的任何設定，例如 `POLYARB__RISK__MAX_TRADE_USD=25`、`POLYARB__ENGINE__REST_SWEEP_S=10`。值按 YAML 解析；鍵名拼錯會拒絕啟動，不會被默默忽略 |

### 容器

`Dockerfile` 以 `python:3.12-slim` 為基礎，依賴以 `requirements.lock` 鎖定版本，只安裝預編譯 wheel，並**已包括實盤 SDK `polymarket-client`**，所以轉實盤無需重新建置。入口程式先把資料目錄交給非 root 用戶 `polyarb`，再以 `setpriv` 降權運行；內建 `HEALTHCHECK`。映像壓縮後約 65 MB。

```bash
docker build -t polyarb .
docker run -p 8080:8080 -v polyarb-data:/data -e POLYARB_DASHBOARD_PASSWORD=change-me polyarb
```

### 實測資源（2026-10-16，Zeabur 東京專屬伺服器，紙盤）

部署時額外設定了 `POLYARB__ENGINE__REST_SWEEP_S=10`（每 10 秒全量掃描一次，預設 6 秒）及 `POLYARB__UNIVERSE__MAX_BOOK_AGE_S=30`，以節省 CPU。

| 項目 | 數值 |
|---|---|
| 監察範圍 | 約 4,640 個事件、35,987 本訂單簿（全部新鮮）；目標：轉換 3,023、YES 籃子 470、階梯 1,086 |
| 首次載入全市場 | 約 100 秒 |
| 全量掃描 | 每次 7–9 秒，每 10 秒一次 |
| WebSocket 及評估 | 每秒約 560–730 條訊息、約 415–440 次目標評估 |
| CPU | 穩定運行時平均約 0.35–0.4 個核心（介乎 0.1–0.55），啟動載入全市場時短暫升至約 0.8 個核心 |
| 記憶體 | 約 0.85–1.0 GB（上限 1.5 GiB） |

---

## 轉實盤

> ⚠️ 實盤涉及真實資金。請先以紙盤運行最少一至兩星期，核對機會頻率、成交及略過原因，再以小額開始。

`POLYARB_MODE=live` 時，程式啟動前會逐項檢查下表。**任何一項必要條件未通過，都會繼續以紙盤運行**，並在控制台頂部顯示原因，不會在設定不完整時下單。

| 檢查 | 必要 | 通過條件 |
|---|---|---|
| `mode` | 否 | `POLYARB_MODE=live` |
| `confirm` | 是 | `POLYARB_LIVE_CONFIRM=yes` |
| `key` | 是 | 已設定 `POLYMARKET_PRIVATE_KEY` |
| `sdk` | 是 | 已安裝官方 SDK（映像已內建） |
| `geo` | 是 | Polymarket 准許此伺服器經 API 下單：geoblock 端點回報 `blocked: false`，或伺服器所在國家屬官方「只限網頁」名單（日本、南韓、愛爾蘭、荷蘭、馬耳他）；查詢失敗視為不通過；運行中每 10 分鐘重查 |
| `password` | 是 | 已設定固定的 `POLYARB_DASHBOARD_PASSWORD` |
| `volume` | 是 | 資料目錄位於永久磁碟（容器內必須是掛載的 volume），持倉及熔斷狀態可跨重啟保存 |

> **Zeabur 東京伺服器可以實盤。** 從東京伺服器查詢 geoblock 端點會回報 `blocked: true, country: JP`，但這個端點位於 polymarket.com，反映的是網頁版的限制。官方文件把日本列為「網頁只可平倉，API 本身不受限」，所以程式按官方地區規則判斷，日本伺服器的「地區檢查」會通過；其他受限地區（美國、英國、新加坡、台灣等）照舊不准實盤。萬一 Polymarket 在 API 層以 HTTP 403 拒絕訂單，程式會即時熔斷，停止所有交易，並在控制台顯示原因。

**步驟（Zeabur）**

1. 確認你本人所在地可以合法使用 Polymarket（見下一節「地區限制」）。**切勿用 VPN 或代理繞過限制。**
2. 以紙盤運行一段時間，確認控制台的「地區檢查」顯示 ✓（Zeabur 東京或香港的伺服器均可；部署到香港：`python deploy/zeabur.py --region hkg1`）。
3. 準備**專用**錢包，只存放打算投入的 pUSD：
   - 不設 `POLYMARKET_WALLET`：使用此私鑰對應的 Deposit Wallet（2026-05-04 之後的預設），經 relayer 送交易，免 gas；
   - 沿用 polymarket.com 帳戶：把 `POLYMARKET_WALLET` 設為該帳戶的代理錢包地址，同樣免 gas；
   - 只有 `POLYMARKET_WALLET` 等於簽名地址本身（EOA）時，才需要少量 POL 支付 gas。
4. 在 Zeabur 服務 →「環境變數」加入：
   ```
   POLYARB_MODE=live
   POLYARB_LIVE_CONFIRM=yes
   POLYMARKET_PRIVATE_KEY=0x...
   POLYARB__RISK__BANKROLL_USD=200
   POLYARB__RISK__MAX_TRADE_USD=25
   POLYARB__RISK__MAX_HOLD_EXPOSURE_USD=150
   POLYARB__RISK__MAX_DAILY_LOSS_USD=10
   ```
   後四項是建議的首次實盤額度，可按需要調整。
5. 在 Zeabur 按「重新啟動」。頂部徽章由「紙盤」變成「實盤」即代表成功；「轉實盤」卡會顯示實際的交易錢包（入金地址）、錢包類型及 pUSD 餘額。每次啟動都會呼叫 SDK 的 `setup_trading_approvals()`，補上缺少的授權（包括 NegRisk 轉換所需的授權），已授權的項目會自動跳過。
6. 隨時可以按「暫停交易」，或把 `POLYARB_MODE` 改回 `paper` 再重啟。紙盤及實盤帳本分開保存，互不影響。

**延遲**：撮合伺服器在倫敦（AWS eu-west-2），從香港或東京往返約 200 毫秒或以上。這足以捕捉持續數秒以上的失衡（多數是長尾市場及階梯），但搶不過在倫敦共置、只需毫秒的對手。

### 地區限制

程式啟動實盤前會呼叫 `https://polymarket.com/api/geoblock`，按官方地區規則判斷能否經 API 下單：端點回報 `blocked: false`，或伺服器所在國家屬下列「只限網頁」名單，才准實盤；查詢失敗視為不通過。根據 2026 年 10 月的[官方文件](https://docs.polymarket.com/api-reference/geoblock)：
- **完全封鎖**（OFAC 制裁）：伊朗、敘利亞、古巴、北韓、烏克蘭的克里米亞、頓涅茨克、盧甘斯克。
- **只可平倉**（網頁及 API 均受限）：美國、英國、法國、德國、意大利、比利時、波蘭、斯洛伐克、俄羅斯、白俄羅斯、澳洲、新西蘭、新加坡、台灣、泰國、巴西、委內瑞拉，加拿大的卑詩、安大略、亞伯達、魁北克，以及多個受制裁或戰亂地區（完整名單見[官方文件](https://docs.polymarket.com/api-reference/geoblock)）。
- **只限網頁只可平倉**（文件註明 API 不受限）：愛爾蘭、日本、荷蘭、南韓；馬耳他只限體育市場。這些地區的端點會回報 `blocked: true`（2026-10-16 從東京伺服器實測如此），程式照樣准許 API 下單；若 Polymarket 實際以 HTTP 403 拒單，即時熔斷。
- **香港不在名單內**。但香港《賭博條例》（第 148 章）限制與未獲授權的博彩營運者下注，Polymarket 是否屬於該範疇是法律問題，請自行諮詢律師。

**切勿使用 VPN 或海外伺服器規避你本人所在地的限制**，這違反 Polymarket 服務條款，帳戶及資金可能被凍結。

---

## 安裝

需要 Python 3.11 或以上。

```bash
cd polymarket-arb-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # 掃描、紙盤、控制台、測試
pip install -e ".[dev,live]"     # 實盤：另外安裝官方 SDK polymarket-client 0.13.x
```

## 測試

```bash
pytest -q        # 128 個單元測試，全部離線執行，不需要網絡或私鑰
```

測試覆蓋手續費與訂單簿計算、籃子定量、階梯解析、三個策略、風控（包括按官方地區規則判斷 API 下單、HTTP 403 拒單即熔斷）、執行器的所有修復路徑（補齊、轉換子集、平倉、保留倉位、轉換失敗）、實盤閘道（以假 SDK 模擬批量送單、延遲撮合對帳、交易錢包資料、地區拒單）、鏈上 calldata 編碼、WebSocket 處理及設定檔；以及控制台的部分：登入及 Cookie、登入限流、Origin 檢查、SSE、控制操作、靜態檔路徑穿越、環境變數覆蓋、日誌輪替、優雅關閉（不會中途切斷執行）、實盤就緒檢查及機密遮蔽。

---

## 快速開始

```bash
polyarb serve                                    # 引擎 + 網頁控制台（預設紙盤，http://localhost:8080）
polyarb geo                                      # 檢查 Polymarket 是否准許此 IP 經 API 下單
polyarb scan                                     # 一次性掃描全部開放事件（約 30 秒）
polyarb scan --json data/scan.json               # 同時把完整結果寫入 JSON
polyarb scan --no-gates                          # 研究用：移除利潤／邊際／年化門檻，列出所有正邊際
polyarb scan --max-pages 5                       # 只抓 500 個事件，快速試用
polyarb ladders --search "valuation"             # 列出偵測到的蘊含階梯及其解析結果
polyarb run --duration 600                       # 紙盤（無控制台）連續運行 10 分鐘
polyarb run --paper-fail-prob 0.2 --seed 1       # 模擬 20% 腿失敗，演練修復／平倉流程
polyarb status                                   # 風控狀態、持倉、盈虧
polyarb reset-kill                               # 人工檢查完畢後解除熔斷
```

全域選項 `-c my.yaml`（只需寫要覆蓋的設定）和 `-v`（除錯日誌）須放在子指令之前，例如 `polyarb -c my.yaml run`。`--no-gates` 只供研究，實盤模式會拒絕執行。

`scan` 的輸出包括：
- 每個策略在「最優價位」的邊際分佈（扣除 taker 手續費後，每單位利潤 > 0、> −0.5¢、> −1¢、> −2¢ 的數目、中位數及最佳值）；
- 通過門檻、已按實際深度定量的可執行機會（每條腿的股數、平均價、限價、手續費、預期利潤、年化回報）。

紙盤以真實訂單簿模擬成交：FOK 不能完全成交即拒絕、成交會扣減流動性、會收取正確的手續費，並且不會重複使用同一筆真實流動性。`polyarb run` 的日誌寫入 `data/journal.jsonl`，風控狀態寫入 `data/state.json`；`polyarb serve` 則寫入 `<POLYARB_DATA_DIR>/<模式>/`。

---

## 運作方式（簡述）

1. **事件發現**：以 Gamma `/events/keyset` 拉取全部開放的多結果事件，約 25 秒。預設每 20 分鐘刷新一次。
2. **範圍篩選**：剔除有延遲撮合的體育比賽市場（`secondsDelay > 0`）、暫停或已結算的市場，再由各策略產生目標（target）。
3. **行情**：以 CLOB `/books` 批量 REST 拉取（每次 500 個 token），預設每 6 秒全量掃描一次，伺服器雜湊未變的訂單簿會略過解析。邊際最接近正數的 300 個目標再以 WebSocket 即時推送。超過 20 秒未更新的訂單簿不會用於評估。
4. **事件驅動評估**：只重新評估訂單簿有變化的目標。
5. **執行**：風控預檢 → 送單前以 REST 重新取書並按最新深度重新定量 → 以最終訂單再做一次額度審批 → 所有腿並行簽名、同一批次送出 FOK 市價單（`amount` 為計劃金額，`max_price` 為觸及的最差價位）。訂單簿變差時整條腿會被取消，不會以差價成交。
6. **部分成交修復**：比較「以新價格補齊缺腿」「轉換已成交的 NegRisk 子集」「賣出平倉」三者，選損失最小的方案。若平倉損失超過 `max_unwind_loss_usd`（預設 $5），保留倉位並觸發熔斷，交由人工處理。
7. **結算**：NO 籃子在鏈上執行 `convertPositions`，等待交易確認；轉換失敗會保留 NO 倉位並熔斷。YES 籃子及階梯持有至結算，計入持倉額度。
8. **關閉**：收到 SIGTERM（例如 Zeabur 重新部署）時停止接受新機會，等待進行中的執行完成才退出，不會留下半套倉位。

---

## 自行託管的實盤（指令列）

不用 Zeabur 時，也可以在自己的伺服器以指令列運行實盤。地區、錢包及風險說明與上文「轉實盤」相同。

1. 安裝實盤依賴：`pip install -e ".[live]"`。
2. 設定環境變數（建議放在不會提交的 `.env` 或密鑰管理工具，`.env` 已在 `.gitignore`）：
   ```bash
   export POLYMARKET_PRIVATE_KEY=0x...      # 簽名私鑰：請使用專用錢包，只存放可承受損失的資金
   export POLYMARKET_WALLET=0x...           # 可選：指定交易錢包地址
   export POLYARB_LIVE_CONFIRM=yes          # 表示你已閱讀本節風險說明
   ```
3. 建立保守的設定檔，例如 `live.yaml`：
   ```yaml
   risk:
     bankroll_usd: 200
     max_trade_usd: 20
     max_event_exposure_usd: 50
     max_hold_exposure_usd: 100
     max_daily_loss_usd: 10
   execution:
     mode: live
     journal_path: data/live/journal.jsonl
     state_path: data/live/state.json
   ```
4. 啟動：`polyarb -c live.yaml run`（無控制台），或 `POLYARB_MODE=live POLYARB_DASHBOARD_PASSWORD=... polyarb -c live.yaml serve`（連控制台，並套用上文的實盤條件檢查）。

**緊急停止**：在工作目錄執行 `touch KILL`（`serve` 模式為 `<POLYARB_DATA_DIR>/KILL`，或按控制台的「暫停交易」），所有新交易立即停止，刪除檔案即恢復。自動熔斷（虧損上限、保留倉位、轉換失敗等）會持久保存在狀態檔，必須人工檢查後執行 `polyarb reset-kill` 或在控制台按「重置熔斷」才會解除。

**人工恢復**：若轉換失敗而持有 NO 倉位，可以手動轉換：

```bash
polyarb -c live.yaml convert --market-id 0x<negRiskMarketID> --indices 0,3,5 --shares 20
```

---

## 設定參考

完整設定及註解見 [`config/default.yaml`](config/default.yaml)。任何設定都可以用環境變數 `POLYARB__<區段>__<參數>` 覆蓋（例如 `POLYARB__CONVERT__MIN_PROFIT_USD=0.5`），雲端部署時無需另備設定檔。最重要的參數如下：

| 參數 | 預設 | 說明 |
|---|---|---|
| `convert.min_profit_usd` / `min_edge` | 0.25 / 0.3% | NO 籃子轉換的最低利潤及最低回報率（已扣手續費） |
| `convert.max_legs` | 12 | 腿越多，腿風險越大；每批最多 15 張單 |
| `yes_basket.min_apr` / `max_days` | 25% / 45 日 | 資金鎖定至結算，所以按年化回報及期限篩選 |
| `yes_basket.require_catch_all` | true | 必須有「其他／以上皆非」類結果，或開放式數值區間，才當作清單完整 |
| `ladder.min_apr` / `max_days` | 20% / 90 日 | 同上 |
| `universe.max_seconds_delay` | 0 | 0 = 不交易延遲撮合的體育比賽市場；設為 3 可納入 |
| `universe.max_book_age_s` | 20 | 超過此秒數未更新的訂單簿不會用於評估 |
| `risk.bankroll_usd` | 1000 | 資金規模 |
| `risk.max_trade_usd` | 100 | 單筆上限 |
| `risk.max_event_exposure_usd` | 250 | 單一事件持倉上限（只計等待結算的策略） |
| `risk.max_hold_exposure_usd` | 600 | 等待結算的持倉總額上限 |
| `risk.max_daily_loss_usd` | 25 | 單日已實現虧損達此數即熔斷 |
| `risk.max_consecutive_failures` | 3 | 連續不完整執行次數達此數即熔斷 |
| `risk.cooldown_s` | 30 | 同一機會及同一事件的冷卻時間 |
| `execution.recheck_books` | true | 送單前重新取書並重新定量 |
| `execution.slippage_ticks` | 0 | 首次送單的額外限價空間（修復時固定 +1 tick） |
| `execution.max_unwind_loss_usd` | 5 | 平倉損失超過此數則保留倉位並熔斷 |
| `engine.rest_sweep_s` | 6 | REST 全量掃描間隔 |
| `engine.ws_max_targets` | 300 | 以 WebSocket 即時監察的目標數 |

---

## 風險控制

- **地區檢查**：按官方地區規則判斷能否經 API 下單（日本等「只限網頁」地區准許），實盤啟動前及運行中定期檢查，查詢失敗視為不通過。
- **實盤閘門**：`POLYARB_MODE=live` 時七項條件中任何一項必要條件未通過，都會留在紙盤，絕不會在設定不完整時下單。
- **熔斷**（需人工解除）：單日虧損上限、連續不完整執行、保留了未對沖倉位、鏈上轉換失敗、Polymarket 以 HTTP 403 拒絕訂單（例如地區限制）。另可隨時以 `KILL` 檔案或控制台的「暫停交易」停止所有新交易。
- **額度**：單筆上限、可用資金、等待結算的持倉總額、單一事件持倉；送單前會以重新定量後的最終訂單再審批一次。
- **價格保護**：FOK 加最差價位限價，訂單簿變差時整腿取消，不會追價。
- **數據新鮮度**：只用 20 秒內更新的訂單簿；WebSocket 心跳中斷時，其訂單簿會自動過期。
- **延遲撮合**：回應為 `delayed` 的訂單會等待市場的延遲時間，然後取消剩餘部分，再查詢實際成交量。
- **機密**：私鑰只從環境變數讀取，所有日誌一律遮蔽；控制台需要密碼登入。
- **可審計**：每個機會、每次執行（逐腿成交、費用、延遲、動作）、心跳及控制台操作都寫入 JSONL 日誌（按大小輪替）。

---

## 專案結構

```
polymarket-arb-bot/
├── polyarb/
│   ├── models.py          市場、事件、腿、機會等資料模型
│   ├── fees.py            taker 手續費：shares × rate × p(1−p)
│   ├── book.py            訂單簿、吃單模擬、NO 鏡像價格
│   ├── sizing.py          等量多腿籃子的深度定量（最低股數、$1 最低金額）
│   ├── universe.py        事件篩選及策略目標
│   ├── engine.py          REST 全量掃描、WebSocket 熱門集合、事件驅動評估、優雅關閉
│   ├── risk.py            風控預檢、熔斷、持倉、盈虧
│   ├── journal.py         JSONL 日誌（按大小輪替）
│   ├── config.py          設定（dataclass，YAML 及 POLYARB__* 環境變數覆蓋）
│   ├── app.py             polyarb serve：實盤條件檢查、紙盤後備、機密遮蔽
│   ├── monitor.py         控制台數據：走勢、排行、事件串流
│   ├── cli.py             polyarb 指令
│   ├── web/               aiohttp 伺服器（登入、SSE、控制操作）及 static/ 前端
│   ├── data/              Gamma、CLOB、訂單簿存儲、WebSocket
│   ├── strategies/        negrisk.py（轉換、YES 籃子）、ladder.py、ladder_parse.py
│   └── execution/         executor.py（修復決策）、paper.py、live.py（官方 SDK）、settlement.py（鏈上轉換）
├── config/default.yaml
├── deploy/zeabur.py       一鍵部署到 Zeabur
├── Dockerfile             容器映像（含實盤 SDK、非 root 運行）
├── entrypoint.sh          容器入口：資料目錄權限、降權
├── requirements.lock      鎖定版本的運行依賴
├── research/              全市場掃描腳本及 2026-10-16 的結果
├── tests/                 128 個離線單元測試
└── RESEARCH_REPORT_ZH.md  研究報告
```

---

## 免責聲明

本專案僅供研究及教育用途，並不構成任何投資、法律或稅務建議。「套利」並不等於零風險：腿風險、結算規則及爭議、智能合約、平台、監管及操作風險都可能導致虧損。紙盤結果不代表實盤表現。使用者須自行確保在其所在司法管轄區使用 Polymarket 屬合法，並自行承擔所有後果。
