"""
Adaptive parameter tuner.

Reads resolved predictions out of data/calibration.db and returns
(kelly_fraction, min_ev) adjustments based on rolling performance.

Rules (lifted from nicolastinkl/hermes_weatherbot, adapted for our
calibration schema):
  - winrate < 0.45  → kelly *= 0.8,  min_ev += 0.01
  - winrate > 0.55 and net_pnl > 0 → kelly *= 1.1,  min_ev -= 0.005
  - otherwise → no change
  - requires MIN_RESOLVED trades before any adjustment fires

Per-city adjustments fall out of the same rules applied with
city=<city>; callers can pass city="" for global. A bot in its first
few cycles with no resolved history gets the static defaults.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "data" / "calibration.db"

# Floor/ceiling on any adjusted value — keep the bot sane when
# a small sample produces a weird winrate.
KELLY_MIN = 0.05
KELLY_MAX = 0.50
EV_MIN = 0.01
EV_MAX = 0.20

# Need this many resolved trades in the lookback window before we tune.
MIN_RESOLVED = 30


def adaptive_params(
    base_kelly: float = 0.25,
    base_min_ev: float = 0.03,
    city: Optional[str] = None,
    days: int = 30,
) -> dict:
    """
    Return tuned (kelly_fraction, min_ev) + diagnostics.

    If insufficient history, returns the base values with reason='cold_start'.
    """
    stats = _rolling_stats(city=city, days=days)

    if stats["n"] < MIN_RESOLVED:
        return {
            "kelly_fraction": base_kelly,
            "min_ev": base_min_ev,
            "reason": "cold_start",
            **stats,
        }

    winrate = stats["winrate"]
    pnl = stats["net_pnl_usd"]

    kelly = base_kelly
    min_ev = base_min_ev
    reason = "neutral"

    if winrate < 0.45:
        kelly *= 0.8
        min_ev += 0.01
        reason = "underperforming"
    elif winrate > 0.55 and pnl > 0:
        kelly *= 1.1
        min_ev -= 0.005
        reason = "outperforming"

    return {
        "kelly_fraction": round(max(KELLY_MIN, min(KELLY_MAX, kelly)), 4),
        "min_ev":         round(max(EV_MIN,    min(EV_MAX,    min_ev)), 4),
        "reason": reason,
        **stats,
    }


def _rolling_stats(city: Optional[str], days: int) -> dict:
    if not DB_PATH.exists():
        return {"n": 0, "winrate": None, "net_pnl_usd": 0.0}

    cutoff = time.time() - days * 86400
    conn = sqlite3.connect(DB_PATH)
    try:
        q = """
            SELECT side, outcome, position_usd, market_price
            FROM predictions
            WHERE resolved=1 AND outcome IS NOT NULL AND timestamp > ?
        """
        params: list = [cutoff]
        if city:
            q += " AND city=?"
            params.append(city)
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()

    n = len(rows)
    if not n:
        return {"n": 0, "winrate": None, "net_pnl_usd": 0.0}

    wins = 0
    pnl = 0.0
    for side, outcome, size, price in rows:
        size = size or 0.0
        price = price or 0.0
        won = (side == "YES" and outcome == 1) or (side == "NO" and outcome == 0)
        if won:
            wins += 1
            # Buying at 'price' for $1 if won → profit = size*(1/price - 1)
            buy_price = price if side == "YES" else (1 - price)
            if buy_price > 0:
                pnl += size * (1.0 / buy_price - 1.0)
        else:
            pnl -= size

    return {
        "n": n,
        "winrate": round(wins / n, 3),
        "net_pnl_usd": round(pnl, 2),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(adaptive_params(), indent=2))
