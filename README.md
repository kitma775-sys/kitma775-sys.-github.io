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
- **Rev 6**：CLOB market WebSocket 追盤口（兩邊都新鮮先 hunt）。HTTP 只喺 WS 斷線先做後備。`maker_window_seconds=0` 停尾盤掛單。taker `min_edge` 維持 0.02。
- 6 小時成交回放：HTTP 式尾盤掛單 $500→$495.21（兩邊齊成交 0 次，單邊對沖／出貨 −EV）。樂觀 1 秒成交推斷 taker 有正期望，但靜態 HTTP ask 合仍然 ≥ 1.01，所以要 WS 先有機會。
- taker 費：`C × feeRate × p × (1-p)`；crypto 0.07，sports 等 0.05，politics 等 0.04，geopolitics **0**
- 中間價 taker 多數死亡；尾盤 0.97+0.01 類先有淨利
- **掃全市場唔會令 taker 互補突然變多**：成交量最高嘅長線盤 `ask_sum` 最少 1.001。最可能有機會嘅係 **15m／1h crypto 升跌窗**（多幣、先掃最臨完場），唔係 sports 標籤、亦唔係 0 費長線 geopolitics。詳情 `research/target_markets.json`
- 便宜腳 + 對手唔貴 = 當過期單，唔做
- 兩邊齊就 merge，加快資金
- 紙盤 maker **唔會當即成交**：掛單鎖現金，要後續盤口 ask 碰到（trade-through）先填；只成交一邊就按 $0 計未配對倉。taker 先按掃描 VWAP 成交（可加滑點 tick）
- 歷史回測：`python3 research/backtest.py --hours 8` 用成交 tape 重放同一套 hunt／rescue（唔用 mid 價），結果喺 `research/backtest_results.json`
- 權益 = 現金 + 凍結掛單 + 可 merge 對數 × $1（未配對倉 = $0）；累計 PnL = 權益 − 本金

呢個唔係投資建議。遵守當地法律同 Polymarket geoblock。

日本／愛爾蘭／荷蘭：官方 geoblock 文件係 **網站 close-only，CLOB API 不限制**。`polymarket.com/api/geoblock` 嘅 `blocked:true` 係網站狀態，唔等於 bot 落唔到 API 單。美國／英國／新加坡等先係 API close-only。
