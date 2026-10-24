"""Генерація метаданих відео з транскрипту через LLM.

LLM повертає JSON, який валідується і «лагодиться» (таймкоди, порядок глав, ліміти YouTube),
тому навіть недосконала відповідь не ламає конвеєр.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from audiotube import llm
from audiotube.config import Config
from audiotube.metadata import Chapter, VideoMeta
from audiotube.transcript import Transcript, clock, transcript_for_prompt

TITLE_MAX = 100
THUMBNAIL_TEXT_MAX = 40
TAGS_MAX = 15


class PlanError(RuntimeError):
    pass


class PlanChapter(BaseModel):
    time: str
    title: str

    @field_validator("time", mode="before")
    @classmethod
    def _normalise(cls, v: Any) -> str:
        seconds = parse_timecode(str(v))
        if seconds is None:
            raise ValueError(f"некоректний таймкод: {v!r}")
        return clock(seconds)

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v: Any) -> str:
        return re.sub(r"\s+", " ", str(v or "")).strip()


class ContentPlan(BaseModel):
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    chapters: list[PlanChapter] = Field(default_factory=list)
    playlist: str | None = None
    visual_keywords: list[str] = Field(default_factory=list)
    thumbnail_text: str | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _clean_title(cls, v: Any) -> str:
        text = re.sub(r"\s+", " ", str(v or "")).strip().strip("\"'«»")
        return text[:TITLE_MAX]

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("tags", "visual_keywords", mode="before")
    @classmethod
    def _clean_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = re.split(r"[,;\n]", v)
        out: list[str] = []
        for item in v:
            tag = str(item).strip().lstrip("#").strip()
            if tag and tag.lower() not in {t.lower() for t in out}:
                out.append(tag)
        return out

    @field_validator("chapters", mode="before")
    @classmethod
    def _parse_chapters(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, list):
            out = []
            for item in v:
                if isinstance(item, str):
                    match = re.match(r"^\s*([0-9]{1,2}(?::[0-9]{1,2}){0,2})\s*[-–—:]?\s*(.+)$", item)
                    if not match:
                        continue
                    out.append({"time": match.group(1), "title": match.group(2).strip()})
                else:
                    out.append(item)
            return out
        return v

    @field_validator("playlist", "thumbnail_text", mode="before")
    @classmethod
    def _clean_optional(cls, v: Any) -> str | None:
        text = re.sub(r"\s+", " ", str(v or "")).strip().strip("\"'«»")
        return text or None


def parse_timecode(value: str) -> float | None:
    """'00:00', '1:02:03', '02:40', '2.40' → секунди."""
    value = value.strip().replace(",", ".")
    value = re.sub(r"[^\d:.]", "", value)
    if not value:
        return None
    parts = value.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])  # "40" — це 40 секунд; хвилини пишуться як "0:40"
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None
    return None


def sanitize_chapters(chapters: list[PlanChapter], duration: float) -> list[Chapter]:
    """Сортує, прибирає дублікати й вихід за межі, гарантує початок з 00:00."""
    parsed: list[tuple[float, str]] = []
    for chapter in chapters:
        seconds = parse_timecode(chapter.time)
        if seconds is None or not chapter.title:
            continue
        seconds = max(0.0, min(seconds, max(duration - 1, 0)))
        parsed.append((seconds, chapter.title[:100]))
    parsed.sort(key=lambda item: item[0])

    unique: list[tuple[float, str]] = []
    for seconds, title in parsed:
        if unique and abs(seconds - unique[-1][0]) < 5:
            continue  # занадто близькі глави YouTube не покаже
        unique.append((seconds, title))
    if not unique or unique[0][0] > 1:
        unique.insert(0, (0.0, "Вступ"))
    return [Chapter(time=clock(s), title=t) for s, t in unique]


def extract_json(text: str) -> dict:
    """Витягує JSON-об'єкт із відповіді LLM (може бути в ```json-блоці або з текстом навколо)."""
    if not text or not text.strip():
        raise PlanError("LLM повернув порожню відповідь")
    candidates = [text.strip()]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
    candidates.extend(block.strip() for block in fenced)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise PlanError("у відповіді LLM не знайдено JSON-об'єкт")


