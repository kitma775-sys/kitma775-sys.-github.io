# Polymarket TWAP/FOK bot (kitma775-sys.-github.io)

## Identity

This repo’s **live bot** (code `DEFAULT_SETTINGS["strategy_rev"]=60`, boot patch `apply_strategy_rev` up to 60) is a **Polymarket 5-minute Up/Down sleeve**. It hunts BTC and ETH windows whose slug matches `{asset}-updown-5m-{unix}`. Entry is **FOK** (live CLOB **FAK**, paper uses the same fill rules). It buys the first **6–40 bps** Chainlink-60s-TWAP vs window-open PTB lead while the ask is in **45–55¢**, waits **250ms**, does **not** chase leftover cheaper asks. Independent clocks: BTC and ETH may take the **same** 5m unix. Scratch / reverse / late-dump / oracle dump are in `app/twap.py` + `app/runtime.py`. Dashboard + Telegram are operator UI. This is **not** GitHub Pages on `main`. This is **not** `hl-auto-trader` (different repo, different venue).

## Git

- Work **only** on `cursor/polymarket-arb-bot-feasibility-5998`.
- Remote: `kitma775-sys/kitma775-sys.-github.io`.
- Open **PR #1 is DRAFT** onto `main`. **Do not merge. Do not push to `main`.**
- `main` is the old GitHub Pages site (almost no `.py`). Bot code lives **only** on this branch. Zeabur deploys **this branch** (claimed live Rev 60).
- Never merge to `main` unless the owner explicitly says so.

## Layout

- `main.py` — process entry (`from app.main import run`).
- `app/` — runtime package (engine, CLOB, Telegram, Dashboard, Chainlink, sqlite).
- `tests/test_logic.py` — pytest suite (must stay green).
- `research/` — offline notebooks/scripts/JSON. **Not** required on Zeabur. Do not import research into `app/` without owner confirmation.
- `Dockerfile`, `zbpack.json`, `requirements.txt` — deploy/run.
- `.env.example` — env **NAMES** only.

## Run

Local (from `README.md`):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill NAMES; never commit .env
python main.py
```

Tests:

```bash
pytest -q
# or
pytest tests/test_logic.py -q
```

Docker: `CMD ["python", "main.py"]` (see `Dockerfile`).

## Deploy

- Zeabur tracks **this branch** (zip/branch deploy). Redeploying **code** does not copy sqlite; sqlite lives on volume.
- Volume path: `/data` (`DATA_DIR`). Sqlite filename: `surf.sqlite` → `{DATA_DIR}/surf.sqlite` (`app/store.py` `connect`).
- Do **not** copy or commit the DB. Do **not** paste dumps.
- `zbpack.json`: Python 3.11, entry `main.py`.
- After code change: commit + push **this branch**, then owner/agent zip-deploys if they want live. Docs-only commits do not need a zip deploy.

## Secrets

Values live in **Zeabur Variables** (and local `.env`, gitignored). **NAMES** (see `.env.example` and `app/config.py` `Env`):

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `TELEGRAM_CHAT_ID`, `DASHBOARD_TOKEN`, `DASHBOARD_PUBLIC_URL`, `PORT`, `TRADING_MODE`, `DATA_DIR`, `ENGINE_AUTOSTART`, `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_WALLET`, `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASSPHRASE`, `FORCE_PAPER`, `PAPER_STARTING_CASH`.

Never write **values** into markdown, git, or chat. Scan diffs for `sk-`, `0x` private keys, bot tokens, sqlite blobs.

## Invariants (positive — current code)

- Sleeve: `strategy_mode=twap`, `taker_fok=True`, `twap_assets=["btc","eth"]`, `twap_horizons=["5m"]`.
- First-cross: `twap_min_lead_bps=6`, `twap_max_lead_bps=40`, `twap_min_ask=0.45`, `twap_max_ask=0.55`, `twap_min_left=120`, `twap_max_left=280`.
- FOK: `fok_delay_ms=250`; retry **same limit** then **at most +1 tick** (`twap_up_tick=0.01`). `twap_no_cheaper=True` — no leftover cheaper-ask chase (`CHEAPER_EPS=0.005`).
- Confirm: CLOB `twap_confirm_px=0.62`; oracle dump `twap_confirm_fair=0.60` (last 90s). Scratch dump floor `scratch_dump_floor=0.22`.
- `apply_strategy_rev` **must not** patch `twap_reverse` or `twap_late_dump` (operator sqlite toggles).
- Hunt pin: Telegram `assets` ∩ `twap_assets` (`slug_allowed` / `twap_hunt_pin`).
- Paper vs live: sqlite `live_trading`; `FORCE_PAPER=1` forces paper. Live CLOB needs keys (`live_keys_ready`). `clamp_live_at_boot` exists — do not flip live flags unless the owner asks.
- Settlement slug allowlist: `{asset}-updown-5m-{unix}` only (`app/twap.py` `parse_window`). Do not redeem other Polymarket bets on the same wallet.
- Do not autodial 6bps, leftover chase, skip 250ms, Binance−PTB, favorite 97–98, or `always_in` (`research/oracle_arb_ship.json` `ship: false`).
- Home Telegram text is short `operator_board` — **must not** contain the substring `FOK`.
- `/health` `twap_funnel`: UTC-day fills/kills/dumps (sqlite) and unique-slug skips (memory). Dump trades store `payload.why`. Do not zip unless asked.
- WS: `WS_MAX_TOKENS=14` on **two** sockets; `clob_ws_connect_kwargs` `max_queue=1024`. Halt backoff 5→10→20→30 min on CLOB `trading is disabled`.

## Do not

- Commit `.env`, `surf.sqlite`, keys, cookies, token files.
- Treat `research/` as production or Zeabur-required.
- Push or merge to `main`.
- Mix this bot’s Telegram token with a Hermes/control bot token.
- Mix **wallet USDC** or **other Polymarket positions** into sleeve PnL (`today_pnl` is sleeve sqlite).
- Restore complement-first, favorite 97–98, or turn `taker_fok` off as default.
- Add a price stop-loss, chase leftover cheaper asks, ship `dump_mid90`, or skip 250ms `fok_delay_ms`.
- Set `live_trading=True`/`False` yourself unless the owner asks.
- Paper-reset unless the owner asks.
- Circumvent geo (Zeabur JP: Gamma/frontend close-only; CLOB API open) — see `app/geo.py`.

## Pointers

For full handoff, current Rev, known pitfalls, next work: **`docs/HANDOFF.md`**.
