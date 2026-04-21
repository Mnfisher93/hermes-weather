#!/usr/bin/env python3
"""
Hermes — Autonomous Polymarket Weather Arbitrage Engine

Usage:
    python main.py              # Default: SIM mode with dashboard
    python main.py --sim        # Simulation mode (no real orders)
    python main.py --live       # Live trading (requires wallet + USDC)
    python main.py --scan-only  # Read-only scan, no orders
    python main.py --halt       # Emergency: cancel all open orders
    python main.py --report     # Print calibration report
    python main.py --no-ui      # Run without Rich dashboard (plain output)
    python main.py --once       # Run one scan cycle and exit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from core.scanner import get_weather_markets
from core.edge import calculate_edge
from core.executor import build_client, place_limit_order, cancel_all
from core.risk import RiskManager
from core.adapt import adaptive_params
from core.calibration import (
    log_prediction,
    brier_score,
    prediction_summary,
    resolve_pending,
)


# ──────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config.json, overlaying .env values where missing."""
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    else:
        config = {}

    config.setdefault("min_edge", float(os.getenv("MIN_EDGE", "0.05")))
    config.setdefault("min_ev", float(os.getenv("MIN_EV", "0.03")))
    config.setdefault("kelly_fraction", float(os.getenv("KELLY_FRACTION", "0.25")))
    config.setdefault("max_bet_usd", float(os.getenv("MAX_BET_USD", "50")))
    config.setdefault("max_exposure_usd", float(os.getenv("MAX_EXPOSURE_USD", "200")))
    config.setdefault("max_drawdown_pct", float(os.getenv("MAX_DRAWDOWN_PCT", "0.20")))
    config.setdefault("min_market_volume", float(os.getenv("MIN_MARKET_VOLUME", "5000")))
    config.setdefault("max_slippage", float(os.getenv("MAX_SLIPPAGE", "0.03")))
    config.setdefault("scan_interval_seconds", int(os.getenv("SCAN_INTERVAL_SECONDS", "3000")))
    return config


# ──────────────────────────────────────────────────────────────────
# Scan cycle
# ──────────────────────────────────────────────────────────────────

def scan_cycle(
    client,
    config: dict,
    risk: RiskManager,
    sim_mode: bool,
    scan_only: bool,
    dashboard=None,
) -> list[dict]:
    """One full scan: markets → weather → edge → risk → execute."""
    log = _log_fn(dashboard)

    log("Starting market scan…", style="accent")

    # Close the calibration loop — cheap, one HTTP call per pending cid.
    try:
        res = resolve_pending(max_markets=100)
        if res["resolved_yes"] or res["resolved_no"]:
            log(
                f"Calibration: {res['resolved_yes']} YES / {res['resolved_no']} NO "
                f"resolved ({res['still_open']} still open)",
                style="muted",
            )
    except Exception as e:
        log(f"Calibration poll error: {e}", style="warn")

    min_vol = int(config.get("min_market_volume", 5000))
    try:
        markets = get_weather_markets(min_volume=min_vol)
    except Exception as e:
        log(f"Scanner error: {e}", style="error")
        return []

    log(f"Found {len(markets)} liquid weather markets")

    # Adaptive parameter tuning — adjusts kelly_fraction and min_ev based
    # on recent (30d) resolved trades in the calibration DB. Falls back to
    # the base values from config.json when cold-started.
    tuned = adaptive_params(
        base_kelly=config.get("kelly_fraction", 0.25),
        base_min_ev=config.get("min_ev", 0.03),
        days=30,
    )
    kelly_now = tuned["kelly_fraction"]
    min_ev_now = tuned["min_ev"]
    if tuned["reason"] != "cold_start":
        log(
            f"Adaptive tuner [{tuned['reason']}]: "
            f"kelly={kelly_now} min_ev={min_ev_now} "
            f"(n={tuned['n']} wr={tuned['winrate']} pnl=${tuned['net_pnl_usd']})",
            style="accent",
        )

    signals: list[dict] = []
    blocked = 0

    for m in markets:
        sig = calculate_edge(
            m,
            kelly_fraction=kelly_now,
            max_bet_usd=config.get("max_bet_usd", 50),
            bankroll=risk.bankroll,
            min_edge=config.get("min_edge", 0.05),
            min_ev=min_ev_now,
        )
        if not sig:
            continue

        allowed, reason = risk.check_trade(sig)
        if not allowed:
            blocked += 1
            if dashboard:
                dashboard.log_risk_block(reason)
            continue

        if dashboard:
            dashboard.log_signal(sig)
        else:
            print(
                f"[{sig['type']}] {sig['city']:12s} {sig['side']:3s} @ "
                f"${sig.get('exec_price', sig['yes_price']):.2f} | "
                f"model={sig['model_prob']:.2f} edge={sig['edge']:.3f} "
                f"ev={sig['ev']:.3f} size=${sig['position_usd']:.2f} | "
                f"{sig['question'][:60]}"
            )

        signals.append(sig)

        if scan_only:
            continue

        try:
            result = place_limit_order(
                client, sig,
                sim_mode=sim_mode,
                max_slippage=config.get("max_slippage", 0.03),
            )
        except Exception as e:
            log(f"Executor error for {sig['city']}: {e}", style="error")
            continue

        if result and result.get("status") in ("simulated", "placed"):
            risk.record_trade(sig)
            try:
                log_prediction(sig)
            except Exception as e:
                log(f"Calibration log error: {e}", style="warn")

    summary = (
        f"Scan complete — {len(signals)} signals, {blocked} blocked | "
        f"Exposure: ${risk.current_exposure:.2f} / ${risk.max_exposure_usd:.2f}"
    )
    log(summary, style="accent")

    if dashboard:
        dashboard.update_scan_results(markets, signals)
        dashboard.update_positions([
            {
                "city": p.city,
                "side": p.side,
                "type": p.signal_type,
                "yes_price": p.entry_price,
                "position_usd": p.size_usd,
                "edge": 0,
                "weather_confidence": "—",
            }
            for p in risk.open_positions
        ])
        dashboard.update_risk_status(risk.status())

        for msg in risk.drain_log():
            dashboard.log(msg, style="muted")

    return signals


