"""
Weather forecast module.

CRITICAL: Polymarket weather markets resolve against Wunderground data from
a SPECIFIC AIRPORT STATION, not the city center. The coordinates below are
pulled from the METAR/aviationweather.gov station registry for each ICAO in
config.json. DO NOT change these to city-center coords — a 10–50 km offset
commonly moves the daily high by 2–5°F, which is the entire width of a
Polymarket resolution bracket.
"""
from __future__ import annotations

import math
import re
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
METAR_URL = "https://aviationweather.gov/api/data/metar"

# Open-Meteo ensemble covers ~15 days forward. Past dates will 400.
MAX_FORECAST_DAYS = 15

# Airport coordinates — each entry matches the ICAO in config.json.
# Pulled from the METAR station registry (aviationweather.gov) on 2026-04-21.
# THIS IS THE RESOLUTION STATION. Forecasting any other point = silent edge loss.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "Atlanta":      (33.6297, -84.4422),   # KATL Hartsfield-Jackson
    "Austin":       (30.1831, -97.6806),   # KAUS Bergstrom
    "Amsterdam":    (52.3150,   4.7900),   # EHAM Schiphol
    "Ankara":       (40.1280,  32.9950),   # LTAC Esenboğa
    "Busan":        (35.1790, 128.9380),   # RKPK Gimhae
    "Cape Town":   (-33.9650,  18.6020),   # FACT Cape Town Intl
    "Chicago":      (41.9602, -87.9316),   # KORD O'Hare
    "Chongqing":    (29.7180, 106.6390),   # ZUCK Jiangbei
    "Dallas":       (32.8384, -96.8358),   # KDAL Love Field
    "Denver":       (39.8466,-104.6562),   # KDEN Denver Intl
    "Houston":      (29.6458, -95.2821),   # KHOU Hobby   (NOT IAH)
    "Istanbul":     (40.9820,  28.8210),   # LTBA Ataturk
    "Jakarta":     ( -6.1250, 106.6590),   # WIII Soekarno-Hatta
    "Jeddah":       (21.6850,  39.1660),   # OEJN King Abdulaziz
    "Kuala Lumpur": ( 2.7470, 101.7140),   # WMKK KL Intl
    "London":       (51.4770,  -0.4610),   # EGLL Heathrow
    "Lucknow":      (26.7610,  80.8890),   # VILK Chaudhary Charan Singh
    "Milan":        (45.4610,   9.2630),   # LIML Linate
    "Munich":       (48.3480,  11.8130),   # EDDM Munich Intl
    "NYC":          (40.7794, -73.8803),   # KLGA LaGuardia  (NOT JFK)
    "Panama City":  ( 9.0560, -79.3910),   # MPTO Tocumen
    "Paris":        (49.0150,   2.5340),   # LFPG Charles de Gaulle
    "Seoul":        (37.4690, 126.4510),   # RKSI Incheon  (NOT Gimpo RKSS)
    "Shanghai":     (31.1460, 121.8000),   # ZSPD Pudong
    "Shenzhen":     (22.6390, 113.8030),   # ZGSZ Bao'an
    "Singapore":    ( 1.3680, 103.9820),   # WSSS Changi
    "Tokyo":        (35.5530, 139.7810),   # RJTT Haneda  (NOT Narita)
    "Toronto":      (43.6790, -79.6290),   # CYYZ Pearson
    "Warsaw":       (52.1630,  20.9610),   # EPWA Chopin
    "Wuhan":        (30.7830, 114.2050),   # ZHHH Tianhe
}

# ──────────────────────────────────────────────────────────────────
# Forecast horizon sanity
# ──────────────────────────────────────────────────────────────────

