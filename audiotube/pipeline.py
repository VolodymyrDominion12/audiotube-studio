"""Оркестратор: inbox -> транскрипт -> метадані -> рендер -> YouTube -> плейлист.

Кожен крок зберігає результат у базі, тому повторний запуск продовжує з місця зупинки
і ніколи не завантажує те саме відео двічі.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from audiotube import AUDIO_EXTENSIONS
from audiotube.config import Config
from audiotube.db import Database, Job, QuotaTracker
from audiotube.llm import LLMError
from audiotube.metadata import (
    MetadataError,
    load_meta,
    merge_generated_meta,
    sidecar_path,
    slugify,
    title_from_filename,
    write_sidecar,
)
from audiotube.planner import PlanError, generate_plan, plan_to_meta
from audiotube.render import render_video
from audiotube.transcribe import (
    SUBTITLES_NAME,
    TRANSCRIPT_NAME,
    TranscriptionError,
    transcribe_audio,
)
from audiotube.transcript import Transcript, write_srt
from audiotube.visuals import choose_background

# порядок кроків: більший індекс — далі по конвеєру
STATUS_ORDER = {"new": 0, "transcribed": 1, "described": 2, "rendered": 3, "uploaded": 4, "done": 5}


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        db: Database | None = None,
        log=print,
        interactive: bool = True,
    ):
        self.cfg = cfg
        self.db = db or Database(cfg.db_path)
        self.quota = QuotaTracker(self.db, cfg.youtube.daily_quota)
        self.log = log
        self.interactive = interactive
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
                job = self.db.add_job(f, slugify(title), title=title)
                if job:
                    added.append(job)
        return added

    # --- транскрипція ------------------------------------------------------
    def transcribe(self, job: Job, force: bool = False) -> Job:
        if not job.audio.exists():
            return self._fail(job, f"аудіо не знайдено: {job.audio}")
        workdir = self.cfg.workspace / job.slug
        try:
            result = transcribe_audio(job.audio, workdir, self.cfg, force=force, log=self.log)
        except TranscriptionError as e:
            return self._fail(job, f"транскрипція: {e}")
        except Exception as e:  # noqa: BLE001 — несподівана помилка не має валити весь запуск
            return self._fail(job, f"транскрипція: {e}")

        self.log(f"[{job.id}] транскрипт готовий: {result.srt_path.name}")
        return self.db.update(
            job.id,
            status=self._next_status(job.status, "transcribed"),
            transcript=result.json_path,
            subtitles=result.srt_path,
            duration=result.transcript.duration or job.duration,
            error=None,
        )

    # --- метадані ----------------------------------------------------------
    def describe(self, job: Job, force: bool = False) -> Job:
        workdir = self.cfg.workspace / job.slug
        transcript_path = job.transcript or (workdir / TRANSCRIPT_NAME)
        if not transcript_path.exists():
            if job.status in ("new", "transcribed"):
                self.log(f"[{job.id}] спершу транскрипція…")
                job = self.transcribe(job)
                transcript_path = job.transcript or (workdir / TRANSCRIPT_NAME)
            if not transcript_path.exists():
                return self._fail(job, f"немає транскрипту: {transcript_path} (спершу transcribe)")

        side = sidecar_path(job.audio)
        if side.exists() and not force:
            try:
                load_meta(job.audio)  # перевіряємо, що ручний YAML валідний
            except MetadataError as e:
                return self._fail(job, str(e))
            self.log(f"[{job.id}] метадані вже є ({side.name}); перегенерувати: describe --force")
            return self.db.update(
                job.id,
                status=self._next_status(job.status, "described"),
                error=None,
            )

        try:
            transcript = Transcript.load(transcript_path)
            fallback_title = job.title or title_from_filename(job.audio)
            self.log(f"[{job.id}] генерація метаданих ({self.cfg.llm.provider}/{self.cfg.llm.model})…")
            plan = generate_plan(transcript, self.cfg, fallback_title=fallback_title)
            generated = plan_to_meta(plan)
            generated.visual_keywords = plan.visual_keywords
            if side.exists():
                generated = merge_generated_meta(load_meta(job.audio), generated)
            header = (
                f"Згенеровано автоматично {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                f"({self.cfg.llm.provider}/{self.cfg.llm.model}). Перевірте перед публікацією."
            )
            write_sidecar(job.audio, generated, header=header)
            (workdir / "plan.json").write_text(
                json.dumps(plan.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except (LLMError, PlanError, MetadataError) as e:
            return self._fail(job, f"метадані: {e}")
        except Exception as e:  # noqa: BLE001
            return self._fail(job, f"метадані: {e}")

        self.log(f"[{job.id}] метадані: «{generated.title}», глав: {len(generated.chapters)}")
        return self.db.update(
            job.id,
            status=self._next_status(job.status, "described"),
            title=generated.title,
            error=None,
        )

    # --- рендер ------------------------------------------------------------
    def render(self, job: Job, preview: float | None = None, force: bool = False) -> Job:
        """preview=N: рендерить лише перші N секунд у окремий файл *_preview.mp4, статус не змінює."""
        if not force and not preview and STATUS_ORDER.get(job.status, 0) >= STATUS_ORDER["rendered"]:
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
        subtitles = self._subtitles_path(job, workdir)
        try:
            bg = choose_background(meta, self.cfg, job.slug, workdir)
            self.log(f"[{job.id}] рендер «{meta.title}» · фон: {bg.path.name} ({bg.source})")
            if subtitles and self.cfg.render.burn_subtitles:
                self.log(f"[{job.id}] вшиті субтитри: повільний прохід перекодування")
            res = render_video(
                job.audio, bg, meta.title, meta.thumbnail_text or meta.title, self.cfg,
                workdir, out_video, out_thumb, preview_seconds=preview, subtitles=subtitles,
            )
        except Exception as e:  # noqa: BLE001 — помилку зберігаємо в базі, пайплайн іде далі
            return self._fail(job, f"рендер: {e}")

        self.log(f"[{job.id}] готово: {res.video} ({res.duration / 60:.1f} хв, {res.encoder})")
        if preview:
            return job
        return self.db.update(
            job.id,
            status=self._next_status(job.status, "rendered"),
            title=meta.title,
            video=res.video,
            thumbnail=res.thumbnail,
            duration=res.duration,
            error=None,
        )

    # --- публікація ----------------------------------------------------------
    @property
    def publisher(self):
        if self._publisher is None:
            from audiotube.youtube import Publisher, build_service

            self._publisher = Publisher(
                build_service(self.cfg, interactive=self.interactive),
                self.cfg,
                self.quota,
                self.log,
            )
        return self._publisher

    def publish(self, job: Job) -> Job:
        from audiotube.youtube import QuotaExceeded

        if STATUS_ORDER.get(job.status, 0) < STATUS_ORDER["rendered"]:
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

                subtitles = self._subtitles_path(job, self.cfg.workspace / job.slug)
                if self.cfg.youtube.upload_captions and subtitles and not job.captions_id:
                    cid = self.publisher.set_captions(vid, subtitles)
                    if cid:
                        job = self.db.update(job.id, captions_id=cid)
                        self.log(f"[{job.id}] субтитри додано ({subtitles.name})")

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

    # --- допоміжне -----------------------------------------------------------
    def _subtitles_path(self, job: Job, workdir: Path) -> Path | None:
        """Файл субтитрів: готовий .srt або згенерований з транскрипту."""
        if job.subtitles and job.subtitles.exists():
            return job.subtitles
        transcript = job.transcript or (workdir / TRANSCRIPT_NAME)
        if transcript.exists():
            try:
                srt = write_srt(
                    Transcript.load(transcript), workdir / SUBTITLES_NAME,
                    max_chars=self.cfg.transcribe.max_chars, max_lines=self.cfg.transcribe.max_lines,
                )
            except Exception:  # noqa: BLE001 — субтитри не критичні для рендеру
                return None
            self.db.update(job.id, subtitles=srt)
            return srt
        return None

    @staticmethod
    def _next_status(current: str, target: str) -> str:
        """Крок ніколи не відкочує статус назад (напр., повторний транскрипт після рендеру)."""
        return target if STATUS_ORDER.get(target, 0) > STATUS_ORDER.get(current, 0) else current

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
    """Знаходить аудіо за шляхом, шляхом від кореня проєкту або просто за іменем в inbox/."""
    raw = Path(value).expanduser()
    candidates = [raw, cfg.root / raw, cfg.inbox / raw, cfg.inbox / raw.name]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(f"файл не знайдено: {value}")
