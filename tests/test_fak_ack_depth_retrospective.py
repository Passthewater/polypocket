"""Unit test for fak_ack_depth_retrospective.py per-fill depth computation.

Step 3.5 spec: ``test_depth_support_with_synthetic_payload`` hand-builds a
``book_at_ack`` dict with known sizes/prices, calls the per-fill depth
function with ``side=up`` and ``limit=0.55``, and asserts the returned USDC
value equals the hand-computed sum across BOTH Polymarket NegRisk match
paths (direct against opposing-side asks AND pair-merge against
same-side-mirror bids).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.fak_ack_depth_retrospective import depth_usdc_at_or_below_limit


def test_depth_support_with_synthetic_payload() -> None:
    """Verify per-fill depth computation against hand-computed math.

    Scenario: BUY UP trade with ``limit=0.55``.

    Two match paths on Polymarket NegRisk:
    (a) Direct: ``up_book["asks"]`` with ``price <= 0.55``.  USDC = ``size * price``.
    (b) Pair-merge: ``down_book["bids"]`` with ``price >= 1 - 0.55 = 0.45``.
        USDC = ``size * (1 - price)``.

    Book layout::

        up_book asks (direct match):
          price=0.55, size=80    -> 80 * 0.55 = 44.00         (at limit, included)
          price=0.60, size=20    -> excluded (0.60 > 0.55)

        down_book bids (pair-merge):
          price=0.60, size=150   -> 150 * (1 - 0.60) = 60.00  (>= 0.45, included)
          price=0.50, size=100   -> 100 * (1 - 0.50) = 50.00  (>= 0.45, included)
          price=0.30, size=200   -> excluded (0.30 < 0.45)

        up_book bids (irrelevant — own-side bids never match our BUY):
          price=0.40, size=999   -> ignored

        down_book asks (irrelevant for BUY UP — only relevant to BUY DOWN):
          price=0.42, size=999   -> ignored

    Hand-computed expected = 44.00 + 60.00 + 50.00 = **154.00 USDC**.
    """
    book_at_ack = {
        "up_book": {
            # Direct match path for BUY UP
            "asks": [
                {"price": "0.55", "size": "80"},    # at limit -> included
                {"price": "0.60", "size": "20"},    # above limit -> excluded
            ],
            # Own-side bids: never match our BUY UP.  Should be ignored.
            "bids": [
                {"price": "0.40", "size": "999"},
            ],
        },
        "down_book": {
            # Pair-merge path for BUY UP
            "bids": [
                {"price": "0.60", "size": "150"},   # >= 0.45 -> included
                {"price": "0.50", "size": "100"},   # >= 0.45 -> included
                {"price": "0.30", "size": "200"},   # < 0.45  -> excluded
            ],
            # Down-side asks: only matter for BUY DOWN; ignored here.
            "asks": [
                {"price": "0.42", "size": "999"},
            ],
        },
    }

    result = depth_usdc_at_or_below_limit(
        side="up",
        entry_price=0.55,
        book_at_ack=book_at_ack,
    )

    # Hand-computed:
    #   direct:    80 * 0.55                       = 44.00
    #   pair-merge:
    #              150 * (1 - 0.60) = 150 * 0.40   = 60.00
    #              100 * (1 - 0.50) = 100 * 0.50   = 50.00
    #   total:                                       154.00
    expected = 154.00
    assert abs(result - expected) < 1e-9, (
        f"Expected depth {expected:.4f} USDC, got {result:.4f}"
    )
