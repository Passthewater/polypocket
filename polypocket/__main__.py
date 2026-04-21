"""CLI entry point for polypocket."""

import asyncio
import logging
import os
import sys


def _validate_live_env() -> None:
    from polypocket.config import (
        CLOB_API_KEY, CLOB_PASSPHRASE, CLOB_SECRET,
        POLYMARKET_PROXY_ADDRESS, PRIVATE_KEY,
    )
    missing = [
        name for name, val in [
            ("PRIVATE_KEY", PRIVATE_KEY),
            ("PROXY_ADDRESS", POLYMARKET_PROXY_ADDRESS),
            ("CLOB_API_KEY", CLOB_API_KEY),
            ("CLOB_SECRET", CLOB_SECRET),
            ("CLOB_PASSPHRASE", CLOB_PASSPHRASE),
        ] if not val
    ]
    if missing:
        print(
            "ERROR: TRADING_MODE=live but missing env vars: "
            + ", ".join(missing)
            + "\nRun `python scripts/derive_clob_creds.py` if you need CLOB_*.",
            file=sys.stderr,
        )
        sys.exit(1)


def _build_bot(db_override: str | None, dry_run: bool):
    """Build a Bot configured for the current TRADING_MODE.

    Shared by `run` and `tui` so the TUI picks up live-mode wiring and the
    live DB path instead of silently falling back to `paper_trades.db`.
    """
    from polypocket.bot import Bot
    from polypocket.config import (
        CLOB_API_KEY, CLOB_PASSPHRASE, CLOB_SECRET,
        LIVE_DB_PATH, MIN_POSITION_USDC, PAPER_DB_PATH,
        POLYMARKET_HOST, POLYMARKET_PROXY_ADDRESS,
        PRIVATE_KEY, TRADING_MODE, CHAIN_ID,
    )
    log = logging.getLogger("polypocket")

    if TRADING_MODE == "live":
        _validate_live_env()
        from polypocket.clients.polymarket import PolymarketClient
        client = PolymarketClient(
            host=POLYMARKET_HOST,
            chain_id=CHAIN_ID,
            private_key=PRIVATE_KEY,
            api_creds={
                "key": CLOB_API_KEY,
                "secret": CLOB_SECRET,
                "passphrase": CLOB_PASSPHRASE,
            },
            proxy_address=POLYMARKET_PROXY_ADDRESS,
            dry_run=dry_run,
        )
        balance = client.get_usdc_balance()
        log.info("Live startup: proxy=%s balance=$%.2f dry_run=%s",
                 POLYMARKET_PROXY_ADDRESS, balance, dry_run)
        if balance < MIN_POSITION_USDC:
            log.error("Balance $%.2f < MIN_POSITION_USDC $%.2f — aborting",
                      balance, MIN_POSITION_USDC)
            sys.exit(1)

        db_path = os.path.abspath(db_override or LIVE_DB_PATH)
        log.info("Live DB: %s", db_path)
        return Bot(db_path=db_path, live_order_client=client)

    if dry_run:
        print("--dry-run is only valid with TRADING_MODE=live", file=sys.stderr)
        sys.exit(1)
    db_path = os.path.abspath(db_override or PAPER_DB_PATH)
    log.info("Paper DB: %s", db_path)
    return Bot(db_path=db_path)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    command = sys.argv[1] if len(sys.argv) > 1 else "observe"
    if command == "observe":
        from polypocket.observer import run_observer

        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        asyncio.run(run_observer(duration))
        return
    if command == "run":
        import argparse

        parser = argparse.ArgumentParser(prog="polypocket run")
        parser.add_argument("--db", default=None, help="Override DB path")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Live mode only: sign orders but do not POST to CLOB",
        )
        args = parser.parse_args(sys.argv[2:])

        bot = _build_bot(db_override=args.db, dry_run=args.dry_run)
        try:
            asyncio.run(bot.run())
        except KeyboardInterrupt:
            pass
        return
    if command == "tui":
        import argparse

        parser = argparse.ArgumentParser(prog="polypocket tui")
        parser.add_argument("--db", default=None, help="Override DB path")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Live mode only: sign orders but do not POST to CLOB",
        )
        args = parser.parse_args(sys.argv[2:])

        bot = _build_bot(db_override=args.db, dry_run=args.dry_run)

        from polypocket.tui import PolypocketApp
        app = PolypocketApp(bot=bot)
        app.run()
        return
    if command == "backtest":
        from polypocket.backtester import run_backtest_cli

        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        asyncio.run(run_backtest_cli(days))
        return

    print(f"Unknown command: {command}")
    print("Usage: python -m polypocket observe [duration_minutes]")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
