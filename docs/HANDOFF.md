# Hermes handoff — Polymarket TWAP/FOK bot

Audience: a **new coding agent** (Hermes on a VPS) plus the Hong Kong Cantonese owner. Hermes will **not** see the Cursor chat that wrote this. Facts below were gathered from **this checkout** (`git`, `app/`, `tests/test_logic.py` actually run). If a live Zeabur fact cannot be proven here, it is marked **UNKNOWN** or **CONJECTURE**.

Always-loaded short law: [`AGENTS.md`](../AGENTS.md). This file is depth.

---

## 1. Status snapshot

| Field | Value |
| --- | --- |
| Snapshot date | 2026-09-07 (UTC) |
| Working branch | `cursor/polymarket-arb-bot-feasibility-5998` |
| Remote | `kitma775-sys/kitma775-sys.-github.io` |
| Code parent SHA (pre-handoff-docs) | `5a11ea56ae37a724ba75ba43853824cd28ec6e6d` — *Retry live redeem when Data API lists the condition as redeemable* |
| After clone | `git rev-parse HEAD` / `git log -1` is the source of truth (this docs commit sits **on top of** `5a11ea5`) |
| Open PR | **PR #1 DRAFT** onto `main`. **Do not merge. Do not push `main`.** |
| Claimed Rev in code | `DEFAULT_SETTINGS["strategy_rev"]=60` (`app/config.py`); boot `apply_strategy_rev` patches sqlite **up to 60** (`app/main.py`) |
| Live Zeabur Rev | **UNKNOWN from this checkout.** Confirm `GET https://surf-arb.zeabur.app/health` field `strategy_rev`. Default public URL in `app/config.py` `load_env` if `DASHBOARD_PUBLIC_URL` empty. |
| Tests (this machine, this parent tree) | `python3 -m pytest tests/test_logic.py -q` → **255 passed**, 0 failed, ~2.6s (2026-09-07) |
| Tracked `*.py` | **59** files (`git ls-files '*.py'`). Untracked `*.py` that should be committed: **none** at gather time. |
| `research/` | Non-runtime. Not required on Zeabur. |

`main` is the old GitHub Pages site (almost no `.py`). **Bot code lives only on this branch.** Zeabur is supposed to deploy **this branch**, not `main`.

---

## 2. What this bot is / is not

**Is:** a Polymarket **5-minute Up/Down** sleeve. Venue = Polymarket CLOB (taker **FAK**; operator UI still says FOK). Settlement oracle = official Chainlink USD RTDS (`crypto_prices_chainlink`), **60s TWAP vs window-open PTB**. Hunt pin = BTC + ETH, slug `{asset}-updown-5m-{unix}` only (`app/twap.py` `parse_window` / `slug_allowed`). Entry band **45–55¢**, lead **6–40 bps**, wait **250ms**, then one-leg FAK. Scratch / reverse / late-dump / oracle dump in `should_scratch`. Telegram + Dashboard are operator UI. Process: `python main.py`.

**Is not:**

- GitHub Pages / `index.html` research site on `main`.
- **`hl-auto-trader`** — **a different bot, different repo, different venue.** Do not copy settings, Telegram tokens, or sleeves across. Owner will hand that repo off in a **separate** Cursor window.
- A complement YES+NO arb bot (library still in `app/hunter.py`; live `strategy_mode=twap` skips it — `test_hunt_twap_mode_skips_complement_hole`).
- A 97–98¢ favorite sniper (library tests only; do not restore as default).
- A 15m / 1H engine (Rev 34+ pins `twap_horizons=["5m"]`; 1H is Binance candles — never this Chainlink engine).
- A wallet PnL bot. `today_pnl` is **sleeve sqlite**. Owner may hold other Polymarket bets on the same account — **do not redeem** those (`parse_window` allowlist).
- Investment advice.

---

## 3. Runtime map

Process entry: `main.py` → `app.main.run()` → sqlite `{DATA_DIR}/surf.sqlite` → `apply_strategy_rev` → `asyncio.gather(uvicorn, engine_loop, run_telegram)`.

`engine_loop` (`app/runtime.py`) gathers four loops: `_universe_loop`, `_ws_loop`, `_chainlink_loop`, `_hunt_loop`.

