"""Integration test: verify active windows are discoverable from live Polymarket API."""

import pytest

from polypocket.feeds.polymarket import fetch_active_windows


@pytest.mark.asyncio
async def test_active_windows_discoverable():
    """Fetch live windows and verify we get at least one with valid structure."""
    windows = await fetch_active_windows()
    assert len(windows) > 0, "No active 5-min BTC windows found"
    for window in windows:
        assert window.condition_id
        assert window.up_token_id
        assert window.down_token_id
        assert window.end_time > 0
        # priceToBeat may be None if Chainlink hasn't set it yet
        if window.price_to_beat is not None:
            assert window.price_to_beat > 0
            assert isinstance(window.price_to_beat, float)
