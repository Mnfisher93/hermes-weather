"""
Calibration tracking — log predictions, poll Gamma for resolutions,
compute Brier scores.

Forecast calibration is the honest scoreboard for a weather bot. Low Brier
on paper trades is the go/no-go signal for flipping to --live. This module
also provides resolve_pending() to close the loop: for every prediction
that has a past end_date, ask Gamma for the final outcome and record it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

import requests


DB_PATH = Path(__file__).parent.parent / "data" / "calibration.db"
GAMMA_URL = "https://gamma-api.polymarket.com"


# ──────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS predictions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id  TEXT NOT NULL,
            city          TEXT NOT NULL,
            question      TEXT,
            side          TEXT NOT NULL,
            signal_type   TEXT,
            model_prob    REAL NOT NULL,
            market_price  REAL NOT NULL,
            edge          REAL,
            ev            REAL,
            position_usd  REAL,
            member_count  INTEGER,
            confidence    TEXT,
            median_high_c REAL,
            threshold_c   REAL,
            comparison    TEXT,
            end_date      TEXT,
            timestamp     REAL NOT NULL,
            resolved      INTEGER DEFAULT 0,
            outcome       INTEGER DEFAULT NULL,
            resolved_at   REAL DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pred_cid ON predictions(condition_id);
        CREATE INDEX IF NOT EXISTS idx_pred_city ON predictions(city);
        CREATE INDEX IF NOT EXISTS idx_pred_ts ON predictions(timestamp);
        CREATE INDEX IF NOT EXISTS idx_pred_resolved ON predictions(resolved);
    """)
    # Add end_date column if the DB was created before this field existed.
    try:
        conn.execute("ALTER TABLE predictions ADD COLUMN end_date TEXT")
    except sqlite3.OperationalError:
        pass
    return conn


# ──────────────────────────────────────────────────────────────────
# Write path
# ──────────────────────────────────────────────────────────────────

def log_prediction(signal: dict) -> int:
    """Log a prediction from the edge pipeline. Returns row ID."""
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO predictions
           (condition_id, city, question, side, signal_type,
            model_prob, market_price, edge, ev, position_usd,
            member_count, confidence, median_high_c, threshold_c,
            comparison, end_date, timestamp)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            signal.get("condition_id", ""),
            signal.get("city", ""),
            signal.get("question", ""),
            signal.get("side", ""),
            signal.get("type", ""),
            signal.get("model_prob", 0),
            signal.get("yes_price", 0),
            signal.get("edge", 0),
            signal.get("ev", 0),
            signal.get("position_usd", 0),
            signal.get("member_count"),
            signal.get("weather_confidence", ""),
            signal.get("median_high_c"),
            signal.get("threshold_c"),
            signal.get("comparison", ""),
            signal.get("end_date", ""),
            time.time(),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def resolve_prediction(condition_id: str, outcome: bool) -> None:
    """Mark every unresolved prediction for condition_id with the final outcome."""
    conn = _connect()
    conn.execute(
        """UPDATE predictions SET resolved=1, outcome=?, resolved_at=?
           WHERE condition_id=? AND resolved=0""",
        (int(outcome), time.time(), condition_id),
    )
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────────
# Resolution loop — polls Gamma for final outcomes
# ──────────────────────────────────────────────────────────────────

def resolve_pending(max_markets: int = 200) -> dict:
    """
    For every unresolved prediction, query Gamma for the current state.
    Mark YES-won / NO-won when the market is closed and outcomePrices
    snaps to [1,0] or [0,1].

    Safe to call every scan cycle — one HTTP request per distinct
    unresolved condition_id, capped at max_markets.
    """
    conn = _connect()
    rows = conn.execute(
        """SELECT DISTINCT condition_id FROM predictions
           WHERE resolved=0 LIMIT ?""",
        (max_markets,),
    ).fetchall()
    conn.close()

    resolved_yes = 0
    resolved_no = 0
    still_open = 0
    errors = 0

    for (cid,) in rows:
        if not cid:
            continue
        try:
            outcome = _fetch_outcome(cid)
        except Exception:
            errors += 1
            continue

        if outcome is None:
            still_open += 1
            continue
        resolve_prediction(cid, outcome)
        if outcome:
            resolved_yes += 1
        else:
            resolved_no += 1

    return {
        "checked": len(rows),
        "resolved_yes": resolved_yes,
        "resolved_no": resolved_no,
        "still_open": still_open,
        "errors": errors,
    }


