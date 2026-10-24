"""Завантаження config.yaml у типізовані налаштування."""

from __future__ import annotations

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


class VisualsCfg(BaseModel):
    default_mode: Literal["image", "loop"] = "image"
    default_category: str = "abstract"


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
    auto_upload: bool = False


class DescriptionCfg(BaseModel):
    footer: str = ""
    default_tags: list[str] = Field(default_factory=list)


class Config(BaseModel):
    paths: PathsCfg = Field(default_factory=PathsCfg)
    render: RenderCfg = Field(default_factory=RenderCfg)
    visuals: VisualsCfg = Field(default_factory=VisualsCfg)
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
    def db_path(self) -> Path:
        return self.data / "audiotube.db"

    def ensure_dirs(self) -> None:
        for d in (self.inbox, self.workspace, self.output, self.data):
            d.mkdir(parents=True, exist_ok=True)
        for kind in ("images", "loops"):
            (self.assets / kind / self.visuals.default_category).mkdir(parents=True, exist_ok=True)
        (self.assets / "fonts").mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path | None = None, root: Path | None = None) -> Config:
    """Читає config.yaml. Якщо файлу немає, повертає значення за замовчуванням."""
    root = (root or Path.cwd()).resolve()
    cfg_path = Path(path) if path else root / "config.yaml"
    data: dict = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        root = cfg_path.resolve().parent
    cfg = Config.model_validate(data)
    cfg.root = root
    return cfg