| Path | Responsibility | Who calls it |
| --- | --- | --- |
| `main.py` | `from app.main import run` | Docker `CMD`, `zbpack.json` entry, local `python main.py` |
| `app/main.py` | Boot: `load_env`, `Store`, `clamp_live_at_boot`, `apply_strategy_rev` (6→60), `_serve` | `main.py` |
| `app/__init__.py` | Package; `__version__ = "0.1.0"` (**stale vs Rev 60** — do not trust this string) | imports |
| `app/config.py` | `DEFAULT_SETTINGS`, `Env`, `load_env`, `live_keys_ready`, `live_switch_blockers`, stake/TP steps | boot, Telegram, Dashboard, risk/TWAP helpers |
| `app/runtime.py` | Engine: hunt, FOK confirm, scratch, redeem, CLOB halt, WS shards (`WS_MAX_TOKENS=14`, two sockets), `Runtime`, `engine_loop`, `operator_board` | `_serve`, Dashboard snapshot |
| `app/twap.py` | Sleeve math: `TwapParams`, `twap_entry_reason`, `should_scratch`, `cheaper_than_first`, `richer_than_up_tick`, hunt pin `hunt_assets` / `slug_allowed` | hunter + runtime |
| `app/hunter.py` | `Setup`, book `walk`, `hunt()` (twap / complement / favorite **library**) | `_scan_markets`, tests |
| `app/broker.py` | `PaperBroker` vs `LiveBroker` CLOB FAK / sell / redeem | `Runtime.broker()` |
| `app/paper_sim.py` | Paper fill = live FAK rules (`simulate_taker`, `fak_one`); maker never instant-fills | broker + FOK confirm |
| `app/store.py` | sqlite: `kv` (settings + PTB), `scans`, `trades`, `events`, `inventory`, `resting` | everyone persistent |
| `app/chainlink.py` | RTDS `wss://ws-live-data.polymarket.com` topic `crypto_prices_chainlink`; PTB + 60s TWAP | `_chainlink_loop` |
| `app/ws_books.py` | CLOB market WS `wss://ws-subscriptions-clob.polymarket.com/ws/market`; in-memory `BookCache` | `_ws_loop` |
| `app/telegram_ui.py` | Owner bot UI; `TOGGLES` includes `twap_reverse`, `twap_late_dump`; `run_telegram` | `_serve` |
| `app/dashboard.py` | FastAPI: `/health` public; `/` and `/api/state?t=` gated; `/api/action/{name}` | uvicorn |
| `app/dashboard.html` | Neon operator wall (not a `.py` module; served by `dashboard.py`) | `GET /` |
| `app/wall.py` | `operator_wall`, `performance_today`; Telegram home stays short `operator_board` | runtime snapshot + Telegram |
| `app/markets.py` | `MarketData`: Gamma/CLOB HTTP, geoblock | universe + FOK HTTP books |
| `app/universe.py` | Event picking (`pick_markets`, 5M tags, asset aliases) | `MarketData.live_events` |
| `app/fees.py` | Official taker `shares × feeRate × p × (1−p)`; crypto default `0.07` | hunt, paper, TWAP |
| `app/risk.py` | `approve()` kill/pause/circuit/band/window/stack | `_scan_markets` |
| `app/rescue.py` | Naked-leg complement rescue + `is_redeemable_market` (wait official 0/1) | `_rescue_naked`, `_redeem_resolved` |
| `app/geo.py` | JP/IE/NL frontend close-only vs US/GB/SG API close-only | `live_switch_blockers` |
| `app/replay.py` | Offline tape replay helper (not Zeabur-required) | `research/` / tests |

**Loops (mental model):**

1. **Universe** — HTTP Gamma events into `rt.universe`; persist PTB keys `ptb:{slug}`.
2. **CLOB WS** — two sockets × up to 8 tokens (cap 14); subscribe/unsubscribe **in place** (Rev 57); `initial_dump=False`.
3. **Chainlink** — one RTDS socket per symbol; recycle if age >20s (Rev 50).
4. **Hunt** — skip if `killed` or not `engine_running`; `_process_resting` then `_scan_markets` (FOK + scratch + redeem).

