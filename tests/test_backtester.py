from polypocket.backtester import WindowResult, fetch_historical_klines, simulate_window


def test_simulate_window_up():
    """Window where price ends above open should return outcome='up'."""
    candles = [
        {"open": 80000, "high": 80050, "low": 79980, "close": 80020, "ts": 0},
        {"open": 80020, "high": 80100, "low": 80010, "close": 80080, "ts": 60},
        {"open": 80080, "high": 80150, "low": 80060, "close": 80120, "ts": 120},
        {"open": 80120, "high": 80200, "low": 80100, "close": 80180, "ts": 180},
        {"open": 80180, "high": 80250, "low": 80150, "close": 80200, "ts": 240},
    ]
    result = simulate_window(candles, sigma_5min=0.0012, market_p_up=0.50)
    assert result.outcome == "up"
    assert result.open_price == 80000
    assert result.close_price == 80200


def test_simulate_window_no_signal():
    """Flat price action should produce no signal."""
    candles = [
        {"open": 80000, "high": 80001, "low": 79999, "close": 80000, "ts": 0},
        {"open": 80000, "high": 80001, "low": 79999, "close": 80000, "ts": 60},
        {"open": 80000, "high": 80001, "low": 79999, "close": 80000, "ts": 120},
        {"open": 80000, "high": 80001, "low": 79999, "close": 80000, "ts": 180},
        {"open": 80000, "high": 80001, "low": 79999, "close": 80000, "ts": 240},
    ]
    result = simulate_window(candles, sigma_5min=0.0012, market_p_up=0.50)
    assert result.signal_fired is False