def _log_fn(dashboard):
    if dashboard:
        return dashboard.log
    def _p(msg: str, style: str = ""):
        print(msg)
    return _p


# ──────────────────────────────────────────────────────────────────
# CLI commands
# ──────────────────────────────────────────────────────────────────

def cmd_halt():
    """Emergency kill switch: cancel all open orders."""
    print("⛔ HALT — cancelling all open orders...")
    try:
        client = build_client()
        cancel_all(client)
        print("All orders cancelled.")
    except Exception as e:
        print(f"Error: {e}")
    sys.exit(0)


def cmd_report():
    """Print calibration report."""
    print("=" * 60)
    print("HERMES CALIBRATION REPORT")
    print("=" * 60)

    # Roll forward any resolvable predictions first
    try:
        r = resolve_pending(max_markets=500)
        print(
            f"\nPolled Gamma for {r['checked']} pending predictions — "
            f"{r['resolved_yes']} YES, {r['resolved_no']} NO, "
            f"{r['still_open']} still open"
        )
    except Exception as e:
        print(f"(resolve_pending error: {e})")

    summary = prediction_summary()
    print(f"\nTotal predictions: {summary['total_predictions']}")
    print(f"Resolved:          {summary['resolved']}")
    print(f"Unresolved:        {summary['unresolved']}")
    if summary["win_rate"] is not None:
        print(f"Win rate:          {summary['win_rate']:.1%}")

    for days in (7, 30, 90):
        bs = brier_score(days=days)
        if bs:
            print(
                f"\nBrier score ({days}d): {bs['brier_score']:.4f} "
                f"({bs['quality']}) [{bs['n_resolved']} resolved]"
            )
            if bs.get("per_city"):
                for city, score in sorted(bs["per_city"].items(), key=lambda x: x[1]):
                    print(f"  {city:15s}: {score:.4f}")
    sys.exit(0)


