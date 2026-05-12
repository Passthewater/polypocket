"""Tests for compute_model_p_up_v2 and the MODEL_VERSION dispatcher.

v2 loads polypocket/model_v2_coefs.json at import time. Tests construct a
small synthetic coefs file and monkeypatch MODEL_V2_COEFS_PATH to it,
keeping these tests independent of the real shipped coefficients.
"""
import json
import logging

import pytest

from polypocket import observer


@pytest.fixture
def fake_coefs(tmp_path, monkeypatch):
    p = tmp_path / "fake_coefs.json"
    p.write_text(json.dumps({
        "model_version": "v2",
        "features": ["z", "t_remaining", "sigma_5min", "market_p_up_normalized"],
        "scaler_mean": [0.0, 200.0, 0.001, 0.5],
        "scaler_scale": [1.0, 60.0, 0.0005, 0.1],
        "logistic_coef": [1.0, 0.0, 0.0, 0.0],
        "logistic_intercept": 0.0,
        "isotonic_x": [0.0, 0.5, 1.0],
        "isotonic_y": [0.0, 0.5, 1.0],
        "feature_hull": {
            "z": [-5.0, 5.0],
            "t_remaining": [30.0, 300.0],
            "sigma_5min": [0.0001, 0.01],
            "market_p_up_normalized": [0.1, 0.9],
        },
    }, indent=2))
    monkeypatch.setenv("MODEL_V2_COEFS_PATH", str(p))
    observer._reset_v2_cache_for_tests()
    yield p
    observer._reset_v2_cache_for_tests()


def test_v2_returns_probability_in_01(fake_coefs):
    p = observer.compute_model_p_up_v2(
        displacement=0.001, t_remaining=200, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    )
    assert 0.0 <= p <= 1.0


def test_v2_is_deterministic(fake_coefs):
    args = dict(displacement=0.001, t_remaining=200, sigma_5min=0.0008,
                up_ask=0.58, down_ask=0.42)
    assert observer.compute_model_p_up_v2(**args) == observer.compute_model_p_up_v2(**args)


def test_dispatcher_v1_default(monkeypatch):
    monkeypatch.delenv("MODEL_VERSION", raising=False)
    p = observer.compute_model_p_up_active(
        displacement=0.001, t_remaining=200, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    )
    expected = observer.compute_model_p_up(0.001, 200, 0.0008)
    assert p == expected


def test_dispatcher_v2_env(monkeypatch, fake_coefs):
    monkeypatch.setenv("MODEL_VERSION", "v2")
    p = observer.compute_model_p_up_active(
        displacement=0.001, t_remaining=200, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    )
    expected = observer.compute_model_p_up_v2(
        displacement=0.001, t_remaining=200, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    )
    assert p == expected


def test_v2_guards_t_remaining_leq_zero(fake_coefs):
    assert observer.compute_model_p_up_v2(
        displacement=0.01, t_remaining=0, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    ) == 1.0
    assert observer.compute_model_p_up_v2(
        displacement=-0.01, t_remaining=0, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    ) == 0.0
    assert observer.compute_model_p_up_v2(
        displacement=0.0, t_remaining=0, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    ) == 0.5


@pytest.fixture
def fake_coefs_no_iso(tmp_path, monkeypatch):
    """Same as fake_coefs but with isotonic_x/y set to null (no-iso shipping config)."""
    p = tmp_path / "fake_coefs_no_iso.json"
    p.write_text(json.dumps({
        "model_version": "v2",
        "features": ["z", "t_remaining", "sigma_5min", "market_p_up_normalized"],
        "scaler_mean": [0.0, 200.0, 0.001, 0.5],
        "scaler_scale": [1.0, 60.0, 0.0005, 0.1],
        "logistic_coef": [1.0, 0.0, 0.0, 0.0],
        "logistic_intercept": 0.0,
        "isotonic_x": None,
        "isotonic_y": None,
        "feature_hull": {
            "z": [-5.0, 5.0],
            "t_remaining": [30.0, 300.0],
            "sigma_5min": [0.0001, 0.01],
            "market_p_up_normalized": [0.1, 0.9],
        },
    }, indent=2))
    monkeypatch.setenv("MODEL_V2_COEFS_PATH", str(p))
    observer._reset_v2_cache_for_tests()
    yield p
    observer._reset_v2_cache_for_tests()


def test_v2_skips_isotonic_when_null(fake_coefs_no_iso):
    """No-iso config returns raw sigmoid(logit). With logistic_coef=[1,0,0,0] and
    intercept=0, the raw sigmoid is just 1/(1+exp(-z_standardized)). z is
    standardized with mean=0/scale=1 here, so it passes through as logit."""
    from math import exp
    # z = displacement/sigma_remaining; with disp=0.001, t=200, sigma_5min=0.0008:
    #   sigma_rem = 0.0008 * sqrt(200/300) = 0.000653; z = 1.531
    # standardized = (1.531 - 0) / 1 = 1.531; logit = 1*1.531 = 1.531
    p = observer.compute_model_p_up_v2(
        displacement=0.001, t_remaining=200, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    )
    expected = 1.0 / (1.0 + exp(-1.5309310892394865))
    assert abs(p - expected) < 1e-9


def test_v2_warns_outside_training_hull(fake_coefs, caplog):
    # z = displacement / sigma_remaining; with sigma_5min=0.0008, t=200 -> sigma_rem ~ 0.000653;
    # displacement=0.01 -> z ~ 15.3, well outside fake hull [-5, 5].
    with caplog.at_level(logging.WARNING, logger="polypocket.observer"):
        observer.compute_model_p_up_v2(
            displacement=0.01, t_remaining=200, sigma_5min=0.0008,
            up_ask=0.58, down_ask=0.42,
        )
    assert any("outside training hull" in r.message for r in caplog.records)
