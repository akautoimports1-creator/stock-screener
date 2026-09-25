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

# Crude, free, keyword-based headline sentiment. This is NOT real NLP — it's a
# word-count heuristic over the last few headlines Yahoo has for the symbol.
# It's meant as a rough "is recent coverage leaning positive or negative"
# signal, not investment research.
POSITIVE_WORDS = {
    "beat", "beats", "beating", "surge", "surges", "surged", "soar", "soars",
    "soared", "jump", "jumps", "jumped", "rally", "rallies", "rallied",
    "upgrade", "upgraded", "outperform", "record", "strong", "growth",
    "profit", "profitable", "gain", "gains", "bullish", "exceed", "exceeds",
    "exceeded", "tops", "top", "raises", "raised", "expands", "expansion",
    "wins", "win", "approval", "approved", "partnership", "breakthrough",
    "positive", "boosts", "boost", "rebound", "rebounds", "upbeat",
}
NEGATIVE_WORDS = {
    "miss", "misses", "missed", "plunge", "plunges", "plunged", "fall",
    "falls", "fell", "drop", "drops", "dropped", "downgrade", "downgraded",
    "underperform", "weak", "loss", "losses", "decline", "declines",
    "declined", "bearish", "cuts", "cut", "lawsuit", "investigation",
    "recall", "bankruptcy", "layoffs", "layoff", "warns", "warning",
    "negative", "fraud", "probe", "sues", "sued", "slump", "slumps",
    "tumble", "tumbles", "tumbled", "scandal", "delist", "delisted",
}


def extract_headline(item):
    """Handles both the old (flat) and new (nested under "content") yfinance
    news item shapes, since Yahoo's undocumented format has changed before."""
    if not isinstance(item, dict):
        return None, None
    content = item.get("content")
    if isinstance(content, dict):
        title = content.get("title")
        url = None
        canonical = content.get("canonicalUrl")
        if isinstance(canonical, dict):
            url = canonical.get("url")
        if not url:
            click = content.get("clickThroughUrl")
            if isinstance(click, dict):
                url = click.get("url")
        return title, url
    return item.get("title"), item.get("link")


def score_sentiment(headlines):
    if not headlines:
        return None, 0
    net = 0
    for h in headlines:
        low = h.lower()
        net += sum(1 for w in POSITIVE_WORDS if w in low)
        net -= sum(1 for w in NEGATIVE_WORDS if w in low)
    label = "Positive" if net > 0 else "Negative" if net < 0 else "Neutral"
    return label, net


def fetch_news(t):
    """Best-effort — a news failure should never blank out the valuation
    data for a symbol, so this always returns something usable."""
    headlines, top_title, top_url = [], None, None
    try:
        items = t.news or []
    except Exception:  # noqa: BLE001
        items = []
    for item in items[:5]:
        title, url = extract_headline(item)
        if title:
            headlines.append(title)
            if top_title is None:
                top_title, top_url = title, url
    label, score = score_sentiment(headlines)
    return {
        "newsSentiment": label,
        "newsScore": score,
        "newsCount": len(headlines),
        "topHeadline": top_title,
        "topHeadlineUrl": top_url,
    }


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
            row.update(fetch_news(t))
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
