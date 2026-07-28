from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Protocol

from .audio_quality_processor import (
    AudioQualityError,
    TranscriptSegment,
    add_event,
    claim_next_task,
    deterministic_quality_check,
    export_daily_report,
    format_completed_reply,
    init_db,
    load_json,
    mark_task_failed,
    save_organized_call,
    save_quality_result,
    save_transcript,
)
from .audio_converter import AudioConversionError, convert_audio_if_needed
from .deepseek_organizer import DeepSeekDialogueOrganizer, DeepSeekOrganizerError
from .fun_asr_client import AliyunFunASRClient, FunASRError, load_env_file
from .local_whisper_client import LocalFasterWhisperClient, LocalWhisperError
from .notifier import schedule_completion_notification
from .semantic_signals import extract_business_signals, summarize_business_signals


class ASRProvider(Protocol):
    def transcribe(self, audio_path: Path) -> list[TranscriptSegment]: ...


@contextmanager
def single_worker_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise AudioQualityError("已有听音质检 Worker 正在运行") from exc
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()


def _load_quality_rules(config: dict[str, Any]) -> dict[str, Any]:
    rules_path = config.get("quality_rules_path")
    if rules_path:
        return load_json(Path(rules_path))
    return config.get("quality", {}) if isinstance(config.get("quality"), dict) else {}


def _build_provider(asr_config: dict[str, Any]) -> ASRProvider:
    provider_name = str(asr_config.get("provider", "aliyun_fun_asr"))
    if provider_name == "local_faster_whisper":
        return LocalFasterWhisperClient(asr_config)
    if provider_name == "aliyun_fun_asr":
        return AliyunFunASRClient(asr_config)
    raise AudioQualityError(f"未知 ASR provider：{provider_name}")


