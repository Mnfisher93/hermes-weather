"""Rich terminal dashboard — 3-panel layout with live updates."""
from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich import box


# ── Color palette ───────────────────────────────────────────────
COLORS = {
    "LAG": "green",
    "LOCK": "cyan",
    "FADE": "yellow",
    "CONTRA": "magenta",
    "YES": "bold green",
    "NO": "bold red",
    "header": "bold white on dark_blue",
    "ok": "green",
    "warn": "yellow",
    "error": "bold red",
    "muted": "dim white",
    "accent": "bold cyan",
}


class Dashboard:
    """
    Three-panel terminal UI:
      ┌──────────────┬──────────────────┐
      │              │   POSITIONS      │
      │  AGENT LOG   ├──────────────────┤
      │              │   MARKET SCAN    │
      └──────────────┴──────────────────┘
    """

    MAX_LOG_LINES = 200

    def __init__(self, sim_mode: bool = True):
        self.console = Console()
        self.sim_mode = sim_mode

        # State
        self._log_lines: list[tuple[str, str, str]] = []  # (timestamp, message, style)
        self._positions: list[dict] = []
        self._scan_results: list[dict] = []
        self._risk_status: dict = {}
        self._cycle_count: int = 0
        self._last_scan: Optional[str] = None

        # Initial welcome
        mode = "[SIM]" if sim_mode else "[LIVE]"
        self.log(f"Hermes Weather Arbitrage Bot — {mode}", style="accent")
        self.log("Initializing...", style="muted")

    # ── Logging ─────────────────────────────────────────────────

    def log(self, message: str, style: str = ""):
        """Add a timestamped entry to the agent log."""
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_lines.append((ts, message, style))
        if len(self._log_lines) > self.MAX_LOG_LINES:
            self._log_lines = self._log_lines[-self.MAX_LOG_LINES:]

    def log_signal(self, signal: dict):
        """Log a trade signal with color coding."""
        sig_type = signal.get("type", "?")
        color = COLORS.get(sig_type, "white")
        city = signal.get("city", "?")
        side = signal.get("side", "?")
        price = signal.get("yes_price", 0)
        prob = signal.get("model_prob", 0)
        edge = signal.get("edge", 0)
        size = signal.get("position_usd", 0)
        self.log(
            f"[{sig_type}] {city} — {side} @ ${price:.2f} | "
            f"model={prob:.2f} edge={edge:.3f} size=${size:.2f}",
            style=color,
        )

    def log_risk_block(self, reason: str):
        """Log a risk-blocked trade."""
        self.log(f"⛔ BLOCKED: {reason}", style="warn")

    # ── State updates ───────────────────────────────────────────

    def update_positions(self, positions: list[dict]):
        self._positions = positions

    def update_scan_results(self, markets: list[dict], signals: list[dict]):
        self._scan_results = signals
        self._cycle_count += 1
        self._last_scan = datetime.now().strftime("%H:%M:%S")

    def update_risk_status(self, status: dict):
        self._risk_status = status

    # ── Panel builders ──────────────────────────────────────────

    def _build_log_panel(self) -> Panel:
        text = Text()
        # Show last ~40 lines that fit
        visible = self._log_lines[-40:]
        for ts, msg, style in visible:
            text.append(f"[{ts}] ", style="dim")
            text.append(f"{msg}\n", style=style or "white")

        return Panel(
            text,
            title="[bold cyan]⚡ Agent Log[/bold cyan]",
            border_style="cyan",
            box=box.ROUNDED,
        )

    def _build_positions_panel(self) -> Panel:
        table = Table(
            box=box.SIMPLE_HEAVY,
            show_edge=False,
            pad_edge=False,
            expand=True,
        )
        table.add_column("City", style="bold white", no_wrap=True)
        table.add_column("Side", justify="center")
        table.add_column("Type", justify="center")
        table.add_column("Entry", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Edge", justify="right")
        table.add_column("Conf.", justify="center")

        if not self._positions:
            table.add_row("—", "—", "—", "—", "—", "—", "—")
        else:
            for p in self._positions:
                side_style = COLORS.get(p.get("side", ""), "white")
                type_style = COLORS.get(p.get("type", ""), "white")
                conf = p.get("weather_confidence", "?")
                conf_style = "green" if conf == "high" else "yellow" if conf == "medium" else "red"
                table.add_row(
                    p.get("city", "?"),
                    Text(p.get("side", "?"), style=side_style),
                    Text(p.get("type", "?"), style=type_style),
                    f"${p.get('yes_price', 0):.2f}",
                    f"${p.get('position_usd', 0):.2f}",
                    f"{p.get('edge', 0):.3f}",
                    Text(conf, style=conf_style),
                )

        # Risk status footer
        rs = self._risk_status
        if rs:
            status_line = (
                f"Bankroll: ${rs.get('bankroll', 0):.2f} | "
                f"Exposure: ${rs.get('current_exposure', 0):.2f}/"
                f"${rs.get('max_exposure', 0):.2f} | "
                f"Drawdown: {rs.get('drawdown_pct', 0):.1%}"
            )
            halted = rs.get("halted", False)
            border = "red" if halted else "green"
            title_extra = " [bold red]⛔ HALTED[/bold red]" if halted else ""
        else:
            status_line = "Waiting for first scan..."
            border = "green"
            title_extra = ""

        return Panel(
            Align.center(table),
            title=f"[bold green]📊 Positions ({len(self._positions)}){title_extra}[/bold green]",
            subtitle=f"[dim]{status_line}[/dim]",
            border_style=border,
            box=box.ROUNDED,
        )

    def _build_scan_panel(self) -> Panel:
        table = Table(
            box=box.SIMPLE,
            show_edge=False,
            pad_edge=False,
            expand=True,
        )
        table.add_column("City", style="bold", no_wrap=True)
        table.add_column("Question", max_width=40)
        table.add_column("Side", justify="center")
        table.add_column("YES$", justify="right")
        table.add_column("Model", justify="right")
        table.add_column("Edge", justify="right")
        table.add_column("EV", justify="right")
        table.add_column("$Size", justify="right")

        if not self._scan_results:
            table.add_row("—", "Scanning...", "—", "—", "—", "—", "—", "—")
        else:
            for s in self._scan_results[:15]:  # top 15
                edge = s.get("edge", 0)
                edge_style = "bold green" if edge > 0.10 else "green" if edge > 0.05 else "yellow"
                type_style = COLORS.get(s.get("type", ""), "white")
                table.add_row(
                    s.get("city", "?"),
                    (s.get("question", "")[:38] + "…") if len(s.get("question", "")) > 38 else s.get("question", ""),
                    Text(f"{s.get('side', '?')}", style=type_style),
                    f"${s.get('yes_price', 0):.2f}",
                    f"{s.get('model_prob', 0):.2f}",
                    Text(f"{edge:.3f}", style=edge_style),
                    f"{s.get('ev', 0):.3f}",
                    f"${s.get('position_usd', 0):.2f}",
                )

        mode_tag = "[yellow]SIM[/yellow]" if self.sim_mode else "[red]LIVE[/red]"
        scan_info = f"Cycle #{self._cycle_count}" if self._cycle_count else "Waiting..."
        if self._last_scan:
            scan_info += f" @ {self._last_scan}"

        return Panel(
            table,
            title=f"[bold yellow]🔍 Market Scan — {mode_tag}[/bold yellow]",
            subtitle=f"[dim]{scan_info} | Signals: {len(self._scan_results)}[/dim]",
            border_style="yellow",
            box=box.ROUNDED,
        )

    # ── Layout assembly ─────────────────────────────────────────

    def build_layout(self) -> Layout:
        layout = Layout()

        # Header
        header_text = Text()
        header_text.append("  ⚡ HERMES ", style="bold white on dark_blue")
        header_text.append(" Weather Arbitrage Engine ", style="bold cyan")
        mode = "SIM MODE" if self.sim_mode else "⚠ LIVE TRADING"
        header_text.append(f" [{mode}] ", style="bold yellow" if self.sim_mode else "bold red on white")

        layout.split_column(
            Layout(Panel(header_text, box=box.HEAVY, style="bold blue"), size=3, name="header"),
            Layout(name="body"),
        )

        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )

        layout["right"].split_column(
            Layout(name="positions", ratio=1),
            Layout(name="scan", ratio=2),
        )

        layout["left"].update(self._build_log_panel())
        layout["positions"].update(self._build_positions_panel())
        layout["scan"].update(self._build_scan_panel())

        return layout

    # ── Run context manager ─────────────────────────────────────

    def start(self) -> Live:
        """Returns a rich.Live context that auto-refreshes the layout."""
        return Live(
            self.build_layout(),
            console=self.console,
            refresh_per_second=2,
            screen=True,
        )

    def refresh(self, live: Live):
        """Update the live display."""
        live.update(self.build_layout())