def _fetch_outcome(condition_id: str) -> Optional[bool]:
    """
    Query Gamma for a market by condition_id. Returns:
        True  — YES won
        False — NO won
        None  — still open / indeterminate
    """
    r = requests.get(
        f"{GAMMA_URL}/markets",
        params={"condition_ids": condition_id, "limit": 1},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list) and data:
        m = data[0]
    elif isinstance(data, dict):
        m = data
    else:
        return None

    if not m.get("closed"):
        return None

    prices_raw = m.get("outcomePrices", "[]")
    if isinstance(prices_raw, str):
        try:
            prices = json.loads(prices_raw)
        except Exception:
            return None
    else:
        prices = prices_raw

    if not prices or len(prices) < 2:
        return None

    try:
        yes = float(prices[0])
        no = float(prices[1])
    except (TypeError, ValueError):
        return None

    # Resolved markets snap to exactly 1 / 0
    if yes >= 0.99 and no <= 0.01:
        return True
    if yes <= 0.01 and no >= 0.99:
        return False
    return None  # still trading or mispriced


# ──────────────────────────────────────────────────────────────────
# Read path — Brier + summary
# ──────────────────────────────────────────────────────────────────

def brier_score(city: Optional[str] = None, days: int = 30) -> Optional[dict]:
    """
    Brier = mean((forecast_prob_of_outcome - actual_outcome)²)

    Lower is better. 0 = perfect, 0.25 = always predicting 0.5 (random),
    >0.25 = worse than random.

    Returns None if no resolved predictions match the window.
    """
    conn = _connect()
    cutoff = time.time() - (days * 86400)
    query = """
        SELECT model_prob, outcome, city, side
        FROM predictions
        WHERE resolved=1 AND outcome IS NOT NULL AND timestamp > ?
    """
    params: list = [cutoff]
    if city:
        query += " AND city=?"
        params.append(city)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        return None

    # Brier uses the probability of the YES outcome. Our model_prob is
    # already P(YES) regardless of which side we bet, so it composes
    # cleanly here.
    n = len(rows)
    brier_sum = sum((prob - actual) ** 2 for prob, actual, _, _ in rows)
    brier = brier_sum / n

    cities: dict[str, dict] = {}
    for prob, actual, c, _ in rows:
        d = cities.setdefault(c, {"sum": 0.0, "n": 0})
        d["sum"] += (prob - actual) ** 2
        d["n"] += 1

    city_scores = {c: round(v["sum"] / v["n"], 4) for c, v in cities.items()}

    return {
        "brier_score": round(brier, 4),
        "n_resolved": n,
        "days": days,
        "per_city": city_scores,
        "quality": (
            "excellent" if brier < 0.10 else
            "good"      if brier < 0.20 else
            "poor"
        ),
    }


def prediction_summary() -> dict:
    """Aggregate counts across the full history."""
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    resolved = conn.execute("SELECT COUNT(*) FROM predictions WHERE resolved=1").fetchone()[0]
    wins = conn.execute(
        """SELECT COUNT(*) FROM predictions
           WHERE resolved=1 AND (
               (side='YES' AND outcome=1) OR
               (side='NO'  AND outcome=0)
           )"""
    ).fetchone()[0]
    conn.close()

    return {
        "total_predictions": total,
        "resolved": resolved,
        "unresolved": total - resolved,
        "wins": wins,
        "win_rate": round(wins / resolved, 3) if resolved else None,
    }
