"""Модель транскрипту, субтитри SRT і текст для LLM.

Один формат для всіх бекендів транскрипції: `transcript.json` у папці задачі.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

MIN_CUE_SECONDS = 1.0
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


def _tokens(text: str, max_chars: int) -> list[str]:
    """Слова тексту; слово, довше за max_chars, ріжеться на шматки (інакше рядок не вмістити)."""
    words: list[str] = []
    for word in re.sub(r"\s+", " ", text).strip().split(" "):
        if word:
            words.extend(_split_long_word(word, max_chars) if len(word) > max_chars else [word])
    return words


def wrap_text(text: str, max_chars: int = 42) -> list[str]:
    """Розбиває текст на рядки не довші за max_chars (слова не рвуться без потреби)."""
    return _greedy_lines(_tokens(text, max_chars), max_chars)


def _greedy_lines(words: list[str], max_chars: int) -> list[str]:
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


# --- правила субтитрів (spec 009) --------------------------------------------
@dataclass(frozen=True)
class SubtitleRules:
    max_chars: int = 42
    max_lines: int = 2
    max_seconds: float = 7.0  # SUB-R1
    min_seconds: float = MIN_CUE_SECONDS  # SUB-R2
    max_cps: float = 17.0  # SUB-R3, символів (без пробілів) за секунду
    merge_gap: float = 0.3  # SUB-R6

    @classmethod
    def from_config(cls, cfg) -> "SubtitleRules":
        t = cfg.transcribe
        return cls(
            max_chars=t.max_chars,
            max_lines=t.max_lines,
            max_seconds=float(t.max_cue_seconds),
            min_seconds=float(t.min_cue_seconds),
            max_cps=float(t.max_cps),
        )


_SENTENCE_END = re.compile(r"[.!?…]+[\"»)\]]*$")
_CLAUSE_END = re.compile(r"[,;:]+[\"»)\]]*$|[—–]$")
_DASHES = {"—", "–", "-"}
_BONUS = {2: 0.35, 1: 0.2, 0: 0.0}  # перевага точки поділу над серединою (SUB-R4)
_PUNCT_SLACK = 1.3  # SUB-R5: розрив на розділовому знаку, якщо довший рядок ≤ 130 % найкращого


def _break_level(words: list[str], i: int) -> int:
    """Рівень розриву ПІСЛЯ words[i]: 2 — кінець речення, 1 — межа фрази, 0 — просто пробіл."""
    word = words[i]
    if _SENTENCE_END.search(word):
        return 2
    if _CLAUSE_END.search(word) or (i + 1 < len(words) and words[i + 1] in _DASHES):
        return 1
    return 0


def _layout_lines(words: list[str], rules: SubtitleRules) -> tuple[str, ...] | None:
    """Рядки титра (SUB-R5, R9) або None, якщо текст не вміщається в max_lines."""
    text = " ".join(words)
    if len(text) <= rules.max_chars:
        return (text,)
    greedy = _greedy_lines(words, rules.max_chars)
    if len(greedy) > rules.max_lines:
        return None
    if len(greedy) > 2:
        return tuple(greedy)
    options = []  # (довший рядок, чи після розділового знака, індекс розриву)
    for b in range(1, len(words)):
        left, right = " ".join(words[:b]), " ".join(words[b:])
        if len(left) <= rules.max_chars and len(right) <= rules.max_chars:
            options.append((max(len(left), len(right)), _break_level(words, b - 1) > 0, b))
    best = min(longer for longer, _, _ in options)
    punct = [o for o in options if o[1] and o[0] <= best * _PUNCT_SLACK]
    if punct:
        _, _, b = min(punct, key=lambda o: (o[0], o[2]))
    else:
        _, _, b = min(options, key=lambda o: (o[0], not o[1], o[2]))
    return (" ".join(words[:b]), " ".join(words[b:]))


def _split_words(words: list[str], seconds_per_char: float, rules: SubtitleRules) -> list[list[str]]:
    """Ділить слова сегмента на титри: вміщаються в рядки і в max_seconds (SUB-R1, R4).

    Рекурсивно ріжемо в найкращій точці: найвищий рівень розриву, ближче до середини.
    """
    pieces: list[list[str]] = []
    stack = [words]
    while stack:
        part = stack.pop()
        text_len = len(" ".join(part))
        fits = (text_len + 1) * seconds_per_char <= rules.max_seconds + 1e-9 and _layout_lines(part, rules)
        if fits or len(part) == 1:
            pieces.append(part)
            continue
        best_b, best_score, left_len = 1, None, -1
        for b in range(1, len(part)):
            left_len += len(part[b - 1]) + 1
            score = _BONUS[_break_level(part, b - 1)] - abs(left_len / text_len - 0.5)
            if best_score is None or score > best_score + 1e-12:
                best_b, best_score = b, score
        stack.append(part[best_b:])  # стек: права частина обробиться після лівої
        stack.append(part[:best_b])
    return pieces


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass(frozen=True)
class CueLayout:
    cues: list[Cue]
    dense: int = 0  # титри, швидкість читання яких лишилася > max_cps (SUB-R3)


def _visible_chars(lines: tuple[str, ...]) -> int:
    return sum(len(line.replace(" ", "")) for line in lines)


def layout_cues(
    segments: list[Segment], rules: SubtitleRules | None = None, duration: float | None = None
) -> CueLayout:
    """Сегменти → титри за правилами spec 009. duration — межа для останнього титра."""
    rules = rules or SubtitleRules()
    raw: list[tuple[float, float, tuple[str, ...]]] = []
    for seg in segments:
        start, end = float(seg.start), float(seg.end)
        words = _tokens(seg.text, rules.max_chars)
        if end <= start or not words:
            continue
        per_char = (end - start) / (len(" ".join(words)) + 1)
        cursor = start
        for piece in _split_words(words, per_char, rules):
            lines = _layout_lines(piece, rules) or tuple(_greedy_lines(piece, rules.max_chars))
            span = (len(" ".join(piece)) + 1) * per_char
            raw.append((cursor, cursor + span, lines))
            cursor += span
    raw.sort(key=lambda r: r[0])

    # вирівнювання часу: SUB-R1 (стеля), R2 (мінімум), R3 (швидкість читання), R6 (короткі паузи)
    cues: list[Cue] = []
    dense = 0
    for i, (start, end, lines) in enumerate(raw):
        cap = start + rules.max_seconds
        if i + 1 < len(raw):
            limit = raw[i + 1][0]
        elif duration is not None:
            limit = max(duration, start)
        else:
            limit = max(end, start + rules.min_seconds)  # без тривалості аудіо — як раніше
        chars = _visible_chars(lines)
        want = max(min(end, cap), start + rules.min_seconds, start + chars / rules.max_cps)
        want = min(want, cap, limit)
        if i + 1 < len(raw) and 0 < limit - want < rules.merge_gap and limit <= cap:
            want = limit
        start_r = round(start, 3)
        # вгору — щоб округлення не зробило титр «щільним»; але не за наступний титр і не за стелю
        end_r = min(
            math.ceil(want * 1000 - 1e-6) / 1000, round(limit, 3), round(start_r + rules.max_seconds, 3)
        )
        if end_r - start_r < 0.001:
            continue  # наступний титр починається там само (перекриті сегменти)
        if chars / (end_r - start_r) > rules.max_cps + 1e-6:
            dense += 1
        cues.append(Cue(start_r, end_r, lines))
    return CueLayout(cues, dense)


def build_cues(
    segments: list[Segment],
    max_chars: int = 42,
    max_lines: int = 2,
    min_seconds: float = MIN_CUE_SECONDS,
    rules: SubtitleRules | None = None,
) -> list[Cue]:
    """Сегменти → титри (сумісна обгортка над layout_cues)."""
    rules = rules or SubtitleRules(max_chars=max_chars, max_lines=max_lines, min_seconds=min_seconds)
    return layout_cues(segments, rules).cues


def to_srt(cues: list[Cue]) -> str:
    blocks = [
        f"{i}\n{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}\n{cue.text}\n"
        for i, cue in enumerate(cues, start=1)
    ]
    return "\n".join(blocks)


def render_srt(transcript: Transcript, rules: SubtitleRules | None = None) -> tuple[str, CueLayout]:
    """SRT-текст і розкладка (для журналу «щільних» титрів)."""
    layout = layout_cues(transcript.segments, rules, duration=transcript.duration or None)
    return to_srt(layout.cues), layout


def write_srt(
    transcript: Transcript,
    path: Path,
    max_chars: int = 42,
    max_lines: int = 2,
    rules: SubtitleRules | None = None,
) -> Path:
    rules = rules or SubtitleRules(max_chars=max_chars, max_lines=max_lines)
    text, _ = render_srt(transcript, rules)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
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
