from __future__ import annotations

from typing import Any

# Official groups: https://docs.polymarket.com/api-reference/geoblock
# The polymarket.com/api/geoblock `blocked` flag is the *website* check.
# Japan / Ireland / Netherlands are frontend close-only; CLOB API is not restricted.

API_FULL_BLOCK_COUNTRIES = frozenset({"IR", "SY", "CU", "KP"})
API_FULL_BLOCK_REGIONS = frozenset({"UA-43", "UA-14", "UA-09"})

API_CLOSE_ONLY_COUNTRIES = frozenset(
    {
        "AU",
        "BY",
        "BE",
        "BI",
        "BR",
        "CF",
        "CD",
        "ET",
        "FR",
        "DE",
        "IQ",
        "IT",
        "LB",
        "LY",
        "MM",
        "NZ",
        "NI",
        "PL",
        "RU",
        "SG",
        "SO",
        "SK",
        "SS",
        "SD",
        "TW",
        "TH",
        "GB",
        "US",
        "UM",
        "VE",
        "YE",
        "ZW",
    }
)
API_CLOSE_ONLY_REGIONS = frozenset({"CA-BC", "CA-ON", "CA-AB", "CA-QC"})

FRONTEND_ONLY_CLOSE = frozenset({"IE", "JP", "NL", "MT"})


def interpret(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = dict(raw or {})
    country = str(src.get("country") or "").upper()
    region = str(src.get("region") or "").upper()
    loc = f"{country}-{region}" if country and region else country
    website = src.get("blocked")

    if country in API_FULL_BLOCK_COUNTRIES or loc in API_FULL_BLOCK_REGIONS:
        api = "full_block"
    elif country in API_CLOSE_ONLY_COUNTRIES or loc in API_CLOSE_ONLY_REGIONS:
        api = "close_only"
    else:
        api = "open"

    src["website_blocked"] = website
    src["api_status"] = api
    src["api_open"] = api == "open"
    src["frontend_only"] = country in FRONTEND_ONLY_CLOSE
    # Keep `blocked` as API-blocked so UI/alerts match how bots actually trade.
    src["blocked"] = api != "open"
    return src


def telegram_line(geo: dict[str, Any] | None) -> str:
    g = geo or {}
    cc = g.get("country") or "?"
    if g.get("error") and g.get("api_status") is None:
        return ""
    if g.get("api_status") == "full_block":
        return f"\n⛔️ IP {cc}：官方 API 全封鎖（制裁區）。"
    if g.get("api_status") == "close_only":
        return f"\n⚠️ IP {cc}：官方 API close-only，新倉會被拒。"
    if g.get("frontend_only"):
        return f"\n🌐 IP {cc}：網站 geoblock；CLOB API 開放。"
    if g.get("website_blocked") is True and g.get("api_open"):
        return f"\n🌐 IP {cc}：網站報 blocked，但官方 API 名單冇封呢區；CLOB 已通。"
    if g.get("api_open"):
        return f"\n🌐 IP {cc}，CLOB API 開放。"
    return ""