def is_forecastable(target_date: str) -> tuple[bool, str]:
    """
    Check whether target_date is in the ensemble-forecastable window.
    Returns (ok, reason_if_not).
    """
    try:
        target = datetime.strptime(target_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return False, f"unparseable date: {target_date}"

    now = datetime.now(timezone.utc)
    delta_days = (target.date() - now.date()).days

    if delta_days < 0:
        return False, f"date already in past ({delta_days}d)"
    if delta_days > MAX_FORECAST_DAYS:
        return False, f"date {delta_days}d out, beyond ensemble horizon ({MAX_FORECAST_DAYS}d)"
    return True, "ok"


# ──────────────────────────────────────────────────────────────────
# Ensemble probability
# ──────────────────────────────────────────────────────────────────

def ensemble_probability(
    city: str,
    target_date: str,
    threshold_c: float,
    comparison: str = "above",
) -> Optional[dict]:
    """
    Fetch the 30-member GFS ensemble for `city`'s resolution airport and
    compute P(daily-high {comparison} threshold_c) on target_date.

    comparison:
      "above"  — high > threshold
      "below"  — high < threshold
      "equal"  — high rounds to threshold (±0.5°C bucket, matches Polymarket
                 range resolution like "be 13°C")

    Returns a dict with probability, member_count, median_high, std, and a
    qualitative confidence tag — or None if the request failed, the city is
    unknown, or the date is outside the forecastable window.
    """
    coords = CITY_COORDS.get(city)
    if not coords:
        return None

    ok, _ = is_forecastable(target_date)
    if not ok:
        return None

    lat, lon = coords
    try:
        resp = requests.get(
            ENSEMBLE_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m",
                "models": "gfs_seamless",
                "start_date": target_date,
                "end_date": target_date,
                "temperature_unit": "celsius",
                "timezone": "UTC",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    hourly = data.get("hourly", {})
    member_keys = sorted(k for k in hourly if k.startswith("temperature_2m_member"))
    # include the deterministic run (no suffix) too
    if "temperature_2m" in hourly:
        member_keys = ["temperature_2m"] + member_keys

    if not member_keys:
        return _single_model_prob(city, target_date, threshold_c, comparison)

    daily_highs: list[float] = []
    for key in member_keys:
        temps = [t for t in hourly[key] if t is not None]
        if temps:
            daily_highs.append(max(temps))

    if len(daily_highs) < 5:
        # Too few members to produce a meaningful probability
        return _single_model_prob(city, target_date, threshold_c, comparison)

    n = len(daily_highs)
    if comparison == "above":
        hits = sum(1 for h in daily_highs if h > threshold_c)
    elif comparison == "below":
        hits = sum(1 for h in daily_highs if h < threshold_c)
    else:  # exact bucket — Polymarket "be 13°C" resolves to ±0.5°C band
        hits = sum(1 for h in daily_highs if abs(h - threshold_c) <= 0.5)

    sorted_highs = sorted(daily_highs)
    median_high = sorted_highs[n // 2]
    mean_high = sum(daily_highs) / n
    variance = sum((h - mean_high) ** 2 for h in daily_highs) / n
    std = variance ** 0.5

    empirical_prob = hits / n

    # ── Gaussian blend for narrow-bucket ("equal") markets ──────────
    # Polymarket "be 13°C" resolves on a ±0.5°C band. At 30 members,
    # the empirical count gives you ~3.3% resolution, which is too
    # coarse for a 1°C-wide bucket when σ < 1.5. Fit a Normal(mean,σ)
    # to the ensemble and compute an analytic probability; blend the
    # two (averaged) to keep empirical weight where the ensemble is
    # non-Gaussian (bimodal fronts, etc.).
    gaussian_prob: Optional[float] = None
    if std > 0.05:
        if comparison == "above":
            gaussian_prob = 1.0 - _norm_cdf(threshold_c, mean_high, std)
        elif comparison == "below":
            gaussian_prob = _norm_cdf(threshold_c, mean_high, std)
        else:  # equal — ±0.5°C band around threshold
            gaussian_prob = (
                _norm_cdf(threshold_c + 0.5, mean_high, std)
                - _norm_cdf(threshold_c - 0.5, mean_high, std)
            )
        gaussian_prob = max(0.0, min(1.0, gaussian_prob))

    if gaussian_prob is not None and comparison == "equal":
        # Blend 50/50 for narrow buckets — empirical alone is too grainy
        probability = 0.5 * empirical_prob + 0.5 * gaussian_prob
    else:
        probability = empirical_prob

    return {
        "city": city,
        "date": target_date,
        "threshold_c": threshold_c,
        "comparison": comparison,
        "probability": probability,
        "empirical_prob": round(empirical_prob, 4),
        "gaussian_prob": round(gaussian_prob, 4) if gaussian_prob is not None else None,
        "member_count": n,
        "median_high_c": round(median_high, 1),
        "mean_high_c": round(mean_high, 1),
        "std_c": round(std, 2),
        "confidence": (
            "high"   if std < 1.5 else
            "medium" if std < 3.0 else
            "low"
        ),
    }


def _norm_cdf(x: float, mean: float, std: float) -> float:
    """CDF of Normal(mean, std) at x. Pure stdlib, no numpy."""
    if std <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (std * math.sqrt(2.0))))


def gaussian_bucket_prob(
    mean_c: float, std_c: float, t_low_c: float, t_high_c: float
) -> float:
    """
    Public helper: P(t_low ≤ X ≤ t_high) under Normal(mean_c, std_c).
    Exposed for callers that want to score a bucket directly from a
    point forecast (e.g., deterministic ECMWF) with an assumed σ.
    """
    if std_c <= 0:
        return 1.0 if t_low_c <= mean_c <= t_high_c else 0.0
    return max(0.0, min(1.0,
        _norm_cdf(t_high_c, mean_c, std_c) - _norm_cdf(t_low_c, mean_c, std_c)
    ))


def _single_model_prob(
    city: str, target_date: str, threshold_c: float, comparison: str
) -> Optional[dict]:
    """Fallback to the deterministic GFS run when ensemble members are missing."""
    coords = CITY_COORDS.get(city)
    if not coords:
        return None
    lat, lon = coords
    try:
        resp = requests.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "start_date": target_date,
                "end_date": target_date,
                "temperature_unit": "celsius",
                "timezone": "UTC",
            },
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        high = data["daily"]["temperature_2m_max"][0]
    except Exception:
        return None

    if high is None:
        return None

    # Crude probability shape — mostly used to gate out low-confidence trades,
    # not to size them. Callers should treat `confidence="low"` as a skip.
    if comparison == "above":
        prob = 0.85 if high > threshold_c + 2 else 0.50 if high > threshold_c else 0.15
    elif comparison == "below":
        prob = 0.85 if high < threshold_c - 2 else 0.50 if high < threshold_c else 0.15
    else:
        prob = 0.70 if abs(high - threshold_c) <= 1.0 else 0.20

    return {
        "city": city,
        "date": target_date,
        "threshold_c": threshold_c,
        "comparison": comparison,
        "probability": prob,
        "member_count": 1,
        "median_high_c": round(high, 1),
        "mean_high_c": round(high, 1),
        "std_c": None,
        "confidence": "low",
    }


