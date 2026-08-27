# Surf Arb Bot

Polymarket YES/NO 互補套利 bot：全自動紙盤預設、Telegram 全按掣、Dashboard、可上 Zeabur。

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

- Dashboard：`http://127.0.0.1:8080/?t=DASHBOARD_TOKEN`
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
- 權益 = 現金 + 凍結掛單 + 可 merge 對數 × $1（互補未配對倉 = $0；大熱單腿按成本計到官方結算）；累計 PnL = 權益 − 本金

呢個唔係投資建議。遵守當地法律同 Polymarket geoblock。

日本／愛爾蘭／荷蘭：官方 geoblock 文件係 **網站 close-only，CLOB API 不限制**。`polymarket.com/api/geoblock` 嘅 `blocked:true` 係網站狀態，唔等於 bot 落唔到 API 單。美國／英國／新加坡等先係 API close-only。
