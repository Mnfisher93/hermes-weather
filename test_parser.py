"""Test parser coverage against all live markets."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

from core.scanner import get_weather_markets
from core.weather import parse_question_threshold

print("Fetching all weather markets...")
markets = get_weather_markets(min_volume=1000)
print(f"Total: {len(markets)} markets\n")

parsed = 0
failed = 0
failed_questions = []
by_comparison = {"above": 0, "below": 0, "equal": 0}

for m in markets:
    q = m["question"]
    threshold, comparison = parse_question_threshold(q)
    if threshold is not None:
        parsed += 1
        by_comparison[comparison] = by_comparison.get(comparison, 0) + 1
    else:
        failed += 1
        if len(failed_questions) < 10:
            failed_questions.append(q)

pct = parsed / len(markets) * 100 if markets else 0
print(f"Parsed:  {parsed}/{len(markets)} ({pct:.1f}%)")
print(f"Failed:  {failed}/{len(markets)} ({100-pct:.1f}%)")
print(f"\nBy comparison type:")
for comp, count in by_comparison.items():
    print(f"  {comp:8s}: {count}")

if failed_questions:
    print(f"\nSample failed questions ({min(10, failed)} shown):")
    for q in failed_questions:
        print(f"  - {q}")
else:
    print("\n✅ All questions parsed successfully!")