Telegram is **not** the trading loop. Dashboard **does not** hunt. Both read/write sqlite settings.

---

## 4. Order / risk machine

Implemented in `app/twap.py` + `app/runtime.py` `_confirm_twap` / `_fok_confirm` / `_scratch_twap` / `_redeem_resolved`. Library complement/favorite paths still exist; **live default is TWAP-only**.

### Entry (first-cross)

- Hunt = Telegram `assets` ∩ `twap_assets` (`hunt_assets`). Pin: `twap_assets=["btc","eth"]`, `twap_horizons=["5m"]`.
- Skip reasons: `twap_entry_reason` (`twap_band`, `twap_lead`, `twap_no_ptb`, `future_listing`, `twap_no_cheaper`, …).
- Band: `twap_min_ask`/`twap_max_ask` via `twap_min_price=0.45`, `twap_max_price=0.55`.
- Lead: `twap_min_lead_bps=6`, `twap_max_lead_bps=40` (wild dump if \|lead\| >40).
- Time: `twap_min_left=120`, `twap_max_left=280`.
- Lock the **first** 6bps 45–55 ask (`_lock_twap_first_px`). Later cheaper leftover asks die: `twap_no_cheaper=True`, `CHEAPER_EPS=0.005`.
- Independent clocks (Rev 55): BTC and ETH may both take the **same** 5m unix (`twap_conflict_open` is per-asset). Same coin still one horizon.

### FOK / FAK

- Flag: `taker_fok=True`, delay `fok_delay_ms=250`.
- Live: after 250ms confirm, **`rtt = 0`** — skip second CLOB RTT walk (Rev 56). Paper still uses `clob_rtt_ms=150` then re-walk with `allow_requote=False`.
- Rev 60: if first FAK misses, requote **same limit** then **at most +1¢** (`twap_up_tick=0.01`) still ≤55¢ (`richer_than_up_tick`). Unmatched live FAK reconfirms with `delay_ms=0` (`_maybe_retry_unmatched_twap`).
- **Never** chase leftover cheaper asks. **Never** skip the 250ms delay as a “latency arb”.
- Live order: CLOB **FAK** (`app/broker.py` `LiveBroker.execute_pair`). Paper: `paper_execute` / `simulate_taker` same clip rules.

### Scratch / dump / TP

`should_scratch` (`app/twap.py`):

- Reverse ON (`twap_reverse`): skip BM better/weak/flip **and** TP; hold to settle except wild lead. Placeholder net so FOK is not killed by `non_positive_net` (`reverse_placeholder_net`, Rev 52).
- Late-dump ON (`twap_late_dump`, **Rev 61 default True**): skip BM better/weak/flip **and** TP; keep last-90s unconfirmed + oracle. Reverse still wins if both on.
- Else: TP at `twap_tp_bid=0.87` (Telegram steps 0/80/85/87/90/95). BM better/weak/flip. Unconfirmed: `left < twap_confirm_left` (90) and same-side high-water **< `twap_confirm_px` (0.62)**. Oracle (Rev 59): last 90s if BM `fair_p < twap_confirm_fair` (0.60) even after CLOB printed 62¢.
- Dump floor `twap_scratch_dump_floor=0.22`. Last 90s: HTTP books if WS cache **> `twap_scratch_hot_ms` (2000)**; rescore `twap_rescore_hot_seconds=3`. Keep last-8s (`scratch_left_min`). **Do not** ship `dump_mid90` or cut the 22¢ floor (commit `3d65fed` + comments).
- **No price stop-loss** (`twap_scratch_adverse` default 0). Do not add one.

### Kill / pause / circuit / paper vs live

- Pause: sqlite `engine_running=False` (hunt loop sleeps; redeem still runs).
- Kill: `killed=True`, `engine_running=False`, `live_trading=False`; cancel resting + live opens (`dashboard.py` `kill`, Telegram equivalent).
- Circuit: `daily_loss_limit_usd` vs **sleeve** `today_pnl` (`Runtime.circuit_tripped`). Dashboard/Telegram can `clear_circuit` (resets **today PnL counter**, not cash).
- Paper vs live: sqlite `live_trading`; env `FORCE_PAPER` forces paper; need `POLYMARKET_PRIVATE_KEY` (`live_keys_ready`). `clamp_live_at_boot` **never auto-enables live**; it only forces paper if `FORCE_PAPER` or no key. Telegram two-step arms live. **Do not flip `live_trading` unless the owner asks.**
- `TRADING_MODE=paper` is the env default until Telegram confirm; a restart must **not** wipe that confirm (`clamp_live_at_boot` docstring).
- Paper leftover vs live inventory are isolated (Rev 37–41). Do not dump paper leftovers through the CLOB.

