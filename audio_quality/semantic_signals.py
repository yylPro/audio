from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .audio_quality_processor import TranscriptSegment
from .text_compat import compact_text, repair_text


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
TV_BOX_INTENT = "tv_box_retention_or_cancel"

DEFAULT_PATTERNS = {
    "target_amount": [
        r"(?:改到|换到|降到|降至|降为|调到|调成|转成|办理|变更|改成|改)\s*(\d{1,4})\s*(?:元|块|块钱)",
        r"(\d{1,4})\s*(?:元|块|块钱)\s*(?:套餐|月租|资费|保号|低消)",
    ],
    "current_amount": [
        r"(?:现在|原来|当前|目前|原套餐|现套餐)\s*(?:是|一个|月租)?\s*(\d{1,4})\s*(?:元|块|块钱)",
    ],
    "target_plan": [
        r"(最低(?:套餐|资费|消费|档位)?)",
        r"(保号(?:套餐)?)",
        r"(?<!\d)(8元(?:套餐|保号)?)",
        r"(低(?:一点|一些|档|套餐|资费))",
    ],
    "fee_amount": [
        r"(\d{1,4})\s*(?:元|块|块钱)",
    ],
}

DEFAULT_INTENT_CLUES = {
    LOW_FEE_INTENT: [
        "改8元", "改成8元", "改低", "降套餐", "降资费", "降低资费", "套餐太贵", "费用高",
        "保号", "最低套餐", "最低消费", "低月租", "不用流量", "用不上流量", "老人机",
    ],
    TV_BOX_INTENT: [
        "机顶盒", "移动高清", "魔百和", "电视", "退掉", "取消", "不用了", "扣15", "扣费",
        "优惠一年", "继续使用", "申请优惠", "减免", "免掉",
    ],
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
    ],
    TV_BOX_INTENT: [
        "客户不用机顶盒，觉得每月扣费，想退掉",
        "客户反馈移动高清扣15元，客服申请优惠继续使用",
        "客户说机顶盒合约包含在套餐里，不应该单独扣费",
        "客户机顶盒没有使用，要求取消或减免费用",
        "客服给客户申请机顶盒优惠，挽留继续使用",
    ],
}

DEFAULT_OBJECT_CLUES = {
    "mobile_plan": ["套餐", "资费", "月租", "流量", "分钟", "低消", "副卡", "保号"],
    "broadband": ["宽带", "家宽", "光猫", "路由器"],
    "tv_box": ["机顶盒", "魔百和", "移动高清", "电视"],
    "camera": ["摄像头", "看家"],
}


def _normalize_patterns(values: Iterable[Any]) -> list[str]:
    return [compact_text(repair_text(item)) for item in values if compact_text(repair_text(item))]


def _clean_text(value: Any) -> str:
    return compact_text(repair_text(value))


def _window(text: str, start: int, end: int, radius: int = 24) -> str:
    return text[max(0, start - radius): min(len(text), end + radius)]


def _first_matches(text: str, patterns: Iterable[str]) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = match.group(1) if match.groups() else match.group(0)
            matches.append((_clean_text(value), _window(text, match.start(), match.end())))
    return matches


def _char_ngrams(text: str, n: int = 2) -> set[str]:
    text = re.sub(r"\s+", "", _clean_text(text))
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
    return len(left_grams & right_grams) / max(1, len(left_grams | right_grams))


def _best_semantic_match(text: str, examples: Iterable[str]) -> tuple[float, str]:
    best_score = 0.0
    best_example = ""
    for example in examples:
        example = _clean_text(example)
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


def _configured_mapping(config: dict[str, Any], key: str, default: dict[str, list[str]]) -> dict[str, list[str]]:
    configured = config.get(key)
    if not isinstance(configured, dict):
        return default
    merged = {name: list(values) for name, values in default.items()}
    for name, values in configured.items():
        if isinstance(values, list):
            merged[str(name)] = [_clean_text(item) for item in values if _clean_text(item)]
    return merged


