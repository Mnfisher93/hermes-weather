"""
Edge calculation pipeline.

Given a market dict from scanner.py, computes:
  1. Parsed temperature threshold from the question
  2. GFS ensemble probability at the resolution airport
  3. Signal classification (LOCK / LAG / CONTRA / FADE)
  4. Expected value gate
  5. Fractional-Kelly position size with hard caps

Returns a fully-formed signal dict ready for the executor, or None.
"""
from __future__ import annotations

from typing import Optional

from core.scanner import classify_signal
from core.weather import (
    ensemble_probability,
    parse_question_threshold,
    is_forecastable,
)


# Minimum ensemble members we'll trust for a full-size trade.
MIN_ENSEMBLE_MEMBERS = 10


def calculate_edge(
    market: dict,
    kelly_fraction: float = 0.25,
    max_bet_usd: float = 50.0,
    bankroll: float = 100.0,
    min_edge: float = 0.05,
    min_ev: float = 0.03,
) -> Optional[dict]:
    """
    Run the full edge pipeline on one market. Returns an actionable
    signal dict or None at any skip point.

    Skip reasons (all silent — callers can log via signal["skip"] if
    we later expose it):
      - unparseable question (precipitation markets, non-temperature)
      - market already resolved (price at extremes)
      - target date outside the ensemble forecast horizon
      - ensemble fetch failed
      - single-model fallback for a LAG/FADE trade (low confidence)
      - no classifier signal
      - EV below threshold
      - Kelly size below $1
    """
    question = market.get("question", "")
    city = market.get("city")
    end_date = (market.get("end_date") or "")[:10]
    yes_price = market.get("yes_price")
    no_price = market.get("no_price")

    if not question or not city or yes_price is None:
        return None

    # Already-resolved guard (scanner also filters, belt + suspenders)
    if yes_price <= 0.01 or yes_price >= 0.99:
        return None

    # Horizon check — skip past-dated and far-future markets before paying
    # for the ensemble request.
    ok, _ = is_forecastable(end_date)
    if not ok:
        return None

    threshold_c, comparison = parse_question_threshold(question)
    if threshold_c is None:
        return None

    wx = ensemble_probability(city, end_date, threshold_c, comparison)
    if wx is None:
        return None

    model_prob = wx["probability"]
    confidence = wx.get("confidence", "low")
    member_count = wx.get("member_count", 0)

    # If we fell back to a single-model forecast, only trust LOCK-style
    # signals (market already near an extreme). Don't take a LAG/FADE
    # position on single-model conviction.
    sig = classify_signal(yes_price, model_prob, min_edge)
    if sig is None:
        return None

    if member_count < MIN_ENSEMBLE_MEMBERS and sig["type"] in {"LAG", "FADE"}:
        return None

    ev = _calculate_ev(sig["side"], yes_price, model_prob)
    if ev < min_ev:
        return None

    position_usd = _kelly_size(
        sig["side"], yes_price, model_prob,
        kelly_fraction, max_bet_usd, bankroll,
    )
    if position_usd < 1.0:
        return None

    # The price we'll actually submit the order at. Prefer the market's
    # quoted NO price when buying NO (handles small book asymmetries).
    exec_price = (
        yes_price if sig["side"] == "YES"
        else (no_price if (no_price and no_price > 0.01) else round(1 - yes_price, 2))
    )

    return {
        **market,
        **sig,
        "threshold_c": threshold_c,
        "comparison": comparison,
        "model_prob": round(model_prob, 4),
        "yes_price": yes_price,
        "no_price": no_price,
        "exec_price": round(exec_price, 2),
        "ev": round(ev, 4),
        "position_usd": round(position_usd, 2),
        "weather_confidence": confidence,
        "median_high_c": wx.get("median_high_c"),
        "mean_high_c": wx.get("mean_high_c"),
        "member_count": member_count,
        "std_c": wx.get("std_c"),
    }


# ──────────────────────────────────────────────────────────────────
# Math
# ──────────────────────────────────────────────────────────────────

def _calculate_ev(side: str, yes_price: float, model_prob: float) -> float:
    """Expected value per $1 staked, net of the stake itself."""
    if side == "YES":
        return model_prob * (1 - yes_price) - (1 - model_prob) * yes_price
    no_price = 1 - yes_price
    no_prob = 1 - model_prob
    return no_prob * (1 - no_price) - model_prob * no_price


def _kelly_size(
    side: str,
    yes_price: float,
    model_prob: float,
    fraction: float,
    max_bet: float,
    bankroll: float,
) -> float:
    """
    Fractional-Kelly sizing with hard caps:
      position = min(kelly * fraction * bankroll, max_bet, 5% bankroll)
    """
    if side == "YES":
        price, prob = yes_price, model_prob
    else:
        price, prob = 1 - yes_price, 1 - model_prob

    if price <= 0 or price >= 1:
        return 0.0

    odds = (1 / price) - 1
    if odds <= 0:
        return 0.0

    lose_prob = 1 - prob
    kelly = (prob * odds - lose_prob) / odds
    kelly = max(0.0, kelly)

    raw = kelly * fraction * bankroll
    return min(raw, max_bet, bankroll * 0.05)
