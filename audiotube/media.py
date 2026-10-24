"""Утиліти навколо ffmpeg / ffprobe."""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise FFmpegError(
                f"{tool} не знайдено. Встановіть FFmpeg (Ubuntu/Debian: sudo apt install ffmpeg)."
            )


def run(cmd: list[str], desc: str = "ffmpeg", cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"{desc} завершився з помилкою:\n{tail}")


def probe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe не зміг прочитати {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def duration(path: Path) -> float:
    info = probe(path)
    d = info.get("format", {}).get("duration")
    if d is None:
        for s in info.get("streams", []):
            if s.get("duration"):
                d = s["duration"]
                break
    if d is None:
        raise FFmpegError(f"не вдалося визначити тривалість {path}")
    return float(d)


@functools.lru_cache(maxsize=4)
def pick_encoder(preference: str = "auto") -> str:
    """auto: пробує NVIDIA NVENC, інакше libx264."""
    if preference != "auto":
        return preference
    test = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-",
    ]
    try:
        ok = subprocess.run(test, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        ok = False
    return "h264_nvenc" if ok else "libx264"


def video_codec_args(encoder: str, crf: int) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(crf + 3), "-b:v", "0",
                "-maxrate", "8M", "-bufsize", "16M"]
    # maxrate: «шумні» лупи (частинки, фрактали) інакше дають гігабайтні файли
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-maxrate", "8M", "-bufsize", "16M"]


def fmt_time(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