def confirm_live() -> bool:
    """Explicit confirmation gate before any real orders fly."""
    print("\n" + "=" * 60)
    print("⚠  LIVE TRADING MODE")
    print("=" * 60)
    print("This will place real orders against Polymarket CLOB using the")
    print("wallet referenced in .env. Funds at risk.\n")
    print("Before continuing, confirm:")
    print("  • You've reviewed the calibration report (python main.py --report)")
    print("  • Brier score is below 0.20 (otherwise you're flying blind)")
    print("  • Risk caps in config.json reflect real bankroll size")
    print("  • You can reach `python main.py --halt` from another shell\n")
    if os.getenv("HERMES_SKIP_LIVE_CONFIRM") == "1":
        print("HERMES_SKIP_LIVE_CONFIRM=1 — skipping interactive confirm")
        return True
    resp = input("Type 'GO LIVE' to continue, anything else to abort: ").strip()
    return resp == "GO LIVE"


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hermes Weather Arbitrage Engine")
    parser.add_argument("--sim", action="store_true", help="Simulation mode (default)")
    parser.add_argument("--live", action="store_true", help="Live trading mode")
    parser.add_argument("--scan-only", action="store_true", help="Read-only scan, no orders")
    parser.add_argument("--halt", action="store_true", help="Emergency: cancel all open orders")
    parser.add_argument("--report", action="store_true", help="Print calibration report")
    parser.add_argument("--no-ui", action="store_true", help="Run without Rich dashboard")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    args = parser.parse_args()

    if args.halt:
        cmd_halt()
    if args.report:
        cmd_report()

    config = load_config()
    sim_mode = not args.live
    scan_only = args.scan_only

    # Live gate
    if not sim_mode:
        if not confirm_live():
            print("Aborted — staying in SIM mode.")
            sim_mode = True

    risk = RiskManager(config)

    client = None
    if not sim_mode:
        try:
            client = build_client()
            print(f"[OK] CLOB client connected")
        except Exception as e:
            print(f"Failed to connect wallet: {e}")
            print("Falling back to SIM mode.")
            sim_mode = True

    # Dashboard or plain mode
    use_dashboard = not args.no_ui
    dashboard = None
    if use_dashboard:
        try:
            from ui.dashboard import Dashboard
            dashboard = Dashboard(sim_mode=sim_mode)
        except ImportError:
            print("Rich not installed, falling back to plain output.")
            use_dashboard = False

    if dashboard and use_dashboard:
        mode = "SIM" if sim_mode else "LIVE"
        if scan_only:
            mode += " (scan-only)"
        dashboard.log(f"Mode: {mode}", style="ok")
        dashboard.log(
            f"Config: edge≥{config['min_edge']} ev≥{config['min_ev']} "
            f"kelly={config['kelly_fraction']} max_bet=${config['max_bet_usd']}",
            style="muted",
        )

        live = dashboard.start()
        with live:
            while True:
                try:
                    scan_cycle(client, config, risk, sim_mode, scan_only, dashboard)
                    dashboard.refresh(live)

                    if args.once:
                        dashboard.log("--once flag: exiting after single scan", style="ok")
                        dashboard.refresh(live)
                        time.sleep(2)
                        break

                    interval = int(config.get("scan_interval_seconds", 3000))
                    dashboard.log(
                        f"Sleeping {interval // 60}m until next scan…",
                        style="muted",
                    )
                    dashboard.refresh(live)

                    for _ in range(interval):
                        time.sleep(1)
                        dashboard.refresh(live)

                except KeyboardInterrupt:
                    dashboard.log("Stopped by user.", style="warn")
                    dashboard.refresh(live)
                    time.sleep(1)
                    break
                except Exception as e:
                    dashboard.log(f"Cycle error: {e}", style="error")
                    dashboard.refresh(live)
                    time.sleep(10)
    else:
        # Plain mode
        mode = "SIM (no real orders)" if sim_mode else "⚠ LIVE TRADING"
        print(f"HERMES WEATHER ARBITRAGE BOT — {mode}\n")

        while True:
            try:
                scan_cycle(client, config, risk, sim_mode, scan_only)
            except KeyboardInterrupt:
                print("\nStopped by user.")
                sys.exit(0)
            except Exception as e:
                print(f"Error: {e}")

            if args.once:
                print("\n--once flag: exiting after single scan")
                sys.exit(0)

            interval = int(config.get("scan_interval_seconds", 3000))
            print(f"\nSleeping {interval // 60} min until next scan…")
            time.sleep(interval)


if __name__ == "__main__":
    main()
