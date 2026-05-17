"""Risk manager: daily loss limit and consecutive loss tracking."""

import logging
import sqlite3
from contextlib import closing

from polypocket.config import MAX_CONSECUTIVE_LOSSES, MAX_DAILY_LOSS, TRADING_MODE, WALLET_LEDGER_DIVERGENCE_HALT_USDC
from polypocket.ledger import get_daily_pnl, get_live_starting_balance, set_live_starting_balance

log = logging.getLogger(__name__)


def compute_expected_usdc_balance(db_path: str) -> float:
    """Compute the proxy expected USDC balance from the ledger.

    Formula:
        expected = starting_balance
                   + SUM(size) for settled+won rows  (payout at $1/share)
                   - SUM(entry_price * size) for status IN ('open', 'settled')

    Notes:
    - `fees` is stored in shares (not USDC), so it is NOT subtracted here.
      Fee drag is implicit in `entry_price * size` (post-only rest price).
    - 'placed' rows are NOT subtracted: Polymarket CLOB does NOT escrow pUSD
      on resting maker orders. The proxy account is debited only at fill time
      (when the order transitions to 'open' or 'settled'). Including 'placed'
      would cause a false-low expected balance that both (a) triggers spurious
      divergence halts immediately after placement and (b) cancels out the
      signal when a silent maker fill actually lands — exactly the scenario the
      watchdog is meant to catch.
    - Only 'open' and 'settled' represent real cash commitments. 'rejected',
      'reserved', and 'placed' are excluded.
    - The function returns starting_balance when no live_account row is set
      (the caller is responsible for bootstrapping the anchor first).
    """
    starting = get_live_starting_balance(db_path)
    if starting is None:
        # Anchor not yet set — return 0.0 as a sentinel; caller should prime.
        return 0.0

    with closing(sqlite3.connect(db_path)) as conn:
        # Settled-and-won payout: $1.00 per share.
        won_row = conn.execute(
            """
            SELECT COALESCE(SUM(size), 0.0)
            FROM trades
            WHERE status = 'settled'
              AND outcome IS NOT NULL
              AND outcome = side
            """
        ).fetchone()
        payout_won = won_row[0]

        # Cost of all non-rejected trades: entry_price × size.
        # 'reserved' rows are in-flight with no cash movement yet — exclude them too.
        cost_row = conn.execute(
            """
            SELECT COALESCE(SUM(entry_price * size), 0.0)
            FROM trades
            WHERE status IN ('open', 'settled')
            """
        ).fetchone()
        total_cost = cost_row[0]

    return starting + payout_won - total_cost


def check_wallet_divergence(client, db_path: str) -> tuple[bool, str | None]:
    """Check that the wallet's actual USDC balance matches the ledger's expectation.

    Returns (True, None) when everything is fine or when in paper mode.
    Returns (False, reason) when the wallet diverges below the threshold.

    Bootstrap semantics:
      - If no starting balance is persisted, calls client.get_usdc_balance()
        and writes it as the anchor.  Returns (True, None) on the priming tick.
      - Subsequent ticks: compute expected from ledger, compare to actual.
    """
    if TRADING_MODE != "live":
        return (True, None)

    starting = get_live_starting_balance(db_path)
    if starting is None:
        # Prime the anchor on the first live tick.
        actual = client.get_usdc_balance()
        set_live_starting_balance(db_path, actual)
        log.info("wallet-watchdog: primed starting balance $%.2f", actual)
        return (True, None)

    expected = compute_expected_usdc_balance(db_path)
    actual = client.get_usdc_balance()

    if actual < expected - WALLET_LEDGER_DIVERGENCE_HALT_USDC:
        divergence = expected - actual
        reason = (
            f"wallet divergence: expected ${expected:.2f}, actual ${actual:.2f} "
            f"(gap ${divergence:.2f} > threshold ${WALLET_LEDGER_DIVERGENCE_HALT_USDC:.2f})"
        )
        log.error(
            "wallet-watchdog HALT: %s", reason,
            extra={"expected_usdc": expected, "actual_usdc": actual, "divergence": divergence},
        )
        return (False, reason)

    return (True, None)


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
