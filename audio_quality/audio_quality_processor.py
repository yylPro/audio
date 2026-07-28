from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .text_compat import compact_text, repair_multiline_text, repair_text


class AudioQualityError(ValueError):
    """Raised when an audio task or quality result cannot be accepted."""


@dataclass(frozen=True)
class IncomingAudioMessage:
    message_id: str
    group_id: str
    sender_user_id: str
    sender_name: str
    text: str
    attachment_id: str
    filename: str
    file_size: int | None = None
    duration_seconds: float | None = None
    attachment_hash: str | None = None


@dataclass(frozen=True)
class AudioSubmission:
    employee_name: str
    order_id: str | None
    accepted_number: str | None = None


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    speaker: str
    text: str


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    start_seconds: float
    quote: str
    description: str
    deduction: int


@dataclass(frozen=True)
class QualityIssue:
    rule_id: str
    start_seconds: float
    quote: str
    reason: str
    suggestion: str
    confidence: float


@dataclass(frozen=True)
class QualityResult:
    employee_speaker: str
    speaker_confidence: float
    dimension_scores: dict[str, int]
    score: int
    qualified: bool
    summary: str
    analysis: str
    optimization: str
    issues: list[QualityIssue]
    needs_human_review: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: Any) -> str:
    return compact_text(value)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AudioQualityError(f"JSON 根节点必须是对象：{path}")
    return value


def extract_accepted_number(*values: str) -> str | None:
    joined = " ".join(normalize_text(value) for value in values if normalize_text(value))
    for pattern in (r"(?:受理号码|受理号|号码|手机号|联系电话)\s*[:：]?\s*(1\d{10})", r"(?<!\d)(1\d{10})(?!\d)"):
        match = re.search(pattern, joined)
        if match:
            return match.group(1)
    return None


def parse_submission(text: str, rules: dict[str, Any]) -> AudioSubmission:
    text = repair_multiline_text(text)
    trigger = normalize_text(rules.get("trigger", "#听音检测"))
    aliases = {normalize_text(item) for item in rules.get("trigger_aliases", [trigger, "#听音质检", "#录音质检"]) if normalize_text(item)}
    if not any(item in text for item in aliases):
        raise AudioQualityError(f"消息缺少触发词：{trigger}")
    known = {item for item in ("#沟通记录", "#听音检测", "#听音质检", "#录音质检", "#工单催办") if item in text}
    if known - aliases:
        raise AudioQualityError("一条消息只能执行一个功能，请只保留一个触发词")
    name_match = re.search(r"(?:员工|员工姓名)\s*[:：]\s*([^\n\r]+)", text)
    employee_name = normalize_text(name_match.group(1)) if name_match else ""
    order_match = re.search(r"(?:工单号|工单流水号|工单编号)\s*[:：]\s*([^\s，,。；;]+)", text)
    order_id = normalize_text(order_match.group(1)) if order_match else None
    if order_id and not re.fullmatch(str(rules.get("order_id_pattern", r"[A-Za-z0-9_-]{4,64}")), order_id):
        raise AudioQualityError(f"工单号格式不合法：{order_id}")
    return AudioSubmission(employee_name, order_id, extract_accepted_number(text))


