#!/usr/bin/env python3
"""Download missing Binance 1s zips for TWAP-60 dates. Resumable."""
from __future__ import annotations

import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/tmp/binance_1s")
VISION = "https://data.binance.vision/data/spot/daily/klines"
UA = {"User-Agent": "Mozilla/5.0 surf-arb-research/month-1s"}
SYMS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
    "hype": "HYPEUSDT",
}


def days():
    d0 = datetime(2026, 8, 14, tzinfo=timezone.utc).date()
    d1 = datetime(2026, 8, 31, tzinfo=timezone.utc).date()
    d = d0
    out = []
    while d <= d1:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def main() -> None:
    for _asset, sym in SYMS.items():
        dest_dir = ROOT / sym
        dest_dir.mkdir(parents=True, exist_ok=True)
        for day in days():
            dest = dest_dir / f"{sym}-1s-{day}.zip"
            if dest.exists() and dest.stat().st_size > 1000:
                continue
            url = f"{VISION}/{sym}/1s/{sym}-1s-{day}.zip"
            req = urllib.request.Request(url, headers=UA)
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    dest.write_bytes(resp.read())
                print(f"ok {dest.name} {dest.stat().st_size}", flush=True)
            except urllib.error.HTTPError as exc:
                print(f"skip {sym} {day} HTTP {exc.code}", flush=True)
                if dest.exists():
                    dest.unlink()
            except Exception as exc:
                print(f"fail {sym} {day} {type(exc).__name__}", flush=True)
                if dest.exists():
                    dest.unlink()


if __name__ == "__main__":
    main()
