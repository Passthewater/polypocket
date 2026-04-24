from polypocket.config import (
    EDGE_FLOOR,
    EDGE_RANGE,
    FEE_RATE,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_LOSS,
    MAX_EDGE_THRESHOLD_UP,
    MAX_POSITION_USDC,
    MIN_EDGE_THRESHOLD,
    MIN_MODEL_CONFIDENCE,
    MIN_MODEL_CONFIDENCE_UP,
    MIN_POSITION_USDC,
    TRADING_MODE,
    VOL_FLOOR,
    VOL_RANGE,
    VOLATILITY_LOOKBACK,
    WINDOW_ENTRY_MIN_ELAPSED,
    WINDOW_ENTRY_MIN_REMAINING,
)


def test_defaults_are_sane():
    assert MIN_EDGE_THRESHOLD == 0.10
    assert FEE_RATE == 0.072
    assert MIN_POSITION_USDC == 5.0
    assert MAX_POSITION_USDC == 20.0
    assert MAX_DAILY_LOSS == 50.0
    assert MAX_CONSECUTIVE_LOSSES == 5
    assert VOLATILITY_LOOKBACK == 50
    assert WINDOW_ENTRY_MIN_ELAPSED == 60
    assert WINDOW_ENTRY_MIN_REMAINING == 30
    assert TRADING_MODE in {"paper", "live"}
    assert MIN_MODEL_CONFIDENCE == 0.60
    assert MIN_MODEL_CONFIDENCE_UP == 0.75
    assert MIN_MODEL_CONFIDENCE_UP > MIN_MODEL_CONFIDENCE
    assert MAX_EDGE_THRESHOLD_UP == 0.25
    assert MAX_EDGE_THRESHOLD_UP > MIN_EDGE_THRESHOLD


def test_dynamic_sizing_params():
    assert VOL_FLOOR == 0.0005
    assert VOL_RANGE == 0.0005
    assert EDGE_FLOOR == 0.03
    assert EDGE_RANGE == 0.17
    assert MIN_POSITION_USDC < MAX_POSITION_USDC


def test_min_edge_plus_fee_is_reasonable():
    """Worst-case effective fee surcharge (at p=0.5) plus threshold must leave room to trade."""
    assert MIN_EDGE_THRESHOLD + FEE_RATE * 0.25 < 0.50


def test_calibration_report_returns_string():
    from polypocket.analyze import calibration_report
    result = calibration_report()
    assert isinstance(result, str)
    assert "Calibration Report" in result
    assert "Bucket" in result


def test_depth_clamp_buffer_default():
    import importlib, polypocket.config as cfg
    importlib.reload(cfg)
    assert cfg.DEPTH_CLAMP_BUFFER == 0.9

def test_min_fill_ratio_default():
    import importlib, polypocket.config as cfg
    importlib.reload(cfg)
    assert cfg.MIN_FILL_RATIO == 0.5

def test_depth_clamp_buffer_env_override(monkeypatch):
    monkeypatch.setenv("DEPTH_CLAMP_BUFFER", "0.75")
    import importlib, polypocket.config as cfg
    importlib.reload(cfg)
    assert cfg.DEPTH_CLAMP_BUFFER == 0.75

def test_min_fill_ratio_env_override(monkeypatch):
    monkeypatch.setenv("MIN_FILL_RATIO", "0.25")
    import importlib, polypocket.config as cfg
    importlib.reload(cfg)
    assert cfg.MIN_FILL_RATIO == 0.25
