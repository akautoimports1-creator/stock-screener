"""
Combines all data/etf-shard-*.json files into the final data/etfs.json that
the website reads, plus data/etf_meta.json with refresh stats. Mirrors
merge.py's job for the stock side.
"""

import glob
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def main():
    shard_files = sorted(glob.glob(str(DATA_DIR / "etf-shard-*.json")))
    if not shard_files:
        print("No ETF shard files found — nothing to merge.")
        return

    merged = {}
    for path in shard_files:
        with open(path, "r") as f:
            rows = json.load(f)
        for row in rows:
            symbol = row.get("symbol")
            if symbol:
                merged[symbol] = row

    rows = sorted(merged.values(), key=lambda r: r.get("symbol", ""))

    with open(DATA_DIR / "etfs.json", "w") as f:
        json.dump(rows, f, separators=(",", ":"))

    meta = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_symbols": len(rows),
        "with_expense_ratio": sum(1 for r in rows if r.get("expenseRatio") is not None),
        "with_aum": sum(1 for r in rows if r.get("totalAssets")),
        "errors": sum(1 for r in rows if r.get("error")),
    }
    with open(DATA_DIR / "etf_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print(f"Merged {len(shard_files)} ETF shards -> {len(rows)} symbols.")
    print(meta)


if __name__ == "__main__":
    main()
