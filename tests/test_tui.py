from datetime import datetime

from polypocket.tui import PolypocketApp


def test_tui_app_instantiates():
    app = PolypocketApp()
    assert app.bot is not None


def test_tui_does_not_override_textual_start_time():
    app = PolypocketApp()
    assert not isinstance(app._start_time, datetime)