def validate_attachment(message: IncomingAudioMessage, rules: dict[str, Any]) -> None:
    if not normalize_text(message.attachment_id):
        raise AudioQualityError("消息缺少音频附件")
    if not normalize_text(message.filename) or Path(message.filename).name != message.filename:
        raise AudioQualityError("音频文件名为空或包含路径")
    suffix = Path(repair_text(message.filename)).suffix.casefold()
    supported = {str(item).casefold() for item in rules.get("supported_audio_suffixes", [])}
    if suffix not in supported:
        raise AudioQualityError(f"不支持的音频格式：{suffix or '<无扩展名>'}")
    if message.file_size is not None:
        maximum = int(rules.get("max_file_size_bytes", 200 * 1024 * 1024))
        if message.file_size <= 0:
            raise AudioQualityError("音频文件为空")
        if message.file_size > maximum:
            raise AudioQualityError("音频文件超过大小限制")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    if not path.is_file():
        raise AudioQualityError(f"音频文件不存在：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_transcript(payload: Any, duration_seconds: float | None = None) -> list[TranscriptSegment]:
    if not isinstance(payload, list) or not payload:
        raise AudioQualityError("转写结果必须是非空数组")
    result: list[TranscriptSegment] = []
    previous_start = -1.0
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise AudioQualityError(f"转写片段 {index} 必须是对象")
        try:
            start = float(item["start_seconds"])
            end = float(item["end_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AudioQualityError(f"转写片段 {index} 时间格式不合法") from exc
        speaker = normalize_text(item.get("speaker"))
        text = normalize_text(item.get("text"))
        if start < 0 or end <= start or start < previous_start:
            raise AudioQualityError(f"转写片段 {index} 时间范围或顺序不合法")
        if duration_seconds is not None and end > duration_seconds + 1:
            raise AudioQualityError(f"转写片段 {index} 超出音频时长")
        if not speaker or not text:
            raise AudioQualityError(f"转写片段 {index} 缺少说话人或文本")
        result.append(TranscriptSegment(start, end, speaker, text))
        previous_start = start
    return result


def _keyword_score(text: str, keywords: list[str]) -> int:
    return sum(1 for keyword in keywords if keyword and keyword in text)


def evaluate_asr_quality(segments: list[TranscriptSegment], rules: dict[str, Any]) -> list[str]:
    config = rules.get("asr_quality")
    if not isinstance(config, dict) or not config.get("enabled", True):
        return []
    text = normalize_text(" ".join(segment.text for segment in segments))
    reasons: list[str] = []
    min_chars = int(config.get("min_total_characters", 0))
    if min_chars and len(text) < min_chars:
        reasons.append(f"ASR有效文本过短（{len(text)}字，低于{min_chars}字）")
    min_segments = int(config.get("min_segment_count", 0))
    if min_segments and len(segments) < min_segments:
        reasons.append(f"ASR片段数量过少（{len(segments)}段，低于{min_segments}段）")
    unknown = {normalize_text(item) for item in config.get("unknown_speakers", ["", "??", "ASR", "UNKNOWN", "unknown"])}
    speakers = [normalize_text(s.speaker) for s in segments]
    distinct = {speaker for speaker in speakers if speaker not in unknown}
    min_speakers = int(config.get("min_distinct_speakers", 0))
    if min_speakers and len(distinct) < min_speakers:
        reasons.append(f"ASR可用声道少于{min_speakers}个")
    if speakers:
        max_unknown_ratio = float(config.get("max_unknown_speaker_ratio", 1.0))
        if max_unknown_ratio < 1.0 and sum(1 for s in speakers if s in unknown) / len(speakers) > max_unknown_ratio:
            reasons.append("ASR说话人分离不可靠")
    patterns = [normalize_text(item) for item in config.get("uncertain_patterns", []) if normalize_text(item)]
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
        reasons.append("ASR文本包含听不清或无法识别标记")
    terms = [normalize_text(item) for item in config.get("key_business_terms", []) if normalize_text(item)]
    if terms and not any(term in text for term in terms):
        reasons.append("ASR未识别到套餐、资费、办理等关键业务信息")
    return reasons


def choose_employee_speaker(segments: list[TranscriptSegment], rules: dict[str, Any]) -> tuple[str, float]:
    if not segments:
        raise AudioQualityError("缺少转写文本，无法判断客服声道")
    opening = [normalize_text(item) for item in rules.get("service_opening_keywords", []) if normalize_text(item)]
    general = [normalize_text(item) for item in rules.get("employee_speaker_keywords", []) if normalize_text(item)]
    opening = opening or ["中国移动", "10086", "工号", "您好", "机主本人", "后四位", "我帮您查询", "请稍等"]
    general = general or ["中国移动", "10086", "工号", "您好", "为您查询", "推荐", "建议", "请稍等"]
    speakers = sorted({segment.speaker for segment in segments})
    if speakers == ["??"]:
        return "??", 0.95
    stats = {speaker: {"opening": 0.0, "early": 0.0, "general": 0.0, "chars": 0.0} for speaker in speakers}
    for segment in segments:
        opening_hits = _keyword_score(segment.text, opening)
        general_hits = _keyword_score(segment.text, general)
        stats[segment.speaker]["chars"] += len(segment.text)
        stats[segment.speaker]["general"] += general_hits
        if segment.start_seconds <= float(rules.get("service_opening_window_seconds", 90)):
            stats[segment.speaker]["early"] += general_hits
            stats[segment.speaker]["opening"] += opening_hits
    ranked = []
    for speaker, item in stats.items():
        ranked.append((speaker, item["opening"] * 12 + item["early"] * 4 + item["general"] * 1.5))
    ranked.sort(key=lambda item: (item[1], stats[item[0]]["opening"], stats[item[0]]["early"], stats[item[0]]["chars"]), reverse=True)
    best, best_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if stats[best]["opening"] <= 0 and stats[best]["early"] < 2:
        return best, 0.35
    confidence = 0.58 + min(0.25, stats[best]["opening"] * 0.08) + min(0.12, (best_score - second) * 0.03)
    if stats[best]["opening"] <= 0:
        confidence = min(confidence, 0.70)
    return best, min(0.95, confidence)


def scan_rule_hits(segments: Iterable[TranscriptSegment], employee_speaker: str, rules: dict[str, Any]) -> list[RuleHit]:
    hits: list[RuleHit] = []
    seen: set[tuple[str, float, str]] = set()
    for segment in segments:
        if segment.speaker != employee_speaker:
            continue
        for rule in rules.get("phrase_rules", []):
            pattern = normalize_text(rule.get("pattern"))
            if not rule.get("enabled", True) or not pattern:
                continue
            matched = re.search(pattern, segment.text) if rule.get("regex") else pattern in segment.text
            key = (str(rule.get("id")), segment.start_seconds, segment.text)
            if matched and key not in seen:
                seen.add(key)
                hits.append(RuleHit(str(rule["id"]), segment.start_seconds, segment.text, normalize_text(rule.get("description")), max(0, int(rule.get("deduction", 0)))))
    return hits


def scan_required_action_gaps(segments: Iterable[TranscriptSegment], employee_speaker: str, rules: dict[str, Any]) -> list[RuleHit]:
    employee_text = "\n".join(segment.text for segment in segments if segment.speaker == employee_speaker)
    gaps: list[RuleHit] = []
    for action in rules.get("required_actions", []):
        rule_id = normalize_text(action.get("id"))
        patterns = [normalize_text(item) for item in action.get("patterns", []) if normalize_text(item)]
        if rule_id and patterns and not any(re.search(pattern, employee_text) for pattern in patterns):
            gaps.append(RuleHit(rule_id, 0.0, "未检测到明确话术", normalize_text(action.get("description")) or normalize_text(action.get("label")), max(0, int(action.get("deduction", 0)))))
    return gaps


def deterministic_quality_check(
    segments: list[TranscriptSegment],
    rules: dict[str, Any],
    employee_speaker_override: str | None = None,
    speaker_confidence_override: float | None = None,
    extra_review_reasons: Iterable[str] | None = None,
    asr_quality_segments: list[TranscriptSegment] | None = None,
) -> QualityResult:
    if employee_speaker_override:
        employee_speaker = normalize_text(employee_speaker_override)
        if employee_speaker not in {segment.speaker for segment in segments}:
            raise AudioQualityError("指定的员工说话人不存在于待评分片段")
        confidence = float(speaker_confidence_override if speaker_confidence_override is not None else 0.85)
    else:
        employee_speaker, confidence = choose_employee_speaker(segments, rules)
    threshold = float(rules.get("speaker_review_threshold", 0.75))
    hits = [] if confidence < threshold else scan_rule_hits(segments, employee_speaker, rules) + scan_required_action_gaps(segments, employee_speaker, rules)
    dimensions = rules.get("dimensions") or {"礼貌规范": {"weight": 0.45, "base_score": 100}, "沟通完整": {"weight": 0.35, "base_score": 90}, "表达清晰": {"weight": 0.20, "base_score": 90}}
    scores: dict[str, int] = {}
    for key, config in dimensions.items():
        score = int(config.get("base_score", 100))
        for hit in hits:
            applies = config.get("rule_ids")
            if not applies or hit.rule_id in applies:
                score -= hit.deduction
        scores[key] = max(0, min(100, score))
    total_weight = sum(max(0.0, float(config.get("weight", 0))) for config in dimensions.values()) or 1.0
    score = int(round(sum(scores[key] * max(0.0, float(config.get("weight", 0))) for key, config in dimensions.items()) / total_weight))
    issues = [QualityIssue(hit.rule_id, hit.start_seconds, hit.quote, hit.description or "命中质检规则", "请按规范话术客观、礼貌沟通，避免引发客户误解。", 1.0) for hit in hits]
    review_reasons = evaluate_asr_quality(asr_quality_segments or segments, rules)
    review_reasons.extend(normalize_text(item) for item in (extra_review_reasons or []) if normalize_text(item))
    needs_review = confidence < threshold or bool(review_reasons)
    if confidence < threshold:
        analysis = "未能通过开头固定话术可靠识别客服声道，未执行自动扣分。"
        optimization = "请人工确认客服声道；确认后再按问、查、算、比、挽留动作进行质检。"
    elif issues:
        analysis = "；".join(f"{issue.rule_id}：{issue.reason}" for issue in issues)
        optimization = "请根据问题证据复核录音，并按规则库调整沟通话术。"
    else:
        analysis = "未命中已配置的确定性扣分规则。"
        optimization = "保持礼貌、客观、清晰表达；后续可按正式评分标准继续补充规则库。"
    if review_reasons:
        analysis = f"{analysis} 需人工复核：{'；'.join(dict.fromkeys(review_reasons))}。"
        optimization = "请先复核 ASR 转写、客服角色和关键资费/套餐信息；确认后再采纳自动评分。"
    summary = f"员工声道：{employee_speaker}；规则评分：{score}；转写摘要：{join_transcript(segments, max_chars=180)}"
    return QualityResult(employee_speaker, confidence, scores, score, score >= int(rules.get("qualified_score", 80)) and not needs_review, summary, analysis, optimization, issues, needs_review)


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS audio_tasks (
            task_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, group_id TEXT NOT NULL,
            sender_user_id TEXT NOT NULL, sender_name TEXT NOT NULL, employee_name TEXT NOT NULL,
            order_id TEXT, accepted_number TEXT, attachment_id TEXT NOT NULL, attachment_hash TEXT NOT NULL,
            original_filename TEXT NOT NULL, file_size INTEGER, duration_seconds REAL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
            error_stage TEXT, error_message TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
            claimed_at TEXT, worker_id TEXT, archived_path TEXT, asr_provider TEXT, provider_task_id TEXT,
            UNIQUE (group_id, message_id, attachment_hash));
        CREATE TABLE IF NOT EXISTS audio_segments (
            task_id TEXT NOT NULL, segment_number INTEGER NOT NULL, start_seconds REAL NOT NULL,
            end_seconds REAL NOT NULL, speaker TEXT NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY (task_id, segment_number));
        CREATE TABLE IF NOT EXISTS quality_results (
            task_id TEXT PRIMARY KEY, employee_speaker TEXT NOT NULL, speaker_confidence REAL NOT NULL,
            dimension_scores TEXT NOT NULL, score INTEGER NOT NULL, qualified INTEGER NOT NULL,
            summary TEXT NOT NULL, analysis TEXT NOT NULL, optimization TEXT NOT NULL,
            issues TEXT NOT NULL, needs_human_review INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audio_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            event_type TEXT NOT NULL, event_data TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS organized_calls (
            task_id TEXT PRIMARY KEY, role_confidence REAL NOT NULL, call_summary TEXT NOT NULL,
            retention_result TEXT NOT NULL, uncertain_parts TEXT NOT NULL, full_dialogue TEXT NOT NULL,
            service_segments TEXT NOT NULL, customer_segments TEXT NOT NULL, created_at TEXT NOT NULL);
    """)
    connection.commit()
    return connection


def add_event(connection: sqlite3.Connection, task_id: str, event_type: str, event_data: dict[str, Any] | None = None) -> None:
    connection.execute("INSERT INTO audio_events(task_id,event_type,event_data,created_at) VALUES(?,?,?,?)", (task_id, event_type, json.dumps(event_data or {}, ensure_ascii=False), utc_now()))


def claim_next_task(connection: sqlite3.Connection, worker_id: str, stale_after_seconds: int = 1800) -> str | None:
    now = datetime.now(timezone.utc)
    stale_before = datetime.fromtimestamp(now.timestamp() - max(1, stale_after_seconds), timezone.utc).isoformat(timespec="seconds")
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("""SELECT task_id,status FROM audio_tasks
        WHERE status='received' OR (status='processing' AND (claimed_at IS NULL OR claimed_at < ?))
        ORDER BY created_at,task_id LIMIT 1""", (stale_before,)).fetchone()
    if not row:
        connection.rollback()
        return None
    task_id, previous = str(row[0]), str(row[1])
    connection.execute("""UPDATE audio_tasks SET status='processing',claimed_at=?,worker_id=?,
        attempt_count=attempt_count+1,error_stage=NULL,error_message=NULL WHERE task_id=?""", (utc_now(), worker_id, task_id))
    add_event(connection, task_id, "claimed", {"worker_id": worker_id, "reclaimed": previous == "processing"})
    connection.commit()
    return task_id


def reserve_task(connection: sqlite3.Connection, message: IncomingAudioMessage, submission: AudioSubmission) -> tuple[str, bool]:
    digest = normalize_text(message.attachment_hash) or hashlib.sha256(f"{message.attachment_id}\0{message.filename}\0{message.file_size}".encode()).hexdigest()
    accepted_number = submission.accepted_number or extract_accepted_number(repair_text(message.filename), repair_multiline_text(message.text))
    connection.execute("BEGIN IMMEDIATE")
    existing = connection.execute("SELECT task_id FROM audio_tasks WHERE group_id=? AND message_id=? AND attachment_hash=?", (message.group_id, message.message_id, digest)).fetchone()
    if existing:
        connection.rollback()
        return str(existing[0]), False
    task_id = "QA-" + datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper()
    now = utc_now()
    connection.execute("""INSERT INTO audio_tasks (
        task_id,message_id,group_id,sender_user_id,sender_name,employee_name,order_id,accepted_number,
        attachment_id,attachment_hash,original_filename,file_size,duration_seconds,status,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'received',?)""", (task_id, message.message_id, message.group_id, message.sender_user_id, message.sender_name, submission.employee_name, submission.order_id, accepted_number, message.attachment_id, digest, message.filename, message.file_size, message.duration_seconds, now))
    add_event(connection, task_id, "received", asdict(submission))
    connection.commit()
    return task_id, True


def attach_archived_file(connection: sqlite3.Connection, task_id: str, archived_path: Path) -> None:
    resolved = archived_path.resolve()
    if not resolved.is_file():
        raise AudioQualityError(f"归档音频不存在：{resolved}")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE audio_tasks SET archived_path=? WHERE task_id=?", (str(resolved), task_id))
    add_event(connection, task_id, "attachment_archived", {"path": str(resolved)})
    connection.commit()


def mark_task_failed(connection: sqlite3.Connection, task_id: str, stage: str, error: Exception | str) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("""UPDATE audio_tasks SET status='failed',error_stage=?,error_message=?,
        completed_at=?,claimed_at=NULL,worker_id=NULL WHERE task_id=?""", (normalize_text(stage), normalize_text(error)[:2000], utc_now(), task_id))
    add_event(connection, task_id, "failed", {"stage": normalize_text(stage), "error": normalize_text(error)[:2000]})
    connection.commit()


def save_transcript(connection: sqlite3.Connection, task_id: str, segments: list[TranscriptSegment]) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("DELETE FROM audio_segments WHERE task_id=?", (task_id,))
    connection.executemany("INSERT INTO audio_segments VALUES(?,?,?,?,?,?)", [(task_id, i, s.start_seconds, s.end_seconds, s.speaker, s.text) for i, s in enumerate(segments, 1)])
    connection.execute("UPDATE audio_tasks SET status='transcribed' WHERE task_id=?", (task_id,))
    add_event(connection, task_id, "transcribed", {"segment_count": len(segments)})
    connection.commit()


def save_organized_call(connection: sqlite3.Connection, task_id: str, organized: Any) -> None:
    now = utc_now()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("""INSERT INTO organized_calls VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET role_confidence=excluded.role_confidence,
        call_summary=excluded.call_summary,retention_result=excluded.retention_result,
        uncertain_parts=excluded.uncertain_parts,full_dialogue=excluded.full_dialogue,
        service_segments=excluded.service_segments,customer_segments=excluded.customer_segments,created_at=excluded.created_at""", (task_id, float(organized.role_confidence), normalize_text(organized.call_summary), normalize_text(organized.retention_result), normalize_text(organized.uncertain_parts), str(organized.full_dialogue), json.dumps([asdict(item) for item in organized.service_segments], ensure_ascii=False), json.dumps([asdict(item) for item in organized.customer_segments], ensure_ascii=False), now))
    add_event(connection, task_id, "organized", {"role_confidence": float(organized.role_confidence)})
    connection.commit()


def save_quality_result(connection: sqlite3.Connection, task_id: str, result: QualityResult) -> None:
    now = utc_now()
    status = "needs_review" if result.needs_human_review else "completed"
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("""INSERT INTO quality_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET employee_speaker=excluded.employee_speaker,
        speaker_confidence=excluded.speaker_confidence,dimension_scores=excluded.dimension_scores,
        score=excluded.score,qualified=excluded.qualified,summary=excluded.summary,
        analysis=excluded.analysis,optimization=excluded.optimization,issues=excluded.issues,
        needs_human_review=excluded.needs_human_review,created_at=excluded.created_at""", (task_id, result.employee_speaker, result.speaker_confidence, json.dumps(result.dimension_scores, ensure_ascii=False), result.score, int(result.qualified), result.summary, result.analysis, result.optimization, json.dumps([asdict(i) for i in result.issues], ensure_ascii=False), int(result.needs_human_review), now))
    connection.execute("UPDATE audio_tasks SET status=?,completed_at=?,claimed_at=NULL,worker_id=NULL WHERE task_id=?", (status, now, task_id))
    add_event(connection, task_id, status, {"score": result.score})
    connection.commit()


def load_segments(connection: sqlite3.Connection, task_id: str) -> list[TranscriptSegment]:
    rows = connection.execute("SELECT start_seconds,end_seconds,speaker,text FROM audio_segments WHERE task_id=? ORDER BY segment_number", (task_id,)).fetchall()
    return [TranscriptSegment(float(row[0]), float(row[1]), str(row[2]), str(row[3])) for row in rows]


def join_transcript(segments: Iterable[TranscriptSegment], max_chars: int | None = None) -> str:
    text = "\n".join(f"[{s.start_seconds:.1f}-{s.end_seconds:.1f}] {s.speaker}: {s.text}" for s in segments)
    if max_chars is not None and len(text) > max_chars:
        return text[:max(0, max_chars - 3)] + "..."
    return text


def _issue_rule_ids(result_issues: str | None) -> set[str]:
    if not result_issues:
        return set()
    try:
        raw = json.loads(result_issues)
    except json.JSONDecodeError:
        return set()
    return {normalize_text(item.get("rule_id")) for item in raw if isinstance(item, dict)}


def _action_status(rule_ids: set[str], missing_rule_id: str) -> str:
    return "未体现" if missing_rule_id in rule_ids else "已体现"


def _compact_issues(issues: str | None) -> str:
    if not issues:
        return ""
    try:
        raw = json.loads(issues)
    except json.JSONDecodeError:
        return normalize_text(issues)
    parts = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and normalize_text(item.get("reason")):
            quote = normalize_text(item.get("quote"))
            parts.append(f"{normalize_text(item.get('rule_id'))}：{normalize_text(item.get('reason'))}" + (f"（{quote}）" if quote and quote != "未检测到明确话术" else ""))
    return "；".join(parts)


def _segments_json_text(value: str | None, role: str) -> str:
    if not value:
        return ""
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return normalize_text(value)
    return "\n".join(f"{role}：{normalize_text(item.get('text'))}" for item in raw if isinstance(item, dict) and normalize_text(item.get("text")))


def _safe_report_filename(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", normalize_text(value) or "transcript")[:120]


def _write_report_text_file(report_path: Path, task_id: str, suffix: str, text: str) -> Path | None:
    if not text:
        return None
    day = report_path.stem[:10] if re.match(r"\d{4}-\d{2}-\d{2}", report_path.stem) else datetime.now().strftime("%Y-%m-%d")
    folder = report_path.parent / "transcript_texts" / day
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{_safe_report_filename(task_id)}_{suffix}.txt"
    output.write_text(text, encoding="utf-8")
    return output


def export_report(connection: sqlite3.Connection, output: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows = connection.execute("""SELECT t.task_id,t.created_at,t.sender_name,t.employee_name,
        t.original_filename,t.file_size,t.duration_seconds,t.accepted_number,t.status,r.score,r.qualified,
        r.summary,r.analysis,r.optimization,r.employee_speaker,r.speaker_confidence,r.dimension_scores,r.issues,
        r.needs_human_review,t.completed_at,o.call_summary,o.retention_result,o.uncertain_parts,o.full_dialogue,o.role_confidence,o.service_segments
        FROM audio_tasks t LEFT JOIN quality_results r ON r.task_id=t.task_id
        LEFT JOIN organized_calls o ON o.task_id=t.task_id
        ORDER BY t.created_at,t.task_id""").fetchall()
    headers = ["序号", "日期", "员工姓名", "员工电话", "客户号码", "通话时间", "问情况", "查情况", "算套餐", "比方案", "是否标签客户", "是否有挽留动作", "其他存在问题", "录音合格率", "销降离原因", "挽留结果", "录音总结", "存在问题分析", "优化意见（写出可执行的具体帮扶动作）", "质检人", "分公司", "音频文件名", "任务号", "质检状态", "人工复核", "ASR不确定/需复核原因", "员工说话人", "说话人置信度", "角色置信度", "整理后客服内容", "整理后对话", "原始转录文本", "原始转录文本文件"]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "降挽质检情况"
    sheet.append(headers)
    for index, raw in enumerate(rows, start=1):
        task_id, created_at, sender_name, employee_name, filename, file_size, duration_seconds, accepted_number, status, score, qualified, summary, analysis, optimization, employee_speaker, speaker_confidence, dimension_scores, issues, needs_review, completed_at, organized_summary, retention_result, uncertain_parts, full_dialogue, role_confidence, service_segments = raw
        rule_ids = _issue_rule_ids(issues)
        transcript = join_transcript(load_segments(connection, task_id))
        transcript_file = _write_report_text_file(output, task_id, "原始转录", transcript)
        review_reason = normalize_text(analysis) if needs_review else ""
        if needs_review and normalize_text(uncertain_parts):
            review_reason = (review_reason + "；" if review_reason else "") + f"DeepSeek不确定片段：{normalize_text(uncertain_parts)}"
        sheet.append([index, created_at[:10] if created_at else "", employee_name or sender_name or "", "", accepted_number or "", completed_at or created_at or "", _action_status(rule_ids, "missing_ask"), _action_status(rule_ids, "missing_check"), _action_status(rule_ids, "missing_calculate"), _action_status(rule_ids, "missing_compare"), "", "否" if "missing_retention_action" in rule_ids else "是", "", score if score is not None else "", "", "", organized_summary or summary or "", _compact_issues(issues) or analysis or "", optimization or "", "", "", filename, task_id, status, "是" if needs_review else ("否" if needs_review is not None else ""), review_reason, employee_speaker or "", speaker_confidence if speaker_confidence is not None else role_confidence if role_confidence is not None else "", role_confidence if role_confidence is not None else "", _segments_json_text(service_segments, "客服"), full_dialogue or "", transcript, str(transcript_file) if transcript_file else ""])
        if transcript_file:
            cell = sheet.cell(row=sheet.max_row, column=len(headers))
            cell.hyperlink = str(transcript_file)
            cell.style = "Hyperlink"
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="5B9BD5")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    widths = [8, 12, 14, 14, 16, 22, 12, 12, 12, 12, 14, 18, 28, 12, 18, 18, 48, 52, 52, 12, 12, 38, 24, 14, 12, 42, 16, 14, 14, 70, 80, 90, 60]
    for i, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(i)].width = width
    for row in sheet.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".tmp" + output.suffix)
    workbook.save(temporary)
    try:
        temporary.replace(output)
        return output
    except PermissionError:
        fallback = output.with_name(f"{output.stem}-{datetime.now().strftime('%H%M%S')}{output.suffix}")
        temporary.replace(fallback)
        return fallback


def export_daily_report(connection: sqlite3.Connection, output_root: Path, when: datetime | None = None) -> Path:
    day = (when or datetime.now()).strftime("%Y-%m-%d")
    return export_report(connection, output_root / f"{day}_降挽质检情况.xlsx")


def format_received_reply(task_id: str) -> str:
    return f"【听音质检】\n任务 {task_id} 已接收，正在处理。"


def format_completed_reply(task_id: str, needs_human_review: bool = False) -> str:
    return "听音检测完成，但需要人工复核。" if needs_human_review else "听音检测完成，已写入质检表。"
