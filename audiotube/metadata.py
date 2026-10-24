"""Метадані відео: назва, опис, теги, плейлист, розділи.

Джерела (за пріоритетом):
1. файл-компаньйон поруч з аудіо: `<назва_аудіо>.yaml`
2. значення за замовчуванням, виведені з імені файлу
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from audiotube.config import Config

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu",
    "я": "ia", "ё": "e", "ы": "y", "э": "e", "ъ": "", "'": "", "’": "",
}

TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500


def slugify(text: str, max_len: int = 60) -> str:
    out = "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return (out[:max_len].rstrip("-")) or "video"


def title_from_filename(path: Path) -> str:
    name = re.sub(r"[_\-]+", " ", path.stem).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:1].upper() + name[1:] if name else "Без назви"


class Chapter(BaseModel):
    time: str  # "00:00" або "1:02:03"
    title: str

    @field_validator("time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        if not re.fullmatch(r"(\d{1,2}:)?\d{1,2}:\d{2}", v.strip()):
            raise ValueError(f"неправильний таймкод розділу: {v!r} (очікується ММ:СС або Г:ММ:СС)")
        return v.strip()


class VideoMeta(BaseModel):
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    playlist: str | None = None
    chapters: list[Chapter] = Field(default_factory=list)

    # візуал
    mode: Literal["image", "loop"] | None = None
    category: str | None = None
    background: str | None = None  # явний шлях до картинки або відео
    thumbnail_text: str | None = None
    visual_keywords: list[str] = Field(default_factory=list)

    # публікація
    privacy: Literal["private", "unlisted", "public"] | None = None
    publish_at: datetime | None = None  # відкладена публікація (тільки з privacy=private)

    @field_validator("chapters", mode="before")
    @classmethod
    def _parse_chapters(cls, v):
        # дозволяємо скорочений запис: ["00:00 Вступ", "03:15 Основи"]
        if v is None:
            return []
        if isinstance(v, list):
            parsed = []
            for item in v:
                if isinstance(item, str):
                    t, _, rest = item.strip().partition(" ")
                    parsed.append({"time": t, "title": rest.strip()})
                else:
                    parsed.append(item)
            return parsed
        return v

    @field_validator("tags", "visual_keywords", mode="before")
    @classmethod
    def _parse_tags(cls, v):
        if v is None:
            return []
        if isinstance(v, str):  # "docker, devops"
            return [t.strip() for t in v.split(",") if t.strip()]
        return v

    @field_validator("description", mode="before")
    @classmethod
    def _none_description(cls, v):
        return v or ""

    def youtube_title(self) -> str:
        t = re.sub(r"[<>]", "", self.title).strip()
        return t[:TITLE_MAX]

    def youtube_tags(self, defaults: list[str]) -> list[str]:
        seen, out, total = set(), [], 0
        for tag in [*self.tags, *defaults]:
            tag = re.sub(r"[<>,]", "", tag).strip()
            if not tag or tag.lower() in seen:
                continue
            cost = len(tag) + (2 if " " in tag else 0) + (1 if out else 0)
            if total + cost > TAGS_MAX_CHARS:
                break
            seen.add(tag.lower())
            out.append(tag)
            total += cost
        return out

    def youtube_description(self, footer: str = "") -> str:
        parts = []
        if self.description.strip():
            parts.append(self.description.strip())
        if self.chapters:
            chapters = list(self.chapters)
            # YouTube розпізнає розділи, лише якщо перший починається з 0:00
            if not re.fullmatch(r"0?0:00|0:00:00", chapters[0].time):
                chapters.insert(0, Chapter(time="00:00", title="Вступ"))
            parts.append("Зміст:\n" + "\n".join(f"{c.time} {c.title}" for c in chapters))
        if footer.strip():
            parts.append(footer.strip())
        text = re.sub(r"[<>]", "", "\n\n".join(parts))
        encoded = text.encode("utf-8")
        if len(encoded) > DESCRIPTION_MAX_BYTES:
            text = encoded[:DESCRIPTION_MAX_BYTES].decode("utf-8", errors="ignore")
        return text


def sidecar_path(audio: Path) -> Path:
    return audio.with_suffix(".yaml")


def load_meta(audio: Path) -> VideoMeta:
    side = sidecar_path(audio)
    if not side.exists():
        return VideoMeta(title=title_from_filename(audio))
    try:
        data = yaml.safe_load(side.read_text(encoding="utf-8")) or {}
        if not data.get("title"):
            data["title"] = title_from_filename(audio)
        return VideoMeta.model_validate(data)
    except (yaml.YAMLError, ValueError) as e:
        raise MetadataError(f"помилка у файлі {side.name}: {e}") from e


class MetadataError(RuntimeError):
    pass


SIDECAR_TEMPLATE = """\
# Метадані для: {audio}
# Усі поля, крім title, необов'язкові.

