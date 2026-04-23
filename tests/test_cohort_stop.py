"""Cohort kill-file gate — bot skips trade evaluation when the file exists.

The cohort watchdog writes `.cohort_stop` when a safety rail trips. The bot
polls this file at the top of every book-update callback; presence of the
file short-circuits trade evaluation. Removable by deleting the file.
"""
from pathlib import Path

from polypocket.bot import cohort_stop_requested


def test_no_file_returns_false(tmp_path):
    assert cohort_stop_requested(tmp_path / ".cohort_stop") is False


def test_file_exists_returns_true(tmp_path):
    kill = tmp_path / ".cohort_stop"
    kill.write_text("loss cap hit\n")
    assert cohort_stop_requested(kill) is True


def test_empty_file_still_counts(tmp_path):
    kill = tmp_path / ".cohort_stop"
    kill.touch()
    assert cohort_stop_requested(kill) is True
