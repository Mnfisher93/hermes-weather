"""
Polymarket weather-market scanner + signal classifier.

Polls Gamma (no auth) for active markets, filters to weather/temperature
markets for the configured city list, and enriches with token IDs and prices.
"""
from __future__ import annotations

import json
import requests
from datetime import datetime, timezone
from typing import Optional

GAMMA_URL = "https://gamma-api.polymarket.com"
MIN_VOLUME = 5000

# ICAOs match config.json → weather.py CITY_COORDS (airport resolution station).
CITY_ICAO: dict[str, str] = {
    "Atlanta": "KATL", "Austin": "KAUS", "Amsterdam": "EHAM",
    "Ankara": "LTAC", "Busan": "RKPK", "Cape Town": "FACT",
    "Chicago": "KORD", "Chongqing": "ZUCK", "Dallas": "KDAL",
    "Denver": "KDEN", "Houston": "KHOU", "Istanbul": "LTBA",
    "Jakarta": "WIII", "Jeddah": "OEJN", "Kuala Lumpur": "WMKK",
    "London": "EGLL", "Lucknow": "VILK", "Milan": "LIML",
    "Munich": "EDDM", "NYC": "KLGA", "Panama City": "MPTO",
    "Paris": "LFPG", "Seoul": "RKSI", "Shanghai": "ZSPD",
    "Shenzhen": "ZGSZ", "Singapore": "WSSS", "Tokyo": "RJTT",
    "Toronto": "CYYZ", "Warsaw": "EPWA", "Wuhan": "ZHHH",
}

# Keywords that flag a market as weather-related. Precipitation markets are
# kept so the scanner finds them, but the parser in weather.py will skip
# them (we don't model precipitation yet).
WEATHER_KEYWORDS = (
    "temperature", "high temp", "degrees", "°f", "°c",
    "weather", "forecast", "rainfall", "precipitation",
)


# ──────────────────────────────────────────────────────────────────
# Market discovery
# ──────────────────────────────────────────────────────────────────

