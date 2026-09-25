"""
Pulls valuation data (P/E, forward P/E, PEG, and a few supporting fields) for
every ticker in the universe and writes it to data/stocks.json.

Data source: Yahoo Finance, via the `yfinance` library. This is an
UNOFFICIAL source (no public API key, no SLA) — it is what makes full-market
coverage possible on a $0 budget, but it can rate-limit, change shape, or go
down without notice. The script is written to degrade gracefully when that
happens: a symbol that fails this run keeps its last known values instead of
being blanked out.

Run in shards so a GitHub Actions job doesn't need to finish 7,000+ lookups
in one pass: set SHARD_INDEX / SHARD_COUNT env vars (0-based index / total
shard count) to process only every Nth symbol. The workflow runs several
shards as a matrix job.
"""

import concurrent.futures
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

from universe import get_universe

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
META_FILE = DATA_DIR / "meta.json"

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))
SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_COUNT = int(os.environ.get("SHARD_COUNT", "1"))
MAX_SYMBOLS = os.environ.get("MAX_SYMBOLS")  # optional cap, for local testing
RETRIES = 2

# Each shard writes its own file so parallel matrix jobs never write-conflict.
# A separate merge step (merge.py) combines them into data/stocks.json.
SHARD_FILE = DATA_DIR / (f"shard-{SHARD_INDEX}.json" if SHARD_COUNT > 1 else "stocks.json")

FIELDS = [
    "symbol", "shortName", "sector", "industry", "exchange",
    "currentPrice", "regularMarketPrice", "marketCap",
    "trailingPE", "forwardPE", "pegRatio",
    "trailingEps", "forwardEps", "dividendYield",
    "fiftyTwoWeekLow", "fiftyTwoWeekHigh",
    "targetMeanPrice", "recommendationKey",
]


def load_existing():
    if SHARD_FILE.exists():
        try:
            with open(SHARD_FILE, "r") as f:
                rows = json.load(f)
            return {r["symbol"]: r for r in rows if r.get("symbol")}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def fetch_one(symbol: str, name: str, exchange: str):
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            t = yf.Ticker(symbol)
            info = t.get_info()
            row = {"symbol": symbol, "name": name or info.get("shortName") or symbol, "exchange": exchange}
            for f in FIELDS:
                if f in ("symbol",):
                    continue
                val = info.get(f)
                row[f] = val
            price = row.get("currentPrice") or row.get("regularMarketPrice")
            row["price"] = price
            row.pop("currentPrice", None)
            row.pop("regularMarketPrice", None)
            row["updated"] = datetime.now(timezone.utc).isoformat()
            return row
        except Exception as e:  # noqa: BLE001 - deliberately broad, this is a best-effort scraper
            last_err = e
            time.sleep(1.5 + random.random() * 2)
    return {"symbol": symbol, "name": name, "exchange": exchange, "error": str(last_err)}


def main():
    universe = get_universe(limit=int(MAX_SYMBOLS) if MAX_SYMBOLS else None)

    if SHARD_COUNT > 1:
        universe = [row for i, row in enumerate(universe) if i % SHARD_COUNT == SHARD_INDEX]

    print(f"Processing {len(universe)} symbols (shard {SHARD_INDEX}/{SHARD_COUNT})", flush=True)

    existing = load_existing()
    results = dict(existing)  # start from what we already have; overwrite as new data lands

    done = 0
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(fetch_one, row["symbol"], row["name"], row["exchange"]): row["symbol"]
            for row in universe
        }
        for fut in concurrent.futures.as_completed(futures):
            symbol = futures[fut]
            try:
                row = fut.result()
            except Exception as e:  # noqa: BLE001
                row = {"symbol": symbol, "error": str(e)}

            if row.get("error"):
                failed += 1
                # keep whatever we had before rather than losing the row
                if symbol not in results:
                    results[symbol] = row
            else:
                results[symbol] = row

            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(universe)} processed ({failed} failed so far)", flush=True)
                # checkpoint periodically so a killed job doesn't lose everything
                write_output(results)

    write_output(results)
    print(f"Done. {done} processed, {failed} failed, {len(results)} total rows in file.", flush=True)


def write_output(results: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = sorted(results.values(), key=lambda r: r.get("symbol", ""))

    tmp_path = SHARD_FILE.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(rows, f, separators=(",", ":"))
    os.replace(tmp_path, SHARD_FILE)


if __name__ == "__main__":
    sys.exit(main())