### Redeem

- `auto_redeem=True`. Wait official Gamma 0/1 (`is_redeemable_market`) — **not** the false 50/50 mid at clock-zero.
- Live: `LiveBroker.redeem` / Data API `list_redeemable`.
- **`5a11ea5`:** after `redeem_wait`, **retry** `redeem` if Data API still lists that `condition_id`. Older code skipped because cid was already in `seen`. **Whether Zeabur is running this SHA is UNKNOWN** until `/health` + inventory prove it. Do not zip-deploy unless the owner asks (booking losers will drop `today_pnl`).

### Fees

Buy cost uses official taker fee `shares × 0.07 × p × (1−p)` (`app/fees.py`). Winner redeem = shares × $1. Do not invent a different fee.

---

## 5. Config surface

**Env NAMES** (values only in Zeabur Variables / local `.env`; see `.env.example`):

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `TELEGRAM_CHAT_ID`, `DASHBOARD_TOKEN`, `DASHBOARD_PUBLIC_URL`, `PORT`, `TRADING_MODE`, `DATA_DIR`, `ENGINE_AUTOSTART`, `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_WALLET`, `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASSPHRASE`, `FORCE_PAPER`, `PAPER_STARTING_CASH`.

`TELEGRAM_CHAT_ID` is an alias for `TELEGRAM_OWNER_ID` (`load_env`). CLOB L2 creds are optional (SDK can derive from the private key). `live_keys_ready` only requires the private key.

**Sqlite** (`{DATA_DIR}/surf.sqlite`, live volume `/data/surf.sqlite`):

- Settings blob in `kv` (merged with `DEFAULT_SETTINGS`). Operator stake is `max_usd_per_trade` (Telegram steps start at **$3** because 5 shares in 45–55¢ cannot fill at $2 — `TRADE_USD_STEPS` comment). **Do not assume $5** just because README historical rev notes say $5.
- PTB persistence: keys `ptb:{slug}`.
- Tables: `kv`, `scans`, `trades`, `events`, `inventory`, `resting`.

**Telegram toggles** (`app/telegram_ui.py` `TOGGLES`): `auto_execute`, `auto_redeem`, `notify_signals`, `notify_rejects`, `taker_fok`, `twap_reverse`, `twap_late_dump`.

**`apply_strategy_rev`:** one-way sqlite patches up to 61. **Must not** patch `twap_reverse`. Rev 61 sets `twap_late_dump=True` once (owner confirmed); do not keep rewriting it on later revs. Rev 60 set `twap_up_tick=0.01`.

**Dashboard:** query param is **`t=`** (not `token=`). `/health` is public (no token). `twap_funnel` on `/health` is UTC-day fills/kills/dumps + unique-slug skips (skip mix resets on boot). `/api/state?t=` is gated.

---

## 6. Deploy

- **Dockerfile:** Python 3.11-slim, `ENV PORT=8080 DATA_DIR=/data`, `CMD ["python", "main.py"]`.
- **zbpack.json:** `"python": { "version": "3.11", "entry": "main.py" }`.
- Zeabur: bind **this branch** (zip or git branch). Volume **must** mount `/data` or every redeploy wipes sqlite.
- **Redeploy wipes:** container filesystem, code copy, env only if you change Variables. **Does not wipe** `/data/surf.sqlite` if the volume is attached.
- **Does not auto-merge** to `main`. Pushing this branch ≠ GitHub Pages.
- Docs-only commits do **not** require a zip deploy.
- Code-change live: commit + push **this branch**, then zip/branch deploy **if the owner wants live**. Redeem-retry (`5a11ea5`) is one such pending live question.

