from polypocket.signal import SignalEngine


def test_signal_engine_no_signal_too_early():
    """Should not produce signal in first 60 seconds."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.001,
        t_elapsed=30.0,
        t_remaining=270.0,
        sigma_5min=0.0012,
        market_p_up=0.55,
    )
    assert signal is None


def test_signal_engine_no_signal_too_late():
    """Should not produce signal with < 30s remaining."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.001,
        t_elapsed=280.0,
        t_remaining=20.0,
        sigma_5min=0.0012,
        market_p_up=0.55,
    )
    assert signal is None


def test_signal_engine_no_signal_insufficient_edge():
    """Small displacement = small edge = no signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.00001,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        market_p_up=0.50,
    )
    assert signal is None


def test_signal_engine_up_signal():
    """Large positive displacement with stale market = UP signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        market_p_up=0.55,
    )
    assert signal is not None
    assert signal.side == "up"
    assert signal.edge > 0.05


def test_signal_engine_down_signal():
    """Large negative displacement with stale market = DOWN signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=-0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        market_p_up=0.50,
    )
    assert signal is not None
    assert signal.side == "down"
    assert signal.edge > 0.05


def test_signal_engine_no_signal_missing_market_price():
    """If market price is None, no signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        market_p_up=None,
    )
    assert signal is None
