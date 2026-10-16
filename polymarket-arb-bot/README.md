# polyarb：Polymarket 低至中風險結構性套利機械人

`polyarb` 針對 **2026 年 10 月現行的 Polymarket**（CLOB V2、pUSD 抵押品、按類別收取 taker 手續費、體育及加密市場的 taker 延遲撮合）設計。它只做「回報由合約或邏輯保證」的結構性套利，不做方向性押注。

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

## 安裝

需要 Python 3.11 或以上。

```bash
cd polymarket-arb-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # 掃描、模擬盤、測試
pip install -e ".[dev,live]"     # 實盤：另外安裝官方 SDK polymarket-client 0.13.x
```

## 測試

```bash
pytest -q        # 88 個單元測試，全部離線執行，不需要網絡或私鑰
```

測試覆蓋手續費與訂單簿計算、籃子定量、階梯解析、三個策略、風控、執行器的所有修復路徑（補齊、轉換子集、平倉、保留倉位、轉換失敗）、實盤閘道（以假 SDK 模擬批量送單、延遲撮合對帳）、鏈上 calldata 編碼、WebSocket 處理及設定檔。

---

## 快速開始

```bash
polyarb geo                                      # 檢查你的 IP 是否被 Polymarket 地區封鎖
polyarb scan                                     # 一次性掃描全部開放事件（約 30 秒）
polyarb scan --json data/scan.json               # 同時把完整結果寫入 JSON
polyarb scan --no-gates                          # 研究用：移除利潤／邊際／年化門檻，列出所有正邊際
polyarb scan --max-pages 5                       # 只抓 500 個事件，快速試用
polyarb ladders --search "valuation"             # 列出偵測到的蘊含階梯及其解析結果
polyarb run --duration 600                       # 模擬盤（paper）連續運行 10 分鐘
polyarb run --paper-fail-prob 0.2 --seed 1       # 模擬 20% 腿失敗，演練修復／平倉流程
polyarb status                                   # 風控狀態、持倉、盈虧
polyarb reset-kill                               # 人工檢查完畢後解除熔斷
```

全域選項 `-c my.yaml`（只需寫要覆蓋的設定）和 `-v`（除錯日誌）須放在子指令之前，例如 `polyarb -c my.yaml run`。`--no-gates` 只供研究，實盤模式會拒絕執行。

`scan` 的輸出包括：
- 每個策略在「最優價位」的邊際分佈（扣除 taker 手續費後，每單位利潤 > 0、> −0.5¢、> −1¢、> −2¢ 的數目、中位數及最佳值）；
- 通過門檻、已按實際深度定量的可執行機會（每條腿的股數、平均價、限價、手續費、預期利潤、年化回報）。

模擬盤（`run`，預設 paper）以真實訂單簿模擬成交：FOK 不能完全成交即拒絕、成交會扣減流動性、會收取正確的手續費，並且不會重複使用同一筆真實流動性。日誌寫入 `data/journal.jsonl`，風控狀態寫入 `data/state.json`。

---

## 運作方式（簡述）

1. **事件發現**：以 Gamma `/events/keyset` 拉取全部開放的多結果事件，約 25 秒。預設每 20 分鐘刷新一次。
2. **範圍篩選**：剔除有延遲撮合的體育比賽市場（`secondsDelay > 0`）、暫停或已結算的市場，再由各策略產生目標（target）。
3. **行情**：以 CLOB `/books` 批量 REST 拉取（每次 500 個 token），每 6 秒全量掃描一次，約 3.6 萬本訂單簿只需約 4 秒。邊際最接近正數的 300 個目標再以 WebSocket 即時推送。超過 20 秒未更新的訂單簿不會用於評估。
4. **事件驅動評估**：只重新評估訂單簿有變化的目標。
5. **執行**：風控預檢 → 送單前以 REST 重新取書並按最新深度重新定量 → 以最終訂單再做一次額度審批 → 所有腿並行簽名、同一批次送出 FOK 市價單（`amount` 為計劃金額，`max_price` 為觸及的最差價位）。訂單簿變差時整條腿會被取消，不會以差價成交。
6. **部分成交修復**：比較「以新價格補齊缺腿」「轉換已成交的 NegRisk 子集」「賣出平倉」三者，選損失最小的方案。若平倉損失超過 `max_unwind_loss_usd`（預設 $5），保留倉位並觸發熔斷，交由人工處理。
7. **結算**：NO 籃子在鏈上執行 `convertPositions`，等待交易確認；轉換失敗會保留 NO 倉位並熔斷。YES 籃子及階梯持有至結算，計入持倉額度。

---

## 實盤設定

> ⚠️ 實盤涉及真實資金。請先以模擬盤運行最少一至兩星期，核對 `data/journal.jsonl` 的機會頻率及規模，再以小額資金開始。