Geo: Zeabur JP is frontend close-only; CLOB API is open (`app/geo.py`, `test_geo_japan_website_block_api_open`). **Do not** build geo-circumvention. US/GB/SG are API close-only (`live_switch_blockers` `geo_close_only`).

CLOB halt: `is_clob_unavailable` / `clob_halt_seconds` backoff **5→10→20→30 min** (Rev 58). Stop posting FAK every 5m during a matching pause.

---

## 7. Tests

```bash
python3 -m pytest tests/test_logic.py -q
# or: pytest -q
```

**This checkout (parent `5a11ea5`):** **255 passed**, 0 failed.

Coverage (not exhaustive): fees/EV, hunt TWAP vs complement vs favorite library, FOK/FAK clip, first-cross no-cheaper / 1-tick up, paper ledger, geo JP vs US, Telegram `home_text` **must not contain `FOK`**, redeem_wait once-then-empty **and** retry-when-redeemable, dashboard paper actions, Chainlink helpers, WS sub plan, operator board chips.

**Known gaps:** no live CLOB integration test; no Zeabur deploy test; `research/*.py` is not in pytest; `__version__` unused; websocket 1013 is environment-dependent.

---

## 8. Change history that still matters

Compressed from `git log --oneline -30`. Skip chat anecdotes not in git.

| SHA | Decision | Why | Still true? |
| --- | --- | --- | --- |
| `5a11ea5` | Retry live redeem when Data API lists cid redeemable | `redeem_wait` + `seen` skipped a second `redeemPositions` | **Yes in git.** Live zip **UNKNOWN**. |
| `f10a3d7` | Oracle-arb delay **is** the 6bps sleeve; `ship: false` | 8828 windows; late leftover ≠ arb; do not skip 250ms | **Yes** (`research/oracle_arb_ship.json`) |
| `6ea4c39` | Telegram `twap_late_dump`, **default off**; do not patch in `apply_strategy_rev` | BM better sells winners on tape; operator toggle | **Yes** |
| `99d8609` / `e379272` | Smarter scratch / every-5m / always-in research: **do not autodial** | High WR and +EV do not cohabit on this sleeve | **Yes** unless owner confirms a new JSON `ship: true` |
| `6aa5f9f` | Live TG fills include Polymarket event URL | Operator deep-link | **Yes** (`polymarket_event_url`) |
| `0f93965` / `b80f21d` / `4a8389c` | Sparse fills = 6bps band + leftover, **not a halt**; 5.5bps tape still **do not autodial** | Weekend/band/lead skips | **Yes** as research conclusion |
| `81e3def` | Do not open two more 5m coins on frozen sleeve | Slot/tape cost | **Yes** (`twap_assets` still btc+eth) |
| `3d65fed` | Last-90s fresh books; keep 22¢ floor | Stale WS cache vs crash tape | **Yes** |
| `dd566c7` / `80fe384` | Higher-TF / online-learn **do not beat** frozen sleeve | Research | **Yes** until a new ship JSON |
| `2b0046e` / `334ab9c` | **Rev 60:** FOK +1 tick; unmatched reconfirm; home board **FOK-free** | Leftover mix still kills | **Yes** |
| `0989aae` | Do not relax 6bps/45–55 to mint fills | −EV leftover | **Yes** |
| `a4c1a4a` / `7fa54b4` / `a3959e9` | **Rev 59** oracle dump fair below 0.60; wall chips; indent fix | CLOB print ≠ settlement TWAP | **Yes** |
| `8e9ffc5` | Do not flip first-cross; CLOB pull is confirmation | Research | **Yes** |
| `88fb94e` | TG CLOB status → Polymarket incidents | Ops | **Yes** |
| `4ef9751` | **Rev 58** halt backoff 5→10→20→30m | Stop FAK spam during pause | **Yes** |
| `c3b3f08` | **Rev 57** prewarm subscribe/unsubscribe **without reconnect** | Reconnect blanks books, misses 45–55 flash | **Yes** |
| `5f21229` | **Rev 56** live skip second RTT walk | Rev 10 already forbade a second wait | **Yes** |
| `82e4e5b` | **Rev 55** per-asset BTC/ETH clocks | Independent tape | **Yes** |
| `6476d35` / `a2ac963` / `9ac1ccb` | **Rev 54** first-cross + 90s unconfirmed dump; TG coin picker still only **buys** BTC+ETH | Hunt pin ∩ | **Yes** |
| `b08c491` | Wounded 20–30¢ bounce: **do not ship** | Research | **Yes** |
| `c747857` | **Rev 53** TP 87¢ + TG steps | No price SL | **Yes** |

