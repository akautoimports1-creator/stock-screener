"""
Pulls fund-level data (expense ratio, AUM, distribution yield, 1-year change)
for every ETF in the universe and writes it to data/etfs.json.

Same shape and philosophy as fetch_data.py (see that file's docstring) — best
effort, degrades gracefully, sharded across a GitHub Actions matrix job. ETFs
don't have earnings, so P/E-style metrics don't apply here; instead we score
on cost (expense ratio) and recent performance (52-week price change).
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

from universe import get_etf_universe

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Lower default concurrency than the stock fetcher — fund "quote" lookups
# on Yahoo seem to get rate-limited harder than plain equity lookups, and
# this job used to run at the same time as the (heavier, 10-shard) stock
# job, doubling the hammering. See RATE_LIMIT_SLEEP below and the workflow
# (fetch-etfs now waits on `merge` so the two jobs don't overlap).
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_COUNT = int(os.environ.get("SHARD_COUNT", "1"))
MAX_SYMBOLS = os.environ.get("MAX_SYMBOLS")
RETRIES = 4

SHARD_FILE = DATA_DIR / (f"etf-shard-{SHARD_INDEX}.json" if SHARD_COUNT > 1 else "etfs.json")

# yfinance has used a few different key names for a fund's expense ratio over
# the years (undocumented, unofficial data source) — try each, take the
# first that's actually present.
EXPENSE_RATIO_KEYS = ["annualReportExpenseRatio", "netExpenseRatio", "expenseRatio"]

# Same idea for the 1-year price change: "52WeekChange" is the field this
# was originally built against, but a first real run only populated it for
# ~0.3% of ETFs (11 of 3332) — far below what a genuine Yahoo coverage gap
# looks like on the stock side, so that's very likely the wrong/an
# inconsistent key for funds specifically. Trying a few plausible
# alternates here is free (same info dict, no extra request); the real
# fix, if none of these help either, is switching to yfinance's
# `Ticker.funds_data.fund_performance` (a separate, slower call per
# symbol) for a properly-reported trailing return.
YEAR_CHANGE_KEYS = ["52WeekChange", "fiftyTwoWeekChange", "ytdReturn"]


def _first_present(info: dict, keys):
    for k in keys:
        v = info.get(k)
        if v is not None:
            return v
    return None


def fetch_one(symbol: str, name: str, exchange: str):
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            t = yf.Ticker(symbol)
            info = t.get_info()
            row = {"symbol": symbol, "name": name or info.get("shortName") or symbol, "exchange": exchange}
            row["category"] = info.get("category")
            row["fundFamily"] = info.get("fundFamily")
            price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("navPrice")
            row["price"] = price
            row["navPrice"] = info.get("navPrice")
            row["totalAssets"] = info.get("totalAssets")
            row["fiftyTwoWeekLow"] = info.get("fiftyTwoWeekLow")
            row["fiftyTwoWeekHigh"] = info.get("fiftyTwoWeekHigh")
            row["yearChange"] = _first_present(info, YEAR_CHANGE_KEYS)
            row["distributionYield"] = info.get("yield")
            row["expenseRatio"] = _first_present(info, EXPENSE_RATIO_KEYS)
            row["updated"] = datetime.now(timezone.utc).isoformat()
            return row
        except Exception as e:  # noqa: BLE001 - best-effort scraper, same as fetch_data.py
            last_err = e
            # A 429 needs a real cooldown, not a quick retry — back off hard
            # and get progressively slower each attempt. A different kind of
            # failure (timeout, bad symbol, etc.) uses the old, shorter delay.
            if "Too Many Requests" in str(e) or "Rate limited" in str(e):
                time.sleep(6 * (attempt + 1) + random.random() * 5)
            else:
                time.sleep(1.5 + random.random() * 2)
    return {"symbol": symbol, "name": name, "exchange": exchange, "error": str(last_err)}


def load_existing():
    if SHARD_FILE.exists():
        try:
            with open(SHARD_FILE, "r") as f:
                rows = json.load(f)
            return {r["symbol"]: r for r in rows if r.get("symbol")}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def main():
    universe = get_etf_universe(limit=int(MAX_SYMBOLS) if MAX_SYMBOLS else None)

    if SHARD_COUNT > 1:
        universe = [row for i, row in enumerate(universe) if i % SHARD_COUNT == SHARD_INDEX]

    print(f"Processing {len(universe)} ETFs (shard {SHARD_INDEX}/{SHARD_COUNT})", flush=True)

    existing = load_existing()
    results = dict(existing)

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
                if symbol not in results:
                    results[symbol] = row
            else:
                results[symbol] = row

            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(universe)} processed ({failed} failed so far)", flush=True)
                write_output(results)

    write_output(results)
    print(f"First pass done. {done} processed, {failed} failed.", flush=True)

    # Second pass, serial and slow: most first-pass failures are Yahoo rate
    # limits, not genuinely bad symbols, so it's worth cooling down and
    # trying the failed ones again one at a time instead of leaving them
    # blank for a full day until the next scheduled run.
    retry_symbols = [s for s, r in results.items() if r.get("error")]
    if retry_symbols:
        print(f"Retrying {len(retry_symbols)} failed symbols after a cooldown...", flush=True)
        time.sleep(20)
        universe_by_symbol = {row["symbol"]: row for row in universe}
        recovered = 0
        for symbol in retry_symbols:
            row_info = universe_by_symbol.get(symbol, {})
            row = fetch_one(symbol, row_info.get("name"), row_info.get("exchange", ""))
            results[symbol] = row
            if not row.get("error"):
                recovered += 1
            time.sleep(0.5)
        print(f"Retry pass recovered {recovered}/{len(retry_symbols)}.", flush=True)
        write_output(results)

    final_failed = sum(1 for r in results.values() if r.get("error"))
    print(f"Done. {done} processed, {final_failed} still failing, {len(results)} total rows in file.", flush=True)


def write_output(results: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = sorted(results.values(), key=lambda r: r.get("symbol", ""))

    tmp_path = SHARD_FILE.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(rows, f, separators=(",", ":"))
    os.replace(tmp_path, SHARD_FILE)


if __name__ == "__main__":
    sys.exit(main())
