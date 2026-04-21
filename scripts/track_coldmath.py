#!/usr/bin/env python3
"""Poll coldmath's Polymarket trades and persist to SQLite.

Run hourly via cron. Dedupes on transactionHash+asset+side so repeated runs
are idempotent. Paginates until it finds trades already stored (or hits the
3000-offset cap).

Output: ~/Antigravity/Hermes/data/coldmath_trades.sqlite
Prints a short summary to stdout for the cron delivery channel.
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

WALLET = "0x594edb9112f526fa6a80b8f858a6379c8a2c1c11"
DB_PATH = Path.home() / "Antigravity/Hermes/data/coldmath_trades.sqlite"
API = "https://data-api.polymarket.com/activity"
PAGE = 500
MAX_OFFSET = 3000  # API hard cap

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    tx_hash     TEXT NOT NULL,
    asset       TEXT NOT NULL,
    side        TEXT NOT NULL,
    timestamp   INTEGER NOT NULL,
    condition_id TEXT,
    title       TEXT,
    slug        TEXT,
    event_slug  TEXT,
    outcome     TEXT,
    outcome_index INTEGER,
    price       REAL,
    size        REAL,
    usdc_size   REAL,
    fetched_at  INTEGER,
    PRIMARY KEY (tx_hash, asset, side)
);
CREATE INDEX IF NOT EXISTS idx_ts      ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_cid     ON trades(condition_id);
CREATE INDEX IF NOT EXISTS idx_slug    ON trades(slug);
"""


def fetch_page(offset: int) -> list[dict]:
    url = f"{API}?user={WALLET}&limit={PAGE}&offset={offset}&type=TRADE"
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-coldmath/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    if isinstance(data, dict) and "error" in data:
        return []
    return data if isinstance(data, list) else []


def upsert(conn: sqlite3.Connection, trades: list[dict]) -> tuple[int, int]:
    now = int(time.time())
    new = dup = 0
    for t in trades:
        row = (
            t["transactionHash"], t["asset"], t["side"], int(t["timestamp"]),
            t.get("conditionId"), t.get("title"), t.get("slug"),
            t.get("eventSlug"), t.get("outcome"), t.get("outcomeIndex"),
            float(t["price"]), float(t["size"]), float(t["usdcSize"]), now,
        )
        try:
            conn.execute(
                "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
            )
            new += 1
        except sqlite3.IntegrityError:
            dup += 1
    return new, dup


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    total_new = total_dup = total_fetched = 0
    offset = 0
    consecutive_dup_pages = 0
    while offset < MAX_OFFSET:
        try:
            trades = fetch_page(offset)
        except Exception as e:
            print(f"[err] offset={offset}: {e}", file=sys.stderr)
            break
        if not trades:
            break
        total_fetched += len(trades)
        n, d = upsert(conn, trades)
        total_new += n
        total_dup += d
        conn.commit()
        # If an entire page was already stored, we've reached known history
        # (recent-first order). Stop early to avoid burning API calls.
        if n == 0:
            consecutive_dup_pages += 1
            if consecutive_dup_pages >= 1:
                break
        else:
            consecutive_dup_pages = 0
        if len(trades) < PAGE:
            break
        offset += PAGE

    # Summary
    cur = conn.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM trades")
    total_rows, ts_min, ts_max = cur.fetchone()
    print(f"coldmath tracker — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"  fetched this run: {total_fetched}  new: {total_new}  dup: {total_dup}")
    print(f"  total stored:     {total_rows}")
    if ts_min and ts_max:
        span_h = (ts_max - ts_min) / 3600
        print(f"  window:           {time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts_min))} "
              f"→ {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ts_max))}  ({span_h:.1f}h)")

    if total_new:
        cur = conn.execute(
            "SELECT title, outcome, side, price, usdc_size "
            "FROM trades ORDER BY timestamp DESC LIMIT 5"
        )
        print("  latest 5 trades:")
        for title, outcome, side, price, usd in cur.fetchall():
            t = (title or "")[:55]
            print(f"    {side} {outcome:<5} @ {price:.3f}  ${usd:>7.2f}  {t}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