# ──────────────────────────────────────────────────────────────────
# Question parser
# ──────────────────────────────────────────────────────────────────

def parse_question_threshold(question: str) -> tuple[Optional[float], str]:
    """
    Extract (threshold_celsius, comparison) from a Polymarket weather
    question. Returns (None, "above") on parse failure.

    Handles every temperature phrasing observed on Polymarket's weather
    tag (as of 2026-04). Precipitation markets legitimately return None
    and should be skipped — this model only scores temperature.

    Comparison values:
      "above" — "above 84°F", "54°F or higher", "exceed 20°C"
      "below" — "below 20°C", "71°F or below", "under 10°C"
      "equal" — "be 13°C", "between 68-69°F", "high 13°c"
    """
    if not question:
        return (None, "above")

    q = question.lower().strip()

    # Precipitation markets — fail fast, no temperature signal here
    if any(k in q for k in ("inches", "precipitation", "mm of", "rainfall")):
        return (None, "above")

    # ── Fahrenheit patterns ────────────────────────────────────────

    f_range = re.search(
        r'(\d+\.?\d*)\s*[–\-]\s*(\d+\.?\d*)\s*°?\s*f', q
    )
    if f_range:
        mid = (float(f_range.group(1)) + float(f_range.group(2))) / 2
        return (_f_to_c(mid), "equal")

    f_or_higher = re.search(
        r'(\d+\.?\d*)\s*°?\s*f\s+or\s+(?:higher|above|more)', q
    )
    if f_or_higher:
        return (_f_to_c(float(f_or_higher.group(1))), "above")

    f_or_lower = re.search(
        r'(\d+\.?\d*)\s*°?\s*f\s+or\s+(?:lower|below|less)', q
    )
    if f_or_lower:
        return (_f_to_c(float(f_or_lower.group(1))), "below")

    f_above = re.search(
        r'(?:above|over|exceed|higher\s+than|at\s+least|≥|>=)\s*(\d+\.?\d*)\s*°?\s*f', q
    )
    if f_above:
        return (_f_to_c(float(f_above.group(1))), "above")

    f_below = re.search(
        r'(?:below|under|less\s+than|at\s+most|≤|<=)\s*(\d+\.?\d*)\s*°?\s*f', q
    )
    if f_below:
        return (_f_to_c(float(f_below.group(1))), "below")

    f_exact = re.search(r'be\s+(\d+\.?\d*)\s*°?\s*f', q)
    if f_exact:
        return (_f_to_c(float(f_exact.group(1))), "equal")

    f_bare = re.search(r'high\s+(?:of\s+)?(\d+\.?\d*)\s*°?\s*f', q)
    if f_bare:
        return (_f_to_c(float(f_bare.group(1))), "equal")

    # ── Celsius patterns ───────────────────────────────────────────

    c_range = re.search(
        r'(\d+\.?\d*)\s*[–\-]\s*(\d+\.?\d*)\s*°?\s*c', q
    )
    if c_range:
        mid = (float(c_range.group(1)) + float(c_range.group(2))) / 2
        return (mid, "equal")

    c_or_higher = re.search(
        r'(\d+\.?\d*)\s*°?\s*c\s+or\s+(?:higher|above|more)', q
    )
    if c_or_higher:
        return (float(c_or_higher.group(1)), "above")

    c_or_lower = re.search(
        r'(\d+\.?\d*)\s*°?\s*c\s+or\s+(?:lower|below|less)', q
    )
    if c_or_lower:
        return (float(c_or_lower.group(1)), "below")

    c_above = re.search(
        r'(?:above|over|exceed|higher\s+than|at\s+least|≥|>=)\s*(\d+\.?\d*)\s*°?\s*c', q
    )
    if c_above:
        return (float(c_above.group(1)), "above")

    c_below = re.search(
        r'(?:below|under|less\s+than|at\s+most|≤|<=)\s*(\d+\.?\d*)\s*°?\s*c', q
    )
    if c_below:
        return (float(c_below.group(1)), "below")

    # Exact: "be 13°C" — most common Polymarket format
    c_exact = re.search(r'be\s+(\d+\.?\d*)\s*°?\s*c', q)
    if c_exact:
        return (float(c_exact.group(1)), "equal")

    c_bare = re.search(r'high\s+(?:of\s+)?(\d+\.?\d*)\s*°?\s*c', q)
    if c_bare:
        return (float(c_bare.group(1)), "equal")

    # ── Last resort ────────────────────────────────────────────────

    generic = re.search(r'be\s+(\d+\.?\d*)\s*°', q)
    if generic:
        val = float(generic.group(1))
        # Heuristic: >45° is almost certainly Fahrenheit
        if val > 45:
            return (_f_to_c(val), "equal")
        return (val, "equal")

    return (None, "above")


def _f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 2)
