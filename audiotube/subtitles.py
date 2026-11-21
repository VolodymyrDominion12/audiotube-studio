"""Перебудова `subtitles.srt` з наявного `transcript.json` (spec 009, SUB-R11…R15).

Без транскрипції й без YouTube: доріжки вже опублікованих відео не оновлюються
(рішення spec 009 §9), нові відео отримують новий SRT через звичайний `upload`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from audiotube.config import Config
from audiotube.db import Database, Job
from audiotube.transcribe import SUBTITLES_NAME, TRANSCRIPT_NAME
from audiotube.transcript import SubtitleRules, Transcript, render_srt

ACTION_UA = {
    "create": ("створити", "створено"),
    "update": ("оновити", "оновлено"),
    "unchanged": ("без змін", "без змін"),
    "exists": ("є", "є"),
    "skip": ("пропуск", "пропущено"),
    "missing": ("не знайдено", "не знайдено"),
}


@dataclass
class SubtitlesItem:
    job_id: int
    action: str  # create | update | unchanged | exists | skip | missing
    before: int | None = None  # титрів у наявному SRT
    after: int | None = None  # титрів за новими правилами
    dense: int = 0
    backup: Path | None = None
    published: bool = False  # відео вже на YouTube — доріжку там не оновлено


@dataclass
class SubtitlesReport:
    items: list[SubtitlesItem] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if any(i.action == "missing" for i in self.items) else 0


def _count_cues(srt_text: str) -> int:
    return sum(1 for line in srt_text.splitlines() if "-->" in line)


def _backup_path(srt: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = srt.with_name(f"subtitles.{stamp}.srt.bak")
    n = 1
    while candidate.exists():  # кілька --force за одну секунду
        candidate = srt.with_name(f"subtitles.{stamp}-{n}.srt.bak")
        n += 1
    return candidate


def _atomic_write(path: Path, text: str) -> None:
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


def rebuild_one(cfg: Config, db: Database, job: Job, *, force: bool, dry_run: bool) -> SubtitlesItem:
    workdir = cfg.workspace / job.slug
    transcript_path = job.transcript or (workdir / TRANSCRIPT_NAME)
    item = SubtitlesItem(job.id, "skip", published=bool(job.video_id))
    if not transcript_path.exists():
        return item

    srt = workdir / SUBTITLES_NAME
    old = srt.read_text(encoding="utf-8") if srt.exists() else None
    if old is not None:
        item.before = _count_cues(old)
        if not force:
            item.action = "exists"
            return item

    new, layout = render_srt(Transcript.load(transcript_path), SubtitleRules.from_config(cfg))
    item.after, item.dense = len(layout.cues), layout.dense
    if old is None:
        item.action = "create"
    elif old == new:
        item.action = "unchanged"
    else:
        item.action = "update"

    if dry_run or item.action == "unchanged":
        return item
    srt.parent.mkdir(parents=True, exist_ok=True)
    if item.action == "update":
        item.backup = _backup_path(srt)
        item.backup.write_text(old, encoding="utf-8")
    _atomic_write(srt, new)
    if job.subtitles != srt:
        db.update(job.id, subtitles=srt)
    return item


def rebuild_subtitles(
    cfg: Config, db: Database, ids: list[int] | None = None, *, force: bool = False, dry_run: bool = False
) -> SubtitlesReport:
    report = SubtitlesReport()
    if ids:
        jobs = []
        for job_id in ids:
            job = db.get(job_id)
            if job is None:
                report.items.append(SubtitlesItem(job_id, "missing"))
            else:
                jobs.append(job)
    else:
        jobs = db.list()
    for job in jobs:
        report.items.append(rebuild_one(cfg, db, job, force=force, dry_run=dry_run))
    report.items.sort(key=lambda i: i.job_id)
    return report


def describe(item: SubtitlesItem, *, dry_run: bool) -> str:
    """Рядок звіту українською для CLI."""
    planned, done = ACTION_UA[item.action]
    head = f"[{item.job_id}] "
    if item.action == "missing":
        return head + "задачу не знайдено"
    if item.action == "skip":
        return head + "немає транскрипту — спершу audiotube transcribe"
    if item.action == "exists":
        return head + f"SRT уже є ({item.before} титрів) — --force, щоб перебудувати"
    verb = planned if dry_run else done
    if item.action == "update":
        text = f"{verb}: титрів {item.before} → {item.after}"
    else:
        text = f"{verb}: {item.after} титрів"
    if item.dense:
        text += f", щільних {item.dense}"
    if item.backup:
        text += f"; попередня версія: {item.backup.name}"
    if item.published and item.action in ("create", "update"):
        text += "; відео вже на YouTube — доріжку там не оновлено (лише локальний файл)"
    return head + text
