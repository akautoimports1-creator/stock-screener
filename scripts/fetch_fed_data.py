"""
Pulls a small "Fed Watch" snapshot for the ETF tab: the current Fed Funds
target rate range, whether the Fed's last move was a hike/cut/hold, the
next scheduled FOMC meeting date, and a rough "which way is the news
leaning" read for that next meeting. Writes data/fed.json.

Three different kinds of data here, on purpose kept separate so a bad one
never blanks out the others:

1. Current rate + last move: official Fed data via the FRED API
   (api.stlouisfed.org), series DFEDTARU/DFEDTARL (the daily upper/lower
   bound of the target range). This is the real, exact number — not a
   guess. Requires a free FRED API key (see FRED_API_KEY below); FRED
   gives these out instantly at https://fred.stlouisfed.org/docs/api/api_key.html
   with no cost and no approval wait.

2. Next meeting date: FOMC meeting dates are published by the Fed itself
   roughly a year or more in advance (federalreserve.gov/monetarypolicy/
   fomccalendars.htm) and don't change once announced, so they're
   hardcoded below rather than scraped — cheaper and more reliable than
   trying to parse a page that isn't built to be machine-read. Update the
   FOMC_MEETINGS list once a year when the Fed publishes the next one.

3. "Which way is the news leaning": there is no free, legitimate API for
   real market-implied odds (that's what CME's FedWatch tool sells access
   to). Instead this does the same honest thing fetch_data.py already does
   for individual stocks — scan recent headlines for hawkish/dovish
   language and report a lean, clearly labeled as a headline-count
   heuristic, not a probability. See HAWKISH_WORDS/DOVISH_WORDS below.
"""

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

FRED_API_KEY = os.environ.get("FRED_API_KEY")
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# The Fed publishes these itself, well ahead of time — see this file's
# docstring. Add next year's dates here once federalreserve.gov posts them
# (usually mid-to-late in the prior year).
FOMC_MEETINGS = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]

# Symbols whose Yahoo "news" feed reliably surfaces Fed/rate-policy
# headlines — a mix of rate-sensitive instruments, not just one ticker,
# so a quiet news day for one doesn't starve the whole scan.
NEWS_SYMBOLS = ["^TNX", "TLT", "^IRX"]

# Only headlines that actually mention the Fed/rate policy are scored —
# otherwise a completely unrelated "cuts jobs" or "raises guidance"
# headline about some company would get miscounted as monetary-policy
# news. This keyword gate runs before the hawkish/dovish word scan.
FED_KEYWORDS = {"fed", "fomc", "powell", "federal reserve", "interest rate", "rate cut", "rate hike"}

DOVISH_WORDS = {
    "cut", "cuts", "dovish", "ease", "eases", "eased", "easing", "pause",
    "paused", "pausing", "cooling", "cooled", "soften", "softening",
    "stimulus", "accommodative", "lower", "lowered", "lowering",
}
HAWKISH_WORDS = {
    "hike", "hikes", "hiked", "hawkish", "tighten", "tightens",
    "tightening", "restrictive", "overheating", "resilient", "sticky",
    "raise", "raises", "raised", "raising",
}


def fetch_fred_series(series_id: str):
    """Returns a list of (date, value) tuples, oldest first, skipping the
    "." placeholder FRED uses for non-trading-day gaps."""
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 400,  # comfortably more than a year of daily data
    }
    resp = requests.get(FRED_URL, params=params, timeout=20)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    out = []
    for o in obs:
        v = o.get("value")
        if v in (None, "."):
            continue
        try:
            out.append((o["date"], float(v)))
        except (TypeError, ValueError):
            continue
    out.reverse()  # oldest first
    return out


def compute_rate_snapshot():
    upper = fetch_fred_series("DFEDTARU")
    lower = fetch_fred_series("DFEDTARL")
    if not upper or not lower:
        return {"error": "FRED returned no usable observations"}

    current_upper = upper[-1][1]
    current_lower = lower[-1][1]

    # Walk backward from the most recent observation to find the last date
    # the *upper* bound actually changed value (the series repeats the
    # same number every day between meetings, so most day-to-day diffs are
    # zero — we want the last nonzero one).
    last_change = None
    for i in range(len(upper) - 1, 0, -1):
        prev_date, prev_val = upper[i - 1]
        cur_date, cur_val = upper[i]
        if cur_val != prev_val:
            bps = round((cur_val - prev_val) * 100)
            last_change = {
                "date": cur_date,
                "direction": "raised" if bps > 0 else "cut",
                "bps": abs(bps),
                "fromUpper": prev_val,
                "toUpper": cur_val,
            }
            break

    return {
        "currentRange": {"lower": current_lower, "upper": current_upper},
        "asOfDate": upper[-1][0],
        "lastChange": last_change,
        "lastMeeting": last_meeting_action(upper, datetime.now(timezone.utc).date()),
    }


