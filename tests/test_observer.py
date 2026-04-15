from math import isclose

from polypocket.observer import (
    ObservationRecord,
    build_observation_record,
    compute_model_p_up,
)


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
