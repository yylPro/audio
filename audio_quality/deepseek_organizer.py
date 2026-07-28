from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .audio_quality_processor import AudioQualityError, TranscriptSegment, join_transcript, normalize_text


class DeepSeekOrganizerError(AudioQualityError):
    pass


@dataclass(frozen=True)
class OrganizedCall:
    service_segments: list[TranscriptSegment]
    customer_segments: list[TranscriptSegment]
    full_dialogue: str
    call_summary: str
    retention_result: str
    uncertain_parts: str
    role_confidence: float


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise DeepSeekOrganizerError(f"环境变量 {name} 未配置")
    return value


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise DeepSeekOrganizerError("DeepSeek 未返回 JSON 对象")
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise DeepSeekOrganizerError(f"DeepSeek JSON 解析失败：{exc}") from exc
    if not isinstance(value, dict):
        raise DeepSeekOrganizerError("DeepSeek JSON 根节点不是对象")
    return value


def _segment_from_item(item: dict[str, Any], role: str) -> TranscriptSegment:
    try:
        start = float(item["start_seconds"])
        end = float(item["end_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DeepSeekOrganizerError(f"{role} 片段时间格式不合法") from exc
    text = normalize_text(item.get("text"))
    if start < 0 or end <= start or not text:
        raise DeepSeekOrganizerError(f"{role} 片段缺少有效时间或文本")
    return TranscriptSegment(start, end, role, text)


def validate_organized_payload(payload: dict[str, Any]) -> OrganizedCall:
    service_raw = payload.get("service_segments", [])
    customer_raw = payload.get("customer_segments", [])
    if not isinstance(service_raw, list) or not isinstance(customer_raw, list):
        raise DeepSeekOrganizerError("service_segments/customer_segments 必须是数组")
    service_segments = [_segment_from_item(item, "客服") for item in service_raw if isinstance(item, dict)]
    customer_segments = [_segment_from_item(item, "客户") for item in customer_raw if isinstance(item, dict)]
    confidence = float(payload.get("role_confidence", 0))
    if not 0 <= confidence <= 1:
        raise DeepSeekOrganizerError("role_confidence 必须在 0 到 1 之间")
    dialogue = normalize_text(payload.get("full_dialogue"))
    if not dialogue:
        dialogue = "\n".join([f"客服：{s.text}" for s in service_segments] + [f"客户：{s.text}" for s in customer_segments])
    return OrganizedCall(service_segments, customer_segments, dialogue, normalize_text(payload.get("call_summary")), normalize_text(payload.get("retention_result")), normalize_text(payload.get("uncertain_parts")), confidence)


class DeepSeekDialogueOrganizer:
    def __init__(self, config: dict[str, Any]):
        self.enabled = bool(config.get("enabled", False))
        self.api_key_env = str(config.get("api_key_env", "DEEPSEEK_API_KEY"))
        self.base_url = str(config.get("base_url", "https://api.deepseek.com/chat/completions"))
        self.model = str(config.get("model", "deepseek-chat"))
        self.timeout_seconds = int(config.get("timeout_seconds", 90))
        self.max_input_chars = int(config.get("max_input_chars", 12000))

    def organize(self, segments: list[TranscriptSegment]) -> OrganizedCall | None:
        if not self.enabled:
            return None
        api_key = _required_env(self.api_key_env)
        transcript = join_transcript(segments)
        if len(transcript) > self.max_input_chars:
            transcript = transcript[: self.max_input_chars] + "\n[后续转录因长度限制省略]"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你只做电话录音转写文本的角色整理，不评分，不编造，不补写原文不存在的信息。只返回 JSON。"},
                {"role": "user", "content": self._build_prompt(transcript)},
            ],
            "temperature": 0.1,
            "stream": False,
        }
        request = urllib.request.Request(self.base_url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise DeepSeekOrganizerError(f"DeepSeek 请求失败：HTTP {exc.code} {detail}") from exc
        except Exception as exc:
            raise DeepSeekOrganizerError(f"DeepSeek 请求失败：{exc}") from exc
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        return validate_organized_payload(_extract_json(content))

    def _build_prompt(self, transcript: str) -> str:
        return f"""
请把下面 ASR 原始转录整理成客服/客户角色对话。注意：
1. 原始录音可能不是从通话开头开始，不要强行寻找开场白。
2. 客服通常负责解释业务、查询/推荐套餐、说明费用/生效/后续处理；客户通常表达诉求、疑问、不满或确认。
3. 客户说话再激动，也不能作为客服不礼貌证据。
4. 不要评分，不要输出扣分结论。
5. 只返回 JSON，不要 Markdown。

JSON 格式：
{{
  "role_confidence": 0.0到1.0,
  "call_summary": "一句话概览",
  "retention_result": "办理/未办理/引导/待跟进/无法判断",
  "uncertain_parts": "无法确认角色或语义的片段说明，没有则空字符串",
  "service_segments": [{{"start_seconds": 0.0, "end_seconds": 1.0, "text": "客服原话或整理后不改变事实的短句"}}],
  "customer_segments": [{{"start_seconds": 0.0, "end_seconds": 1.0, "text": "客户原话或整理后不改变事实的短句"}}],
  "full_dialogue": "按时间顺序整理后的 客服：...\\n客户：... 对话"
}}

ASR 原始转录：
{transcript}
""".strip()
