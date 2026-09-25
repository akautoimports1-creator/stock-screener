"""
Combines all data/shard-*.json files into the final data/stocks.json that the
website actually reads, plus data/meta.json with refresh stats. Run after all
shard jobs in the matrix have finished.
"""

import glob
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def main():
    shard_files = sorted(glob.glob(str(DATA_DIR / "shard-*.json")))
    if not shard_files:
        print("No shard files found — nothing to merge.")
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

    with open(DATA_DIR / "stocks.json", "w") as f:
        json.dump(rows, f, separators=(",", ":"))

    meta = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_symbols": len(rows),
        "with_pe": sum(1 for r in rows if r.get("trailingPE")),
        "with_forward_pe": sum(1 for r in rows if r.get("forwardPE")),
        "with_peg": sum(1 for r in rows if r.get("pegRatio")),
        "with_news": sum(1 for r in rows if r.get("newsCount")),
        "errors": sum(1 for r in rows if r.get("error")),
    }
    with open(DATA_DIR / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print(f"Merged {len(shard_files)} shards -> {len(rows)} symbols.")
    print(meta)


if __name__ == "__main__":
    main()
