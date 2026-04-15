"""CLI entry point for polypocket."""

import asyncio
import logging
import sys


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
        from polypocket.bot import Bot

        bot = Bot()
        try:
            asyncio.run(bot.run())
        except KeyboardInterrupt:
            pass
        return
    if command == "tui":
        from polypocket.tui import PolypocketApp

        app = PolypocketApp()
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
