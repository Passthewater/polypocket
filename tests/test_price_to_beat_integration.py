"""Integration test: verify priceToBeat is available from live Polymarket API."""

import pytest

from polypocket.feeds.polymarket import fetch_active_windows


@pytest.mark.asyncio
async def test_active_windows_have_price_to_beat():
    """Fetch live windows and verify each has a numeric priceToBeat."""
    windows = await fetch_active_windows()
    for window in windows:
        assert window.price_to_beat > 0, f"{window.slug} has invalid priceToBeat: {window.price_to_beat}"
        assert isinstance(window.price_to_beat, float)
