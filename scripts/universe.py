"""
Builds the list of tickers to screen.

Source: Nasdaq Trader's public symbol directory files. These are plain-text,
pipe-delimited files that list every symbol on Nasdaq plus every symbol on
NYSE/NYSE American/Cboe/IEX ("otherlisted"). No API key required.

  https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
  https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt

We filter out test issues, warrants, units, and other non-common-stock
listings so the screener isn't full of SPAC warrants and preferred shares.
"""

import csv
import io
import re
import sys
import requests

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# Symbols with these characters are usually warrants/units/rights/preferreds,
# not plain common stock. This is a heuristic, not a guarantee.
JUNK_SUFFIX_RE = re.compile(r"[.\-^=~]")


def _fetch(url: str, timeout: int = 30) -> str:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "stock-screener/1.0"})
    resp.raise_for_status()
    return resp.text


def _parse_nasdaq(text: str, want_etf: bool):
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    out = []
    for row in reader:
        symbol = (row.get("Symbol") or "").strip()
        name = (row.get("Security Name") or "").strip()
        if not symbol or symbol.startswith("File Creation Time"):
            continue
        if row.get("Test Issue") == "Y":
            continue
        is_etf = row.get("ETF") == "Y"
        if is_etf != want_etf:
            continue
        if JUNK_SUFFIX_RE.search(symbol):
            continue
        out.append({"symbol": symbol, "name": name, "exchange": "NASDAQ"})
    return out


def _parse_other(text: str, want_etf: bool):
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    out = []
    for row in reader:
        symbol = (row.get("ACT Symbol") or "").strip()
        name = (row.get("Security Name") or "").strip()
        if not symbol or symbol.startswith("File Creation Time"):
            continue
        if row.get("Test Issue") == "Y":
            continue
        is_etf = row.get("ETF") == "Y"
        if is_etf != want_etf:
            continue
        if JUNK_SUFFIX_RE.search(symbol):
            continue
        exch_code = (row.get("Exchange") or "").strip()
        exch_map = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEXG"}
        out.append({"symbol": symbol, "name": name, "exchange": exch_map.get(exch_code, exch_code or "OTHER")})
    return out


def _build_universe(want_etf: bool, limit: int | None):
    nasdaq_text = _fetch(NASDAQ_URL)
    other_text = _fetch(OTHER_URL)

    rows = _parse_nasdaq(nasdaq_text, want_etf) + _parse_other(other_text, want_etf)

    seen = set()
    deduped = []
    for r in rows:
        if r["symbol"] in seen:
            continue
        seen.add(r["symbol"])
        deduped.append(r)

    deduped.sort(key=lambda r: r["symbol"])

    if limit:
        deduped = deduped[:limit]

    return deduped


def get_universe(limit: int | None = None):
    """Returns a de-duplicated list of {symbol, name, exchange} dicts for
    plain common stock (ETFs excluded — see get_etf_universe)."""
    return _build_universe(want_etf=False, limit=limit)


def get_etf_universe(limit: int | None = None):
    """Same idea as get_universe(), but returns ETFs instead of common stock."""
    return _build_universe(want_etf=True, limit=limit)


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    universe = get_universe(limit=limit)
    print(f"{len(universe)} stock symbols")
    for row in universe[:20]:
        print(row)
    etfs = get_etf_universe(limit=limit)
    print(f"{len(etfs)} ETF symbols")
    for row in etfs[:20]:
        print(row)