def _detect_business_object(text: str, config: dict[str, Any]) -> str | None:
    object_clues = _configured_mapping(config, "business_object_clues", DEFAULT_OBJECT_CLUES)
    scored: list[tuple[str, int]] = []
    for object_id, clues in object_clues.items():
        score = sum(1 for clue in clues if _clean_text(clue) and _clean_text(clue) in text)
        if score:
            scored.append((str(object_id), score))
    if not scored:
        return None
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[0][0]


def _build_evidence(segments: list[TranscriptSegment], evidence_texts: set[str], semantic_evidence: Evidence | None) -> list[Evidence]:
    evidence: list[Evidence] = []
    if semantic_evidence is not None:
        evidence.append(semantic_evidence)
    for text in evidence_texts:
        source = next((segment for segment in segments if text and text[: min(12, len(text))] in segment.text), None)
        evidence.append(Evidence(source.speaker if source else "", source.start_seconds if source else 0.0, text))
    return evidence[:4]


def _build_signal(intent: str, slots: dict[str, Any], votes: list[dict[str, Any]], evidence: list[Evidence]) -> BusinessSignal:
    vote_confidence = sum(float(vote["confidence"]) for vote in votes) / max(1, len(votes))
    confidence = min(0.95, vote_confidence + len(slots) * 0.04 + min(0.08, len(votes) * 0.02))
    return BusinessSignal(intent, round(confidence, 2), slots, evidence, votes)


def _clue_evidence(full_text: str, clues: Iterable[str]) -> set[str]:
    evidence_texts: set[str] = set()
    for clue in clues:
        clue = _clean_text(clue)
        if clue and clue in full_text:
            start = full_text.find(clue)
            evidence_texts.add(_window(full_text, start, start + len(clue)))
            if len(evidence_texts) >= 4:
                break
    return evidence_texts


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_low_fee_signal(
    segments: list[TranscriptSegment],
    full_text: str,
    config: dict[str, Any],
    patterns: dict[str, list[str]],
    intent_clues: dict[str, list[str]],
    intent_examples: dict[str, list[str]],
    semantic_threshold: float,
) -> BusinessSignal | None:
    clues = intent_clues.get(LOW_FEE_INTENT, [])
    clue_score = sum(1 for clue in clues if _clean_text(clue) and _clean_text(clue) in full_text)
    amount_matches = _first_matches(full_text, patterns.get("target_amount", []))
    plan_matches = _first_matches(full_text, patterns.get("target_plan", []))
    semantic_score, semantic_example, semantic_evidence = _segment_semantic_vote(segments, intent_examples.get(LOW_FEE_INTENT, []), semantic_threshold)

    votes: list[dict[str, Any]] = []
    if clue_score:
        votes.append({"source": "keyword_clues", "label": LOW_FEE_INTENT, "confidence": min(0.95, 0.50 + clue_score * 0.08), "detail": f"{clue_score} clues"})
    if amount_matches:
        votes.append({"source": "slot_amount", "label": LOW_FEE_INTENT, "confidence": 0.78, "detail": amount_matches[0][0]})
    if plan_matches:
        votes.append({"source": "slot_plan", "label": LOW_FEE_INTENT, "confidence": 0.74, "detail": plan_matches[0][0]})
    if semantic_evidence is not None:
        votes.append({"source": "semantic_examples", "label": LOW_FEE_INTENT, "confidence": round(min(0.90, 0.45 + semantic_score), 2), "detail": semantic_example})
    if not votes:
        return None

    slots: dict[str, Any] = {}
    amount = _safe_int(amount_matches[0][0]) if amount_matches else None
    if amount is not None:
        slots["target_amount_yuan"] = amount
    current_amount = _first_matches(full_text, patterns.get("current_amount", []))
    current = _safe_int(current_amount[0][0]) if current_amount else None
    if current is not None:
        slots["current_amount_yuan"] = current
    if plan_matches:
        slots["target_plan"] = plan_matches[0][0]
    business_object = _detect_business_object(full_text, config)
    if business_object:
        slots["business_object"] = business_object
    evidence_texts = {item[1] for item in amount_matches[:2] + plan_matches[:2]}
    evidence_texts.update(_clue_evidence(full_text, clues))
    return _build_signal(LOW_FEE_INTENT, slots, votes, _build_evidence(segments, evidence_texts, semantic_evidence))


