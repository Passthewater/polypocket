"""SQLite trade and paper-balance ledger."""

import sqlite3
from contextlib import closing

from polypocket.config import PAPER_STARTING_BALANCE


def init_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    window_slug TEXT NOT NULL UNIQUE,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    size REAL NOT NULL,
                    fees REAL NOT NULL,
                    model_p_up REAL,
                    market_p_up REAL,
                    edge REAL,
                    outcome TEXT,
                    pnl REAL,
                    status TEXT NOT NULL DEFAULT 'open'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash_balance REAL NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO paper_account (id, cash_balance)
                VALUES (1, ?)
                """,
                (PAPER_STARTING_BALANCE,),
            )

            # Auto-clean duplicate window_slugs (keep earliest row per slug)
            conn.execute(
                """
                DELETE FROM trades
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM trades GROUP BY window_slug
                )
                """
            )

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)"
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_window_slug
                ON trades(window_slug)
                """
            )
            # Idempotent column adds for live trading (nullable — paper rows remain valid).
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
            if "external_order_id" not in existing_cols:
                conn.execute("ALTER TABLE trades ADD COLUMN external_order_id TEXT")
            if "error" not in existing_cols:
                conn.execute("ALTER TABLE trades ADD COLUMN error TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS window_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    window_slug TEXT NOT NULL,
                    snapshot_type TEXT NOT NULL,
                    btc_price REAL,
                    window_open_price REAL,
                    ptb_provisional INTEGER,
                    displacement REAL,
                    sigma_5min REAL,
                    model_p_up REAL,
                    t_remaining REAL,
                    up_ask REAL,
                    down_ask REAL,
                    market_p_up REAL,
                    edge REAL,
                    preview_side TEXT,
                    quote_status TEXT,
                    up_book_json TEXT,
                    down_book_json TEXT,
                    trade_fired INTEGER,
                    skip_reason TEXT,
                    outcome TEXT,
                    final_price REAL,
                    UNIQUE(window_slug, snapshot_type)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_window ON window_snapshots(window_slug)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_type ON window_snapshots(snapshot_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON window_snapshots(timestamp DESC)"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def find_duplicate_window_slugs(db_path: str) -> list[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT window_slug
            FROM trades
            GROUP BY window_slug
            HAVING COUNT(*) > 1
            ORDER BY window_slug
            """
        ).fetchall()
        return [row[0] for row in rows]


def find_trade_by_window_slug(db_path: str, window_slug: str) -> dict | None:
    """Return the most recent trade for a window slug, if one exists."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE window_slug = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (window_slug,),
        ).fetchone()
        return dict(row) if row else None


def get_open_trade_by_window_slug(db_path: str, window_slug: str) -> dict | None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE window_slug = ? AND status = 'open'
            ORDER BY id DESC
            LIMIT 1
            """,
            (window_slug,),
        ).fetchone()
        return dict(row) if row else None


def find_unsettled_trades(db_path: str) -> list[dict]:
    """Return all trades with status 'open' or 'reserved' (not yet settled)."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE status IN ('open', 'reserved')
            ORDER BY id
            """,
        ).fetchall()
        return [dict(row) for row in rows]


def log_trade(
    db_path: str,
    window_slug: str,
    side: str,
    entry_price: float,
    size: float,
    fees: float,
    model_p_up: float,
    market_p_up: float,
    edge: float,
    outcome: str | None,
    pnl: float | None,
    status: str,
) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        cursor = conn.execute(
            """
            INSERT INTO trades (
                window_slug, side, entry_price, size, fees,
                model_p_up, market_p_up, edge, outcome, pnl, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                window_slug,
                side,
                entry_price,
                size,
                fees,
                model_p_up,
                market_p_up,
                edge,
                outcome,
                pnl,
                status,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def update_trade(
    db_path: str,
    trade_id: int,
    outcome: str | None,
    pnl: float | None,
    status: str,
    external_order_id: str | None = None,
    error: str | None = None,
) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            UPDATE trades
            SET outcome = ?, pnl = ?, status = ?,
                external_order_id = COALESCE(?, external_order_id),
                error = COALESCE(?, error)
            WHERE id = ?
            """,
            (outcome, pnl, status, external_order_id, error, trade_id),
        )
        conn.commit()


def update_trade_status(db_path: str, trade_id: int, status: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE trades SET status = ? WHERE id = ?", (status, trade_id))
        conn.commit()


def get_recent_trades(db_path: str, limit: int = 20) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_daily_pnl(db_path: str) -> float:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0.0)
            FROM trades
            WHERE date(timestamp, 'localtime') = date('now', 'localtime')
              AND pnl IS NOT NULL
            """
        ).fetchone()
        return row[0]


def get_session_stats(db_path: str, since: str | None = None) -> dict:
    with closing(sqlite3.connect(db_path)) as conn:
        if since:
            rows = conn.execute(
                """
                SELECT pnl
                FROM trades
                WHERE timestamp >= ? AND pnl IS NOT NULL
                """,
                (since,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT pnl FROM trades WHERE pnl IS NOT NULL"
            ).fetchall()

    wins = sum(1 for row in rows if row[0] > 0)
    losses = sum(1 for row in rows if row[0] < 0)
    return {
        "wins": wins,
        "losses": losses,
        "total": wins + losses,
        "pnl": sum(row[0] for row in rows),
    }


def get_paper_balance(db_path: str) -> float:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT cash_balance FROM paper_account WHERE id = 1"
        ).fetchone()
        return row[0]


def deduct_paper_balance(db_path: str, amount: float) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            UPDATE paper_account
            SET cash_balance = cash_balance - ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (amount,),
        )
        conn.commit()


