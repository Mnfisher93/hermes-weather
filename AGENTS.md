# AGENTS.md — Handoff for Antigravity / Claude Code / Hermes

> This file is the ground truth for autonomous agents working on this repo.
> Read it before CLAUDE.md. If CLAUDE.md and this file disagree, this file wins.

## Project status (2026-04-20)

Hermes is a working Polymarket weather-arbitrage bot. The scaffold is complete:

  core/scanner.py        fetches Gamma markets, filters weather-tagged, classifies
  core/weather.py        GFS ensemble + Gaussian-bucket blend, per-airport coords
  core/edge.py           full pipeline: parse → ensemble → classify → EV → Kelly
  core/executor.py       SIM/LIVE via py-clob-client with $0.01 price floor
  core/risk.py           7 mandatory risk controls
  core/calibration.py    SQLite: log predictions, resolve via Gamma, Brier scores
  core/adapt.py          adaptive kelly_fraction + min_ev from calibration history
  ui/dashboard.py        Rich 3-panel live terminal UI
  main.py                CLI: --sim --live --scan-only --halt --report --no-ui --once
  config.json            externalized params

Last verified scan: 141–144 live markets, ~3 min full cycle (GFS ensemble is
the bottleneck at ~1.3s/city), produces 2–5 actionable signals per cycle after
sanity filters.

## Recent upgrades (this session)

Two ideas mined from nicolastinkl/hermes_weatherbot:

1. **Gaussian-bucket probability** in `core/weather.py`. For narrow "equal"
   buckets (Polymarket "be 13°C" resolves on ±0.5°C), we now fit Normal(mean,σ)
   to the 30-member ensemble and blend 50/50 with empirical counts. Empirical
   alone is too grainy at that resolution. Gate this on `comparison == "equal"`.
   New helper: `gaussian_bucket_prob(mean_c, std_c, t_low_c, t_high_c)`.

2. **Adaptive tuner** in `core/adapt.py`. Reads resolved predictions, computes
   winrate + net PnL, adjusts kelly_fraction/min_ev:
     - winrate < 0.45      → kelly ×0.8, min_ev +0.01  ("underperforming")
     - winrate > 0.55, pnl>0 → kelly ×1.1, min_ev −0.005 ("outperforming")
     - n < 30 resolved     → base values ("cold_start")
   Bounds: kelly ∈ [0.05, 0.50], min_ev ∈ [0.01, 0.20].
   Wired into `main.scan_cycle` — reported to dashboard on every cycle.

## Known gaps / next work

- Weekly/monthly Brier trend view in `--report` (right now it only shows
  windowed means, not a time series).
- Telegram notifications hook. Code is trivial; skipped so far to avoid
  leaking a bot token. When adding: store token in .env, never config.json.
- Per-city adaptive params. `adaptive_params(city=...)` is already supported
  but not yet called per-market in the edge pipeline.
- `scripts/track_coldmath.py` — pulls @coldmath's positions via Data API;
  not yet integrated into signal confirmation.
- `data/calibration.db` is empty on fresh installs. The tuner returns
  cold_start until ~30 predictions resolve — expect ~2 weeks of SIM run
  before adaptation kicks in.

## Rules for agents editing this repo

1. Never hardcode city coordinates to city centers. The ICAO→lat/lon table
   in core/weather.py CITY_COORDS comes from aviationweather.gov — those
   are the exact resolution stations. Silent edge loss if you shift them.
2. Never add imports without verifying they're in `.venv` already. Current
   deps: py-clob-client, web3, python-dotenv, requests, rich, aiohttp,
   aiosqlite. No numpy/pandas — everything is stdlib math.
3. Do not commit `.env`, config.json with real Telegram tokens, or any
   `pip_tmp/` directory. The reference repo (nicolastinkl/hermes_weatherbot)
   leaked a live bot token in config.json — do not repeat that.
4. SIM mode is the default. `--live` requires typing "GO LIVE" at the
   prompt unless HERMES_SKIP_LIVE_CONFIRM=1 is set.
5. The ensemble API 400s if you send both `start_date` and `forecast_days`.
   Send only `start_date`+`end_date` (already fixed — don't revert).
6. The question parser handles ~94% of temperature markets. The 6% that
   fail are legitimately precipitation markets; they return (None,"above")
   and edge.py skips them. Do not add precipitation handling unless you
   also add a precipitation forecast source — Open-Meteo has one.

## Running it

```
cd ~/Antigravity/Hermes
source .venv/bin/activate

python main.py --scan-only --no-ui --once     # one clean scan, plain output
python main.py --sim --once                   # dashboard, one cycle
python main.py --report                       # calibration state
python main.py                                # default: SIM + dashboard loop
python main.py --live                         # real orders (prompts)
python main.py --halt                         # cancel all open orders
```

## Quick sanity check after any weather.py / edge.py edit

```
python -c "
from core.weather import gaussian_bucket_prob, ensemble_probability
from core.adapt import adaptive_params
print('gauss  22.2±1 in [22,23] →', round(gaussian_bucket_prob(22.2,1.0,22,23),3))
print('adapt cold-start →', adaptive_params())
r = ensemble_probability('Ankara','2026-04-22',13.0,'equal')
print('live ensemble →', r['probability'] if r else None)
"
```

Expected: 0.367, cold_start dict, a number between 0 and 1.
