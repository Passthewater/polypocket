from datetime import datetime
from pathlib import Path

from polypocket.ledger import init_db
from polypocket.tui import PolypocketApp, StatusPanel, WindowPanel


def test_status_panel_surfaces_quotes_and_status_fields(tmp_path: Path):
    db_path = tmp_path / "tui.db"
    init_db(str(db_path))

    panel = StatusPanel()
    panel.update_stats(
        {
            "btc_price": 84350.0,
            "window_open_price": 84198.0,
            "displacement": 0.0018,
            "model_p_up": 0.61,
            "market_p_up": 0.57,
            "edge": 0.04,
            "preview_side": "down",
            "preview_market_price": 0.43,
            "sigma_5min": 0.03,
            "position": None,
            "up_ask": 0.57,
            "down_ask": 0.43,
            "quote_status": "valid",
            "execution_status": "open",
        },
        str(db_path),
    )

    rendered = str(panel.render())

    assert "Up Ask: 57.0%" in rendered
    assert "Down Ask: 43.0%" in rendered
    assert "Quote Status: valid" in rendered
    assert "Execution Status: open" in rendered
    assert "Preview: DOWN @ 43.0%" in rendered


def test_window_panel_surfaces_side_aware_preview(tmp_path: Path):
    db_path = tmp_path / "tui.db"
    init_db(str(db_path))

    panel = WindowPanel()
    panel.update_stats(
        {
            "window_slug": "btc-updown-5m-123",
            "t_remaining": 145.0,
            "model_p_up": 0.61,
            "market_p_up": 0.57,
            "edge": 0.04,
            "preview_side": "down",
            "preview_market_price": 0.43,
        }
    )

    rendered = str(panel.render())

    assert "Model: 61.0%  Up Ask: 57.0%" in rendered
    assert "Preview: DOWN @ 43.0%" in rendered


def test_tui_app_instantiates():
    app = PolypocketApp()
    assert app.bot is not None


def test_tui_does_not_override_textual_start_time():
    app = PolypocketApp()
    assert not isinstance(app._start_time, datetime)
