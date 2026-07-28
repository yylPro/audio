from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Print recent send audit batches.")
    parser.add_argument("--database", type=Path, default=Path("state") / "assistant_v2.sqlite3")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not args.database.exists():
        print(f"database not found: {args.database}")
        return 1
    connection = sqlite3.connect(args.database)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "send_batches" not in tables:
            print("send_batches table not found")
            return 0
        for row in connection.execute("SELECT * FROM send_batches ORDER BY created_at DESC LIMIT ?", (args.limit,)):
            print(row)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
