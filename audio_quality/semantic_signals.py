from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .audio_quality_processor import TranscriptSegment
from .text_compat import compact_text


@dataclass(frozen=True)
class Evidence:
    speaker: str
    start_seconds: float
    text: str


@dataclass(frozen=True)
class BusinessSignal:
    intent: str
    confidence: float
    slots: dict[str, Any]
    evidence: list[Evidence]
    votes: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "slots": self.slots,
            "evidence": [asdict(item) for item in self.evidence],
            "votes": self.votes,
        }


LOW_FEE_INTENT = "lower_fee_or_keep_number"

DEFAULT_PATTERNS = {
    "target_amount": [
        r"(?:改|换|降到|降至|降为|调到|调成|转成|办理|变更|改成)\s*(\d{1,4})\s*(?:元|块|块钱)",
        r"(\d{1,4})\s*(?:元|块|块钱)\s*(?:套餐|月租|资费|保号)",
    ],
    "current_amount": [
        r"(?:现在|原来|当前|目前|原套餐|现套餐)\s*(?:是|为|月租)?\s*(\d{1,4})\s*(?:元|块|块钱)",
    ],
    "target_plan": [
        r"(最低(?:套餐|资费|消费|档位)?)",
        r"(保号(?:套餐)?)",
        r"(?<!\d)(8元(?:套餐|保号)?)",
        r"(低(?:一点|一些|档|套餐|资费))",
    ],
}

DEFAULT_INTENT_CLUES = {
    LOW_FEE_INTENT: [
        "改8元", "改成8元", "改低", "降套餐", "降资费", "降低资费", "套餐太贵", "费用高",
        "保号", "最低套餐", "最低消费", "低月租", "不用流量", "用不上流量", "老人机",
    ]
}

DEFAULT_INTENT_EXAMPLES = {
    LOW_FEE_INTENT: [
        "客户要求改8元套餐",
        "客户想改成18元低资费套餐",
        "客户觉得套餐太贵，想降到最低",
        "客户只想保留号码，不需要流量",
        "客户老人机不用流量，想换低月租",
        "老人家手机只接电话，月租能不能弄低一点",
        "平时用得少，想把月租调低一点",
        "不用这么多资源，想换便宜一点的套餐",
        "客户要求降低资费或改最低消费",
        "客户用不上当前套餐，要求降档",
    ]
}

DEFAULT_OBJECT_CLUES = {
    "mobile_plan": ["套餐", "资费", "月租", "流量", "分钟", "低消", "副卡"],
    "broadband": ["宽带", "家宽", "光猫", "路由器"],
    "tv_box": ["机顶盒", "魔百和", "移动高清"],
    "camera": ["摄像头", "看家"],
}


def _normalize_patterns(values: Iterable[Any]) -> list[str]:
    return [compact_text(item) for item in values if compact_text(item)]


def _window(text: str, start: int, end: int, radius: int = 24) -> str:
    return text[max(0, start - radius): min(len(text), end + radius)]


def _first_matches(text: str, patterns: Iterable[str]) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = match.group(1) if match.groups() else match.group(0)
            matches.append((compact_text(value), _window(text, match.start(), match.end())))
    return matches


def _char_ngrams(text: str, n: int = 2) -> set[str]:
    text = re.sub(r"\s+", "", compact_text(text))
    if not text:
        return set()
    if len(text) <= n:
        return {text}
    return {text[index:index + n] for index in range(0, len(text) - n + 1)}


def _semantic_similarity(left: str, right: str) -> float:
    left_grams = _char_ngrams(left)
    right_grams = _char_ngrams(right)
    if not left_grams or not right_grams:
        return 0.0
    overlap = len(left_grams & right_grams)
    return overlap / max(1, len(left_grams | right_grams))


def _best_semantic_match(text: str, examples: Iterable[str]) -> tuple[float, str]:
    best_score = 0.0
    best_example = ""
    for example in examples:
        example = compact_text(example)
        if not example:
            continue
        score = _semantic_similarity(text, example)
        if score > best_score:
            best_score = score
            best_example = example
    return best_score, best_example


def _segment_semantic_vote(segments: list[TranscriptSegment], examples: Iterable[str], threshold: float) -> tuple[float, str, Evidence | None]:
    best = (0.0, "", None)
    for segment in segments:
        score, example = _best_semantic_match(segment.text, examples)
        if score > best[0]:
            best = (score, example, Evidence(segment.speaker, segment.start_seconds, segment.text))
    if best[0] >= threshold:
        return best
    return best[0], best[1], None


def _detect_business_object(text: str, config: dict[str, Any]) -> str | None:
    object_clues = config.get("business_object_clues") if isinstance(config.get("business_object_clues"), dict) else DEFAULT_OBJECT_CLUES
    scored: list[tuple[str, int]] = []
    for object_id, clues in object_clues.items():
        score = sum(1 for clue in clues if compact_text(clue) and compact_text(clue) in text)
        if score:
            scored.append((str(object_id), score))
    if not scored:
        return None
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[0][0]