def get_weather_markets(
    min_volume: int = MIN_VOLUME,
    max_markets: int = 500,
    max_pages: int = 10,
    progress_cb=None,
) -> list[dict]:
    """
    Scan Gamma's events endpoint for weather-tagged events, then flatten out
    the per-bucket markets inside each event.

    Uses /events?tag_slug=weather which is ~100x faster than paging through
    all active markets: weather events total ~200, each containing 5–15
    bucketed markets. One call typically returns everything we need.

    progress_cb(pages_done, max_pages, found_so_far) fires after each page
    so dashboards can stream a live indicator.

    Each returned market dict contains:
        condition_id, question, city, icao,
        yes_token_id, no_token_id, yes_price, no_price,
        volume, end_date, accepting_orders
    """
    results: list[dict] = []
    seen_ids: set[str] = set()
    offset = 0
    limit = 100
    pages_done = 0

    while len(results) < max_markets and pages_done < max_pages:
        try:
            resp = requests.get(
                f"{GAMMA_URL}/events",
                params={
                    "tag_slug": "weather",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException:
            break

        events = resp.json()
        if not events:
            break

        for event in events:
            # Event-level filter: skip non-temperature events (rainfall,
            # annual-ranking markets, etc.). The temperature question parser
            # will reject anything else downstream anyway.
            title = (event.get("title") or "").lower()
            if not any(kw in title for kw in TEMP_EVENT_KEYWORDS):
                continue

            for m in event.get("markets", []) or []:
                cid = m.get("conditionId")
                if not cid or cid in seen_ids:
                    continue

                question = (m.get("question") or "").lower()
                city = _extract_city(m.get("question") or "") or _extract_city(title)
                if not city:
                    continue

                vol = float(m.get("volume") or 0)
                if vol < min_volume:
                    continue

                prices_raw = m.get("outcomePrices", "[]")
                if isinstance(prices_raw, str):
                    try:
                        prices_raw = json.loads(prices_raw)
                    except Exception:
                        continue
                if len(prices_raw) < 2:
                    continue

                # Event markets don't always ship clobTokenIds — refetch if missing
                token_ids_raw = m.get("clobTokenIds", "[]")
                if isinstance(token_ids_raw, str):
                    try:
                        token_ids_raw = json.loads(token_ids_raw)
                    except Exception:
                        token_ids_raw = []
                if len(token_ids_raw) < 2:
                    # Skip silently — we can't place orders without token IDs
                    continue

                try:
                    yes_price = float(prices_raw[0])
                    no_price = float(prices_raw[1])
                except (TypeError, ValueError):
                    continue

                # Resolved-market sanity filter
                if yes_price <= 0.005 and no_price <= 0.005:
                    continue
                if yes_price >= 0.995 or no_price >= 0.995:
                    continue

                results.append({
                    "condition_id": cid,
                    "question": m.get("question"),
                    "city": city,
                    "icao": CITY_ICAO.get(city),
                    "yes_token_id": token_ids_raw[0],
                    "no_token_id": token_ids_raw[1],
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume": vol,
                    "end_date": (m.get("endDate") or event.get("endDate") or "")[:10],
                    "accepting_orders": bool(m.get("acceptingOrders", True)),
                })
                seen_ids.add(cid)

        offset += limit
        pages_done += 1
        if progress_cb:
            try:
                progress_cb(pages_done, max_pages, len(results))
            except Exception:
                pass
        if len(events) < limit:
            break

    return sorted(results, key=lambda x: x["volume"], reverse=True)


# Event-title keywords that indicate a temperature bucket event (not rainfall,
# annual rankings, or other weather-tagged curiosities).
TEMP_EVENT_KEYWORDS = (
    "highest temperature",
    "temperature in",
    "high temp",
    "warmest day",
)


# ──────────────────────────────────────────────────────────────────
# Signal classifier
# ──────────────────────────────────────────────────────────────────

def classify_signal(
    yes_price: float,
    model_prob: float,
    min_edge: float = 0.05,
) -> Optional[dict]:
    """
    Classify a (yes_price, model_prob) pair into a trade signal.

    Signal types (evaluated in priority order — first match wins):

      LOCK    — model says < 3% chance AND NO is cheap enough to clear
                min_edge after paying spread. Buys NO.
      LAG     — model strongly favors YES (≥55%) and market hasn't caught
                up. Buys YES.
      CONTRA  — market prices YES at ≥55% but model says ≤40%. Buys NO.
      FADE    — moderate YES edge, lower conviction (model 30-55%). Buys YES.

    All paths require edge ≥ min_edge on the side actually being purchased.
    """
    if not (0 < yes_price < 1):
        return None

    edge_yes = model_prob - yes_price
    no_price = 1.0 - yes_price
    no_prob = 1.0 - model_prob
    edge_no = no_prob - no_price  # identical to -edge_yes, but clearer by name

    # LOCK — near-certain NO with enough edge to cover spread
    if model_prob <= 0.03 and no_price <= 0.97 and edge_no >= min_edge:
        return {
            "type": "LOCK", "side": "NO",
            "edge": round(edge_no, 4),
            "model_prob": model_prob,
        }

    # LAG — strong YES signal the market hasn't priced in
    if model_prob >= 0.55 and yes_price <= 0.88 and edge_yes >= min_edge:
        return {
            "type": "LAG", "side": "YES",
            "edge": round(edge_yes, 4),
            "model_prob": model_prob,
        }

    # CONTRA — market overprices YES, model disagrees hard
    if model_prob <= 0.40 and yes_price >= 0.55 and edge_no >= min_edge:
        return {
            "type": "CONTRA", "side": "NO",
            "edge": round(edge_no, 4),
            "model_prob": model_prob,
        }

    # FADE — moderate YES edge, not quite LAG territory
    if model_prob >= 0.30 and yes_price <= 0.70 and edge_yes >= min_edge:
        return {
            "type": "FADE", "side": "YES",
            "edge": round(edge_yes, 4),
            "model_prob": model_prob,
        }

    return None


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _extract_city(question: str) -> Optional[str]:
    """Return the first configured city whose name appears in the question."""
    q = question.lower()
    # Match longer city names first so "Panama City" wins over a false match
    for city in sorted(CITY_ICAO.keys(), key=len, reverse=True):
        if city.lower() in q:
            return city
    return None