**地區限制**：程式啟動實盤前會呼叫 `https://polymarket.com/api/geoblock`，若回報 `blocked` 即拒絕運行；查詢失敗亦視為封鎖。根據 2026 年 10 月的官方文件：
- **完全封鎖**（OFAC 制裁）：伊朗、敘利亞、古巴、北韓、烏克蘭的克里米亞、頓涅茨克、盧甘斯克。
- **只可平倉**（網頁及 API 均受限）：美國、英國、法國、德國、意大利、比利時、波蘭、斯洛伐克、俄羅斯、白俄羅斯、澳洲、新西蘭、新加坡、台灣、泰國、巴西、委內瑞拉，加拿大的卑詩、安大略、亞伯達、魁北克，以及多個受制裁或戰亂地區（完整名單見[官方文件](https://docs.polymarket.com/api-reference/geoblock)）。
- **只限網頁只可平倉**（API 不受限）：愛爾蘭、日本、荷蘭、南韓；馬耳他只限體育市場。
- **香港不在名單內**。但香港《賭博條例》（第 148 章）限制與未獲授權的博彩營運者下注，Polymarket 是否屬於該範疇是法律問題，請自行諮詢律師。

**切勿使用 VPN 或海外伺服器規避你本人所在地的限制**，這違反 Polymarket 服務條款，帳戶及資金可能被凍結。

**步驟**：

1. 安裝實盤依賴：`pip install -e ".[live]"`。
2. 準備 Polymarket 帳戶及 pUSD 餘額。2026-05-04 之後建立的帳戶預設使用 Deposit Wallet，交易經 relayer 送出，免 gas。
3. 設定環境變數（建議放在不會提交的 `.env` 或密鑰管理工具，`.env` 已在 `.gitignore`）：
   ```bash
   export POLYMARKET_PRIVATE_KEY=0x...      # 簽名私鑰：請使用專用錢包，只存放可承受損失的資金
   export POLYMARKET_WALLET=0x...           # 可選：指定交易錢包地址
   export POLYARB_LIVE_CONFIRM=yes          # 表示你已閱讀本節風險說明
   ```
4. 建立保守的設定檔，例如 `live.yaml`：
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
5. 啟動：`polyarb -c live.yaml run`。每次啟動都會呼叫 SDK 的 `setup_trading_approvals()`，補上缺少的授權（交易所及 NegRisk 抵押品轉換器），已授權的項目會自動跳過。轉換所需的 `ConditionalTokens.setApprovalForAll(NegRiskCtfCollateralAdapter)` 已包括在內。

**緊急停止**：在工作目錄執行 `touch KILL`，所有新交易立即停止，刪除檔案即恢復。自動熔斷（虧損上限、保留倉位、轉換失敗等）則會持久保存在狀態檔，必須人工檢查後執行 `polyarb reset-kill` 才會解除。

**人工恢復**：若轉換失敗而持有 NO 倉位，可以手動轉換：

```bash
polyarb -c live.yaml convert --market-id 0x<negRiskMarketID> --indices 0,3,5 --shares 20
```

---

## 設定參考

完整設定及註解見 [`config/default.yaml`](config/default.yaml)。最重要的參數如下：

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

- **地區檢查**：實盤啟動前及每次運行開始時檢查，查詢失敗亦視為封鎖。
- **熔斷**（需人工解除）：單日虧損上限、連續不完整執行、保留了未對沖倉位、鏈上轉換失敗。另可隨時以 `KILL` 檔案暫停所有新交易。
- **額度**：單筆上限、可用資金、等待結算的持倉總額、單一事件持倉；送單前會以重新定量後的最終訂單再審批一次。
- **價格保護**：FOK 加最差價位限價，訂單簿變差時整腿取消，不會追價。
- **數據新鮮度**：只用 20 秒內更新的訂單簿；WebSocket 心跳中斷時，其訂單簿會自動過期。
- **延遲撮合**：回應為 `delayed` 的訂單會等待市場的延遲時間，然後取消剩餘部分，再查詢實際成交量。
- **可審計**：每個機會、每次執行（逐腿成交、費用、延遲、動作）及心跳都寫入 JSONL 日誌。

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
│   ├── engine.py          REST 全量掃描、WebSocket 熱門集合、事件驅動評估
│   ├── risk.py            風控預檢、熔斷、持倉、盈虧
│   ├── journal.py         JSONL 日誌
│   ├── config.py          設定（dataclass，YAML 覆蓋）
│   ├── cli.py             polyarb 指令
│   ├── data/              Gamma、CLOB、訂單簿存儲、WebSocket
│   ├── strategies/        negrisk.py（轉換、YES 籃子）、ladder.py、ladder_parse.py
│   └── execution/         executor.py（修復決策）、paper.py、live.py（官方 SDK）、settlement.py（鏈上轉換）
├── config/default.yaml
├── research/              全市場掃描腳本及 2026-10-16 的結果
├── tests/                 88 個離線單元測試
└── RESEARCH_REPORT_ZH.md  研究報告
```

---

## 免責聲明

本專案僅供研究及教育用途，並不構成任何投資、法律或稅務建議。「套利」並不等於零風險：腿風險、結算規則及爭議、智能合約、平台、監管及操作風險都可能導致虧損。使用者須自行確保在其所在司法管轄區使用 Polymarket 屬合法，並自行承擔所有後果。
