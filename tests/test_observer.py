from math import isclose

from polypocket.observer import (
    ObservationRecord,
    build_observation_record,
    calibrate_p_up,
    compute_model_p_up,
)


def test_calibrate_p_up_identity_when_factors_one():
    for p in (0.01, 0.25, 0.5, 0.75, 0.99):
        assert isclose(calibrate_p_up(p, up_factor=1.0, down_factor=1.0), p, abs_tol=1e-9)


def test_calibrate_p_up_collapses_to_half_when_factors_zero():
    for p in (0.1, 0.4, 0.6, 0.9):
        assert isclose(calibrate_p_up(p, up_factor=0.0, down_factor=0.0), 0.5, abs_tol=1e-9)


def test_calibrate_p_up_applies_down_factor_below_half():
    p_raw = 0.20
    p_cal = calibrate_p_up(p_raw, up_factor=1.0, down_factor=0.5)
    assert isclose(p_cal, 0.5 + (0.20 - 0.5) * 0.5, abs_tol=1e-9)
    assert p_cal > p_raw  # shrinkage toward 0.5 raises low p


def test_calibrate_p_up_applies_up_factor_above_half():
    p_raw = 0.90
    p_cal = calibrate_p_up(p_raw, up_factor=0.80, down_factor=0.5)
    assert isclose(p_cal, 0.5 + (0.90 - 0.5) * 0.80, abs_tol=1e-9)
    assert p_cal < p_raw  # shrinkage toward 0.5 lowers high p


def test_calibrate_p_up_at_half_is_half():
    for up, down in ((1.0, 1.0), (0.5, 0.2), (0.0, 0.0)):
        assert calibrate_p_up(0.5, up_factor=up, down_factor=down) == 0.5


def test_compute_model_p_up_btc_above_open():
    """When BTC is above window open with time remaining, P(Up) > 0.5."""
    probability = compute_model_p_up(
        displacement=0.0005,
        t_remaining=120.0,
        sigma_5min=0.0012,
    )
    assert probability > 0.5
    assert probability < 1.0


def test_compute_model_p_up_btc_below_open():
    """When BTC is below window open, P(Up) < 0.5."""
    probability = compute_model_p_up(
        displacement=-0.0005,
        t_remaining=120.0,
        sigma_5min=0.0012,
    )
    assert probability < 0.5
    assert probability > 0.0


def test_compute_model_p_up_no_displacement():
    """Zero displacement gives P(Up) = 0.5 regardless of time or vol."""
    probability = compute_model_p_up(
        displacement=0.0,
        t_remaining=120.0,
        sigma_5min=0.0012,
    )
    assert isclose(probability, 0.5, abs_tol=1e-9)


def test_compute_model_p_up_near_expiry():
    """With very little time left and positive displacement, P(Up) -> 1.0."""
    probability = compute_model_p_up(
        displacement=0.001,
        t_remaining=1.0,
        sigma_5min=0.0012,
    )
    assert probability > 0.99


def test_compute_model_p_up_zero_remaining():
    """With zero time remaining, P(Up) is 1.0 if positive, 0.0 if negative."""
    p_up = compute_model_p_up(displacement=0.001, t_remaining=0.0, sigma_5min=0.0012)
    p_down = compute_model_p_up(displacement=-0.001, t_remaining=0.0, sigma_5min=0.0012)
    assert p_up == 1.0
    assert p_down == 0.0


def test_observation_record():
    record = ObservationRecord(
        timestamp=1713100000.0,
        window_slug="btc-updown-5m-123",
        btc_price=84231.42,
        window_open_price=84198.00,
        displacement=0.0004,
        t_remaining=180.0,
        sigma_5min=0.0012,
        model_p_up=0.62,
        market_p_up=0.575,
        edge=0.045,
    )
    assert record.edge == 0.045


def test_build_observation_record_uses_price_to_beat():
    record = build_observation_record(
        timestamp=1713100000.0,
        window_slug="btc-updown-5m-123",
        btc_price=84231.42,
        price_to_beat=84198.00,
        t_remaining=180.0,
        sigma_5min=0.0012,
        market_p_up=0.575,
    )
    assert record.window_open_price == 84198.00
    assert isclose(record.displacement, (84231.42 - 84198.00) / 84198.00)


def test_compute_model_p_up_extreme_displacement():
    """With large displacement and time remaining, P(Up) should be high but not 1.0."""
    probability = compute_model_p_up(
        displacement=0.002,
        t_remaining=120.0,
        sigma_5min=0.0012,
    )
    assert probability > 0.5
    assert probability < 1.0


def test_compute_model_p_up_matches_norm():
    """Verify output matches scipy norm CDF directly."""
    from math import sqrt
    from scipy.stats import norm

    displacement = 0.001
    t_remaining = 150.0
    sigma_5min = 0.0010
    sigma_remaining = sigma_5min * sqrt(t_remaining / 300.0)
    expected = float(norm.cdf(displacement / sigma_remaining))

    actual = compute_model_p_up(displacement, t_remaining, sigma_5min)
    assert isclose(actual, expected, rel_tol=1e-9)
