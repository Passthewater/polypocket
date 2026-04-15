"""Polymarket 5-minute BTC market discovery and order book feed."""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

import aiohttp
import websockets

from polypocket.config import POLYMARKET_WS

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
HEARTBEAT_INTERVAL = 8
BTC_5MIN_SLUG_PATTERN = re.compile(r"btc-updown-5m-\d+")


@dataclass
class Window:
    """A single 5-minute BTC up/down market window."""

    condition_id: str
    question: str
    up_token_id: str
    down_token_id: str
    end_time: float
    slug: str
    price_to_beat: float
    up_ask: float | None = None
    up_ask_size: float | None = None
    down_ask: float | None = None
    down_ask_size: float | None = None

    @property
    def start_time(self) -> float:
        return self.end_time - 300.0

    @property
    def up_implied_prob(self) -> float | None:
        return self.up_ask

    @property
    def down_implied_prob(self) -> float | None:
        return self.down_ask


def parse_5min_btc_markets(markets: list[dict]) -> list[Window]:
    """Filter Gamma API markets to active 5-minute BTC windows."""
    windows: list[Window] = []
    for market in markets:
        slug = market.get("slug", "")
        if not BTC_5MIN_SLUG_PATTERN.fullmatch(slug):
            continue
        if market.get("closed"):
            continue

        event_meta = market.get("eventMetadata", {})
        if isinstance(event_meta, str):
            try:
                event_meta = json.loads(event_meta)
            except (TypeError, ValueError):
                event_meta = {}
        price_to_beat = event_meta.get("priceToBeat")
        if price_to_beat is None:
            log.warning("Skipping %s: no priceToBeat in eventMetadata", slug)
            continue
        price_to_beat = float(price_to_beat)

        up_token_id = None
        down_token_id = None
        for token in market.get("tokens", []):
            outcome = token.get("outcome", "").strip().lower()
            if outcome == "up":
                up_token_id = token.get("token_id")
            elif outcome == "down":
                down_token_id = token.get("token_id")

        if not up_token_id or not down_token_id:
            continue

        end_iso = market.get("end_date_iso", "")
        try:
            end_time = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            continue

        windows.append(
            Window(
                condition_id=market["condition_id"],
                question=market.get("question", ""),
                up_token_id=up_token_id,
                down_token_id=down_token_id,
                end_time=end_time,
                slug=slug,
                price_to_beat=price_to_beat,
            )
        )
    return windows


def parse_book_event(msg: dict) -> dict:
    """Extract best ask price and size from a WS book event."""
    asks = msg.get("asks", [])
    best_ask = None
    best_ask_size = None
    if asks:
        best_ask = float(asks[0]["price"])
        best_ask_size = float(asks[0]["size"])
    return {
        "asset_id": msg.get("asset_id"),
        "best_ask": best_ask,
        "best_ask_size": best_ask_size,
    }


async def fetch_active_windows() -> list[Window]:
    """Fetch currently active 5-minute BTC windows from Gamma."""
    params = {"closed": "false", "limit": 100, "tag": "btc"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{GAMMA_API}/events", params=params) as response:
            if response.status != 200:
                log.error("Gamma API returned %d", response.status)
                return []
            events = await response.json()

    all_markets: list[dict] = []
    for event in events:
        event_meta = event.get("eventMetadata", {})
        for market in event.get("markets", [event]):
            market_copy = dict(market)
            market_copy["eventMetadata"] = event_meta
            all_markets.append(market_copy)

    return parse_5min_btc_markets(all_markets)


async def subscribe_and_stream(
    windows: list[Window],
    on_book_update,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Subscribe to token IDs and stream best-ask updates."""
    token_to_window: dict[str, tuple[Window, str]] = {}
    for window in windows:
        token_to_window[window.up_token_id] = (window, "up")
        token_to_window[window.down_token_id] = (window, "down")

    if not token_to_window:
        return

    backoff = 1
    while stop_event is None or not stop_event.is_set():
        try:
            async with websockets.connect(POLYMARKET_WS) as websocket:
                await websocket.send(
                    json.dumps({"type": "market", "assets_ids": list(token_to_window)})
                )
                backoff = 1
                last_ping = time.time()

                while stop_event is None or not stop_event.is_set():
                    if time.time() - last_ping >= HEARTBEAT_INTERVAL:
                        await websocket.send("PING")
                        last_ping = time.time()

                    try:
                        raw_message = await asyncio.wait_for(
                            websocket.recv(), timeout=HEARTBEAT_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        continue

                    if raw_message == "PONG":
                        continue

                    try:
                        messages = json.loads(raw_message)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(messages, list):
                        messages = [messages]

                    for message in messages:
                        if message.get("event_type") != "book":
                            continue
                        asset_id = message.get("asset_id")
                        if asset_id not in token_to_window:
                            continue

                        parsed = parse_book_event(message)
                        window, side = token_to_window[asset_id]
                        if side == "up":
                            window.up_ask = parsed["best_ask"]
                            window.up_ask_size = parsed["best_ask_size"]
                        else:
                            window.down_ask = parsed["best_ask"]
                            window.down_ask_size = parsed["best_ask_size"]

                        if on_book_update is not None:
                            await on_book_update(window, side)
        except Exception as exc:
            log.error("Polymarket WS error: %s (reconnect in %ds)", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
