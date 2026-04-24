"""Tests for BinanceFeed hires buffer utilities."""

from polypocket.feeds.binance import BinanceFeed


def test_get_path_returns_samples_in_range():
    feed = BinanceFeed()
    for i in range(10):
        feed._on_trade({"price": 100.0 + i, "timestamp": (1000 + i) * 1000})
    path = feed.get_path(1002.0, 1005.0)
    assert len(path) == 4
    assert path[0][1] == 102.0
    assert path[-1][1] == 105.0


def test_get_path_returns_empty_when_buffer_cold():
    """Startup case: no ticks received yet — get_path must return []."""
    feed = BinanceFeed()
    assert feed.get_path(1000.0, 1060.0) == []


def test_get_path_returns_empty_when_range_disjoint():
    """Range entirely outside the buffer window — return [] not a partial match."""
    feed = BinanceFeed()
    for i in range(5):
        feed._on_trade({"price": 100.0 + i, "timestamp": (1000 + i) * 1000})
    assert feed.get_path(2000.0, 2060.0) == []
