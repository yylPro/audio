from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any


AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr"}


def parse_filename_metadata(filename: str) -> tuple[str, str | None]:
    date_match = re.search(r"(?<!\d)(20\d{2})[-_.年](\d{1,2})[-_.月](\d{1,2})(?:日)?(?!\d)", filename)
    if not date_match:
        date_match = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", filename)
    if not date_match:
        raise ValueError(f"文件名中未找到日期：{filename}")
    recording_date = date(*(int(item) for item in date_match.groups())).isoformat()
    phone_match = re.search(r"(?<!\d)(1[3-9]\d{9})(?:号码)?(?!\d)", filename)
    return recording_date, phone_match.group(1) if phone_match else None


def transcribe_one(model: Any, audio_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    segments, info = model.transcribe(
        str(audio_path), language="zh", beam_size=5, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False, word_timestamps=False,
    )
    items = [
        {"start_seconds": round(segment.start, 3), "end_seconds": round(segment.end, 3), "speaker": "UNKNOWN", "text": segment.text.strip()}
        for segment in segments if segment.text.strip()
    ]
    recording_date, service_number = parse_filename_metadata(audio_path.name)
    return {
        "filename": audio_path.name,
        "recording_date": recording_date,
        "service_number": service_number,
        "engine": "faster-whisper",
        "model": "small",
        "device": "cpu",
        "compute_type": "int8",
        "language": info.language,
        "language_probability": round(info.language_probability, 6),
        "duration_seconds": round(info.duration, 3),
        "duration_after_vad_seconds": round(info.duration_after_vad, 3),
        "transcription_seconds": round(time.perf_counter() - started, 3),
        "segments": items,
        "transcript_text": "\n".join(item["text"] for item in items),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch transcribe audio files with one local model load.")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少 faster_whisper 依赖；当前脚本只用于本地 faster-whisper 批量转写。") from exc
    audio_files = sorted(path for path in args.input_dir.iterdir() if path.is_file() and path.suffix.casefold() in AUDIO_SUFFIXES)
    if not audio_files:
        parser.error(f"input directory contains no supported audio: {args.input_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model = WhisperModel("small", device="cpu", compute_type="int8", download_root=str(args.model_dir))
    failures = 0
    for index, audio_path in enumerate(audio_files, 1):
        try:
            payload = transcribe_one(model, audio_path)
            output_path = args.output_dir / f"{audio_path.stem}.json"
            output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"OK {index}/{len(audio_files)} {audio_path.name} {payload['transcription_seconds']}s")
        except Exception as exc:
            failures += 1
            print(f"ERROR {index}/{len(audio_files)} {audio_path.name}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