def run_one(connection: sqlite3.Connection, provider: ASRProvider, worker_id: str,
            asr_config: dict[str, Any], quality_rules: dict[str, Any] | None = None,
            output_root: Path | None = None, organizer: DeepSeekDialogueOrganizer | None = None,
            completion_notify: dict[str, Any] | None = None,
            audio_conversion: dict[str, Any] | None = None,
            stale_after_seconds: int = 600) -> str | None:
    task_id = claim_next_task(connection, worker_id, stale_after_seconds=stale_after_seconds)
    if task_id is None:
        return None
    row = connection.execute("SELECT archived_path FROM audio_tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row or not row[0]:
        mark_task_failed(connection, task_id, "archive", "任务缺少归档音频路径")
        return task_id
    archived_path = Path(row[0])
    try:
        conversion = convert_audio_if_needed(archived_path, task_id, audio_conversion)
        audio_path = conversion.audio_path
        if conversion.converted:
            add_event(connection, task_id, "audio_converted", {
                "source_path": str(conversion.source_path),
                "converted_path": str(conversion.audio_path),
                "reason": conversion.reason,
            })
            connection.commit()
    except AudioConversionError as exc:
        if audio_conversion and audio_conversion.get("fallback_to_original", False):
            audio_path = archived_path
            add_event(connection, task_id, "audio_conversion_failed_fallback", {"error": str(exc)[:1000], "source_path": str(archived_path)})
            connection.commit()
        else:
            mark_task_failed(connection, task_id, "convert", exc)
            return task_id

    attempts = max(1, int(asr_config.get("max_attempts", 3)))
    delays = [max(0, int(item)) for item in asr_config.get("retry_delays_seconds", [10, 30, 90])]
    for attempt in range(1, attempts + 1):
        try:
            segments = provider.transcribe(audio_path)
            save_transcript(connection, task_id, segments)
            business_signals = extract_business_signals(segments, (quality_rules or {}).get("semantic_signals", {}))
            business_signal_summary = summarize_business_signals(business_signals)
            if business_signals:
                add_event(connection, task_id, "business_signals_extracted", {
                    "signals": [signal.to_dict() for signal in business_signals],
                    "summary": business_signal_summary,
                })
                connection.commit()
            scoring_segments = segments
            employee_speaker_override = None
            speaker_confidence_override = None
            review_reasons: list[str] = []
            if organizer is not None:
                try:
                    organized = organizer.organize(segments)
                    if organized is not None:
                        save_organized_call(connection, task_id, organized)
                        if organized.service_segments:
                            scoring_segments = organized.service_segments
                            employee_speaker_override = "客服"
                            speaker_confidence_override = organized.role_confidence
                        if organized.uncertain_parts:
                            review_reasons.append(f"DeepSeek整理存在不确定片段：{organized.uncertain_parts}")
                        if organized.role_confidence < float((quality_rules or {}).get("speaker_review_threshold", 0.75)):
                            review_reasons.append("DeepSeek角色整理置信度偏低")
                except DeepSeekOrganizerError as exc:
                    add_event(connection, task_id, "organizer_failed", {"error": str(exc)[:1000]})
                    connection.commit()
            result = deterministic_quality_check(scoring_segments, quality_rules or {}, employee_speaker_override, speaker_confidence_override, review_reasons, segments)
            if business_signal_summary:
                result = replace(result, summary=f"业务信号：{business_signal_summary}；{result.summary}")
            save_quality_result(connection, task_id, result)
            report_path = export_daily_report(connection, output_root) if output_root is not None else None
            connection.execute("UPDATE audio_tasks SET asr_provider=? WHERE task_id=?", (str(asr_config.get("provider", "aliyun_fun_asr")), task_id))
            reply = format_completed_reply(task_id, result.needs_human_review)
            notify_job_id = None
            if completion_notify and completion_notify.get("enabled", False):
                try:
                    notify_job_id = schedule_completion_notification(completion_notify, task_id, reply)
                    add_event(connection, task_id, "completion_notify_scheduled", {"job_id": notify_job_id})
                except Exception as exc:
                    add_event(connection, task_id, "completion_notify_failed", {"error": str(exc)[:1000]})
            add_event(connection, task_id, "completion_reply_ready", {"reply": reply, "report_path": str(report_path) if report_path else "", "notify_job_id": notify_job_id or ""})
            connection.commit()
            return task_id
        except FunASRError as exc:
            if not exc.retryable or attempt >= attempts:
                mark_task_failed(connection, task_id, "asr", exc)
                return task_id
            time.sleep(delays[min(attempt - 1, len(delays) - 1)] if delays else 0)
        except Exception as exc:
            mark_task_failed(connection, task_id, "worker", exc)
            return task_id
    return task_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Single-concurrency audio-quality worker.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=3)
    args = parser.parse_args()
    config = load_json(args.config)
    load_env_file(Path(config["env_file"]) if config.get("env_file") else None)
    asr_config = config.get("asr", {})
    worker_config = config.get("worker", {}) if isinstance(config.get("worker", {}), dict) else {}
    organizer_config = config.get("organizer", {}) if isinstance(config.get("organizer", {}), dict) else {}
    try:
        provider = _build_provider(asr_config)
    except (FunASRError, LocalWhisperError, AudioQualityError) as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 3
    lock_path = Path(config["database_path"]).with_name("audio_quality_worker.lock")
    quality_rules = _load_quality_rules(config)
    output_root = Path(config["output_root"]) if config.get("output_root") else None
    organizer = DeepSeekDialogueOrganizer(organizer_config) if organizer_config.get("enabled") else None
    completion_notify = config.get("completion_notify", {}) if isinstance(config.get("completion_notify", {}), dict) else {}
    with single_worker_lock(lock_path):
        connection = init_db(Path(config["database_path"]))
        try:
            worker_id = f"audio-worker-{os.getpid()}"
            while True:
                task_id = run_one(connection, provider, worker_id, asr_config, quality_rules, output_root, organizer, completion_notify, config.get("audio_conversion", {}), int(worker_config.get("stale_after_seconds", 600)))
                if args.once:
                    return 0
                if task_id is None:
                    time.sleep(max(0.5, args.poll_seconds))
        finally:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
