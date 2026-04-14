"""Helpers for Polymarket's Chainlink-backed 5-minute BTC windows.

Findings from the live Polymarket event payload:
- Resolution source is exposed as ``resolutionSource`` on the event and
  ``resolution_source`` / ``resolutionSource`` on the nested market payload.
- The opening reference price is exposed as ``eventMetadata.priceToBeat``.
- A closing reference price is not exposed directly in the event payload;
  resolved events provide final binary outcome state instead. If we need the
  exact close value later, we will need a direct Chainlink-side data source.

This means the execution pipeline should not try to infer the opening price
 from Binance ticks. It should fetch ``priceToBeat`` from Polymarket's event
 API for the active window, while still using Binance as a faster proxy feed
 for signal generation.
"""

import logging
import re

import aiohttp

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_EVENT_API = "https://polymarket.com/api/event"
BTC_5MIN_PATTERN = re.compile(r"btc-updown-5m-\d+")
KNOWN_SAMPLE_SLUGS = [
    "btc-updown-5m-1773418500",
]


def extract_price_to_beat(event: dict) -> float | None:
    """Return the Polymarket opening reference price for a 5-minute window."""
    metadata = event.get("eventMetadata") or {}
    price_to_beat = metadata.get("priceToBeat")
    return float(price_to_beat) if price_to_beat is not None else None


async def fetch_event_by_slug(slug: str) -> dict | None:
    """Fetch Polymarket's event payload for a known event slug."""
    async with aiohttp.ClientSession() as session:
        async with session.get(POLYMARKET_EVENT_API, params={"slug": slug}) as response:
            if response.status != 200:
                log.warning("Event API returned %d for %s", response.status, slug)
                return None
            return await response.json()


async def fetch_resolved_5min_markets(limit: int = 10) -> list[dict]:
    """Fetch recently resolved BTC 5-minute markets, if Gamma still exposes them."""
    params = {
        "closed": "true",
        "limit": max(limit, 1),
        "order": "end_date_iso",
        "ascending": "false",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{GAMMA_API}/markets", params=params) as response:
            if response.status != 200:
                log.warning("Gamma API returned %d", response.status)
                return []
            data = await response.json()

    markets = []
    for market in data:
        slug = market.get("slug", "")
        question = market.get("question", "")
        if BTC_5MIN_PATTERN.fullmatch(slug) or "Bitcoin Up or Down -" in question:
            markets.append(market)
    return markets[:limit]


async def investigate_resolution() -> None:
    """Print the fields relevant to opening-price discovery and resolution."""
    markets = await fetch_resolved_5min_markets(10)
    sample_slugs = [market.get("slug") for market in markets if market.get("slug")]
    if not sample_slugs:
        sample_slugs = KNOWN_SAMPLE_SLUGS

    print(f"\nInspecting {len(sample_slugs)} 5-minute BTC market(s):\n")
    for slug in sample_slugs:
        event = await fetch_event_by_slug(slug)
        if not event:
            print(f"Slug: {slug}")
            print("  Failed to fetch event payload.\n")
            continue

        price_to_beat = extract_price_to_beat(event)
        print(f"Slug: {slug}")
        print(f"  Title: {event.get('title')}")
        print(f"  Start time: {event.get('startTime')}")
        print(f"  End date: {event.get('endDate')}")
        print(f"  resolutionSource: {event.get('resolutionSource')}")
        print(f"  eventMetadata.priceToBeat: {price_to_beat}")

        market_payloads = event.get("markets") or []
        if market_payloads:
            market_payload = market_payloads[0]
            print(f"  market.resolution_source: {market_payload.get('resolution_source')}")
            print(f"  market.umaResolutionStatus: {market_payload.get('umaResolutionStatus')}")
            print(f"  market.outcomes: {market_payload.get('outcomes')}")
            print(f"  market.outcomePrices: {market_payload.get('outcomePrices')}")
        print()

    print("Summary:")
    print("  Opening price field: eventMetadata.priceToBeat")
    print("  Resolution source field: resolutionSource / resolution_source")
    print("  Closing price field: not exposed directly in the event payload")
    print("  Operational implication: use Polymarket event API for window open;")
    print("    do not derive the official opening price from Binance.")
