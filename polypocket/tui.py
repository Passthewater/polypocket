"""Textual TUI dashboard for Polypocket."""

import asyncio
import logging
import threading
from datetime import datetime

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, RichLog, Static

from polypocket.bot import Bot
from polypocket.config import MAX_DAILY_LOSS, MAX_POSITION_USDC, MIN_EDGE_THRESHOLD, MIN_POSITION_USDC, TRADING_MODE
from polypocket.ledger import get_paper_balance, get_recent_trades, get_session_stats

log = logging.getLogger(__name__)


class StatusPanel(Static):
    def update_stats(self, stats: dict, db_path: str) -> None:
        btc = stats.get("btc_price")
        open_price = stats.get("window_open_price")
        displacement = stats.get("displacement")
        model = stats.get("model_p_up")
        market = stats.get("market_p_up")
        edge = stats.get("edge")
        preview_side = stats.get("preview_side")
        preview_market_price = stats.get("preview_market_price")
        up_ask = stats.get("up_ask")
        down_ask = stats.get("down_ask")
        quote_status = stats.get("quote_status")
        execution_status = stats.get("execution_status")
        sigma = stats.get("sigma_5min")
        position = stats.get("position")

        balance = get_paper_balance(db_path)
        total_pnl = get_session_stats(db_path)["pnl"]

        lines = ["[bold]STATUS[/bold]", ""]
        ptb_provisional = stats.get("ptb_provisional", False)
        lines.append(f"BTC Price: ${btc:,.2f}" if btc else "BTC Price: --")
        if open_price:
            ptb_label = "Window Open: ~$" if ptb_provisional else "Window Open: $"
            lines.append(f"{ptb_label}{open_price:,.2f}")
        else:
            lines.append("Window Open: --")
        lines.append(f"Displacement: {displacement:+.4%}" if displacement is not None else "Displacement: --")
        lines.append(f"P(Up) Model: {model:.1%}" if model is not None else "P(Up) Model: --")
        lines.append(f"P(Up) Market: {market:.1%}" if market is not None else "P(Up) Market: --")
        lines.append(f"Up Ask: {up_ask:.1%}" if up_ask is not None else "Up Ask: --")
        lines.append(f"Down Ask: {down_ask:.1%}" if down_ask is not None else "Down Ask: --")
        if preview_side is not None and preview_market_price is not None and edge is not None:
            lines.append(f"Preview: {preview_side.upper()} @ {preview_market_price:.1%}")
        lines.append(f"Edge: {edge:+.1%}" if edge is not None else "Edge: --")
        lines.append(f"Quote Status: {escape(str(quote_status))}" if quote_status else "Quote Status: --")
        lines.append(f"Execution Status: {escape(str(execution_status))}" if execution_status else "Execution Status: --")
        lines.append(f"Volatility: {sigma:.4%}" if sigma else "Volatility: --")
        lines.append("")
        lines.append(f"Paper Balance: ${balance:,.2f}")
        lines.append(f"Total P&L: ${total_pnl:+,.2f}")
        if position:
            lines.append(f"Position: {escape(str(position))}")
        self.update("\n".join(lines))


class WindowPanel(Static):
    def update_stats(self, stats: dict) -> None:
        slug = stats.get("window_slug", "--")
        t_remaining = stats.get("t_remaining")
        model = stats.get("model_p_up")
        up_ask = stats.get("up_ask")
        edge = stats.get("edge")
        preview_side = stats.get("preview_side")
        preview_market_price = stats.get("preview_market_price")

        lines = ["[bold]ACTIVE WINDOW[/bold]", ""]
        lines.append(f"Window: {slug}")
        if t_remaining is not None and t_remaining > 0:
            minutes, seconds = divmod(int(t_remaining), 60)
            lines.append(f"Time Left: {minutes}m {seconds:02d}s")
        else:
            lines.append("Time Left: --")

        if model is not None and up_ask is not None:
            lines.append(f"Model: {model:.1%}  Up Ask: {up_ask:.1%}")
            if edge is not None:
                indicator = " SIGNAL" if edge >= MIN_EDGE_THRESHOLD else ""
                if preview_side is not None and preview_market_price is not None:
                    lines.append(
                        f"Preview: {preview_side.upper()} @ {preview_market_price:.1%}  "
                        f"Edge: {edge:+.1%}{indicator}"
                    )
                else:
                    lines.append(f"Edge: {edge:+.1%}{indicator}")
        self.update("\n".join(lines))


class TradesPanel(Static):
    def update_trades(self, db_path: str) -> None:
        trades = get_recent_trades(db_path, limit=8)
        lines = ["[bold]RECENT TRADES[/bold]", ""]
        if not trades:
            lines.append("  No trades yet")
        for trade in trades:
            timestamp = trade["timestamp"][:8] if trade["timestamp"] else ""
            side = trade["side"].upper()
            status = trade["status"]
            pnl = trade["pnl"]
            model = trade.get("model_p_up")
            market = trade.get("market_p_up")
            if pnl is not None:
                outcome = "Won" if pnl > 0 else "Lost"
                pnl_str = f"${pnl:+.2f}"
                model_str = f"model {model:.0%}" if model else ""
                market_str = f"mkt {market:.0%}" if market else ""
                lines.append(f"  {timestamp} {side:4s} {outcome} {pnl_str}  ({model_str} / {market_str})")
            else:
                lines.append(f"  {timestamp} {side:4s} {status}")
        self.update("\n".join(lines))


