from polypocket.feeds.binance import BinanceFeed


def test_binance_feed_init():
    feed = BinanceFeed()
    assert feed.latest_price is None
    assert feed.prices == []


def test_binance_feed_on_trade_updates_price():
    feed = BinanceFeed()
    feed._on_trade({"price": 84231.42, "timestamp": 1713100000000})
    assert feed.latest_price == 84231.42
    assert len(feed.prices) == 1


def test_binance_feed_rolling_returns():
    """Test 5-minute return calculation from price history."""
    feed = BinanceFeed()
    base_ts = 1713100000000
    prices = [80000.0, 80100.0, 80050.0, 80200.0, 80150.0]
    for i, price in enumerate(prices):
        feed._on_trade({"price": price, "timestamp": base_ts + i * 300_000})
    returns = feed.get_5min_returns()
    assert len(returns) == 4
    assert abs(returns[0] - 0.00125) < 1e-6
