"""Safe BTC / ETH / SOL / XRP 5m · 15m · 1h Arbitrage Suite (``crypto_short``).

Implements four strictly non-gambling, mathematically grounded mechanisms:

1. **Post-Close 100% Zero-Risk Oracle Lock (``post_close_lock``)**:
   * Once ``now >= T_end`` (within ``postclose_window_s`` while CLOB still accepts
     orders), compares the exact settling Chainlink 60s-TWAP ``finalPrice`` (for
     ``5m``/``15m``) or closed Binance ``1h`` candle ``Close`` (for ``1h``) against
     the immutable ``priceToBeat`` / ``Open``.
   * Sweeps stale asks as Taker AND exposes the 100%-decided winner to
     ``MakerArbManager`` (``maker_crypto_short``) to post 0-fee ``postOnly`` bids
     at ``<= 0.990`` (+1.01% net + 20% Maker Rebate, redeemed in ~60–90 seconds).

2. **Exact-Integral Late-Window 60s-TWAP Lock (``integral_twap_lock``)**:
   * During the final ``[min_left_s, max_left_s]`` seconds of a ``5m`` or ``15m``
     window, integrates the **already-elapsed** ``(60 - r)`` seconds of 1s Chainlink
     prints and stress-tests the remaining ``r`` seconds against an instantaneous
     sustained ``shock_bps`` flash crash/spike (default ``90 bps`` > 2x 7-day p99.9
     1-minute BTC move), plus Chainlink & Binance spot agreement.

3. **5m <-> 15m Same-Chainlink-Stream Synchronized Lock (``sync_5m_15m``)**:
   * During the 3rd 5m sub-window ``[T0 + 600, T0 + 900]``, both the ``15m`` market
     (benchmark ``S_15``) and ``5m`` market (benchmark ``S_5``) settle at the exact
     same millisecond ``T0 + 900`` on the exact same Chainlink ``twap_sixty`` print:
     - ``S_5 > S_15`` => ``Down_5 + Up_15`` pays ``>= $1.00`` (``$2.00`` on ``[S_15, S_5)``)
     - ``S_5 < S_15`` => ``Up_5 + Down_15`` pays ``>= $1.00`` (``$2.00`` on ``[S_5, S_15)``)

4. **1h Up/Down <-> 1h Above Same-Binance-Candle Anchor Lock (``anchor_1h``)**:
   * Pairs ``<Coin> Up or Down - <H_start> ET`` (begins at ``H_start``, Binance 1h
     ``Close >= Open``) with ``<Coin> above ___ on <H_start + 1h> ET?`` (ends at
     ``H_start + 1h``, exact same Binance 1h ``Close > K``):
     - ``K < Open``  => ``Down_1h + YES(above K)`` pays ``>= $1.00`` (``$2.00`` on ``(K, Open)``)
     - ``K >= Open`` => ``Up_1h + NO(above K)``   pays ``>= $1.00`` (``$2.00`` on ``[Open, K]``)
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from polyarb.config import CryptoShortConfig
from polyarb.data.binance import ET_ZONE, BinanceFeed
from polyarb.data.rtds import SHORT_ASSETS, ChainlinkRtdsFeed
from polyarb.models import Event, Market, Opportunity, Outcome, Settlement
from polyarb.sizing import LegBook, floor_shares, size_basket, unit_cost
from polyarb.strategies.base import Books, Target, buy_leg, utc

_SLUG_5M_15M_RE = re.compile(r"^([a-z0-9]+)-updown-(5m|15m)-(\d{9,12})$", re.I)
_SLUG_1H_UD_RE = re.compile(r"^(bitcoin|ethereum|solana|xrp)-up-or-down-.+-(\d{1,2}(?:am|pm))-et$", re.I)
_HOURLY_ABOVE_RE = re.compile(
    r"^(?:Will\s+)?([A-Za-z0-9]+)\s+(?:be\s+)?above\s+___\s+on\s+[A-Za-z]+\s+\d{1,2},\s*\d{1,2}\s*(?:AM|PM)\s*ET\??$",
    re.I,
)
_NUM_RE = re.compile(r"([\d,]+(?:\.\d+)?)")
_FULL_TO_ASSET = {v["full"]: k for k, v in SHORT_ASSETS.items()}


def _parse_num(text: str) -> float | None:
    m = _NUM_RE.search(text.replace("$", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _side_label(market: Market, outcome: Outcome) -> str:
    if market.is_up_down:
        return "UP" if outcome is Outcome.YES else "DOWN"
    return outcome.value


@dataclass(frozen=True, slots=True)
class ShortSingleSpec:
    kind: str       # "twap_single" | "binance_1h_updown"
    asset: str      # "btc" | "eth" | "sol" | "xrp"
    duration: str   # "5m" | "15m" | "1h"
    start_ts: int
    end_ts: int
    market: Market


@dataclass(frozen=True, slots=True)
class Sync5m15mSpec:
    asset: str
    start_15m: int
    start_5m: int
    end_ts: int
    ev_15m: Event
    ev_5m: Event
    mkt_15m: Market
    mkt_5m: Market


@dataclass(frozen=True, slots=True)
class Anchor1hSpec:
    asset: str
    end_ts: int
    strike: float
    ev_updown: Event
    ev_above: Event
    mkt_updown: Market
    mkt_above: Market


def parse_short_single(ev: Event) -> ShortSingleSpec | None:
    markets = ev.tradeable_markets
    if len(markets) != 1:
        return None
    mkt = markets[0]
    if not mkt.is_up_down:
        return None

    m = _SLUG_5M_15M_RE.match(ev.slug.strip())
    if m and (ev.oracle_sig or "").startswith("chainlink_twap60"):
        asset = m.group(1).lower()
        dur = m.group(2).lower()
        start_ts = int(m.group(3))
        end_ts = start_ts + (300 if dur == "5m" else 900)
        if asset in SHORT_ASSETS:
            return ShortSingleSpec("twap_single", asset, dur, start_ts, end_ts, mkt)

    m2 = _SLUG_1H_UD_RE.match(ev.slug.strip())
    if m2 and (ev.oracle_sig or "").startswith("binance_1h"):
        asset = _FULL_TO_ASSET.get(m2.group(1).lower(), "")
        end_dt = mkt.end_date or ev.end_date
        if asset and end_dt is not None:
            end_ts = int(end_dt.timestamp())
            start_dt = ev.start_time or mkt.event_start_time
            start_ts = int(start_dt.timestamp()) if start_dt is not None else (end_ts - 3600)
            return ShortSingleSpec("binance_1h_updown", asset, "1h", start_ts, end_ts, mkt)

    return None


class CryptoShortArb:
    name = "crypto_short"

    def __init__(
        self,
        cfg: CryptoShortConfig,
        rtds_feed: ChainlinkRtdsFeed | None = None,
        binance_feed: BinanceFeed | None = None,
        max_trade_usd: float = math.inf,
        flip_ask_price: float = 0.995,
        flip_min_instant_bid: float = 0.990,
        flip_min_profit_usd: float = 0.05,
    ) -> None:
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.rtds_feed = rtds_feed
        self.binance_feed = binance_feed
        self.max_trade_usd = max_trade_usd
        self.flip_ask_price = flip_ask_price
        self.flip_min_instant_bid = flip_min_instant_bid
        self.flip_min_profit_usd = flip_min_profit_usd

    def targets(self, events: Iterable[Event]) -> list[Target]:
        ev_list = list(events)
        out: list[Target] = []

        singles_5m: dict[tuple[str, int], tuple[Event, ShortSingleSpec]] = {}
        singles_15m: dict[tuple[str, int], tuple[Event, ShortSingleSpec]] = {}
        singles_1h: dict[tuple[str, int], tuple[Event, ShortSingleSpec]] = {}
        above_1h: dict[tuple[str, int], Event] = {}

        for ev in ev_list:
            sp = parse_short_single(ev)
            if sp is not None:
                out.append(Target(f"cshort:{ev.id}", self.name, ev, (sp.market.yes_token,), sp))
                if sp.duration == "5m":
                    singles_5m[(sp.asset, sp.end_ts)] = (ev, sp)
                elif sp.duration == "15m":
                    singles_15m[(sp.asset, sp.end_ts)] = (ev, sp)
                elif sp.duration == "1h":
                    singles_1h[(sp.asset, sp.end_ts)] = (ev, sp)
                continue

            if (ev.oracle_sig or "").startswith("binance_1h:") and (m := _HOURLY_ABOVE_RE.match(ev.title.strip())):
                raw_coin = m.group(1).lower()
                asset = SHORT_ASSETS.get(raw_coin, {}).get("cl", "").split("/")[0] or _FULL_TO_ASSET.get(raw_coin, "")
                if asset and ev.end_date is not None:
                    above_1h[(asset, int(ev.end_date.timestamp()))] = ev

        if not self.cfg.sync_pair_enabled:
            return out

        # Pair 5m (3rd sub-window) <-> 15m sharing exact same (asset, end_ts)
        for key, (ev15, sp15) in singles_15m.items():
            if key not in singles_5m:
                continue
            ev5, sp5 = singles_5m[key]
            if sp5.start_ts == sp15.start_ts + 600:
                pair_spec = Sync5m15mSpec(
                    asset=sp15.asset,
                    start_15m=sp15.start_ts,
                    start_5m=sp5.start_ts,
                    end_ts=sp15.end_ts,
                    ev_15m=ev15,
                    ev_5m=ev5,
                    mkt_15m=sp15.market,
                    mkt_5m=sp5.market,
                )
                out.append(
                    Target(
                        f"csync:{ev5.id}:{ev15.id}",
                        self.name,
                        ev5,
                        (sp5.market.yes_token, sp15.market.yes_token),
                        pair_spec,
                    )
                )

        # Pair 1h Up/Down (begins H_start, ends H_end) <-> 1h Above ___ (ends H_end)
        for key, (ev_ud, sp_ud) in singles_1h.items():
            ev_ab = above_1h.get(key)
            if ev_ab is None:
                continue
            strike_mkts: list[tuple[float, Market]] = []
            for mkt in ev_ab.tradeable_markets:
                if not mkt.is_yes_no:
                    continue
                k = _parse_num(mkt.group_title or mkt.question)
                if k is not None:
                    strike_mkts.append((k, mkt))
            strike_mkts.sort(key=lambda x: x[0])
            for k, mkt in strike_mkts:
                anc = Anchor1hSpec(
                    asset=sp_ud.asset,
                    end_ts=sp_ud.end_ts,
                    strike=k,
                    ev_updown=ev_ud,
                    ev_above=ev_ab,
                    mkt_updown=sp_ud.market,
                    mkt_above=mkt,
                )
                out.append(
                    Target(
                        f"canchor:{ev_ud.id}:{mkt.id}",
                        self.name,
                        ev_ud,
                        (sp_ud.market.yes_token, mkt.yes_token),
                        anc,
                    )
                )

        return out

    # ------------------------------------------------------------------ benchmarks & lock helpers

    def resolve_open_benchmark(self, ev: Event, spec: ShortSingleSpec) -> float | None:
        if ev.price_to_beat is not None and ev.price_to_beat > 0:
            return ev.price_to_beat
        if spec.kind == "twap_single" and self.rtds_feed is not None:
            return self.rtds_feed.twap_at(spec.asset, spec.start_ts, tolerance_s=0)
        if spec.kind == "binance_1h_updown" and self.binance_feed is not None:
            end_key = datetime.fromtimestamp(spec.end_ts, ET_ZONE).strftime("%Y-%m-%d %H:00")
            return self.binance_feed.hourly_open(SHORT_ASSETS[spec.asset]["binance"], end_key)
        return None

    def single_lock_state(
        self,
        ev: Event,
        spec: ShortSingleSpec,
        now_s: float,
        shock_bps_override: float | None = None,
    ) -> tuple[Outcome, str, bool] | None:
        """Return ``(Outcome, proof, is_post_close)`` when decided, else None."""
        cfg = self.cfg
        open_px = self.resolve_open_benchmark(ev, spec)
        if open_px is None or open_px <= 0:
            return None

        # 1. Post-close 100% deterministic lock: 0 <= now_s - end_ts <= postclose_window_s
        if 0.0 <= (now_s - spec.end_ts) <= cfg.postclose_window_s:
            if spec.kind == "twap_single":
                end_px = ev.final_price
                from_gamma = end_px is not None and end_px > 0
                if not from_gamma and self.rtds_feed is not None:
                    end_px = self.rtds_feed.twap_at(spec.asset, spec.end_ts, tolerance_s=0)
                if end_px is None or end_px <= 0:
                    return None
                if not from_gamma and abs(end_px - open_px) / open_px < 0.05 / 10_000.0:
                    return None
                out = Outcome.YES if end_px >= open_px else Outcome.NO
                return out, f"post-close TWAP={end_px:.2f} vs open={open_px:.2f}", True

            if spec.kind == "binance_1h_updown":
                close_px = ev.final_price
                from_gamma = close_px is not None and close_px > 0
                if not from_gamma and self.binance_feed is not None:
                    end_key = datetime.fromtimestamp(spec.end_ts, ET_ZONE).strftime("%Y-%m-%d %H:00")
                    close_px = self.binance_feed.hourly_close(SHORT_ASSETS[spec.asset]["binance"], end_key)
                if close_px is None or close_px <= 0:
                    return None
                if not from_gamma and abs(close_px - open_px) / open_px < 0.05 / 10_000.0:
                    return None
                out = Outcome.YES if close_px >= open_px else Outcome.NO
                return out, f"1h closed C={close_px:.2f} vs O={open_px:.2f}", True

        # 2. Exact-integral pre-close 60s-TWAP lock in final [min_left_s, max_left_s] seconds
        rem_s = spec.end_ts - now_s
        if (
            spec.kind == "twap_single"
            and cfg.preclose_lock_enabled
            and self.rtds_feed is not None
            and cfg.min_left_s <= rem_s <= cfg.max_left_s
        ):
            sh = cfg.shock_bps if shock_bps_override is None else shock_bps_override
            res = self.rtds_feed.integral_locked_outcome(spec.asset, open_px, spec.end_ts, now_s, shock_bps=sh)
            if res is not None:
                return res[0], res[1], False

        return None

    def active_pair_legs(
        self, payload: Sync5m15mSpec | Anchor1hSpec, now_s: float
    ) -> tuple[list[tuple[Market, Outcome]], datetime, str, list[str]] | None:
        """Resolve dynamic 2-leg guaranteed-$1 pair specs for ``sync_5m_15m`` and ``anchor_1h``."""
        if isinstance(payload, Sync5m15mSpec):
            if not (payload.start_5m <= now_s < payload.end_ts - 2.0):
                return None
            sp15 = ShortSingleSpec("twap_single", payload.asset, "15m", payload.start_15m, payload.end_ts, payload.mkt_15m)
            sp5 = ShortSingleSpec("twap_single", payload.asset, "5m", payload.start_5m, payload.end_ts, payload.mkt_5m)
            s15 = self.resolve_open_benchmark(payload.ev_15m, sp15)
            s5 = self.resolve_open_benchmark(payload.ev_5m, sp5)
            if s15 is None or s5 is None or abs(s5 - s15) / s15 < 0.20 / 10_000.0:
                return None
            resolves_at = datetime.fromtimestamp(payload.end_ts + 90, tz=utc(now_s).tzinfo)
            if s5 > s15:
                legs = [(payload.mkt_5m, Outcome.NO), (payload.mkt_15m, Outcome.YES)]
                title = f"{payload.asset.upper()} 5m(DN<{s5:.1f}) ⇄ 15m(UP≥{s15:.1f})"
            else:
                legs = [(payload.mkt_5m, Outcome.YES), (payload.mkt_15m, Outcome.NO)]
                title = f"{payload.asset.upper()} 5m(UP≥{s5:.1f}) ⇄ 15m(DN<{s15:.1f})"
            return legs, resolves_at, title, [payload.ev_5m.id, payload.ev_15m.id]

        if isinstance(payload, Anchor1hSpec):
            if now_s >= payload.end_ts - 3.0:
                return None
            sp_ud = ShortSingleSpec("binance_1h_updown", payload.asset, "1h", payload.end_ts - 3600, payload.end_ts, payload.mkt_updown)
            open_px = self.resolve_open_benchmark(payload.ev_updown, sp_ud)
            if open_px is None or open_px <= 0:
                return None
            # Only evaluate the tightest strikes within 0.6% of Open so books are liquid & tight
            if abs(payload.strike - open_px) / open_px > 0.006:
                return None
            resolves_at = datetime.fromtimestamp(payload.end_ts + 300, tz=utc(now_s).tzinfo)
            k = payload.strike
            if k < open_px - 1e-6:
                legs = [(payload.mkt_updown, Outcome.NO), (payload.mkt_above, Outcome.YES)]
                title = f"{payload.asset.upper()} 1h(DN<{open_px:g}) ⇄ Above(>{k:g})"
            else:
                legs = [(payload.mkt_updown, Outcome.YES), (payload.mkt_above, Outcome.NO)]
                title = f"{payload.asset.upper()} 1h(UP≥{open_px:g}) ⇄ NO-Above(≤{k:g})"
            return legs, resolves_at, title, [payload.ev_updown.id, payload.ev_above.id]

        return None

    # ------------------------------------------------------------------ MakerArbManager hooks

    def locked_markets(self, target: Target, now_dt: Any) -> list[tuple[Market, Outcome, Any]]:
        """Expose 0-hedge-leg locked 5m/15m/1h winners to ``MakerArbManager`` (``maker_crypto_short``)."""
        if not isinstance(target.payload, ShortSingleSpec):
            return []
        spec = target.payload
        now_s = now_dt.timestamp()
        state = self.single_lock_state(target.event, spec, now_s)
        if state is None:
            return []
        locked, _, _ = state
        resolves_at = datetime.fromtimestamp(spec.end_ts + 90, tz=now_dt.tzinfo)
        return [(spec.market, locked, resolves_at)]

    def still_safely_locked(self, target: Target, expected_outcome: Outcome, now_s: float) -> bool:
        """Used by ``MakerArbManager`` to cancel pre-close resting quotes in <5ms if a shock erodes the buffer."""
        if not isinstance(target.payload, ShortSingleSpec):
            return True
        spec = target.payload
        if now_s >= spec.end_ts:
            st = self.single_lock_state(target.event, spec, now_s)
            return st is not None and st[0] is expected_outcome
        keep_shock = max(35.0, self.cfg.shock_bps * 0.5)
        st = self.single_lock_state(target.event, spec, now_s, shock_bps_override=keep_shock)
        return st is not None and st[0] is expected_outcome

    # ------------------------------------------------------------------ Strategy interface

    def top_edge(self, target: Target, books: Books) -> float | None:
        now_s = time.time()
        payload = target.payload
        if isinstance(payload, ShortSingleSpec):
            st = self.single_lock_state(target.event, payload, now_s)
            if st is None:
                return None
            locked, _, _ = st
            b = books.get(payload.market.yes_token)
            if b is None:
                return None
            levels = b.asks if locked is Outcome.YES else b.no_asks()
            c = unit_cost(levels, payload.market.fee)
            return None if c is None else (1.0 - c)

        pair = self.active_pair_legs(payload, now_s)
        if pair is None:
            return None
        legs, _, _, _ = pair
        total = 0.0
        for m, out in legs:
            bm = books.get(m.yes_token)
            if bm is None:
                return None
            levels = bm.asks if out is Outcome.YES else bm.no_asks()
            c = unit_cost(levels, m.fee)
            if c is None:
                return None
            total += c
        return 1.0 - total

    def evaluate(self, target: Target, books: Books, now: float, max_cost: float | None = None) -> list[Opportunity]:
        cfg = self.cfg
        cap = self.max_trade_usd if max_cost is None else min(self.max_trade_usd, max_cost)
        if cap <= 0:
            return []
        payload = target.payload

        if isinstance(payload, ShortSingleSpec):
            st = self.single_lock_state(target.event, payload, now)
            if st is None:
                return []
            locked, proof, is_post = st
            mkt = payload.market
            b = books.get(mkt.yes_token)
            if b is None:
                return []
            levels = b.asks if locked is Outcome.YES else b.no_asks()
            if not levels:
                return []
            if not is_post and levels[0].price > cfg.preclose_max_ask + 1e-9:
                return []
            first_cost = unit_cost(levels, mkt.fee)
            if first_cost is None or 1.0 - first_cost < cfg.min_unit_edge:
                return []
            # Live FOK dies when it asks for size that was on the 2nd–3rd level a second ago.
            # Keep the order inside the best ask, leaving a fraction behind so a partial pull still fills.
            top_shares = levels[0].size * cfg.taker_top_fraction
            if top_shares + 1e-9 < mkt.min_size:
                top_shares = levels[0].size
            fill = size_basket(
                [LegBook(levels, mkt.fee, mkt.min_size)],
                1.0,
                min_unit_edge=cfg.min_unit_edge,
                max_shares=floor_shares(top_shares),
                max_cost=cap,
            )
            if fill is None:
                return []
            resolves_at = utc(payload.end_ts + (90 if payload.duration in ("5m", "15m") else 300))
            lbl = f"{_side_label(mkt, locked)} {target.event.title[:48]}"
            opp = Opportunity(
                strategy=self.name,
                key=f"{target.key}:{mkt.id}:{locked.value}",
                event_id=target.event.id,
                title=target.event.title,
                legs=[buy_leg(mkt, locked, fill.fills[0], lbl)],
                shares=fill.shares,
                cost=fill.cost,
                payout_floor=fill.payout,
                settlement=Settlement.RESOLUTION,
                risk="low",
                resolves_at=resolves_at,
                detected_at=now,
                meta={
                    "mode": "post_close_lock" if is_post else "integral_twap_lock",
                    "asset": payload.asset,
                    "duration": payload.duration,
                    "proof": proof,
                    "fast_turnover": True,
                    "fast_flip": cfg.flip_enabled,
                    "flip_ask": self.flip_ask_price,
                    "flip_min_bid": self.flip_min_instant_bid,
                    "flip_min_profit_usd": self.flip_min_profit_usd,
                    "independent_legs": True,
                    "condition_ids": [mkt.condition_id] if mkt.condition_id else [],
                },
            )
            if opp.profit >= cfg.min_profit_usd and opp.edge >= cfg.min_edge:
                return [opp]
            return []

        pair = self.active_pair_legs(payload, now)
        if pair is None:
            return []
        leg_specs, resolves_at, pair_title, ev_ids = pair
        leg_books: list[LegBook] = []
        for m, out in leg_specs:
            bm = books.get(m.yes_token)
            if bm is None:
                return []
            levels = bm.asks if out is Outcome.YES else bm.no_asks()
            if not levels:
                return []
            leg_books.append(LegBook(levels, m.fee, m.min_size))

        if sum(unit_cost(lb.levels, lb.fee) or 1.0 for lb in leg_books) > 1.0 - cfg.min_unit_edge:
            return []
        top_caps = []
        for lb in leg_books:
            top = lb.levels[0].size * cfg.taker_top_fraction
            if top + 1e-9 < lb.min_size:
                top = lb.levels[0].size
            top_caps.append(top)
        fill = size_basket(
            leg_books,
            1.0,
            min_unit_edge=cfg.min_unit_edge,
            max_shares=floor_shares(min(top_caps)),
            max_cost=cap,
        )
        if fill is None:
            return []

        legs = [
            buy_leg(m, out, f, f"{_side_label(m, out)} {m.group_title or m.question[:42]}")
            for (m, out), f in zip(leg_specs, fill.fills)
        ]
        opp = Opportunity(
            strategy=self.name,
            key=target.key,
            event_id=target.event.id,
            title=pair_title,
            legs=legs,
            shares=fill.shares,
            cost=fill.cost,
            payout_floor=fill.payout,
            settlement=Settlement.RESOLUTION,
            risk="low",
            resolves_at=resolves_at,
            detected_at=now,
            meta={
                "mode": "sync_5m_15m" if isinstance(payload, Sync5m15mSpec) else "anchor_1h",
                "fast_turnover": True,
                "independent_legs": True,
                "event_ids": ev_ids,
                "condition_ids": [m.condition_id for m, _ in leg_specs if m.condition_id],
            },
        )
        if opp.profit >= cfg.min_profit_usd and opp.edge >= cfg.min_edge:
            return [opp]
        return []
