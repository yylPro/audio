from __future__ import annotations

import argparse
from pathlib import Path

from audio_quality.audio_quality_processor import export_report, init_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Export current audio quality database to an Excel report.")
    parser.add_argument("--database", type=Path, default=Path("state") / "assistant_v2.sqlite3")
    parser.add_argument("--report", type=Path, default=Path("audio_quality_output") / "offline_audio_quality.xlsx")
    args = parser.parse_args()
    connection = init_db(args.database)
    try:
        print(export_report(connection, args.report))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