Older README rev notes (23–52) are **history**. Live law is Rev 61 + sqlite operator toggles.

---

## 9. Known pitfalls

Only if evidenced. Conjecture labelled.

1. **`apply_strategy_rev` wiping operator toggles** — comments + `6ea4c39`. Never add `twap_reverse` to a rev patch. Rev 61 is the one-time `twap_late_dump=True` owner ship. Docstring patches to 61.
2. **Leftover cheaper chase** after first-cross — `twap_no_cheaper`, `CHEAPER_EPS`, Rev 54/56/60 tests. Looks like “better fill”; it is the 97–98 cousin.
3. **Binance/USDT minus Gamma PTB ≈ 9 bps basis** — `app/twap.py` module docstring. Mixed-oracle −EV.
4. **False 50/50 mid redeem** — `is_redeemable_market` docstring; Rev 19. Wait official 0/1.
5. **CLOB WS 1013 slow consumer** — `clob_ws_connect_kwargs` (`max_queue=1024`), `WS_MAX_TOKENS=14`, `initial_dump=False`. **CONJECTURE:** 1013 can still happen on JP; do not “fix” by skipping 250ms or chasing leftover.
6. **Reconnect + `initial_dump=false` blanks books** — Rev 57 commit. Prewarm must stay in-place subscribe/unsubscribe.
7. **Redeem wait never retried until `5a11ea5`** — commit + `test_redeem_wait_retries_when_data_api_says_redeemable`. Live may still be on an older zip (**UNKNOWN**).
8. **Home text must not contain `FOK`** — `test_home_text_is_short_operator_board`. Wall/chips can talk FOK; Telegram home cannot.
9. **Wallet USDC ≠ sleeve PnL** — `operator_board` live shows CLOB USDC; `today_pnl` is sqlite sleeve. Owner also buys other bets on the same Polymarket account.
10. **README vs live stake** — README historical bullets still say `$5`. Code default `max_usd_per_trade=5.0` but **operator sqlite wins**. Confirm `/health` `max_usd_per_trade`. **CONJECTURE** from ops chats: last live stake was $3 — verify live, do not write a number into git as fact.
11. **Sparse / no-trade hours** — research commits say band+lead, not halt. **CONJECTURE** for any specific day: open `/health` `twap_skips` / `twap_gate` before “fixing” hunt.
12. **`FORCE_PAPER` / missing key** silently keeps paper — `Runtime.mode()`. Easy to think live is on when sqlite `live_trading` is true but env is locked.
13. **Do not mix Hermes control-bot token with this bot’s `TELEGRAM_BOT_TOKEN`.** Two bots, two tokens. Values stay in Zeabur / VPS env, never in markdown.

---

## 10. Owner operating contract

Platform cutover target: **2026-09-12** (Cursor Pro ends; Hermes on VPS takes this branch).

- Clone **this branch only**: `git fetch origin cursor/polymarket-arb-bot-feasibility-5998 && git checkout cursor/polymarket-arb-bot-feasibility-5998`.
- Hermes **edits on the VPS clone**, tests, **pushes the same branch**. Live secrets **stay in Zeabur Variables**. Do not copy `.env` into the git repo or into Hermes memory files.
- **Never merge PR #1** unless the owner explicitly says to replace GitHub Pages with the bot on `main`.
- **Never push `main`.**
- **Never mix** this bot’s Telegram token with a Hermes / control Telegram token.
- **`hl-auto-trader` is a different repo.** Same owner, same prompt family, **not this tree**.
- Do not set `live_trading` True/False yourself; do not paper-reset; do not zip-deploy unless asked.
- Do not restore complement-first, favorite 97–98, or turn `taker_fok` off as default.
- Do not autodial 6bps, leftover chase, skip 250ms, Binance−PTB, `always_in`, extra coins, or `research/oracle_arb_ship.json` (`ship: false`, `pick: null`).
- Research scripts may be run offline; they are **not** production. Do not import `research/` into `app/` without owner confirmation.
- Co-author commits as the owner prefers; do not put secrets in commit messages.

