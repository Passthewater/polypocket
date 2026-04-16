from polypocket.config import (
    FEE_RATE,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_LOSS,
    MIN_EDGE_THRESHOLD,
    MIN_MODEL_CONFIDENCE,
    MIN_MODEL_CONFIDENCE_UP,
    POSITION_SIZE_USDC,
    TRADING_MODE,
    VOLATILITY_LOOKBACK,
    WINDOW_ENTRY_MIN_ELAPSED,
    WINDOW_ENTRY_MIN_REMAINING,
)


def test_defaults_are_sane():
    assert MIN_EDGE_THRESHOLD == 0.03
    assert FEE_RATE == 0.072
    assert POSITION_SIZE_USDC == 10.0
    assert MAX_DAILY_LOSS == 50.0
    assert MAX_CONSECUTIVE_LOSSES == 5
    assert VOLATILITY_LOOKBACK == 50
    assert WINDOW_ENTRY_MIN_ELAPSED == 60
    assert WINDOW_ENTRY_MIN_REMAINING == 30
    assert TRADING_MODE == "paper"


def test_up_confidence_threshold_is_higher():
    assert MIN_MODEL_CONFIDENCE_UP == 0.65
    assert MIN_MODEL_CONFIDENCE_UP > MIN_MODEL_CONFIDENCE


def test_min_edge_plus_fee_is_reasonable():
    """Signal engine requires edge > MIN_EDGE_THRESHOLD + FEE_RATE to trade."""
    assert MIN_EDGE_THRESHOLD + FEE_RATE < 0.50


def test_calibration_report_returns_string():
    from polypocket.analyze import calibration_report
    result = calibration_report()
    assert isinstance(result, str)
    assert "Calibration Report" in result
    assert "Bucket" in result
