"""Завантаження config.yaml у типізовані налаштування."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class PathsCfg(BaseModel):
    inbox: str = "inbox"
    workspace: str = "workspace"
    output: str = "output"
    assets: str = "assets"
    data: str = "data"
    cache: str = "cache"


class RenderCfg(BaseModel):
    width: int = 1920
    height: int = 1080
    fps: int = 30
    encoder: Literal["auto", "libx264", "h264_nvenc"] = "auto"
    crf: int = 20
    audio_bitrate: str = "192k"
    segment_seconds: int = Field(60, ge=5)
    zoom: float = Field(0.08, ge=0, le=0.5)
    loop_pingpong: bool = False
    overlay_title: bool = True
    font: str | None = None
    # вшиті субтитри: окремий повільний прохід перекодування (для 20 хв — кілька хвилин)
    burn_subtitles: bool = False
    subtitle_font_size: int = Field(40, ge=12, le=120)
    subtitle_margin: int = Field(60, ge=0)


class VisualsCfg(BaseModel):
    default_mode: Literal["image", "loop"] = "image"
    default_category: str = "abstract"


class TranscribeCfg(BaseModel):
    # auto: faster-whisper -> whisper.cpp -> OpenAI-сумісний API -> помилка
    backend: Literal["auto", "faster-whisper", "whisper-cli", "openai", "none"] = "auto"
    model: str = "small"  # назва моделі faster-whisper: tiny/base/small/medium/large-v3
    language: str = "uk"
    device: str = "auto"  # auto | cpu | cuda
    compute_type: str = "int8"  # int8 (CPU) | float16 (GPU) | auto
    beam_size: int = Field(5, ge=1, le=10)
    vad: bool = True  # відсікання тиші — швидше і менше галюцинацій
    model_dir: str | None = None  # куди завантажувати моделі; null = ~/.cache/huggingface
    # whisper.cpp
    command: str | None = None  # напр. whisper-cli; null = автопошук
    model_path: str | None = None  # шлях до ggml-моделі для whisper.cpp
    # OpenAI-сумісний API (OpenAI, Groq, локальний сервер)
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    timeout: int = Field(600, ge=10)
    # субтитри
    max_chars: int = Field(42, ge=20, le=120)  # символів у рядку субтитра
    max_lines: int = Field(2, ge=1, le=4)


class LLMCfg(BaseModel):
    provider: Literal["ollama", "openai", "anthropic", "none"] = "ollama"
    model: str = "qwen2.5:7b"
    base_url: str | None = None  # null = типова адреса провайдера
    api_key_env: str | None = None  # null = OPENAI_API_KEY / ANTHROPIC_API_KEY
    temperature: float = Field(0.4, ge=0, le=2)
    timeout: int = Field(180, ge=10)
    max_tokens: int = Field(4096, ge=256)
    retries: int = Field(1, ge=0, le=5)
    language: str = "uk"


class PlanCfg(BaseModel):
    known_playlists: list[str] = Field(default_factory=list)
    default_playlist: str | None = None
    min_chapters: int = Field(4, ge=1, le=30)
    max_chapters: int = Field(10, ge=1, le=30)
    max_transcript_chars: int = Field(24000, ge=2000)


class PipelineCfg(BaseModel):
    auto_transcribe: bool = True
    auto_describe: bool = True
    auto_render: bool = True


class YouTubeCfg(BaseModel):
    client_secret: str = "data/client_secret.json"
    token: str = "data/token.json"
    default_privacy: Literal["private", "unlisted", "public"] = "private"
    playlist_privacy: Literal["private", "unlisted", "public"] = "public"
    category_id: str = "27"
    language: str = "uk"
    made_for_kids: bool = False
    contains_synthetic_media: bool = True
    daily_quota: int = 10000
    upload_cost: int = 1600
    upload_captions: bool = True
    captions_language: str = "uk"
    captions_name: str = "Українська"
    captions_cost: int = 400
    auto_upload: bool = False


class DescriptionCfg(BaseModel):
    footer: str = ""
    default_tags: list[str] = Field(default_factory=list)


class Config(BaseModel):
    paths: PathsCfg = Field(default_factory=PathsCfg)
    render: RenderCfg = Field(default_factory=RenderCfg)
    visuals: VisualsCfg = Field(default_factory=VisualsCfg)
    transcribe: TranscribeCfg = Field(default_factory=TranscribeCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    plan: PlanCfg = Field(default_factory=PlanCfg)
    pipeline: PipelineCfg = Field(default_factory=PipelineCfg)
    youtube: YouTubeCfg = Field(default_factory=YouTubeCfg)
    description: DescriptionCfg = Field(default_factory=DescriptionCfg)

    # корінь проєкту, від якого рахуються відносні шляхи (не з YAML)
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    def path(self, value: str | Path) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.root / p)

    @property
    def inbox(self) -> Path:
        return self.path(self.paths.inbox)

    @property
    def workspace(self) -> Path:
        return self.path(self.paths.workspace)

    @property
    def output(self) -> Path:
        return self.path(self.paths.output)

    @property
    def assets(self) -> Path:
        return self.path(self.paths.assets)

    @property
    def data(self) -> Path:
        return self.path(self.paths.data)

    @property
    def cache(self) -> Path:
        return self.path(self.paths.cache)

    @property
    def db_path(self) -> Path:
        return self.data / "audiotube.db"

    def ensure_dirs(self) -> None:
        for d in (self.inbox, self.workspace, self.output, self.data, self.cache):
            d.mkdir(parents=True, exist_ok=True)
        for kind in ("images", "loops"):
            (self.assets / kind / self.visuals.default_category).mkdir(parents=True, exist_ok=True)
        (self.assets / "fonts").mkdir(parents=True, exist_ok=True)


def load_dotenv(path: Path) -> None:
    """Читає .env (KEY=VALUE) і додає змінні, яких ще немає в оточенні."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: str | Path | None = None, root: Path | None = None) -> Config:
    """Читає config.yaml. Якщо файлу немає, повертає значення за замовчуванням."""
    root = (root or Path.cwd()).resolve()
    cfg_path = Path(path) if path else root / "config.yaml"
    data: dict = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        root = cfg_path.resolve().parent
    load_dotenv(root / ".env")
    cfg = Config.model_validate(data)
    cfg.root = root
    return cfg
