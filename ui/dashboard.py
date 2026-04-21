"""Rich terminal dashboard — live-streaming 5-zone layout.

Zones:
    ┌─────────────────────────── HEADER (mode + cycle + uptime + countdown) ──┐
    ├──────────────────────── STATS STRIP (bankroll/exposure/PnL/signals) ────┤
    ├──────────── AGENT LOG ───────────────┬──────── POSITIONS ───────────────┤
    │ 14:22:01 Scanning…                   │ City    Side Type  Entry  Size   │
    │ 14:23:05 [LOCK] Seoul NO @ 0.03      │ Seoul   NO   LOCK  0.03  $10.00  │
    │ …                                    │                                  │
    │                                      ├──────── SIGNALS ─────────────────┤
    │                                      │ City   Q  Side YES$ Model Edge  │
    ├──────────────────────── LIVE SCAN PROGRESS (bar + current city) ────────┤
    └──────────────────────────────────────────────────────────────────────────┘

The progress panel is the fix for the "loading forever" problem — callers
should invoke dashboard.tick_progress(i, n, city) inside the edge-loop so
the bar animates in real time instead of freezing for ~3 minutes.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from rich.progress_bar import ProgressBar
from rich import box


# ── Palette ─────────────────────────────────────────────────────
# Kept intentionally tight so the terminal stays readable. If you
# add a new signal type, add it here and it'll propagate.
COLORS = {
    "LAG":    "bright_green",
    "LOCK":   "bright_cyan",
    "FADE":   "yellow",
    "CONTRA": "magenta",
    "YES":    "bold bright_green",
    "NO":     "bold bright_red",
    "ok":     "bright_green",
    "warn":   "yellow",
    "error":  "bold bright_red",
    "muted":  "dim white",
    "accent": "bold bright_cyan",
}


class Dashboard:
    MAX_LOG_LINES = 400

    def __init__(self, sim_mode: bool = True):
        self.console = Console()
        self.sim_mode = sim_mode

        # Rolling state
        self._log_lines: list[tuple[str, str, str]] = []
        self._positions: list[dict] = []
        self._scan_results: list[dict] = []
        self._risk_status: dict = {}
        self._cycle_count: int = 0
        self._last_scan_at: Optional[str] = None
        self._last_cycle_secs: Optional[float] = None

        # Live progress during edge-loop
        self._progress_i: int = 0
        self._progress_n: int = 0
        self._progress_city: str = ""
        self._progress_active: bool = False

        # Adaptive + calibration snapshots
        self._tuner: dict = {}
        self._brier: Optional[dict] = None
        self._signals_today: int = 0
        self._pnl_today: float = 0.0

        # Countdown until next scan — set by main.py
        self._next_scan_at: Optional[float] = None  # epoch seconds
        self._started_at = time.time()

        self.log("Hermes — Weather Arbitrage Engine", style="accent")
        self.log(f"Mode: {'SIM' if sim_mode else 'LIVE'}", style="ok" if sim_mode else "error")

    # ── Logging API ─────────────────────────────────────────────

    def log(self, message: str, style: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_lines.append((ts, message, style))
        if len(self._log_lines) > self.MAX_LOG_LINES:
            self._log_lines = self._log_lines[-self.MAX_LOG_LINES:]

    def log_signal(self, signal: dict):
        t = signal.get("type", "?")
        color = COLORS.get(t, "white")
        self.log(
            f"[{t}] {signal.get('city', '?')} — {signal.get('side', '?')} @ "
            f"${signal.get('exec_price', signal.get('yes_price', 0)):.2f} │ "
            f"model={signal.get('model_prob', 0):.2f} "
            f"edge={signal.get('edge', 0):.3f} "
            f"ev={signal.get('ev', 0):.3f} "
            f"size=${signal.get('position_usd', 0):.2f}",
            style=color,
        )
        self._signals_today += 1

    def log_risk_block(self, reason: str):
        self.log(f"⛔ BLOCKED  {reason}", style="warn")

    # ── State mutators ──────────────────────────────────────────

    def update_positions(self, positions: list[dict]):
        self._positions = positions

    def update_scan_results(self, markets: list[dict], signals: list[dict]):
        self._scan_results = signals
        self._cycle_count += 1
        self._last_scan_at = datetime.now().strftime("%H:%M:%S")

    def update_risk_status(self, status: dict):
        self._risk_status = status

    def update_tuner(self, tuner: dict):
        self._tuner = tuner

    def update_brier(self, brier: Optional[dict]):
        self._brier = brier

    def set_next_scan_eta(self, epoch_seconds: float):
        self._next_scan_at = epoch_seconds

    def set_cycle_elapsed(self, secs: float):
        self._last_cycle_secs = secs

    # ── Live scan progress ──────────────────────────────────────

    def start_progress(self, total: int, label: str = "Scanning markets"):
        self._progress_active = True
        self._progress_i = 0
        self._progress_n = total
        self._progress_city = label

    def tick_progress(self, i: int, n: int, city: str = ""):
        self._progress_active = True
        self._progress_i = i
        self._progress_n = max(n, 1)
        self._progress_city = city

    def end_progress(self):
        self._progress_active = False
        self._progress_city = ""

    # ── Panel builders ──────────────────────────────────────────

    def _build_header(self) -> Panel:
        mode_badge = Text(" SIM ", style="black on bright_yellow") if self.sim_mode \
            else Text(" ⚠ LIVE ", style="bold white on bright_red")

        uptime = _fmt_duration(time.time() - self._started_at)
        next_in = ""
        if self._next_scan_at:
            secs = max(0, self._next_scan_at - time.time())
            next_in = f" │ Next scan in {_fmt_duration(secs)}"

        cycle_str = f"Cycle #{self._cycle_count}" if self._cycle_count else "Cycle #0 (warming up)"
        last_cycle = ""
        if self._last_cycle_secs:
            last_cycle = f" │ Last cycle {self._last_cycle_secs:.0f}s"

        bar = Text()
        bar.append(" ⚡ HERMES ", style="bold white on dark_blue")
        bar.append("  Weather Arbitrage ", style="bold bright_cyan")
        bar.append_text(mode_badge)
        bar.append(f"   {cycle_str}", style="bold white")
        bar.append(last_cycle, style="dim")
        bar.append(f"   Uptime {uptime}", style="dim")
        bar.append(next_in, style="yellow" if self._next_scan_at else "dim")

        return Panel(bar, box=box.HEAVY, border_style="bright_blue", padding=(0, 1))

    def _build_stats(self) -> Panel:
        rs = self._risk_status or {}
        bankroll = rs.get("bankroll", 0.0)
        exposure = rs.get("current_exposure", 0.0)
        max_exp = rs.get("max_exposure", 0.0) or 1.0
        drawdown = rs.get("drawdown_pct", 0.0) or 0.0
        halted = rs.get("halted", False)

        t = Table.grid(expand=True, padding=(0, 1))
        for _ in range(6):
            t.add_column(justify="center", ratio=1)

        def _cell(label: str, value: str, style: str = "bright_white") -> Text:
            out = Text()
            out.append(f"{label}\n", style="dim")
            out.append(value, style=style)
            return out

        exposure_style = "bright_green" if exposure < max_exp * 0.7 else \
                         "yellow" if exposure < max_exp * 0.9 else "bright_red"
        dd_style = "bright_green" if drawdown < 0.05 else \
                   "yellow" if drawdown < 0.15 else "bright_red"
        halt_text = "HALTED" if halted else "RUNNING"
        halt_style = "bold bright_red" if halted else "bold bright_green"

        pnl_style = "bright_green" if self._pnl_today >= 0 else "bright_red"
        pnl_str = f"{'+' if self._pnl_today >= 0 else ''}${self._pnl_today:.2f}"

        brier_str = "—"
        brier_style = "dim"
        if self._brier:
            bs = self._brier["brier_score"]
            brier_str = f"{bs:.3f} ({self._brier['quality']})"
            brier_style = "bright_green" if bs < 0.15 else "yellow" if bs < 0.25 else "bright_red"

        t.add_row(
            _cell("Bankroll", f"${bankroll:.2f}", "bright_white"),
            _cell("Exposure", f"${exposure:.2f} / ${max_exp:.0f}", exposure_style),
            _cell("Session PnL", pnl_str, pnl_style),
            _cell("Signals today", f"{self._signals_today}", "bright_cyan"),
            _cell("Brier (30d)", brier_str, brier_style),
            _cell("Status", halt_text, halt_style),
        )

        return Panel(t, box=box.ROUNDED, border_style="bright_blue", padding=(0, 0))

    def _build_log_panel(self) -> Panel:
        text = Text()
        visible = self._log_lines[-60:]
        for ts, msg, style in visible:
            text.append(f" {ts}  ", style="dim")
            text.append(f"{msg}\n", style=style or "white")

        return Panel(
            text,
            title="[bold bright_cyan]⚡ Agent Log[/bold bright_cyan]",
            border_style="bright_cyan",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _build_positions_panel(self) -> Panel:
        table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, expand=True)
        table.add_column("City", style="bold white", no_wrap=True)
        table.add_column("Side", justify="center")
        table.add_column("Type", justify="center")
        table.add_column("Entry", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Conf", justify="center")

        if not self._positions:
            table.add_row(
                Text("— no open positions —", style="dim"),
                "", "", "", "", "",
            )
        else:
            for p in self._positions:
                side_style = COLORS.get(p.get("side", ""), "white")
                type_style = COLORS.get(p.get("type", ""), "white")
                conf = p.get("weather_confidence", "?")
                conf_style = "bright_green" if conf == "high" else \
                             "yellow" if conf == "medium" else "bright_red"
                table.add_row(
                    p.get("city", "?"),
                    Text(p.get("side", "?"), style=side_style),
                    Text(p.get("type", "?"), style=type_style),
                    f"${p.get('yes_price', 0):.2f}",
                    f"${p.get('position_usd', 0):.2f}",
                    Text(conf, style=conf_style),
                )

        return Panel(
            table,
            title=f"[bold bright_green]📊 Open Positions · {len(self._positions)}[/bold bright_green]",
            border_style="bright_green",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _build_signals_panel(self) -> Panel:
        table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, expand=True)
        table.add_column("Type", justify="center", no_wrap=True)
        table.add_column("City", style="bold", no_wrap=True)
        table.add_column("Question", max_width=34)
        table.add_column("Side", justify="center")
        table.add_column("Px", justify="right")
        table.add_column("Model", justify="right")
        table.add_column("Edge", justify="right")
        table.add_column("Size", justify="right")

        if not self._scan_results:
            if self._progress_active:
                table.add_row(Text("scanning…", style="dim"), "", "", "", "", "", "", "")
            else:
                table.add_row(Text("— no signals this cycle —", style="dim"), "", "", "", "", "", "", "")
        else:
            for s in sorted(self._scan_results, key=lambda x: -abs(x.get("edge", 0)))[:12]:
                t = s.get("type", "?")
                t_style = COLORS.get(t, "white")
                side_style = COLORS.get(s.get("side", ""), "white")
                edge = s.get("edge", 0)
                edge_style = "bold bright_green" if edge > 0.10 else \
                             "bright_green" if edge > 0.05 else "yellow"
                q = s.get("question", "") or ""
                q_trim = q[:32] + "…" if len(q) > 32 else q

                table.add_row(
                    Text(t, style=t_style),
                    s.get("city", "?"),
                    q_trim,
                    Text(s.get("side", "?"), style=side_style),
                    f"${s.get('exec_price', s.get('yes_price', 0)):.2f}",
                    f"{s.get('model_prob', 0):.2f}",
                    Text(f"{edge:+.3f}", style=edge_style),
                    f"${s.get('position_usd', 0):.2f}",
                )

        scan_at = f" · {self._last_scan_at}" if self._last_scan_at else ""
        return Panel(
            table,
            title=f"[bold bright_yellow]🎯 Signals · {len(self._scan_results)}{scan_at}[/bold bright_yellow]",
            border_style="bright_yellow",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _build_progress_panel(self) -> Panel:
        """Live scan progress bar — this is the thing that kills the 'loading forever' feel."""
        if self._progress_active and self._progress_n > 0:
            frac = self._progress_i / self._progress_n
            spinner = _spinner_frame()
            bar = ProgressBar(
                total=self._progress_n,
                completed=self._progress_i,
                width=50,
                complete_style="bright_cyan",
                finished_style="bright_green",
            )
            left = Text(f" {spinner} ", style="bright_cyan")
            right = Text()
            right.append(f"  {self._progress_i}/{self._progress_n} ", style="bold white")
            right.append(f"({frac*100:.0f}%)", style="dim")
            if self._progress_city:
                right.append("  ·  ", style="dim")
                right.append(f"scoring {self._progress_city}", style="bright_yellow")

            body = Columns([left, bar, right], padding=(0, 0), expand=False)
            title = "[bold bright_cyan]⟳ Scanning markets[/bold bright_cyan]"
            border = "bright_cyan"
            return Panel(body, title=title, border_style=border, box=box.ROUNDED, padding=(0, 1))

        if self._next_scan_at:
            secs = max(0, self._next_scan_at - time.time())
            line = Text()
            line.append(" ✓ Idle  · ", style="bright_green")
            line.append(f"Next scan in {_fmt_duration(secs)}", style="bright_yellow")
            if self._tuner and self._tuner.get("reason") not in (None, "cold_start"):
                line.append(
                    f"  · Tuner[{self._tuner['reason']}] "
                    f"kelly={self._tuner['kelly_fraction']} "
                    f"min_ev={self._tuner['min_ev']} "
                    f"(n={self._tuner.get('n', 0)} wr={self._tuner.get('winrate')})",
                    style="dim magenta",
                )
            else:
                line.append(f"  · Tuner: cold-start (waiting for resolved predictions)", style="dim")
            title = "[bold bright_green]✓ Ready[/bold bright_green]"
            border = "bright_green"
            return Panel(line, title=title, border_style=border, box=box.ROUNDED, padding=(0, 1))

        line = Text(" Initializing scanner… ", style="dim")
        return Panel(line, title="[bold dim]· [/bold dim]", border_style="dim",
                     box=box.ROUNDED, padding=(0, 1))

    # ── Layout assembly ─────────────────────────────────────────

    def build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._build_header(), size=3, name="header"),
            Layout(self._build_stats(),  size=4, name="stats"),
            Layout(name="body"),
            Layout(self._build_progress_panel(), size=3, name="progress"),
        )
        layout["body"].split_row(
            Layout(self._build_log_panel(), ratio=3, name="log"),
            Layout(name="right", ratio=2),
        )
        layout["right"].split_column(
            Layout(self._build_positions_panel(), ratio=1, name="positions"),
            Layout(self._build_signals_panel(),   ratio=2, name="signals"),
        )
        return layout

    # ── Run ─────────────────────────────────────────────────────

    def start(self) -> Live:
        return Live(
            self.build_layout(),
            console=self.console,
            refresh_per_second=6,
            screen=True,
            auto_refresh=True,
            transient=False,
            vertical_overflow="crop",
            get_renderable=self.build_layout,
        )

    def refresh(self, live: Live):
        # With get_renderable set + auto_refresh=True, this is mostly a no-op
        # but we keep it so main.py can trigger an immediate repaint.
        live.refresh()


# ── helpers ────────────────────────────────────────────────────

_SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

def _spinner_frame() -> str:
    i = int(time.time() * 10) % len(_SPIN_FRAMES)
    return _SPIN_FRAMES[i]


def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    h, rem = divmod(secs, 3600)
    return f"{h}h {rem // 60:02d}m"
