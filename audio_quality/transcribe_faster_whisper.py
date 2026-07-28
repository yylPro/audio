from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local faster-whisper transcription benchmark.")
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="small")
    parser.add_argument("--model-dir", type=Path, default=Path(r"D:\代维\工单提醒\audio_quality_runtime\models"))
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    args = parser.parse_args()

    if not args.audio.is_file():
        parser.error(f"audio file does not exist: {args.audio}")
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError as exc:
        raise SystemExit("缺少 faster_whisper 依赖；请先在 audio_quality_runtime\\.venv 中安装 faster-whisper。") from exc

    started = time.perf_counter()
    args.model_dir.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type, download_root=str(args.model_dir))
    model_loaded = time.perf_counter()
    segments, info = model.transcribe(
        str(args.audio),
        language="zh",
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,
        word_timestamps=False,
    )
    items = [
        {
            "start_seconds": round(segment.start, 3),
            "end_seconds": round(segment.end, 3),
            "speaker": "UNKNOWN",
            "text": segment.text.strip(),
        }
        for segment in segments
        if segment.text.strip()
    ]
    completed = time.perf_counter()
    payload = {
        "engine": "faster-whisper",
        "model": args.model,
        "model_dir": str(args.model_dir.resolve()),
        "device": args.device,
        "compute_type": args.compute_type,
        "language": info.language,
        "language_probability": round(info.language_probability, 6),
        "duration_seconds": round(info.duration, 3),
        "duration_after_vad_seconds": round(info.duration_after_vad, 3),
        "model_load_seconds": round(model_loaded - started, 3),
        "transcription_seconds": round(completed - model_loaded, 3),
        "total_seconds": round(completed - started, 3),
        "segments": items,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "segments"}, ensure_ascii=False))
    print(f"segments={len(items)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