title: "{title}"

description: |
  Короткий опис: про що цей розбір і що глядач зрозуміє після перегляду.

tags: []

# Плейлист на YouTube (створиться автоматично, якщо його ще немає)
playlist: null

# Розділи (з'являться в описі та як chapters на YouTube)
chapters:
  # - "00:00 Вступ"
  # - "03:10 Основна ідея"

# Візуал
mode: {mode}              # image | loop
category: {category}      # папка в assets/images/ або assets/loops/
background: null          # або явний шлях до картинки/відео
thumbnail_text: null      # короткий текст для обкладинки (2-4 слова)
visual_keywords: []       # ключові слова для підбору фону (заповнює LLM)

# Публікація
privacy: null             # private | unlisted | public (null = з конфігу)
publish_at: null          # напр. 2026-10-25T18:00:00+03:00 (відео буде private до цього часу)
"""


def write_sidecar_template(audio: Path, cfg: Config, overwrite: bool = False) -> Path:
    side = sidecar_path(audio)
    if side.exists() and not overwrite:
        return side
    side.write_text(
        SIDECAR_TEMPLATE.format(
            audio=audio.name,
            title=title_from_filename(audio).replace('"', "'"),
            mode=cfg.visuals.default_mode,
            category=cfg.visuals.default_category,
        ),
        encoding="utf-8",
    )
    return side


# --- запис метаданих, згенерованих LLM ---------------------------------------
def _yaml_scalar(value: str | None) -> str:
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


def _yaml_block(text: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join(f"  {line}" if line.strip() else "" for line in lines)


def dump_sidecar(meta: VideoMeta, header: str = "") -> str:
    """Людиночитний YAML, який коректно читається назад через load_meta."""
    lines: list[str] = []
    for line in header.splitlines():
        lines.append(f"# {line}" if line.strip() else "#")
    if lines:
        lines.append("")
    lines.append(f"title: {_yaml_scalar(meta.title)}")
    if meta.description.strip():
        lines.append("description: |-")
        lines.append(_yaml_block(meta.description.strip()))
    else:
        lines.append('description: ""')
    lines.append("tags:" if meta.tags else "tags: []")
    lines.extend(f"  - {_yaml_scalar(tag)}" for tag in meta.tags)
    lines.append(f"playlist: {_yaml_scalar(meta.playlist)}")
    if meta.chapters:
        lines.append("chapters:")
        lines.extend(f"  - {_yaml_scalar(f'{c.time} {c.title}')}" for c in meta.chapters)
    else:
        lines.append("chapters: []")
    if meta.visual_keywords:
        lines.append("visual_keywords:")
        lines.extend(f"  - {_yaml_scalar(k)}" for k in meta.visual_keywords)
    else:
        lines.append("visual_keywords: []")
    lines.append(f"thumbnail_text: {_yaml_scalar(meta.thumbnail_text)}")
    lines.append("")
    lines.append("# Візуал")
    lines.append(f"mode: {_yaml_scalar(meta.mode)}")
    lines.append(f"category: {_yaml_scalar(meta.category)}")
    lines.append(f"background: {_yaml_scalar(meta.background)}")
    lines.append("")
    lines.append("# Публікація")
    lines.append(f"privacy: {_yaml_scalar(meta.privacy)}")
    lines.append(f"publish_at: {_yaml_scalar(meta.publish_at.isoformat() if meta.publish_at else None)}")
    return "\n".join(lines) + "\n"


def write_sidecar(audio: Path, meta: VideoMeta, header: str = "") -> Path:
    side = sidecar_path(audio)
    side.write_text(dump_sidecar(meta, header), encoding="utf-8")
    return side


def merge_generated_meta(existing: VideoMeta, generated: VideoMeta) -> VideoMeta:
    """Згенерований контент + ручні налаштування (візуал, публікація) з наявного файлу."""
    return generated.model_copy(
        update={
            "mode": existing.mode,
            "category": existing.category,
            "background": existing.background,
            "privacy": existing.privacy,
            "publish_at": existing.publish_at,
            "thumbnail_text": existing.thumbnail_text or generated.thumbnail_text,
        }
    )
