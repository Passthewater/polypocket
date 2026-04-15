"""Risk manager: daily loss limit and consecutive loss tracking."""

import logging

from polypocket.config import MAX_CONSECUTIVE_LOSSES, MAX_DAILY_LOSS
from polypocket.ledger import get_daily_pnl

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._consecutive_losses = 0

    def check(self) -> tuple[bool, str]:
        """Check if trading is allowed."""
        daily_pnl = get_daily_pnl(self.db_path)
        if daily_pnl < -MAX_DAILY_LOSS:
            return False, f"Daily loss limit hit: ${daily_pnl:.2f} < -${MAX_DAILY_LOSS}"

        if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return (
                False,
                f"Consecutive loss limit: {self._consecutive_losses} >= {MAX_CONSECUTIVE_LOSSES}",
            )

        return True, ""

    def record_loss(self) -> None:
        self._consecutive_losses += 1
        log.warning(
            "Consecutive losses: %d / %d",
            self._consecutive_losses,
            MAX_CONSECUTIVE_LOSSES,
        )

    def record_win(self) -> None:
        self._consecutive_losses = 0
