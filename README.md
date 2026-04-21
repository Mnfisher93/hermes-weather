# Hermes

Autonomous Polymarket weather-arbitrage bot. Runs the 30-member GFS ensemble forecast (via Open-Meteo) against Polymarket's implied odds on temperature markets, bets where the model disagrees with the crowd, and sizes positions with fractional Kelly.

The competitive edge isn't a better forecast — it's latency. Pro forecast APIs refresh every 2–6 hours; Polymarket prices lag. Hermes exploits that gap.

Status: SIM mode works end-to-end. Live trading is gated behind an explicit `GO LIVE` typed confirmation.

---

## How it works

```
scanner.py   →  Gamma API: pull all weather-tagged markets, filter by volume
weather.py   →  Open-Meteo ensemble: 30 GFS members at the resolution airport
edge.py      →  P(model) − P(market)  →  EV gate  →  fractional Kelly sizing
risk.py      →  7 hard caps: per-bet, exposure, drawdown, slippage, liquidity
executor.py  →  py-clob-client: book check, then GTC limit order on CLOB
calibration  →  SQLite log of every prediction; Brier score + Gamma resolution
adapt.py     →  auto-tunes kelly_fraction + min_ev from rolling winrate
```

One scan cycle ≈ 3 minutes against ~140 live weather markets (GFS ensemble fetch is the bottleneck).

### Signal types

- **LAG** — model says YES ≥ 60%, market still underpricing → buy YES
- **LOCK** — model says ≤ 3% YES, NO trades at 95–99¢ → sell YES / buy NO cheap
- **CONTRA** — market at extreme 95–99¢ YES, model disagrees → buy NO at a discount (coldmath-style)
- **FADE** — market overpricing, model slightly against → buy the other side

---

## Critical detail: resolution airports

Polymarket weather markets resolve against Wunderground station data at **a specific airport**, not the city center. NYC resolves against LaGuardia (KLGA), not JFK. Dallas is Love Field (KDAL), not DFW. Tokyo is Haneda (RJTT), not Narita.

`core/weather.py` ships with a hardcoded `CITY_COORDS` table pulled from the aviationweather.gov station registry. **Do not change these to city-center coords** — a 10–50 km offset routinely shifts the daily high by 2–5°F, which is the entire width of a Polymarket resolution bracket.

---

## Setup

```bash
git clone https://github.com/Chadwick-Astor/hermes-weather
cd hermes-weather
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your Polygon trading wallet key
```

You need:
- A fresh Polygon wallet (don't use your main wallet — export key from MetaMask/Rabby)
- USDC.e on Polygon (token `0x2791Bca1...4174`, NOT native USDC)
- ~0.5 MATIC for gas
- For first-time use: deposit once on polymarket.com to trigger the required token approvals

### First run (SIM mode, no orders)

```bash
python main.py --scan-only --once --no-ui
```

Takes ~3 min. You should see 0–5 signals with EV > 3% against the live Polymarket book. Zero signals on a given day is normal — not every day has mispricings.

### Full dashboard

```bash
python main.py           # SIM mode + rich 3-panel UI, loops every 50 min
python main.py --once    # single cycle then exit
```

### Live

```bash
python main.py --report  # check calibration state first
python main.py --live    # prompts for GO LIVE
python main.py --halt    # emergency: cancels all open orders
```

Before flipping live: shrink `config.json` to `max_bet_usd: 5`, `max_exposure_usd: 20`, `kelly_fraction: 0.10`. Your first live session exists to confirm the wire-up, not to make money.

---

## Config

`config.json`:

```json
{
  "min_edge": 0.05,
  "min_ev": 0.03,
  "kelly_fraction": 0.25,
  "max_bet_usd": 50,
  "max_exposure_usd": 200,
  "max_drawdown_pct": 0.20,
  "min_market_volume": 5000,
  "max_slippage": 0.03,
  "scan_interval_seconds": 3000,
  "cities": ["Atlanta","Austin","Chicago","Dallas",...]
}
```

`.env`:

```
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER=            # leave blank for standard EOA
CHAIN_ID=137
CLOB_HOST=https://clob.polymarket.com
```

---

## Architecture

```
hermes/
├── core/
│   ├── scanner.py       # Gamma polling + signal classification
│   ├── weather.py       # GFS ensemble + Gaussian bucket blend + parser
│   ├── edge.py          # full pipeline orchestrator
│   ├── executor.py      # CLOB client: book check + order submission
│   ├── risk.py          # 7 hard risk controls + kill switch
│   ├── calibration.py   # SQLite predictions + Brier + Gamma resolution poller
│   └── adapt.py         # adaptive kelly + min_ev from rolling performance
├── ui/
│   └── dashboard.py     # rich 3-panel terminal UI
├── scripts/
│   └── track_coldmath.py # scrape @coldmath's trade history from Data API
├── data/                # calibration.db + per-market snapshots (gitignored)
├── config.json
├── main.py              # CLI entry: --sim --live --scan-only --halt --report
└── requirements.txt
```

---

## Math

**Expected value**
```
EV_YES = p_model * (1 - price) - (1 - p_model) * price
EV_NO  = (1 - p_model) * (1 - (1-price)) - p_model * (1 - price)
```

**Fractional Kelly**
```
odds  = 1/price - 1
kelly = (p_model * odds - (1-p_model)) / odds
size  = min(kelly * 0.25 * bankroll, max_bet, 5% * bankroll)
```

**Gaussian bucket** (for narrow "be 13°C" markets that resolve on ±0.5°C)
```
mean, σ = ensemble stats
p_bucket = Φ((threshold+0.5 - mean)/σ) - Φ((threshold-0.5 - mean)/σ)
blended  = 0.5 * empirical_count + 0.5 * p_bucket
```

---

## References

Forecast:
- [Open-Meteo ensemble API](https://open-meteo.com/en/docs/ensemble-api)
- [NOAA/NWS](https://www.weather.gov/documentation/services-web-api) — resolution-source validation
- [aviationweather.gov METAR](https://aviationweather.gov/data/api/) — station registry

Polymarket:
- [py-clob-client](https://github.com/Polymarket/py-clob-client)
- [Gamma API](https://docs.polymarket.com/#gamma-markets-api) — market metadata, no auth
- [Data API](https://docs.polymarket.com/#data-api) — portfolio + trade history

Related bots:
- [alteregoeth-ai/weatherbot](https://github.com/alteregoeth-ai/weatherbot) — Kelly + EV filter reference
- [suislanchez/polymarket-kalshi-weather-bot](https://github.com/suislanchez/polymarket-kalshi-weather-bot) — 31-member ensemble + Brier calibration
- [Polymarket/agents](https://github.com/Polymarket/agents) — official agent framework

---

## License

MIT

---

**⚠ This bot trades real markets with real money when run in `--live` mode. Start with caps < $25, watch the first cycles live, and test `--halt` from a second terminal before you need it. Past performance does not guarantee future results.**
