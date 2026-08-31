# Surf Arb Bot

Polymarket BTC 5m Chainlink TWAP bot：全自動紙盤預設、Telegram 全按掣、Dashboard、可上 Zeabur。

研究對賬仍然喺 [`index.html`](index.html)。呢個目錄係可運行系統。

## 預設行為

- 引擎開機即跑（`ENGINE_AUTOSTART=true`）
- **全自動**：合規缺口唔會逐單問你
- **紙盤本金可改**：Telegram「💵 紙盤本金」或 Dashboard 輸入金額；「♻️ 重置紙盤」會清倉並用新本金重開。預設 $500。
- 實盤要 `POLYMARKET_PRIVATE_KEY` + Telegram 撳兩次確認
- 緊急停機、日虧熔斷、單邊裸倉閘門、官方費用曲線

## 本地跑

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 最少填 TELEGRAM_BOT_TOKEN；Dashboard 無 token 會喺 log 印一條
python main.py
```

- Dashboard：`http://127.0.0.1:8080/?t=DASHBOARD_TOKEN`（Telegram 主頁有「🖥 開 Dashboard」一撳打開；要 `DASHBOARD_PUBLIC_URL` + `DASHBOARD_TOKEN`）
- Telegram：搵 bot 撳 **Start**，第一個進嚟嘅人成為主人（或設 `TELEGRAM_OWNER_ID`）

```bash
pytest -q
```

## Zeabur

1. 用呢個 Git repo 開一個 service（有 `Dockerfile` 會按 Docker 起）
2. Variables 貼：

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_ID=
TELEGRAM_CHAT_ID=
DASHBOARD_TOKEN=
# 可選。空則 Telegram「開 Dashboard」用 https://surf-arb.zeabur.app
DASHBOARD_PUBLIC_URL=
DATA_DIR=/data
TRADING_MODE=paper
ENGINE_AUTOSTART=true
PORT=8080
PAPER_STARTING_CASH=500
FORCE_PAPER=true
```

3. 加一個 volume 掛 `/data`，唔係每次 deploy 會清 SQLite
4. 綁 domain 之後 Dashboard 用 `https://你的網址/?t=DASHBOARD_TOKEN`
5. 試運行穩咗先加 `POLYMARKET_PRIVATE_KEY`，再喺 Telegram 確認實盤

你之後交 Zeabur key／Telegram token／Polymarket key 就喺平台 Variables 填，**唔好貼入 chat 或 commit**。

## 邏輯（同研究一致）

