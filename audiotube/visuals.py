"""Вибір фону з локальної бібліотеки або генерація запасного абстрактного фону.

Бібліотека:
    assets/images/<категорія>/*.jpg|png|webp   — для режиму image
    assets/loops/<категорія>/*.mp4|mov|webm    — для режиму loop

Якщо в імені файлу є слово з тегів/назви, такий файл отримує пріоритет.
Вибір детермінований (залежить від slug), щоб повторний рендер давав той самий результат.
"""

from __future__ import annotations

import colorsys
import hashlib
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from audiotube import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from audiotube.config import Config
from audiotube.metadata import VideoMeta, slugify


@dataclass
class Background:
    path: Path
    kind: str  # "image" | "video"
    source: str  # "explicit" | "library" | "generated"


def _files(folder: Path, exts: set[str]) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts)


def _score(path: Path, keywords: set[str]) -> int:
    name = slugify(path.stem)
    return sum(1 for k in keywords if k and k in name)


def _pick(candidates: list[Path], keywords: set[str], seed: str) -> Path | None:
    if not candidates:
        return None
    best = max(_score(p, keywords) for p in candidates)
    pool = [p for p in candidates if _score(p, keywords) == best]
    rng = random.Random(seed)
    return rng.choice(pool)


def choose_background(meta: VideoMeta, cfg: Config, slug: str, workdir: Path) -> Background:
    mode = meta.mode or cfg.visuals.default_mode

    if meta.background:
        p = cfg.path(meta.background)
        if not p.exists():
            raise FileNotFoundError(f"фон не знайдено: {p}")
        kind = "video" if p.suffix.lower() in VIDEO_EXTENSIONS else "image"
        return Background(p, kind, "explicit")

    category = meta.category or cfg.visuals.default_category
    keywords = (
        {slugify(t) for t in meta.tags}
        | {slugify(t) for t in meta.visual_keywords}
        | set(slugify(meta.title).split("-"))
    )
    keywords = {k for k in keywords if len(k) >= 3}

    if mode == "loop":
        clips = _files(cfg.assets / "loops" / category, VIDEO_EXTENSIONS) or _files(
            cfg.assets / "loops", VIDEO_EXTENSIONS
        )
        chosen = _pick(clips, keywords, slug)
        if chosen:
            return Background(chosen, "video", "library")
        # немає відеолупів — падаємо на картинку

    images = _files(cfg.assets / "images" / category, IMAGE_EXTENSIONS) or _files(
        cfg.assets / "images", IMAGE_EXTENSIONS
    )
    chosen = _pick(images, keywords, slug)
    if chosen:
        return Background(chosen, "image", "library")

    out = workdir / "background_generated.png"
    generate_abstract_background(out, seed=slug, size=(cfg.render.width, cfg.render.height))
    return Background(out, "image", "generated")


def generate_abstract_background(out: Path, seed: str, size: tuple[int, int] = (1920, 1080)) -> Path:
    """М'який абстрактний фон: темний градієнт + розмиті кольорові плями.
    Кольорова гама залежить від seed, тож у кожної теми свій відтінок."""
    w, h = size
    digest = hashlib.sha256(seed.encode()).digest()
    rng = random.Random(digest)
    base_hue = digest[0] / 255

    def rgb(hue: float, light: float, sat: float) -> tuple[int, int, int]:
        r, g, b = colorsys.hls_to_rgb(hue % 1, light, sat)
        return int(r * 255), int(g * 255), int(b * 255)

    # працюємо в меншій роздільності — швидше, а після розмиття різниці не видно
    sw, sh = w // 4, h // 4
    img = Image.new("RGB", (sw, sh))
    top, bottom = rgb(base_hue, 0.10, 0.5), rgb(base_hue + 0.08, 0.04, 0.6)
    draw = ImageDraw.Draw(img)
    for y in range(sh):
        t = y / max(sh - 1, 1)
        draw.line([(0, y), (sw, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))

    blobs = Image.new("RGB", (sw, sh), (0, 0, 0))
    bd = ImageDraw.Draw(blobs)
    for _ in range(7):
        r = rng.randint(sh // 5, sh // 2)
        cx, cy = rng.randint(0, sw), rng.randint(0, sh)
        hue = base_hue + rng.uniform(-0.12, 0.18)
        bd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=rgb(hue, rng.uniform(0.35, 0.55), 0.75))
    blobs = blobs.filter(ImageFilter.GaussianBlur(sh // 6))

    img = Image.blend(img, blobs, 0.55)
    img = img.resize((w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(2))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=95)
    return out
