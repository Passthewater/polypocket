"""SQLite trade and paper-balance ledger."""

import sqlite3
from contextlib import closing

from polypocket.config import PAPER_STARTING_BALANCE


def init_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                window_slug TEXT NOT NULL,
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
            );

            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash_balance REAL NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            INSERT OR IGNORE INTO paper_account (id, cash_balance)
            VALUES (1, {PAPER_STARTING_BALANCE});
            """
        )
        duplicates = conn.execute(
            """
            SELECT window_slug
            FROM trades
            GROUP BY window_slug
            HAVING COUNT(*) > 1
            ORDER BY window_slug
            """
        ).fetchall()
        if duplicates:
            slugs = ", ".join(row[0] for row in duplicates)
            raise RuntimeError(f"Duplicate window_slug values exist: {slugs}")

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_window_slug
                ON trades(window_slug);
            """
        )


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


def update_trade(db_path: str, trade_id: int, outcome: str, pnl: float, status: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE trades SET outcome = ?, pnl = ?, status = ? WHERE id = ?",
            (outcome, pnl, status, trade_id),
        )
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
            WHERE date(timestamp) = date('now') AND pnl IS NOT NULL
            """
        ).fetchone()
        return row[0]


def get_session_stats(db_path: str) -> dict:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT pnl
            FROM trades
            WHERE date(timestamp) = date('now') AND pnl IS NOT NULL
            """
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