- 用 ask/bid 深度，唔用 mid
- **Rev 35**：5m-only 之後，下一 5 分鐘窗喺開盤前已係 45–55¢，但 14 個 CLOB 槽仍掛住而家啲 0.99 仙價（`future_listing` 喺 buffer 之前就 skip，PTB 要等到 T0）。而家 **開盤前 45 秒預熱下一窗**（唔要 PTB）、**鎖死仙價唔佔槽**（持倉除外）、清走 sqlite 剩低嘅 15m PTB。Hunt 仍然 skip `future_listing` / `twap_no_ptb`。規則仍然 45–55¢ / 6bps / scratch / $5。紙盤未重置、未開實盤。
- **Rev 34**：只做 **5 分鐘多幣種** Chainlink TWAP。15 分鐘同 5 分鐘搶 14 個 CLOB 槽、又冇獨立 15m 盤帶；1 小時係 Binance 收線，永遠唔入場。Telegram／Dashboard 週期鎖定 5M。規則仍然 45–55¢ / 6bps / scratch / $5。紙盤未重置、未開實盤。
- **Rev 33**：15m 入 12–280s 而且真正 45–55 時，唔好俾鎖死 1.00 嘅 5m 佔晒 14 個 CLOB 槽。訂閱改 **價帶優先**（`outcomePrices`，唔信 stale Gamma `bestAsk`；先 5m 再 15m）。規則仍然 45–55¢ / 6bps / scratch / $5。紙盤未重置、未開實盤。
- **Rev 32**：Rev 31 喺 **16 token 同之後 14 token** 仍然 1013。CLOB 拆 **兩條 socket × 8 token**、關掉 `initial_dump`、總 cap 14（7 隻 5m 唔再加 15m）、遠 15m 唔配對。規則仍然 45–55¢ / 6bps / scratch / $5。唔抄雙邊鎖倉。紙盤未重置、未開實盤。
- **Rev 31**：Rev 30 喺 15m 入獵窗之後仍然訂 **28 token**，JP host ~2.5 分鐘後再 1013。而家 CLOB **有 PTB 先訂**、優先 5m、**最多 16 token**；下一窗唔再 HTTP 狂拉。規則仍然 45–55¢ / 6bps / scratch / $5。唔抄雙邊鎖倉。紙盤未重置、未開實盤。
- **Rev 30**：頂級健康洞係 **CLOB 一次訂 70 token → 1013 slow consumer**、同 `twap_gate` 永遠揀最近完場（鎖死 1.00／15m 無 PTB）掩蓋真正 45–55 近成交。而家 CLOB 只訂獵窗（12–280s +45s 預熱）＋持倉／掛單；閘口揀 signal／lead／價帶而唔係最近盤；PTB 寫入 sqlite 跨重啟。規則仍然 45–55¢ / 6bps / scratch / $5。唔抄雙邊鎖倉。紙盤未重置、未開實盤。
- **Rev 29**：全幣 5m/15m 唔夠嘅唔係種類，係 **Chainlink 多 symbol 一條 socket 會停 feed**、`scan_limit=24` 同「尾 3 分鐘永遠第一」會擠走 15m 中間價、同鐘鎖死晒 8 個幣。而家每個幣獨立 RTDS、掃描 40、12–280s 兩面 TWAP 盤優先、只鎖同幣跨週期同 BTC/ETH 同鐘。規則仍然 45–55¢ / 6bps / scratch / $5。唔抄雙邊鎖倉。紙盤未重置、未開實盤。
- **Rev 28**：頂級閘係**結算來源**。5m 同 15m Chainlink TWAP-60；1H Binance 收線永遠唔入場。Telegram 幣／週期＝掃描過濾。
- **Rev 26**：引擎鎖定 **BTC 5m Chainlink TWAP**。唔再 hunt YES+NO 互補、唔再買大熱 97–98。Telegram／Dashboard 收乾舊策略掣。當時 TWAP 規則 45–55¢、6bps、12–180s。紙盤未重置、未開實盤。
- **Rev 25**：紙盤成交＝實盤 CLOB FAK dry-run。BUY 用官方 **USDC `amount` + `max_price`**（唔再用 `shares`，真錢會被 client 拒絕）；scratch **SELL `shares` + `min_price`**（紙盤都會行同一條 dump，唔再只記帳）。FOK 確認後再等 **`clob_rtt_ms=150`** 重走簿、唔 requote；盤走咗就 `clob_rtt_miss`。TWAP 仍然 180s / 6bps / scratch。紙盤未重置、未開實盤。
- **Rev 24**：TWAP 窗開到 **剩餘 180s**（抄頂級方向盤戶中位入場 ~160–210s），仍然 **lead ≥6 bps + scratch**。唔抄分時雙邊鎖倉（7% taker 費後 −EV）、唔延遲跟單。詳情 `research/copy_top.json`。紙盤未重置、未開實盤。
- **Rev 23**：預設 **BTC 5m 官方 Chainlink 60s TWAP vs 窗開價**（`strategy_mode=twap`）。只喺 **45–55¢**、lead ≥6 bps、當時剩餘 12–120s、fair P 清 ask+費+0.04 先入場；每 15s 重估，弱倉 **scratch 出貨、唔對沖**。YES+NO 互補洞仍然會先吃（`min_edge=0.02`、FOK、maker 關）。大熱 97–98 仍停、Telegram 可切回。紙盤未重置、未開實盤。同源校準 `research/twap_engine.json`（train +$78 / holdout +$155）；**唔好用 Binance 減 Gamma PTB**（~9 bps 基差）。RTDS `wss://ws-live-data.polymarket.com` topic `crypto_prices_chainlink`。中途加入要等到下一個 5 分鐘開盤先鎖 PTB。
- **Rev 22**：停大熱 97–98 hunt。當時預設只做 YES+NO 互補（`strategy_mode=complement`，`min_edge=0.02`、FOK、maker 關）。Telegram 仍可切回大熱。紙盤未重置、未開實盤。詳情 `research/top_5m.json`。
- **Rev 21**：大熱仍然 **尾 60s 97–98¢ $5**。要 **最好賣價本身喺 97–98**（唔抬 63¢ 簿後面掛住嘅 97），**bid 未穿到 99¢**（唔好 0.98 FOK 殺咗之後再抬剩餘 97），WS 斷線／HTTP 後備簿唔入。同一 5 分鐘窗 BTC/ETH 只做一隻。紙盤未重置、未開實盤。
- **Rev 20**：大熱改 **尾 60s**、要盤口鎖住（bid ≥90¢、spread <4¢、對手 ask <10¢），同一 5 分鐘窗 BTC/ETH 只做一隻。紙盤未重置、未開實盤。
- **Rev 19**：完場 **等官方 0/1** 先 redeem（唔好用結束瞬間嘅 50/50 mid 入帳——呢個先係日虧熔斷主因）。預設大熱 **97–98¢**、**$5/注**、taker。紙盤未重置、未開實盤。
- **Rev 18**：真 live 前對齊。大熱尾窗釘 **180s**（同而家紙盤 bot）。Telegram／Dashboard／`/health` 顯示同一套狀態。實盤 taker 用 **FAK market**，唔再誤掛 GTC。Kill／熔斷會 `cancel_all`。FORCE_PAPER 仍然鎖實盤。紙盤未重置、未開實盤。
- **Rev 17**：預設 **只做大熱 90–98¢**，每注 **$5**。停 YES+NO 雙邊差價（`strategy_mode=favorite`，互補掛單仍然關）。尾窗唔改。紙盤未重置、未開實盤。
- **Rev 16**：完場後自動 **redeem** 取回注碼（紙盤按官方結果入帳；實盤打 `redeemPositions`）。暫停／熔斷／緊急停機仍然會取回。紙盤未重置、未開實盤。
- **Rev 15**：大熱價帶改 **90–99¢**（第一手仍係價帶內第一個 print）。尾窗唔改。紙盤未重置、未開實盤。
- **Rev 14**：大熱同一盤唔再疊過 `max_usd_per_trade`（預設 $25）。日虧熔斷時仍然掃盤；Telegram／Dashboard 可「解除今日熔斷」（今日 PnL 由 0 再計，現金／倉唔清）。紙盤未重置。
- **Rev 13**：大熱可試 **全段 95–99¢**（Telegram「大熱尾窗」循環：30／45／90／180／5M／15M／全段）。方向可調自動／只 Up／只 Down。翻盤風險比尾 45 秒大。紙盤未重置。
- **Rev 12**：對齊「尾窗 97–99¢」——97¢ 掛單唔再擋住抬 97–99 ask；用 WS 偵測有冇砸中（唔再每 2 秒 HTTP）；尾盤有 WS 就唔狂拉 HTTP，避免 socket 1013 slow consumer。紙盤未重置。
- **Rev 11**：現有 bot 加 `strategy_mode`（Telegram 可切：自動／只互補／只大熱）。自動＝兩邊 ask 互補優先；否則最後 **30 秒** 只買 **95–99¢** 大熱（區間可調），250ms 後單腿 FAK。定價掛單預設喺下限 **95¢** 掛買（maker 0 費，被人砸中先成交、唔對沖、拿到結算）。ZEC 式 0.99/0.01 空簿跳過。紙盤未重置、未開實盤。詳情 `research/favorite_97_99.json`
- **Rev 10**：taker 仍然等 250ms，確認 **FAK 剩餘 +EV 量**，限價沒了就 requote。掛單（互補）仍然關。`min_edge` 維持 0.02。
- **Rev 9**：taker **pair FOK**。Hunt 仍然用當刻盤口找信號；成交前等 250ms 再 HTTP 重拉兩邊簿。兩邊都要喺限價內掃滿原數量，否則 **整單取消、唔入紙盤 PnL**。全量 FOK 對 sticky 洞太死（剩餘量同 1 tick 變價都會殺）。Dashboard 會分開「snapshot 會成」同「確認殺單」。
- **Rev 8**：掃 **5M＋15M＋1H**（5 分鐘窗每小時完場次數係 15m 的 3 倍）。每圈 24 盤；最後 3 分鐘優先，中段單邊 1 分盤唔好佔晒位。`best_ask=0` 會清 WS 賣盤，避免抽走後仲當有單。
- **Rev 7**：WS 盤口 hold 60 秒——一邊 ask 郁可以用另一邊最後簿去配。尾盤 ≤120s 每秒補一次 HTTP。Gamma `bestAsk≥0.99` 喺最後 3 分鐘唔再當空盤丟掉。
- **Rev 6 教訓**：要求兩邊都 <2s 新鮮會跳過 MM requote；尾盤贏家 ask 被抽走就做唔到互補。HTTP 式尾盤掛單 6 小時 −$4.79、0 次兩邊成交。
- 6 小時成交回放：HTTP 式尾盤掛單 $500→$495.21（兩邊齊成交 0 次，單邊對沖／出貨 −EV）。樂觀 1 秒成交推斷 taker 有正期望，但靜態 HTTP ask 合仍然 ≥ 1.01，所以要 WS 先有機會。
- taker 費：`C × feeRate × p × (1-p)`；crypto 0.07，sports 等 0.05，politics 等 0.04，geopolitics **0**
- 中間價 taker 多數死亡；尾盤 0.97+0.01 類先有淨利。**0.72+0.26=0.98 仍然 −EV**（7% 費食晒 2¢ 缺口），唔好為咗「有單」而減 `min_edge`。
- **掃全市場唔會令 taker 互補突然變多**：成交量最高嘅長線盤 `ask_sum` 最少 1.001。最可能有機會嘅係 **5m／15m／1h crypto 升跌窗**（多幣、先掃最臨完場同兩腿賣盤），唔係 sports 標籤、亦唔係 0 費長線 geopolitics。詳情 `research/target_markets.json`
- 便宜腳 + 對手唔貴 = 當過期單，唔做
- 兩邊齊就 merge，加快資金
- 紙盤 maker **唔會當即成交**：掛單鎖現金，要後續盤口 ask 碰到（trade-through）先填；只成交一邊就按 $0 計未配對倉。taker 先按掃描 VWAP 成交（可加滑點 tick）
- 歷史回測：`python3 research/backtest.py --hours 8` 用成交 tape 重放同一套 hunt／rescue（唔用 mid 價），結果喺 `research/backtest_results.json`
- **BTC 5m 95–99¢ 翻盤**：`python3 research/btc_5m_reversal.py`，14 日 ~4000 盤。抬 99¢ **唔係 100%**；taker 99¢ 勝率高過 98% 仍然可以 −EV。詳情 `research/btc_5m_reversal.json`
- **30 日 BTC+ETH 97–98¢ 反轉解剖**：`python3 research/reverse_30d.py`（公開 Gamma／trades，cache `/tmp/reverse_30d_cache`）。尾 60s 第一手 97–98¢、$5、每 5 分鐘窗一注，費後反轉損益平衡 97¢ ≈ 2.80%、98¢ ≈ 1.86%。一個月樣本反轉 ~2.9%，PnL 略負。成交當刻 tape 同贏盤幾乎一樣；真反轉約 91% 係入場後先砸到 90¢。完場成交量高係砸盤結果（前視），唔好用嚟濾盤。Holdout 先轉正嘅 filter（跳過第一 tick、只做 BTC、跳過某幾個 UTC 鐘）全樣本唔穩，**唔好當 hunter 訊號**。詳情 `research/reverse_30d.json` 嘅 `findings`。
- **預測反轉（PTB + 1s 現貨）**：`python3 research/reverse_predict.py`。官方 `priceToBeat`／`finalPrice` 加 Binance 1 秒路徑。同源 TWAP 收市方向同官方贏家一致 ~96.5%；Binance 減 Chainlink PTB 唔得（~9 bps 基差）。入場時贏／輸盤 lead 同 Brownian fair P 幾乎一樣（fair≈87% vs 付 97¢）。**冇高精度事前 skip**。詳情 `research/reverse_predict.json`。
- **5 分鐘頂層贏家 vs TWAP 中間價**：`python3 research/top_5m.py`。CRYPTO 週榜真正打 5m 嘅錢包多數喺 **0.47–0.57 雙邊累積**，唔係抬 97¢。無 scratch 嘅 hold-to-settle follow（`follow_2bps`）train −EV、holdout 先轉正，**唔上線**。Rev 23 改用官方 Chainlink + scratch，校準 `python3 research/twap_engine.py` → `research/twap_engine.json`。同期大熱 97–98 仍然略負。
- **複製頂級戶（閉倉拆 lock vs leftover）**：`python3 research/copy_top.py`。鎖倉收割機分時買齊兩面、pair VWAP<$1，但 **$5 taker 7% 費後 −EV**。PnL 更大嘅係方向盤戶，中位剩餘 ~165s。Rev 27 抄入場時間：TWAP max_left **280s** + ETH 5m，keep 6bps+scratch+45–55¢。詳情 `research/copy_top.json`、`research/twap_freq.json`。
- 權益 = 現金 + 凍結掛單 + 可 merge 對數 × $1（互補未配對倉 = $0；大熱／TWAP 單腿按成本計到官方結算或 scratch）；累計 PnL = 權益 − 本金

呢個唔係投資建議。遵守當地法律同 Polymarket geoblock。

日本／愛爾蘭／荷蘭：官方 geoblock 文件係 **網站 close-only，CLOB API 不限制**。`polymarket.com/api/geoblock` 嘅 `blocked:true` 係網站狀態，唔等於 bot 落唔到 API 單。美國／英國／新加坡等先係 API close-only。
