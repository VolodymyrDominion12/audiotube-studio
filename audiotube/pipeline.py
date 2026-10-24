"""Оркестратор: inbox -> рендер -> YouTube -> плейлист.

Кожен крок зберігає результат у базі, тому повторний запуск продовжує з місця зупинки
і ніколи не завантажує те саме відео двічі.
"""

from __future__ import annotations

from pathlib import Path

from audiotube import AUDIO_EXTENSIONS
from audiotube.config import Config
from audiotube.db import Database, Job, QuotaTracker
from audiotube.metadata import MetadataError, load_meta, slugify, title_from_filename
from audiotube.render import render_video
from audiotube.visuals import choose_background


class Pipeline:
    def __init__(self, cfg: Config, db: Database | None = None, log=print):
        self.cfg = cfg
        self.db = db or Database(cfg.db_path)
        self.quota = QuotaTracker(self.db, cfg.youtube.daily_quota)
        self.log = log
        self._publisher = None

    # --- реєстрація --------------------------------------------------------
    def scan(self) -> list[Job]:
        added = []
        if not self.cfg.inbox.is_dir():
            return added
        for f in sorted(self.cfg.inbox.iterdir()):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                try:
                    title = load_meta(f).title
                except MetadataError:
                    title = title_from_filename(f)  # помилку покаже рендер
                job = self.db.add_job(f, slugify(title))
                if job:
                    added.append(job)
        return added

    # --- рендер ------------------------------------------------------------
    def render(self, job: Job, preview: float | None = None, force: bool = False) -> Job:
        """preview=N: рендерить лише перші N секунд у окремий файл *_preview.mp4, статус не змінює."""
        if job.status != "new" and not force and not preview:
            self.log(f"[{job.id}] вже відрендерено ({job.status}); для повтору: --force")
            return job
        if job.video_id and force and not preview:
            self.log(f"[{job.id}] ! відео вже на YouTube ({job.url}); новий рендер не замінить його там")
        if not job.audio.exists():
            return self._fail(job, f"аудіо не знайдено: {job.audio}")

        try:
            meta = load_meta(job.audio)
        except MetadataError as e:
            return self._fail(job, str(e))
        workdir = self.cfg.workspace / job.slug
        suffix = "_preview" if preview else ""
        out_video = self.cfg.output / f"{job.slug}{suffix}.mp4"
        out_thumb = self.cfg.output / f"{job.slug}{suffix}.jpg"
        try:
            bg = choose_background(meta, self.cfg, job.slug, workdir)
            self.log(f"[{job.id}] рендер «{meta.title}» · фон: {bg.path.name} ({bg.source})")
            res = render_video(
                job.audio, bg, meta.title, meta.thumbnail_text or meta.title, self.cfg,
                workdir, out_video, out_thumb, preview_seconds=preview,
            )
        except Exception as e:  # noqa: BLE001 — помилку зберігаємо в базі, пайплайн іде далі
            return self._fail(job, f"рендер: {e}")

        self.log(f"[{job.id}] готово: {res.video} ({res.duration / 60:.1f} хв, {res.encoder})")
        if preview:
            return job
        return self.db.update(
            job.id, status="rendered", title=meta.title, video=res.video, thumbnail=res.thumbnail,
            duration=res.duration, error=None,
        )

    # --- публікація ----------------------------------------------------------
    @property
    def publisher(self):
        if self._publisher is None:
            from audiotube.youtube import Publisher, build_service

            self._publisher = Publisher(build_service(self.cfg, interactive=True), self.cfg, self.quota, self.log)
        return self._publisher

    def publish(self, job: Job) -> Job:
        from audiotube.youtube import QuotaExceeded

        if job.status == "new":
            self.log(f"[{job.id}] спершу відрендеріть відео")
            return job
        if job.status == "done":
            return job
        try:
            meta = load_meta(job.audio)
            if not job.video_id:
                if not job.video or not job.video.exists():
                    return self._fail(job, f"файл відео не знайдено: {job.video}")
                self.log(f"[{job.id}] завантаження на YouTube: «{meta.title}»")
                vid = self.publisher.upload(job.video, meta)
                job = self.db.update(job.id, video_id=vid, status="uploaded", error=None)
                self.log(f"[{job.id}] завантажено: {job.url}")
                if job.thumbnail and job.thumbnail.exists():
                    self.publisher.set_thumbnail(vid, job.thumbnail)

            if meta.playlist and not job.playlist_id:
                pid = self.publisher.ensure_playlist(meta.playlist)
                self.publisher.add_to_playlist(pid, job.video_id)
                job = self.db.update(job.id, playlist_id=pid)
                self.log(f"[{job.id}] додано в плейлист «{meta.playlist}»")

            return self.db.update(job.id, status="done", error=None)
        except QuotaExceeded as e:
            self.log(f"[{job.id}] {e}")
            return self.db.update(job.id, error=str(e))
        except Exception as e:  # noqa: BLE001
            return self._fail(job, f"YouTube: {e}")

    def _fail(self, job: Job, msg: str) -> Job:
        self.log(f"[{job.id}] ПОМИЛКА {msg}")
        return self.db.update(job.id, error=msg)

    # --- вибірки -------------------------------------------------------------
    def select(self, ids: list[int] | None, statuses: tuple[str, ...]) -> list[Job]:
        if ids:
            jobs = [self.db.get(i) for i in ids]
            missing = [i for i, j in zip(ids, jobs) if j is None]
            if missing:
                raise SystemExit(f"немає задач з id: {missing}")
            return jobs
        return self.db.list(statuses)


def resolve_audio_arg(cfg: Config, value: str) -> Path:
    p = Path(value).expanduser()
    if not p.exists():
        p = cfg.inbox / value
    if not p.exists():
        raise SystemExit(f"файл не знайдено: {value}")
    return p