def credit_paper_balance(db_path: str, amount: float) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            UPDATE paper_account
            SET cash_balance = cash_balance + ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (amount,),
        )
        conn.commit()


def log_snapshot(
    db_path: str,
    window_slug: str,
    snapshot_type: str,
    stats: dict,
    book_depth: dict | None = None,
    trade_fired: bool | None = None,
    skip_reason: str | None = None,
    outcome: str | None = None,
    final_price: float | None = None,
) -> None:
    """Write a window snapshot (open/decision/close) for finetuning data capture."""
    import json

    up_book_json = None
    down_book_json = None
    if book_depth is not None:
        up_book_json = json.dumps(book_depth.get("up"))
        down_book_json = json.dumps(book_depth.get("down"))

    trade_fired_int = None
    if trade_fired is not None:
        trade_fired_int = 1 if trade_fired else 0

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO window_snapshots (
                window_slug, snapshot_type,
                btc_price, window_open_price, ptb_provisional, displacement,
                sigma_5min, model_p_up, t_remaining,
                up_ask, down_ask, market_p_up, edge, preview_side, quote_status,
                up_book_json, down_book_json,
                trade_fired, skip_reason,
                outcome, final_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                window_slug,
                snapshot_type,
                stats.get("btc_price"),
                stats.get("window_open_price"),
                1 if stats.get("ptb_provisional") else 0,
                stats.get("displacement"),
                stats.get("sigma_5min"),
                stats.get("model_p_up"),
                stats.get("t_remaining"),
                stats.get("up_ask"),
                stats.get("down_ask"),
                stats.get("market_p_up"),
                stats.get("edge"),
                stats.get("preview_side"),
                stats.get("quote_status"),
                up_book_json,
                down_book_json,
                trade_fired_int,
                skip_reason,
                outcome,
                final_price,
            ),
        )
        conn.commit()


def get_snapshots_for_window(db_path: str, window_slug: str) -> list[dict]:
    """Retrieve all snapshots for a given window slug."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM window_snapshots WHERE window_slug = ? ORDER BY id",
            (window_slug,),
        ).fetchall()
        return [dict(row) for row in rows]