def extract_business_signals(segments: list[TranscriptSegment], config: dict[str, Any] | None = None) -> list[BusinessSignal]:
    signal_config = config or {}
    if not signal_config.get("enabled", True):
        return []

    patterns = dict(DEFAULT_PATTERNS)
    for key, values in (signal_config.get("slot_patterns") or {}).items():
        patterns[str(key)] = _normalize_patterns(values)
    intent_clues = signal_config.get("intent_clues") if isinstance(signal_config.get("intent_clues"), dict) else DEFAULT_INTENT_CLUES
    intent_examples = signal_config.get("intent_examples") if isinstance(signal_config.get("intent_examples"), dict) else DEFAULT_INTENT_EXAMPLES
    weak_config = signal_config.get("weak_supervision") if isinstance(signal_config.get("weak_supervision"), dict) else {}
    semantic_threshold = float(weak_config.get("semantic_threshold", 0.28))

    full_text = "\n".join(segment.text for segment in segments)
    low_fee_score = sum(1 for clue in intent_clues.get(LOW_FEE_INTENT, []) if compact_text(clue) and compact_text(clue) in full_text)
    amount_matches = _first_matches(full_text, patterns.get("target_amount", []))
    plan_matches = _first_matches(full_text, patterns.get("target_plan", []))
    semantic_score, semantic_example, semantic_evidence = _segment_semantic_vote(segments, intent_examples.get(LOW_FEE_INTENT, []), semantic_threshold)

    votes: list[dict[str, Any]] = []
    if low_fee_score:
        votes.append({"source": "keyword_clues", "label": LOW_FEE_INTENT, "confidence": min(0.95, 0.50 + low_fee_score * 0.08), "detail": f"{low_fee_score} clues"})
    if amount_matches:
        votes.append({"source": "slot_amount", "label": LOW_FEE_INTENT, "confidence": 0.78, "detail": amount_matches[0][0]})
    if plan_matches:
        votes.append({"source": "slot_plan", "label": LOW_FEE_INTENT, "confidence": 0.74, "detail": plan_matches[0][0]})
    if semantic_evidence is not None:
        votes.append({"source": "semantic_examples", "label": LOW_FEE_INTENT, "confidence": round(min(0.90, 0.45 + semantic_score), 2), "detail": semantic_example})

    if not votes:
        return []

    slots: dict[str, Any] = {}
    if amount_matches:
        slots["target_amount_yuan"] = int(amount_matches[0][0])
    current_amount = _first_matches(full_text, patterns.get("current_amount", []))
    if current_amount:
        slots["current_amount_yuan"] = int(current_amount[0][0])
    if plan_matches:
        slots["target_plan"] = plan_matches[0][0]
    business_object = _detect_business_object(full_text, signal_config)
    if business_object:
        slots["business_object"] = business_object

    evidence_texts = {item[1] for item in amount_matches[:2] + plan_matches[:2]}
    for clue in intent_clues.get(LOW_FEE_INTENT, []):
        clue = compact_text(clue)
        if clue and clue in full_text:
            start = full_text.find(clue)
            evidence_texts.add(_window(full_text, start, start + len(clue)))
            if len(evidence_texts) >= 4:
                break

    evidence: list[Evidence] = []
    if semantic_evidence is not None:
        evidence.append(semantic_evidence)
    for text in evidence_texts:
        source = next((segment for segment in segments if text and text[: min(12, len(text))] in segment.text), None)
        if source is None:
            source = next((segment for segment in segments if any(token and token in segment.text for token in text.split())), None)
        evidence.append(Evidence(source.speaker if source else "", source.start_seconds if source else 0.0, text))

    positive_votes = [vote for vote in votes if vote["label"] == LOW_FEE_INTENT]
    vote_confidence = sum(float(vote["confidence"]) for vote in positive_votes) / max(1, len(positive_votes))
    confidence = min(0.95, vote_confidence + len(slots) * 0.04 + min(0.08, len(positive_votes) * 0.02))
    return [BusinessSignal(LOW_FEE_INTENT, round(confidence, 2), slots, evidence[:4], votes)]


def summarize_business_signals(signals: list[BusinessSignal]) -> str:
    if not signals:
        return ""
    parts = []
    for signal in signals:
        slot_parts = []
        if "target_amount_yuan" in signal.slots:
            slot_parts.append(f"目标金额={signal.slots['target_amount_yuan']}元")
        if "target_plan" in signal.slots:
            slot_parts.append(f"目标档位={signal.slots['target_plan']}")
        if "business_object" in signal.slots:
            slot_parts.append(f"对象={signal.slots['business_object']}")
        parts.append(f"{signal.intent}({', '.join(slot_parts) or '无明确槽位'}, 置信度={signal.confidence}, 投票={len(signal.votes)})")
    return "；".join(parts)
