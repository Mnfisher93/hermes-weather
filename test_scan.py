"""Quick smoke test — no wallet needed, SIM_MODE only."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

from core.scanner import get_weather_markets
from core.edge import calculate_edge

bankroll = float(os.getenv("MAX_EXPOSURE_USD", "200"))
max_bet = float(os.getenv("MAX_BET_USD", "50"))
min_edge = float(os.getenv("MIN_EDGE", "0.05"))

print("Fetching weather markets...")
markets = get_weather_markets(min_volume=5000)
print(f"Found {len(markets)} liquid weather markets\n")

signals = []
for m in markets[:20]:  # test first 20
    print(f"  Checking {m['city']} — {m['question'][:60]}...")
    sig = calculate_edge(m, max_bet_usd=max_bet, bankroll=bankroll, min_edge=min_edge)
    if sig:
        signals.append(sig)
        tag = f"[{sig['type']}]"
        print(f"    {tag} {sig['side']} @ {sig['yes_price']:.2f} | "
              f"model={sig['model_prob']:.2f} | edge={sig['edge']:.3f} | "
              f"size=${sig['position_usd']:.2f}")

print(f"\n{'='*60}")
print(f"Signals found: {len(signals)} / {min(20, len(markets))} markets checked")
for s in signals:
    print(f"  {s['type']:4s} | {s['city']:15s} | {s['side']} @ ${s['yes_price']:.2f} "
          f"| prob={s['model_prob']:.2f} | ev={s['ev']:.3f} | ${s['position_usd']:.2f}")