def last_meeting_action(upper_series, today: date):
    """What actually happened at the most recent PAST scheduled FOMC meeting
    — distinct from last_change above, which is the last time the rate
    moved at all (could be several meetings ago if the Fed has been
    holding). This answers "what did they do last meeting", including the
    "held steady" case last_change alone can't express."""
    past_meetings = [d for d in FOMC_MEETINGS if datetime.strptime(d, "%Y-%m-%d").date() <= today]
    if not past_meetings:
        return None
    meeting_date = past_meetings[-1]

    before_val = None
    after_val = None
    for d, v in upper_series:
        if d < meeting_date:
            before_val = v
        if d >= meeting_date and after_val is None:
            after_val = v

    if before_val is None or after_val is None:
        return {"date": meeting_date, "action": "unknown", "bps": None}

    bps = round((after_val - before_val) * 100)
    action = "raised" if bps > 0 else "cut" if bps < 0 else "held"
    return {"date": meeting_date, "action": action, "bps": abs(bps)}


def next_meeting(today: date):
    for d in FOMC_MEETINGS:
        meeting_date = datetime.strptime(d, "%Y-%m-%d").date()
        if meeting_date >= today:
            return {"date": d}
    return None


def score_headline(title: str):
    low = title.lower()
    if not any(k in low for k in FED_KEYWORDS):
        return None  # not Fed/rate-policy news, skip entirely
    dovish_hits = sorted(w for w in DOVISH_WORDS if w in low)
    hawkish_hits = sorted(w for w in HAWKISH_WORDS if w in low)
    return {
        "title": title,
        "dovishHits": dovish_hits,
        "hawkishHits": hawkish_hits,
        "score": len(dovish_hits) - len(hawkish_hits),
    }


def fetch_news_lean():
    scored = []
    seen_titles = set()
    for sym in NEWS_SYMBOLS:
        try:
            items = yf.Ticker(sym).news or []
        except Exception:  # noqa: BLE001 - best effort, same philosophy as fetch_data.py
            items = []
        for item in items[:10]:
            content = item.get("content", item) if isinstance(item, dict) else {}
            title = content.get("title") or item.get("title")
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            result = score_headline(title)
            if result:
                scored.append(result)

    if not scored:
        return {
            "label": "No recent Fed-specific headlines found",
            "dovishCount": 0,
            "hawkishCount": 0,
            "headlineCount": 0,
            "headlines": [],
        }

    total = sum(h["score"] for h in scored)
    dovish_count = sum(1 for h in scored if h["score"] > 0)
    hawkish_count = sum(1 for h in scored if h["score"] < 0)
    if total > 0:
        label = "Leaning toward a cut"
    elif total < 0:
        label = "Leaning toward holding/hiking"
    else:
        label = "Mixed / no clear lean"

    return {
        "label": label,
        "dovishCount": dovish_count,
        "hawkishCount": hawkish_count,
        "headlineCount": len(scored),
        "headlines": scored[:8],
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = {"generated": datetime.now(timezone.utc).isoformat()}

    if not FRED_API_KEY:
        out["rate"] = {"error": "FRED_API_KEY not set — see this script's docstring for how to get a free key"}
    else:
        try:
            out["rate"] = compute_rate_snapshot()
        except Exception as e:  # noqa: BLE001 - best effort, never block the other data
            out["rate"] = {"error": str(e)}

    out["nextMeeting"] = next_meeting(datetime.now(timezone.utc).date())

    try:
        out["newsLean"] = fetch_news_lean()
    except Exception as e:  # noqa: BLE001
        out["newsLean"] = {"error": str(e)}

    with open(DATA_DIR / "fed.json", "w") as f:
        json.dump(out, f, indent=2)

    print("Wrote data/fed.json:")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    sys.exit(main())
