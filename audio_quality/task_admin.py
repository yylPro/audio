from __future__ import annotations

import argparse
from pathlib import Path

from .audio_quality_processor import init_db, load_json


def requeue_task(config_path: Path, task_id: str) -> int:
    config = load_json(config_path)
    connection = init_db(Path(config["database_path"]))
    try:
        row = connection.execute("SELECT status FROM audio_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            print(f"任务不存在：{task_id}")
            return 2
        connection.execute(
            """UPDATE audio_tasks
               SET status='received', error_stage=NULL, error_message=NULL,
                   claimed_at=NULL, worker_id=NULL
               WHERE task_id=?""",
            (task_id,),
        )
        connection.commit()
        print(f"已重新入队：{task_id}，原状态：{row[0]}")
        return 0
    finally:
        connection.close()


def show_latest(config_path: Path, limit: int) -> int:
    config = load_json(config_path)
    connection = init_db(Path(config["database_path"]))
    try:
        rows = connection.execute(
            """SELECT task_id,status,attempt_count,original_filename,error_stage,
                      substr(coalesce(error_message,''),1,160)
               FROM audio_tasks
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        for row in rows:
            print(row)
        return 0
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Audio quality task admin helper.")
    parser.add_argument("--config", type=Path, default=Path("audio_quality_config.json"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    requeue = subparsers.add_parser("requeue", help="Reset one task back to received.")
    requeue.add_argument("task_id")

    latest = subparsers.add_parser("latest", help="Show latest tasks.")
    latest.add_argument("--limit", type=int, default=5)

    args = parser.parse_args()
    if args.command == "requeue":
        return requeue_task(args.config, args.task_id)
    if args.command == "latest":
        return show_latest(args.config, max(1, args.limit))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
