# Surf Arb Bot

Polymarket YES/NO 互補套利 bot：全自動紙盤預設、Telegram 全按掣、Dashboard、可上 Zeabur。

研究對賬仍然喺 [`index.html`](index.html)。呢個目錄係可運行系統。

## 預設行為

- 引擎開機即跑（`ENGINE_AUTOSTART=true`）
- **全自動**：合規缺口唔會逐單問你
- **紙盤**：用真盤口計數，唔簽名、唔落單
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
```

3. 加一個 volume 掛 `/data`，唔係每次 deploy 會清 SQLite
4. 綁 domain 之後 Dashboard 用 `https://你的網址/?t=DASHBOARD_TOKEN`
5. 試運行穩咗先加 `POLYMARKET_PRIVATE_KEY`，再喺 Telegram 確認實盤

你之後交 Zeabur key／Telegram token／Polymarket key 就喺平台 Variables 填，**唔好貼入 chat 或 commit**。

## 邏輯（同研究一致）

- 用 ask/bid 深度，唔用 mid
- taker 費：`C × feeRate × p × (1-p)`
- 中間價 taker 多數死亡；尾盤 0.97+0.01 類先有淨利
- 便宜腳 + 對手唔貴 = 當過期單，唔做
- 兩邊齊就 merge，加快資金

呢個唔係投資建議。遵守當地法律同 Polymarket geoblock。