---

## 11. Next work

Concrete tickets a new agent could pick. Empty cells would mean none; these are real.

1. **Live redeem retry zip** — File: already in `app/runtime.py` `_redeem_resolved` (`5a11ea5`). Acceptance: owner asks; Zeabur running this SHA; Data API redeemable 5m losers leave `inventory`; `/health` still Rev 60. Warn: `today_pnl` will book the losses.
2. **Confirm live zip SHA vs git** — File: ops only (`/health`). Acceptance: write the live SHA/Rev into a *new* handoff snapshot; do not guess.
3. **Stale comment** — File: `app/main.py` `apply_strategy_rev` docstring “rev 29”. Acceptance: docstring says patches through 60; tests unchanged.
4. **Stale package version** — File: `app/__init__.py`. Acceptance: either document “unused” or set a non-misleading string; do not drive logic from it.
5. **README identity** — File: `README.md` top. Acceptance: opening paragraph matches Rev 60 (TWAP 5m BTC+ETH, not “paper default $5” as if live). Historical rev list can stay.
6. **WS 1013 watch** — File: `app/runtime.py` `_ws_socket`. Acceptance: only change if new logs show queue overflow / reconnect storm; **no** 250ms skip, **no** leftover chase, **no** `initial_dump=True`.
7. **Do not ship oracle-arb / always_in / easy_entry / two_alts** — Files: `research/*_ship.json`. `keep_late_dump` **shipped Rev 61** after owner confirm (`research/rev61_ship.json`).
8. **Operator late-dump vs default** — File: sqlite / Telegram `twap_late_dump`. Acceptance: code default **True** (Rev 61); Telegram can still turn it off. Do not rewrite the toggle on rev ≥62.
9. **Scratch research already done** — File: `research/smart_scratch.py` / JSON. Acceptance: no engine change unless a new holdout beats frozen sleeve **and** owner confirms.
10. **Fill-rate temptation** — File: `twap_min_lead_bps` / band. Acceptance: `0989aae` / `4a8389c` still hold; any relaxation needs a new research JSON + owner yes.
11. **Non-5m inventory on the same wallet** — File: `parse_window` / redeem allowlist. Acceptance: bot still ignores weather/EPL/15m/1H.
12. **Hermes clone drill** — File: this handoff. Acceptance: section 12 checklist all green on the VPS.

---

## 12. Verification checklist (first clone)

Run in order. Stop if any fail.

```bash
git fetch origin cursor/polymarket-arb-bot-feasibility-5998
git checkout cursor/polymarket-arb-bot-feasibility-5998
git branch --show-current   # must print cursor/polymarket-arb-bot-feasibility-5998
git status                  # should be clean after clone
test "$(git branch --show-current)" != "main"
git log -1 --oneline
git ls-files '*.py' | wc -l   # expect 59 tracked py (plus AGENTS/HANDOFF which are not py)
test -f AGENTS.md && test -f docs/HANDOFF.md
test ! -f .env
git ls-files | grep -E '\.env$|surf\.sqlite|id_rsa|\.pem$' && echo FAIL_SECRETS || echo no_secret_files
# Secret VALUES scan (names in docs are OK):
grep -RInE 'BEGIN (RSA |OPENSSH )?PRIVATE|sk-[A-Za-z0-9]{20,}|[0-9]{8,}:[A-Za-z0-9_-]{30,}' AGENTS.md docs/HANDOFF.md && echo FAIL || echo docs_clean
python3 -m pip install -r requirements.txt   # or venv first
python3 -m pytest tests/test_logic.py -q     # expect 255 passed on parent 5a11ea5; handoff docs should not change this
```

Dashboard (optional, needs token in env, **do not paste it**): `GET /health` public; `GET /api/state?t=<DASHBOARD_TOKEN>`.

If tests fail, **do not** “fix” by relaxing 6bps / chasing leftover / skipping 250ms. Open the failing test name first.

---

*End of handoff. Update section 1 SHA/test counts when you cut a new snapshot. Keep secrets out of this file.*
