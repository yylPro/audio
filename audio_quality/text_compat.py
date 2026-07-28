from __future__ import annotations

import codecs
import re
from typing import Any


_ESCAPED_UNICODE_RE = re.compile(r"\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}|\\x[0-9a-fA-F]{2}")
_CONTROL_ESCAPE_RE = re.compile(r"\\[nrt]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_MOJIBAKE_HINT_RE = re.compile(r"[鍚浠宸鎻涓瀹璇煶妫€瑙触嬫穦歖]")


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "").replace("\u200b", "")
    return " ".join(text.strip().split())


def _decode_escaped_text(text: str, decode_controls: bool = False) -> str:
    if not (_ESCAPED_UNICODE_RE.search(text) or (decode_controls and _CONTROL_ESCAPE_RE.search(text))):
        return text
    try:
        return codecs.decode(text, "unicode_escape")
    except Exception:
        return text


def _try_fix_mojibake(text: str) -> str:
    if not text or not _MOJIBAKE_HINT_RE.search(text):
        return text
    candidates = [text]
    for source_encoding in ("gbk", "cp936", "latin1"):
        try:
            candidates.append(text.encode(source_encoding).decode("utf-8"))
        except Exception:
            pass
    return max(candidates, key=lambda item: (len(_CJK_RE.findall(item)), -item.count("\ufffd"), -item.count("?")))


def repair_text(value: Any) -> str:
    text = compact_text(value)
    if not text:
        return ""
    decoded = _decode_escaped_text(text)
    fixed = _try_fix_mojibake(decoded)
    return compact_text(fixed)


def repair_multiline_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "").replace("\u200b", "").strip()
    decoded = _decode_escaped_text(text, decode_controls=True)
    return _try_fix_mojibake(decoded).strip()
