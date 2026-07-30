from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .audio_quality_processor import (
    TranscriptSegment,
    add_event,
    clear_audio_tasks,
    deterministic_quality_check,
    export_daily_report,
    extract_employee_name,
    extract_recording_date,
    filter_customer_perspective_evidence,
    find_process_action_evidence,
    init_db,
    join_transcript,
    load_json,
    load_segments,
    normalize_text,
    save_quality_result,
    resolve_retention_result,
)
from .semantic_signals import extract_business_signals, summarize_business_signals
from .deepseek_quality import DeepSeekQualityError, DeepSeekQualityScorer, assessment_to_quality_result, reconcile_retention_semantics, reinforce_ask_evidence, reinforce_location_lookup_evidence, reinforce_observed_record_evidence, reinforce_verification_lookup_evidence
from .fun_asr_client import load_env_file


def requeue_tasks(config_path: Path, task_ids: list[str], clear_results: bool = False) -> int:
    config = load_json(config_path)
    connection = init_db(Path(config["database_path"]))
    try:
        missing: list[str] = []
        for task_id in task_ids:
            row = connection.execute("SELECT status FROM audio_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                missing.append(task_id)
                continue
            if clear_results:
                connection.execute("DELETE FROM audio_segments WHERE task_id=?", (task_id,))
                connection.execute("DELETE FROM organized_calls WHERE task_id=?", (task_id,))
                connection.execute("DELETE FROM quality_results WHERE task_id=?", (task_id,))
            connection.execute(
                """UPDATE audio_tasks
                   SET status='received', error_stage=NULL, error_message=NULL,
                       claimed_at=NULL, worker_id=NULL
                   WHERE task_id=?""",
                (task_id,),
            )
            print(f"requeued: {task_id}, previous_status={row[0]}")
        connection.commit()
        for task_id in missing:
            print(f"task not found: {task_id}")
        return 2 if missing else 0
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


def show_duplicates(config_path: Path) -> int:
    config = load_json(config_path)
    connection = init_db(Path(config["database_path"]))
    try:
        rows = connection.execute(
            """SELECT group_id,attachment_hash,count(*),group_concat(task_id),group_concat(original_filename,' | ')
               FROM audio_tasks
               GROUP BY group_id,attachment_hash
               HAVING count(*) > 1
               ORDER BY count(*) DESC, min(created_at)"""
        ).fetchall()
        if not rows:
            print("no duplicate audio tasks found")
            return 0
        for row in rows:
            print(row)
        return 1
    finally:
        connection.close()


def clear_tasks(config_path: Path) -> int:
    config = load_json(config_path)
    connection = init_db(Path(config["database_path"]))
    try:
        counts = clear_audio_tasks(connection)
        print("cleared audio task tables:")
        for name, count in counts.items():
            print(f"  {name}: {count}")
        return 0
    finally:
        connection.close()


def _stored_segments(value: str | None, speaker: str) -> list[TranscriptSegment]:
    try:
        items = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    result: list[TranscriptSegment] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            result.append(TranscriptSegment(
                float(item["start_seconds"]),
                float(item["end_seconds"]),
                speaker,
                str(item["text"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return result


def rescore_tasks(config_path: Path, task_ids: list[str], all_tasks: bool = False) -> int:
    config = load_json(config_path)
    rules = load_json(Path(config["quality_rules_path"]))
    connection = init_db(Path(config["database_path"]))
    try:
        if all_tasks:
            selected = connection.execute(
                "SELECT task_id FROM audio_tasks WHERE status IN ('completed','needs_review') ORDER BY rowid"
            ).fetchall()
            task_ids = [str(row[0]) for row in selected]
        if not task_ids:
            print("没有需要重新评分的任务")
            return 0

        report_days: set[str] = set()
        missing: list[str] = []
        for task_id in task_ids:
            task = connection.execute(
                "SELECT original_filename FROM audio_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            raw_segments = load_segments(connection, task_id)
            if not task or not raw_segments:
                missing.append(task_id)
                continue

            organized = connection.execute(
                "SELECT role_confidence,retention_result,uncertain_parts,service_segments,call_summary FROM organized_calls WHERE task_id=?",
                (task_id,),
            ).fetchone()
            scoring_segments = raw_segments
            employee_speaker = None
            speaker_confidence = None
            retention_result = None
            review_reasons: list[str] = []
            if organized:
                service_segments = _stored_segments(organized[3], "客服")
                if service_segments:
                    scoring_segments = service_segments
                    employee_speaker = "客服"
                    speaker_confidence = float(organized[0])
                retention_result = resolve_retention_result(str(organized[1] or ""), str(organized[4] or ""))
                if organized[2]:
                    review_reasons.append(f"角色整理存在不确定片段：{organized[2]}")

            signals = extract_business_signals(raw_segments, rules.get("semantic_signals", {}))
            result = deterministic_quality_check(
                scoring_segments,
                rules,
                employee_speaker,
                speaker_confidence,
                review_reasons,
                raw_segments,
                [signal.to_dict() for signal in signals],
                retention_result,
            )
            signal_summary = summarize_business_signals(signals)
            if signal_summary:
                result = replace(result, summary=f"业务信号：{signal_summary}；{result.summary}")
            save_quality_result(connection, task_id, result)
            add_event(connection, task_id, "rescored", {"score": result.score})
            connection.commit()
            day = extract_recording_date(str(task[0] or ""))
            if day:
                report_days.add(day)
            print(f"已重新评分：{task_id}，得分={result.score}，状态={'需复核' if result.needs_human_review else '完成'}")

        output_root = Path(config["output_root"])
        for day in sorted(report_days):
            export_daily_report(connection, output_root, datetime.strptime(day, "%Y-%m-%d"), rules)
            print(f"已重新生成报表：{day}")
        for task_id in missing:
            print(f"任务不存在或缺少转写：{task_id}")
        return 2 if missing else 0
    finally:
        connection.close()


def _save_deepseek_assessment(connection, task_id: str, result, assessment) -> None:
    connection.execute(
        """INSERT INTO deepseek_quality_assessments
           (task_id,score,qualified,summary,analysis,issues,actions,uncertain_parts,raw_response,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(task_id) DO UPDATE SET score=excluded.score,qualified=excluded.qualified,
           summary=excluded.summary,analysis=excluded.analysis,issues=excluded.issues,
           actions=excluded.actions,uncertain_parts=excluded.uncertain_parts,
           raw_response=excluded.raw_response,created_at=excluded.created_at""",
        (task_id, result.score, int(result.qualified), result.summary, result.analysis,
         json.dumps([issue.__dict__ for issue in result.issues], ensure_ascii=False),
         json.dumps(assessment.actions, ensure_ascii=False), assessment.uncertain_parts,
         assessment.raw_response, datetime.now().isoformat(timespec="seconds")),
    )


def export_deepseek_comparison(connection, output_root: Path, days: set[str], rules: dict[str, Any] | None = None) -> list[Path]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    paths: list[Path] = []
    for day in sorted(days):
        rows = connection.execute(
            """SELECT t.task_id,t.original_filename,t.employee_name,t.sender_name,t.accepted_number,
                       d.score,d.summary,d.analysis,d.actions,d.uncertain_parts,
                       o.full_dialogue,o.service_segments,o.customer_segments,o.role_confidence,
                       o.retention_result,o.call_summary,o.uncertain_parts
               FROM audio_tasks t
               JOIN deepseek_quality_assessments d ON d.task_id=t.task_id
               LEFT JOIN organized_calls o ON o.task_id=t.task_id
               ORDER BY t.rowid"""
        ).fetchall()
        filtered = [row for row in rows if extract_recording_date(str(row[1] or "")) == day]
        if not filtered:
            continue
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "方案对比"
        headers = [
            "序号", "日期", "员工姓名", "客户号码", "录音文件名", "规则库评分", "语义评分", "分差",
            "Python问证据", "Python查证据", "Python比证据", "Python算证据",
            "DeepSeek问", "DeepSeek查", "DeepSeek比", "DeepSeek算", "DeepSeek挽留动作", "DeepSeek挽留成功",
            "语义评分摘要", "语义评分问题分析", "需人工复核", "整理后的录音文本", "原始转录文本"
        ]
        sheet.append(headers)
        for index, row in enumerate(filtered, 1):
            task_id, filename, employee_name, sender_name, accepted_number, ai_score, summary, analysis, actions_json, uncertain, organized_text, service_segments, customer_segments, role_confidence, retention_result, call_summary, organized_uncertain = row
            try:
                actions = json.loads(actions_json)
            except json.JSONDecodeError:
                actions = {}
            raw_segments = load_segments(connection, task_id)
            service_segment_list = _stored_segments(service_segments, "客服")
            customer_segment_list = _stored_segments(customer_segments, "客户")
            scoring_segments = service_segment_list or raw_segments
            review_reasons = [
                f"角色整理存在不确定片段：{organized_uncertain}"
            ] if organized_uncertain else []
            signals = extract_business_signals(raw_segments, (rules or {}).get("semantic_signals", {}))
            python_result = deterministic_quality_check(
                scoring_segments,
                rules or {},
                "客服" if service_segment_list else None,
                float(role_confidence) if service_segment_list and role_confidence is not None else None,
                review_reasons,
                raw_segments,
                [signal.to_dict() for signal in signals],
                resolve_retention_result(retention_result, call_summary),
            )
            python_score = python_result.score
            python_evidence = find_process_action_evidence(
                scoring_segments,
                "客服" if service_segment_list else None,
                raw_segments,
                rules or {},
            )
            python_statuses = [
                filter_customer_perspective_evidence(
                    python_evidence.get(rule_id, "未找到明确证据"),
                    customer_segment_list,
                )
                for rule_id in ("missing_ask", "missing_check", "missing_compare", "missing_calculate")
            ]
            statuses = []
            for rule_id in ("missing_ask", "missing_check", "missing_compare", "missing_calculate", "missing_retention_action", "missing_retention_success"):
                action = actions.get(rule_id, {}) if isinstance(actions, dict) else {}
                evidence = "；".join(action.get("evidence", [])) if isinstance(action, dict) else ""
                statuses.append(filter_customer_perspective_evidence(
                    ("已体现：" if action.get("present") else "未体现：")
                    + (evidence or normalize_text(action.get("reason")) or "缺乏证据"),
                    customer_segment_list,
                ))
            raw_transcript = join_transcript(raw_segments)
            sheet.append([index, day, extract_employee_name(filename or "") or employee_name or sender_name or "", accepted_number or "", filename or "", python_score if python_score is not None else "", ai_score, "" if python_score is None else ai_score - python_score, *python_statuses, *statuses, summary, analysis, uncertain or "", organized_text or "", raw_transcript])
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="4472C4")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column in sheet.columns:
            width = 18 if column[0].column <= 8 else (80 if column[0].column >= 18 else 42)
            sheet.column_dimensions[column[0].column_letter].width = width
            for cell in column:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        output_root.mkdir(parents=True, exist_ok=True)
        path = output_root / f"{day}_降挽质检方案对比-语义评分.xlsx"
        temporary = path.with_name(path.stem + ".tmp" + path.suffix)
        workbook.save(temporary)
        try:
            temporary.replace(path)
            paths.append(path)
        except PermissionError:
            fallback = path.with_name(f"{path.stem}-{datetime.now().strftime('%H%M%S')}{path.suffix}")
            temporary.replace(fallback)
            paths.append(fallback)
    return paths


def deepseek_rescore_tasks(config_path: Path, task_ids: list[str], all_tasks: bool = False, apply_primary: bool = False) -> int:
    config = load_json(config_path)
    load_env_file(Path(config["env_file"]) if config.get("env_file") else None)
    rules = load_json(Path(config["quality_rules_path"]))
    scorer = DeepSeekQualityScorer(config.get("deepseek_scoring") or config.get("organizer") or {})
    quality_config = config.get("quality", {}) if isinstance(config.get("quality", {}), dict) else {}
    mode = str(quality_config.get("deepseek_mode", "")).strip().lower()
    apply_primary = apply_primary or mode in {"deepseek_score_primary", "deepseek_primary", "deepseek_primary_with_python_compare"}
    connection = init_db(Path(config["database_path"]))
    try:
        if all_tasks:
            task_ids = [str(row[0]) for row in connection.execute("SELECT task_id FROM audio_tasks WHERE status IN ('completed','needs_review') ORDER BY rowid").fetchall()]
        if not task_ids:
            print("没有需要进行 DeepSeek 评分的任务")
            return 0
        days: set[str] = set()
        failed: list[str] = []
        for task_id in task_ids:
            row = connection.execute("SELECT original_filename FROM audio_tasks WHERE task_id=?", (task_id,)).fetchone()
            segments = load_segments(connection, task_id)
            if not row or not segments:
                failed.append(task_id)
                continue
            try:
                assessment = reinforce_ask_evidence(scorer.assess(segments), segments)
                assessment = reinforce_location_lookup_evidence(assessment, segments)
                assessment = reinforce_observed_record_evidence(assessment, segments)
                assessment = reinforce_verification_lookup_evidence(assessment, segments)
                assessment = reconcile_retention_semantics(assessment, segments)
                result = assessment_to_quality_result(assessment, segments, rules)
                _save_deepseek_assessment(connection, task_id, result, assessment)
                add_event(connection, task_id, "deepseek_rescored", {"score": result.score})
                if apply_primary:
                    connection.commit()
                    save_quality_result(connection, task_id, result)
                else:
                    connection.commit()
                day = extract_recording_date(str(row[0] or ""))
                if day:
                    days.add(day)
                print(f"已完成语义评分：{task_id}，得分 {result.score}" + ("，已回填正式质检结果" if apply_primary else ""))
            except DeepSeekQualityError as exc:
                connection.rollback()
                failed.append(task_id)
                print(f"语义评分失败：{task_id}，{exc}")
        for path in export_deepseek_comparison(connection, Path(config["output_root"]), days, rules):
            print(f"已生成方案对比表：{path}")
        if apply_primary:
            for day in sorted(days):
                export_daily_report(connection, Path(config["output_root"]), datetime.strptime(day, "%Y-%m-%d"), rules)
                print(f"已重新生成正式质检表：{day}")
        return 2 if failed else 0
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Audio quality task admin helper.")
    parser.add_argument("--config", type=Path, default=Path("audio_quality_config.json"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    requeue = subparsers.add_parser("requeue", help="Reset tasks back to received.")
    requeue.add_argument("task_ids", nargs="+")
    requeue.add_argument("--clear-results", action="store_true", help="Delete old transcript, organized call, and quality result before rerun.")

    latest = subparsers.add_parser("latest", help="Show latest tasks.")
    latest.add_argument("--limit", type=int, default=5)

    subparsers.add_parser("duplicates", help="Show duplicate audio tasks by group and attachment hash.")
    subparsers.add_parser("clear", help="Delete all audio task records from the database.")
    rescore = subparsers.add_parser("rescore", help="Recalculate saved tasks with current quality rules without calling ASR or AI.")
    rescore.add_argument("task_ids", nargs="*")
    rescore.add_argument("--all", action="store_true", dest="all_tasks")
    deepseek_rescore = subparsers.add_parser("deepseek-rescore", help="Use DeepSeek semantic scoring and export a comparison workbook without replacing Python results.")
    deepseek_rescore.add_argument("task_ids", nargs="*")
    deepseek_rescore.add_argument("--all", action="store_true", dest="all_tasks")
    deepseek_rescore.add_argument("--apply-primary", action="store_true", help="Also write DeepSeek scores into the official quality results/report.")

    args = parser.parse_args()
    if args.command == "requeue":
        return requeue_tasks(args.config, args.task_ids, args.clear_results)
    if args.command == "latest":
        return show_latest(args.config, max(1, args.limit))
    if args.command == "duplicates":
        return show_duplicates(args.config)
    if args.command == "clear":
        return clear_tasks(args.config)
    if args.command == "rescore":
        if not args.all_tasks and not args.task_ids:
            parser.error("rescore requires task IDs or --all")
        return rescore_tasks(args.config, args.task_ids, args.all_tasks)
    if args.command == "deepseek-rescore":
        if not args.all_tasks and not args.task_ids:
            parser.error("deepseek-rescore requires task IDs or --all")
        return deepseek_rescore_tasks(args.config, args.task_ids, args.all_tasks, args.apply_primary)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