def parse_plan(text: str, cfg: Config, duration: float, fallback_title: str) -> ContentPlan:
    data = extract_json(text)
    try:
        plan = ContentPlan.model_validate(data)
    except ValidationError as e:
        raise PlanError(f"відповідь не відповідає схемі: {e}") from e

    plan.title = plan.title or fallback_title
    plan.tags = plan.tags[:TAGS_MAX]
    plan.chapters = [
        PlanChapter(time=c.time, title=c.title)
        for c in sanitize_chapters(plan.chapters, duration)
    ]
    plan.playlist = plan.playlist or cfg.plan.default_playlist
    if plan.thumbnail_text:
        plan.thumbnail_text = plan.thumbnail_text[:THUMBNAIL_TEXT_MAX]
    return plan


SYSTEM_PROMPT = """\
Ти — редактор українського освітнього YouTube-каналу, де виходять розбори складних тем \
тривалістю ~20 хвилин. Твоє завдання — з транскрипту аудіо зробити метадані відео.

Поверни ЛИШЕ валідний JSON-об'єкт (без markdown, без коментарів) з полями:
{
  "title": "назва до 100 символів, з гачком і конкретикою, без клікбейту",
  "description": "3–5 речень: про що відео і що глядач зрозуміє. Без списку розділів і без таймкодів.",
  "tags": ["5–12 коротких тегів українською, без #"],
  "chapters": [{"time": "00:00", "title": "коротка назва розділу"}],
  "playlist": "назва плейлиста",
  "visual_keywords": ["3–6 англійських слів для пошуку фонового відео"],
  "thumbnail_text": "2–4 слова для обкладинки"
}

Правила:
- Пиши українською, природно, без «води» і без звертання до глядача в описі.
- Глави: від 4 до 10, перша завжди 00:00, таймкоди MM:SS у межах тривалості, по змістовних переходах.
- Плейлист: якщо у списку відомих є доречний — використай його назву дослівно; інакше запропонуй нову \
коротку назву в стилі «Тема за 20 хвилин».
- visual_keywords і thumbnail_text — саме те, що допоможе підібрати фон і зробити обкладинку.
"""


def build_messages(
    transcript: Transcript, cfg: Config, fallback_title: str, known_playlists: list[str] | None = None
) -> list[dict[str, str]]:
    playlists = known_playlists if known_playlists is not None else cfg.plan.known_playlists
    prompt = transcript_for_prompt(transcript, max_chars=cfg.plan.max_transcript_chars)
    user = (
        f"Файл: {fallback_title}\n"
        f"Тривалість: {clock(transcript.duration)}\n"
        f"Відомі плейлисти: {', '.join(playlists) if playlists else '(немає)'}\n\n"
        f"Транскрипт (таймкоди у квадратних дужках):\n{prompt}"
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def generate_plan(
    transcript: Transcript,
    cfg: Config,
    fallback_title: str,
    known_playlists: list[str] | None = None,
    client: llm.LLMClient | None = None,
) -> ContentPlan:
    """Запитує LLM, валідує відповідь і за потреби просить виправлення."""
    client = client or llm.build_client(cfg.llm)
    messages = build_messages(transcript, cfg, fallback_title, known_playlists)
    chapters_rule = f"від {cfg.plan.min_chapters} до {cfg.plan.max_chapters}"
    messages[0]["content"] = messages[0]["content"].replace("від 4 до 10", chapters_rule)
    last_error: Exception | None = None

    for attempt in range(cfg.llm.retries + 1):
        reply = client.complete(messages, json_mode=True)
        try:
            return parse_plan(reply, cfg, transcript.duration, fallback_title)
        except PlanError as e:
            last_error = e
            if attempt == cfg.llm.retries:
                break
            messages = [
                *messages,
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": (
                        f"Твоя відповідь не пройшла перевірку: {e}. "
                        "Поверни виправлений валідний JSON-об'єкт із тими самими полями, без пояснень."
                    ),
                },
            ]
    raise PlanError(f"LLM не зміг згенерувати коректні метадані: {last_error}")


def plan_to_meta(plan: ContentPlan) -> VideoMeta:
    """Переносить згенерований план у метадані відео (без візуальних/публікаційних полів)."""
    return VideoMeta(
        title=plan.title,
        description=plan.description,
        tags=plan.tags,
        playlist=plan.playlist,
        chapters=[Chapter(time=c.time, title=c.title) for c in plan.chapters],
        thumbnail_text=plan.thumbnail_text,
        visual_keywords=plan.visual_keywords,
    )
