# Ratio Exchange

A stock screener covering the whole US market (NASDAQ + NYSE + NYSE American),
scored on P/E, forward P/E, and PEG. Refreshes nightly, runs entirely on free
infrastructure: GitHub Actions does the data pull, GitHub stores the result,
Vercel serves the site.

## How it works

- `scripts/universe.py` pulls the full list of NASDAQ + NYSE/AMEX symbols
  from Nasdaq Trader's public symbol directory (no API key needed).
- `scripts/fetch_data.py` looks up P/E, forward P/E, PEG, sector, market cap
  and a few other fields for each symbol via Yahoo Finance (through the
  `yfinance` library).
- `.github/workflows/refresh-data.yml` runs that nightly, split into 10
  parallel shards so no single job has to process thousands of tickers
  sequentially, then merges the results into `data/stocks.json` and commits
  it back to the repo.
- `index.html` is the site itself — a single static page with no build step.
  It fetches `data/stocks.json` and `data/meta.json` and does all filtering,
  sorting, and the undervalued/fair/overvalued scoring in the browser.

**Important limitation:** Yahoo Finance has no official public API. This
project uses an unofficial library to read the same data your browser would
see on finance.yahoo.com. That's what makes full-market coverage possible
at zero cost, but it means:
- Yahoo can rate-limit or block requests if the job runs too aggressively
  (the shard split + delays are there to reduce that risk, not eliminate it).
- Their page structure can change and break the library — if data stops
  refreshing, check whether `yfinance` needs an update
  (`pip install -U yfinance`).
- Smaller, newer, or foreign-listed companies often have missing forward
  P/E or PEG data — that's Yahoo's coverage gap, not a bug here.

**News sentiment is a heuristic, not real analysis.** For each symbol the
job pulls Yahoo's last few headlines and scores them by counting
positive/negative words (`POSITIVE_WORDS`/`NEGATIVE_WORDS` in
`fetch_data.py`) — no NLP, no paid sentiment API (none are free at
full-market scale). Treat the Positive/Neutral/Negative tag as "which way
recent headlines lean," not a verdict. This also roughly doubles the
nightly job's API calls (one extra request per symbol for news) versus the
valuation-only version, so it's slower and has more rate-limit exposure —
if the job starts timing out or Yahoo starts blocking it, dropping the
`fetch_news()` call in `fetch_data.py` is the first thing to try.

If you outgrow this, swap `fetch_data.py`'s data source for a paid API
(Financial Modeling Prep, Polygon.io, EOD Historical Data all have bulk
fundamentals) — the rest of the site (scoring, filtering, UI) doesn't change.

## Deploying this yourself

### 1. Put this code on GitHub

```
cd stock-screener
git init
git add .
git commit -m "Initial commit"
```

Create a new empty repository on github.com (no README/gitignore — you
already have them), then:

```
git remote add origin https://github.com/<your-username>/<repo-name>.git
git branch -M main
git push -u origin main
```

### 2. Turn on the scheduled data job

GitHub Actions is enabled automatically for a public repo. Go to the
**Actions** tab of your repo — you'll see "Refresh stock data" listed. It
runs nightly at 06:00 UTC on its own, but for the first run, click into it
and use **Run workflow** to trigger it manually so you don't have to wait
until tomorrow. It takes roughly 30-90 minutes to complete a full pass,
depending on how Yahoo responds that day.

If your repo is **private**, Actions still works but is capped at 2,000 free
minutes/month — a full nightly run across 10 shards uses some of that
budget. A public repo has no such cap.

### 3. Deploy the site on Vercel

1. Go to vercel.com, sign up with your GitHub account (free).
2. Click **Add New → Project**, pick this repo.
3. Vercel will detect it as a static site automatically — no build command,
   no output directory needed. Click **Deploy**.
4. You'll get a live URL (`your-project.vercel.app`) in under a minute.

Every time the nightly GitHub Action commits new data, Vercel automatically
redeploys with it — nothing else to do.

### 4. (Optional) Custom domain

In the Vercel project settings → Domains, add your own domain and follow
the DNS instructions it gives you.

## Adjusting the screen

- **Change refresh time:** edit the `cron` line in
  `.github/workflows/refresh-data.yml` (times are UTC).
- **Add a filter criterion:** the data already includes `dividendYield` and
  `targetMeanPrice` per symbol (see `scripts/fetch_data.py`'s `FIELDS` list)
  that aren't wired into the UI yet — add an input in `index.html`'s filter
  panel and a matching check in `applyFilters()`. (`recommendationKey` and
  the 52-week range are already shown, in the table and the detail card.)
- **Change the undervalued thresholds:** edit the `scoreOne` calls inside
  `computeVerdict()` in `index.html`.
- **Narrow the universe:** pass a limit or add extra filtering logic in
  `scripts/universe.py`'s `get_universe()` — e.g. drop OTC-adjacent tickers,
  restrict to a market-cap floor, or hardcode the S&P 500 list instead of
  the full exchange listing.

## ETF section

There's a second tab on the site ("ETFs") that's a separate pipeline from
the stock screener, since ETFs don't have a P/E to score:

- `scripts/universe.py`'s `get_etf_universe()` pulls ETF tickers the same
  way `get_universe()` pulls stocks, just keeping rows the symbol
  directories flag as `ETF: Y` instead of dropping them.
- `scripts/fetch_etf_data.py` looks up expense ratio, AUM, distribution
  yield, and 1-year price change per fund (sharded the same way as the
  stock job, just fewer shards since there are far fewer ETFs than stocks).
- `scripts/merge_etfs.py` combines the shards into `data/etfs.json` and
  `data/etf_meta.json`, same idea as `merge.py`.
- The "Best value / Fair / Costly" verdict on that tab scores each fund on
  expense ratio (cheap is good) and 1-year return (higher is good) —
  see `computeEtfVerdict()` in `index.html`. It's a cost/performance score,
  not investment advice.
- Both refresh jobs (`fetch`/`merge` for stocks, `fetch-etfs`/`merge-etfs`
  for ETFs) run nightly off the same workflow and push to the repo
  independently, so one can succeed even if the other fails.
