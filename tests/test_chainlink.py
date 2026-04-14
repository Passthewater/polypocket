import pytest

from polypocket.feeds.chainlink import extract_price_to_beat, fetch_resolved_5min_markets


def test_extract_price_to_beat():
    event = {"eventMetadata": {"priceToBeat": 71741.348981}}
    assert extract_price_to_beat(event) == 71741.348981


@pytest.mark.asyncio
async def test_fetch_resolved_markets_returns_list():
    """Integration test — hits real Gamma API."""
    markets = await fetch_resolved_5min_markets(5)
    assert isinstance(markets, list)
