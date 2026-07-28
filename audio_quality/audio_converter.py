from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AudioConversionError(Exception):
    pass


@dataclass(frozen=True)
class AudioConversionResult:
    source_path: Path
    audio_path: Path
    converted: bool
    reason: str = ""


def _resolve_ffmpeg(config: dict[str, Any]) -> str:
    configured = config.get("ffmpeg_path")
    if configured:
        path = Path(str(configured))
        if path.is_file():
            return str(path)
        raise AudioConversionError(f"ffmpeg_path 不存在：{path}")

    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered

    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise AudioConversionError("未找到 ffmpeg，请在 audio_conversion.ffmpeg_path 配置 ffmpeg.exe，或安装 imageio-ffmpeg") from exc


def convert_audio_if_needed(source_path: Path, task_id: str, config: dict[str, Any] | None) -> AudioConversionResult:
    conversion_config = config or {}
    if not conversion_config.get("enabled", False):
        return AudioConversionResult(source_path=source_path, audio_path=source_path, converted=False, reason="disabled")

    source_suffix = source_path.suffix.lower()
    convert_suffixes = {str(item).lower() for item in conversion_config.get("convert_suffixes", [".m4a", ".aac", ".amr", ".ogg", ".flac"])}
    if source_suffix not in convert_suffixes:
        return AudioConversionResult(source_path=source_path, audio_path=source_path, converted=False, reason="suffix_skipped")

    if not source_path.is_file():
        raise AudioConversionError(f"源音频不存在：{source_path}")

    target_suffix = str(conversion_config.get("target_suffix", ".wav")).lower()
    if target_suffix not in {".wav", ".mp3"}:
        raise AudioConversionError(f"不支持的转换目标格式：{target_suffix}")

    work_dir = Path(str(conversion_config.get("work_dir") or source_path.parent / "converted"))
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / f"{task_id}{target_suffix}"

    ffmpeg = _resolve_ffmpeg(conversion_config)
    sample_rate = int(conversion_config.get("sample_rate", 16000))
    channels = conversion_config.get("channels", "auto")

    command = [ffmpeg, "-y", "-i", str(source_path)]
    if channels not in (None, "", "auto"):
        command.extend(["-ac", str(int(channels))])
    command.extend(["-ar", str(sample_rate), "-vn"])
    if target_suffix == ".wav":
        command.extend(["-acodec", "pcm_s16le", "-f", "wav"])
    else:
        bitrate = str(conversion_config.get("mp3_bitrate", "64k"))
        command.extend(["-acodec", "libmp3lame", "-b:a", bitrate, "-f", "mp3"])
    command.append(str(output_path))

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=int(conversion_config.get("timeout_seconds", 300)), check=False)
    except subprocess.TimeoutExpired as exc:
        raise AudioConversionError(f"音频转换超时：{source_path.name}") from exc
    except OSError as exc:
        raise AudioConversionError(f"音频转换执行失败：{exc}") from exc

    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "").strip()
        raise AudioConversionError(f"音频转换失败：{error[:1000]}")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise AudioConversionError(f"音频转换未生成有效文件：{output_path}")

    return AudioConversionResult(source_path=source_path, audio_path=output_path, converted=True, reason="converted")
