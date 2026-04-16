from polypocket.signal import SignalEngine


def test_signal_engine_no_signal_too_early():
    """Should not produce signal in first 60 seconds."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.001,
        t_elapsed=30.0,
        t_remaining=270.0,
        sigma_5min=0.0012,
        up_ask=0.55,
        down_ask=0.45,
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
        up_ask=0.55,
        down_ask=0.45,
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
        up_ask=0.50,
        down_ask=0.50,
    )
    assert signal is None


def test_signal_engine_up_signal_uses_fee_adjusted_up_ask():
    """Large positive displacement should select the fee-adjusted up side."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.55,
        down_ask=0.80,
    )
    assert signal is not None
    assert signal.side == "up"
    assert signal.market_price == 0.55
    assert signal.edge == signal.up_edge
    assert signal.up_edge > signal.down_edge


def test_signal_engine_down_signal_uses_down_ask():
    """Large negative displacement should select the fee-adjusted down side."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=-0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.99,
        down_ask=0.15,
    )
    assert signal is not None
    assert signal.side == "down"
    assert signal.market_price == 0.15
    assert signal.edge == signal.down_edge
    assert signal.down_edge > signal.up_edge


def test_signal_engine_no_signal_when_both_sides_are_too_expensive():
    """If both asks are expensive, there should be no signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.001,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.99,
        down_ask=0.99,
    )
    assert signal is None


def test_signal_engine_no_signal_missing_ask():
    """If one side is missing, no signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=None,
        down_ask=0.50,
    )
    assert signal is None


def test_signal_engine_no_signal_with_nonpositive_sigma():
    """If sigma is not positive, no signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    assert signal is None


def test_signal_engine_no_signal_with_zero_up_ask():
    """An ask of zero should be rejected."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.0,
        down_ask=0.45,
    )
    assert signal is None


def test_signal_engine_no_signal_with_over_one_down_ask():
    """An ask above 1.0 should be rejected."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.55,
        down_ask=1.01,
    )
    assert signal is None


def test_signal_engine_no_signal_when_model_disagrees_with_direction():
    """Model says neutral/contra -> no signal even if edge > threshold."""
    engine = SignalEngine()
    # Tiny positive displacement -> model_p_up ~0.50 (neutral)
    # With cheap up_ask, edge might be positive, but model isn't confident
    signal = engine.evaluate(
        displacement=0.00001,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.40,
        down_ask=0.40,
    )
    assert signal is None



def test_signal_engine_fires_when_model_strongly_aligned():
    """Strong displacement + cheap ask + model alignment -> signal fires."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.003,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.50,
        down_ask=0.80,
    )
    assert signal is not None
    assert signal.side == "up"
    assert signal.model_p_up >= 0.60