class StatsBar(Static):
    def update_stats(self, db_path: str, since: str | None = None) -> None:
        stats = get_session_stats(db_path, since=since)
        wins, losses, total = stats["wins"], stats["losses"], stats["total"]
        pnl = stats["pnl"]
        win_rate = f"{wins / total:.0%}" if total > 0 else "--"
        self.update(
            f"[bold]STATS[/bold]  {wins}W / {losses}L / {total} total  |  "
            f"P&L: ${pnl:+,.2f}  |  Win rate: {win_rate}"
        )


class PolypocketApp(App):
    CSS = """
    #top { height: 12; }
    #status { width: 1fr; }
    #window { width: 1fr; }
    #trades { height: 12; }
    #stats-bar { height: 3; }
    #log { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("e", "adjust_edge", "Edge"),
        Binding("s", "adjust_size", "Size"),
        Binding("l", "adjust_loss", "Loss Limit"),
        Binding("r", "report", "Report"),
    ]

    def __init__(self, bot: Bot | None = None):
        super().__init__()
        self.bot = bot if bot is not None else Bot()
        self._session_start_time = datetime.now()
        self._bot_ready = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Horizontal(
            StatusPanel(id="status"),
            WindowPanel(id="window"),
            id="top",
        )
        yield TradesPanel(id="trades")
        yield StatsBar(id="stats-bar")
        # markup=False: log lines may contain user-controlled strings (CLOB
        # error payloads like "{'error': 'invalid fee rate (0), ...}") that
        # would otherwise be parsed as Rich markup and crash every refresh.
        yield RichLog(id="log", highlight=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Polypocket [{TRADING_MODE.upper()}]"
        rich_log = self.query_one("#log", RichLog)

        class TUIHandler(logging.Handler):
            def __init__(self, widget):
                super().__init__()
                self.widget = widget

            def emit(self, record):
                try:
                    self.widget.write(self.format(record))
                except Exception:
                    pass

        handler = TUIHandler(rich_log)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
        )
        logging.root.addHandler(handler)
        logging.root.setLevel(logging.INFO)

        # Init DB before bot thread starts so the refresh timer can query safely
        from polypocket.ledger import init_db
        init_db(self.bot.db_path)

        # Render panels immediately so they're not blank while waiting for data
        self._refresh_panels()

        self.bot.on_stats_update = lambda stats: self.call_from_thread(self._refresh_panels)
        self._bot_thread = threading.Thread(target=self._run_bot, daemon=True)
        self._bot_thread.start()
        self.set_interval(1.0, self._refresh_panels)

    def _run_bot(self) -> None:
        self._bot_ready = True
        asyncio.run(self.bot.run())

    def _refresh_panels(self) -> None:
        try:
            self.query_one("#status", StatusPanel).update_stats(self.bot.stats, self.bot.db_path)
            self.query_one("#window", WindowPanel).update_stats(self.bot.stats)
            self.query_one("#trades", TradesPanel).update_trades(self.bot.db_path)
            self.query_one("#stats-bar", StatsBar).update_stats(self.bot.db_path)
        except Exception as exc:
            log.error("Panel refresh error: %s", exc)

        elapsed = datetime.now() - self._session_start_time
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        self.title = f"Polypocket [{TRADING_MODE.upper()}]  Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}"

    def action_report(self) -> None:
        rich_log = self.query_one("#log", RichLog)
        stats = get_session_stats(self.bot.db_path)
        rich_log.write("\n--- SESSION REPORT ---")
        rich_log.write(f"Wins: {stats['wins']}  Losses: {stats['losses']}")
        rich_log.write(f"Total P&L: ${stats['pnl']:+,.2f}")
        rich_log.write(
            f"Win rate: {stats['wins'] / stats['total']:.0%}" if stats["total"] > 0 else "No trades"
        )
        rich_log.write(f"Paper balance: ${get_paper_balance(self.bot.db_path):,.2f}")

    def action_quit(self) -> None:
        self.bot.stop.set()
        self.exit()

    def action_adjust_edge(self) -> None:
        self.query_one("#log", RichLog).write(
            f"Current min edge: {MIN_EDGE_THRESHOLD:.1%}. Type new value (e.g. 0.05 for 5%) and press Enter."
        )

    def action_adjust_size(self) -> None:
        self.query_one("#log", RichLog).write(
            f"Position size: ${MIN_POSITION_USDC:.0f}-${MAX_POSITION_USDC:.0f} (edge x vol scaled)."
        )

    def action_adjust_loss(self) -> None:
        self.query_one("#log", RichLog).write(
            f"Current max daily loss: ${MAX_DAILY_LOSS:.2f}."
        )
