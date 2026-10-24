"""Модель транскрипту, субтитри SRT і текст для LLM.

Один формат для всіх бекендів транскрипції: `transcript.json` у папці задачі.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MIN_CUE_SECONDS = 0.8
MAX_PROMPT_CHARS = 24000


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    language: str
    duration: float
    segments: list[Segment] = field(default_factory=list)
    backend: str = ""
    model: str = ""

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "duration": self.duration,
            "backend": self.backend,
            "model": self.model,
            "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        segments = [
            Segment(float(s["start"]), float(s["end"]), str(s.get("text", "")).strip())
            for s in data.get("segments", [])
        ]
        duration = float(data.get("duration") or (segments[-1].end if segments else 0.0))
        return cls(
            language=str(data.get("language") or "uk"),
            duration=duration,
            segments=segments,
            backend=str(data.get("backend") or ""),
            model=str(data.get("model") or ""),
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "Transcript":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def clock(seconds: float) -> str:
    """MM:SS або H:MM:SS — для таймкодів розділів і промптів."""
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _split_long_word(word: str, max_chars: int) -> list[str]:
    return [word[i:i + max_chars] for i in range(0, len(word), max_chars)] or [word]


def wrap_text(text: str, max_chars: int = 42) -> list[str]:
    """Розбиває текст на рядки не довші за max_chars (слова не рвуться без потреби)."""
    words: list[str] = []
    for word in re.sub(r"\s+", " ", text).strip().split(" "):
        if not word:
            continue
        words.extend(_split_long_word(word, max_chars) if len(word) > max_chars else [word])
    if not words:
        return []
    lines, current = [], words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= max_chars:
            current += " " + word
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def build_cues(
    segments: list[Segment], max_chars: int = 42, max_lines: int = 2, min_seconds: float = MIN_CUE_SECONDS
) -> list[Cue]:
    """Перетворює сегменти у субтитри: не більше max_lines рядків, час — пропорційно тексту."""
    raw: list[tuple[float, float, list[str]]] = []
    for seg in segments:
        start, end = float(seg.start), float(seg.end)
        if end <= start or not seg.text.strip():
            continue
        lines = wrap_text(seg.text, max_chars)
        if not lines:
            continue
        chunks = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        weights = [sum(len(line) for line in chunk) for chunk in chunks]
        total = sum(weights) or 1
        cursor = start
        for chunk, weight in zip(chunks, weights):
            duration = (end - start) * weight / total
            raw.append((cursor, cursor + duration, chunk))
            cursor += duration

    # вирівнювання: мінімальна тривалість, без перекриттів і без «нульових» титрів
    cues: list[Cue] = []
    for i, (start, end, lines) in enumerate(raw):
        next_start = raw[i + 1][0] if i + 1 < len(raw) else None
        end = max(end, start + min_seconds)
        if next_start is not None:
            end = min(end, next_start)
        if end - start < 0.05:
            continue
        cues.append(Cue(round(start, 3), round(end, 3), tuple(lines)))
    return cues


def to_srt(cues: list[Cue]) -> str:
    blocks = [
        f"{i}\n{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}\n{cue.text}\n"
        for i, cue in enumerate(cues, start=1)
    ]
    return "\n".join(blocks)


def write_srt(transcript: Transcript, path: Path, max_chars: int = 42, max_lines: int = 2) -> Path:
    cues = build_cues(transcript.segments, max_chars=max_chars, max_lines=max_lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_srt(cues), encoding="utf-8")
    return path


def transcript_for_prompt(
    transcript: Transcript, block_seconds: int = 45, max_chars: int = MAX_PROMPT_CHARS
) -> str:
    """Текст із таймкодами, згрупований у блоки ~block_seconds: компактно для LLM і дає опори для глав."""
    lines: list[str] = []
    block_start: float | None = None
    buffer: list[str] = []
    last_end = 0.0

    def flush() -> None:
        if buffer and block_start is not None:
            lines.append(f"[{clock(block_start)}] {' '.join(buffer)}")

    for seg in transcript.segments:
        if not seg.text.strip():
            continue
        if block_start is None or seg.start - block_start >= block_seconds:
            flush()
            block_start, buffer = seg.start, [seg.text.strip()]
        else:
            buffer.append(seg.text.strip())
        last_end = seg.end
    flush()
    if block_start is not None and last_end:
        lines.append(f"[{clock(last_end)}] (кінець)")

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # обрізаємо по межі рядка, щоб не розірвати речення
    cut = text.rfind("\n", 0, max_chars)
    return text[: cut if cut > max_chars * 0.5 else max_chars] + "\n[…транскрипт скорочено]"