def _extract_tv_box_signal(
    segments: list[TranscriptSegment],
    full_text: str,
    config: dict[str, Any],
    patterns: dict[str, list[str]],
    intent_clues: dict[str, list[str]],
    intent_examples: dict[str, list[str]],
    semantic_threshold: float,
) -> BusinessSignal | None:
    clues = intent_clues.get(TV_BOX_INTENT, [])
    clue_score = sum(1 for clue in clues if _clean_text(clue) and _clean_text(clue) in full_text)
    semantic_score, semantic_example, semantic_evidence = _segment_semantic_vote(segments, intent_examples.get(TV_BOX_INTENT, []), semantic_threshold)
    business_object = _detect_business_object(full_text, config)

    votes: list[dict[str, Any]] = []
    if clue_score:
        votes.append({"source": "keyword_clues", "label": TV_BOX_INTENT, "confidence": min(0.95, 0.52 + clue_score * 0.06), "detail": f"{clue_score} clues"})
    if semantic_evidence is not None:
        votes.append({"source": "semantic_examples", "label": TV_BOX_INTENT, "confidence": round(min(0.90, 0.45 + semantic_score), 2), "detail": semantic_example})
    if business_object == "tv_box":
        votes.append({"source": "business_object", "label": TV_BOX_INTENT, "confidence": 0.76, "detail": "tv_box"})
    if not votes:
        return None

    fee_matches = _first_matches(full_text, patterns.get("fee_amount", []))
    amounts = sorted({amount for value, _ in fee_matches if (amount := _safe_int(value)) is not None})
    slots: dict[str, Any] = {"business_object": "tv_box"}
    if amounts:
        slots["fee_amounts_yuan"] = amounts
    if any(word in full_text for word in ["退掉", "取消", "不用了", "不用", "不想用", "停掉"]):
        slots["customer_goal"] = "cancel_or_stop_using"
    if any(word in full_text for word in ["优惠", "继续使用", "申请", "减免", "免掉", "返还"]):
        slots["retention_offer"] = "discount_or_fee_waiver"
    evidence_texts = _clue_evidence(full_text, clues)
    evidence_texts.update(item[1] for item in fee_matches[:2])
    return _build_signal(TV_BOX_INTENT, slots, votes, _build_evidence(segments, evidence_texts, semantic_evidence))


def extract_business_signals(segments: list[TranscriptSegment], config: dict[str, Any] | None = None) -> list[BusinessSignal]:
    signal_config = config or {}
    if not signal_config.get("enabled", True):
        return []

    patterns = {key: list(values) for key, values in DEFAULT_PATTERNS.items()}
    for key, values in (signal_config.get("slot_patterns") or {}).items():
        patterns[str(key)] = _normalize_patterns(values)
    intent_clues = _configured_mapping(signal_config, "intent_clues", DEFAULT_INTENT_CLUES)
    intent_examples = _configured_mapping(signal_config, "intent_examples", DEFAULT_INTENT_EXAMPLES)
    weak_config = signal_config.get("weak_supervision") if isinstance(signal_config.get("weak_supervision"), dict) else {}
    semantic_threshold = float(weak_config.get("semantic_threshold", 0.28))

    full_text = "\n".join(_clean_text(segment.text) for segment in segments)
    signals = [
        _extract_low_fee_signal(segments, full_text, signal_config, patterns, intent_clues, intent_examples, semantic_threshold),
        _extract_tv_box_signal(segments, full_text, signal_config, patterns, intent_clues, intent_examples, semantic_threshold),
    ]
    return [signal for signal in signals if signal is not None]


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
        if "fee_amounts_yuan" in signal.slots:
            slot_parts.append(f"费用={','.join(str(item) for item in signal.slots['fee_amounts_yuan'])}元")
        if "customer_goal" in signal.slots:
            slot_parts.append(f"诉求={signal.slots['customer_goal']}")
        if "retention_offer" in signal.slots:
            slot_parts.append(f"挽留={signal.slots['retention_offer']}")
        parts.append(f"{signal.intent}({', '.join(slot_parts) or '无明确槽位'}, 置信度={signal.confidence}, 投票={len(signal.votes)})")
    return "；".join(parts)
