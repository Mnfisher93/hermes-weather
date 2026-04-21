"""Quick smoke test — scan live markets and ensemble API."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

from core.scanner import get_weather_markets
from core.weather import ensemble_probability, parse_question_threshold

print("=== MARKET SCANNER ===")
markets = get_weather_markets(min_volume=1000)
print(f"Found {len(markets)} liquid weather markets\n")

for m in markets[:8]:
    city = m["city"]
    yp = m["yes_price"]
    vol = m["volume"]
    q = m["question"][:65]
    print(f"  {city:15s} | YES={yp:.2f} | vol=${vol:,.0f} | {q}")

if markets:
    print("\n=== ENSEMBLE TEST ===")
    m0 = markets[0]
    threshold_c, comparison = parse_question_threshold(m0["question"])
    print(f"Market: {m0['question'][:80]}")
    print(f"City: {m0['city']}, Threshold: {threshold_c}°C, Comparison: {comparison}")
    if threshold_c is not None:
        wx = ensemble_probability(m0["city"], m0["end_date"], threshold_c, comparison)
        if wx:
            print(f"Ensemble prob: {wx['probability']:.3f} ({wx['member_count']} members)")
            print(f"Median high: {wx['median_high_c']}°C, Std: {wx['std_c']}°C")
            print(f"Confidence: {wx['confidence']}")
        else:
            print("Ensemble returned None")
    else:
        print("Could not parse threshold from question")
else:
    print("No markets found!")
