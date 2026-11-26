"""Автоматичний цикл: синхронізація -> підготовка -> публікація в межах бюджету.

`audiotube daemon` призначений для запуску з systemd/cron. Він:
- бере файл-лок, щоб два екземпляри не дублювали завантаження;
- тримає денний бюджет відео і мінімальний інтервал між публікаціями;
- не виходить за квоту YouTube (перевіряє залишок до кожного відео);
- після 3 невдач (налаштовується) перестає чіпати задачу;
- за бажанням видаляє .mp4 після успішної публікації, лишаючи аудіо й субтитри;
- надсилає сповіщення (desktop/Telegram, spec 007): публікація, остаточна невдача,
  протухла авторизація, денне зведення.
"""

from __future__ import annotations

import json
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from audiotube.config import Config
from audiotube.db import Job
from audiotube.media import has_ffmpeg
from audiotube.notebooklm import NotebookLMError, NotebookLMUnavailable
from audiotube.notify import Notifier, build_notifier
from audiotube.pipeline import STATUS_ORDER, Pipeline
from audiotube.sync import SyncService
from audiotube.youtube import AuthRequired

COST_THUMBNAIL = 50
COST_PLAYLIST = 52
READY_STATUSES = ("new", "transcribed", "described", "rendered")

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]


class FileLock:
    """Мінімальний ексклюзивний лок на файлі (flock)."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+")
        if fcntl is None:  # pragma: no cover
            return True
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        if fcntl is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        self._fh.close()
        self._fh = None

    def is_locked(self) -> bool:
        """Чи тримає замок хтось інший. Файл не створює, якщо його ще немає."""
        if not self.path.exists():
            return False
        if self._fh is not None:
            return True
        probe = FileLock(self.path)
        if not probe.acquire():
            return True
        probe.release()
        return False

    def __enter__(self) -> "FileLock":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def daemon_lock(cfg: Config) -> FileLock:
    """Єдиний замок для циклу демона і синку з веб-UI (spec 008 §9)."""
    return FileLock(cfg.data / "daemon.lock")


def next_upload_at(cfg: Config, db) -> datetime | None:
    """Коли демон наступного разу має право публікувати; None — вже зараз.

    Денний бюджет вичерпано → початок завтрашнього дня (локальний час).
    """
    today = db.uploads_count()
    if today >= cfg.daemon.videos_per_day:
        now = datetime.now().astimezone()
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    last = db.last_upload_at()
    if last is None or today == 0:
        return None
    nxt = last + timedelta(hours=cfg.daemon.min_hours_between)
    return nxt if nxt > datetime.now(last.tzinfo) else None


@dataclass
class CycleStats:
    synced: int = 0
    planned: int = 0
    published: int = 0
    failed: int = 0
    reason: str = ""
    published_jobs: list[Job] = field(default_factory=list)


def estimate_upload_cost(cfg: Config) -> int:
    """Скільки одиниць квоти з'їсть одне відео з поточними налаштуваннями."""
    yt = cfg.youtube
    cost = yt.upload_cost + COST_THUMBNAIL
    if yt.upload_captions:
        cost += yt.captions_cost
    if yt.manage_playlists:
        cost += COST_PLAYLIST
    return cost


class Daemon:
    def __init__(
        self,
        cfg: Config,
        pipe: Pipeline | None = None,
        log=print,
        notifier: Notifier | None = None,
    ):
        self.cfg = cfg
        self._owns_pipe = pipe is None
        self.pipe = pipe or Pipeline(cfg, log=log)
        self.log = log
        self.notifier = notifier or build_notifier(cfg, self.pipe.db, log)
        self._stop = False

    # --- життєвий цикл ------------------------------------------------------
    def request_stop(self) -> None:
        self._stop = True

    def _close(self) -> None:
        if self._owns_pipe:
            self.pipe.db.close()

    def run(
        self,
        *,
        once: bool = False,
        dry_run: bool = False,
        max_videos: int | None = None,
        sync: bool | None = None,
    ) -> int:
        if dry_run is False and self.cfg.pipeline.auto_render and not has_ffmpeg():
            self.log(
                "ffmpeg/ffprobe не знайдено — демон не зможе рендерити відео. "
                "Встановіть: sudo apt install ffmpeg"
            )
            self._close()
            return 2

        lock = daemon_lock(self.cfg)
        if not dry_run and not lock.acquire():
            self.log("демон уже працює (файл-лок зайнятий) — вихід")
            self._close()
            return 3

        self._install_signals()
        try:
            while not self._stop:
                stats = self._cycle(dry_run=dry_run, max_videos=max_videos, sync=sync)
                self._print_cycle(stats, dry_run=dry_run)
                if once or dry_run or self._stop:
                    break
                self._sleep(self.cfg.daemon.interval_minutes * 60)
        finally:
            lock.release()
            self._close()
        return 0

    # --- один цикл ----------------------------------------------------------
    def _cycle(
        self, *, dry_run: bool, max_videos: int | None, sync: bool | None
    ) -> CycleStats:
        stats = self._cycle_body(dry_run=dry_run, max_videos=max_videos, sync=sync)
        if not dry_run:
            self.notifier.daily_summary()
        return stats

    def _cycle_body(
        self, *, dry_run: bool, max_videos: int | None, sync: bool | None
    ) -> CycleStats:
        cfg = self.cfg
        stats = CycleStats()

        do_sync = cfg.notebooklm.enabled if sync is None else sync
        if do_sync and cfg.daemon.sync_before_run:
            try:
                report = SyncService(cfg, db=self.pipe.db, log=self.log).run(dry_run=dry_run)
                stats.synced = report.total_new
                if report.errors:
                    self.log(f"синк: {len(report.errors)} помилок (див. audiotube sync)")
            except NotebookLMError as e:
                self.log(f"синк пропущено: {e}")
                if not dry_run and not isinstance(e, NotebookLMUnavailable):
                    self.notifier.auth_expired("notebooklm", str(e))
            if not dry_run:
                self.pipe.scan()

        daily = cfg.daemon.videos_per_day
        if max_videos is not None:
            daily = min(daily, max_videos)
        remaining = max(0, daily - self.pipe.db.uploads_count())
        if remaining == 0:
            stats.reason = "денний ліміт відео вичерпано"
            return stats

        published_today = self.pipe.db.uploads_count()
        last = self.pipe.db.last_upload_at()
        if last is not None and published_today > 0:
            gap = datetime.now(last.tzinfo) - last
            if gap < timedelta(hours=cfg.daemon.min_hours_between):
                stats.reason = f"наступна публікація не раніше ніж через {cfg.daemon.min_hours_between:g} год"
                return stats

        cost = estimate_upload_cost(cfg)
        if self.pipe.quota.remaining() - cfg.daemon.quota_reserve < cost:
            stats.reason = (
                f"квота: лишилось {self.pipe.quota.remaining()} з {self.pipe.quota.daily_limit}, "
                f"треба ~{cost}"
            )
            return stats

        # spec 014: незавершеній задачі без аудіо нема чим допомогти — не палимо спроби.
        # Уже відрендерене відео без аудіо ще можна опублікувати, тому його лишаємо.
        audio_deleted = self.pipe.db.audio_deleted_job_ids()
        jobs = [
            j
            for j in self.pipe.db.list(READY_STATUSES)
            if j.attempts < cfg.daemon.max_attempts
            and not (j.id in audio_deleted and STATUS_ORDER[j.status] < STATUS_ORDER["rendered"])
        ]
        if cfg.daemon.order == "newest":
            jobs.reverse()
        if not jobs:
            stats.reason = "немає задач у черзі"
            return stats

        if not dry_run:
            # NTF-R7: протухлий токен не має «спалювати» спроби кожної задачі
            try:
                self.pipe.check_publish_auth()
            except AuthRequired as e:
                self.log(f"публікацію пропущено: {e}")
                self.notifier.auth_expired("youtube", str(e))
                stats.reason = "YouTube: потрібна повторна авторизація (audiotube auth)"
                return stats
            except Exception as e:  # noqa: BLE001 — інші збої обробить publish() задачі
                self.log(f"перевірка токена YouTube не вдалася: {e}")

        for job in jobs:
            if self._stop or remaining <= 0:
                break
            if self.pipe.quota.remaining() - cfg.daemon.quota_reserve < cost:
                stats.reason = "квота вичерпана під час циклу"
                break

            if dry_run:
                stats.planned += 1
                self.log(f"[{job.id}] буде оброблено: {job.title or job.audio.name} ({job.status})")
                continue

            # задачу взято на повторну спробу — старий текст помилки не має блокувати кроки
            job = self.pipe.db.update(job.id, error=None)
            try:
                job = self._advance(job)
            except Exception as e:  # noqa: BLE001 — одна задача не має валити цикл
                self.log(f"[{job.id}] помилка: {e}")
                job = self.pipe.db.update(job.id, error=str(e))

            if job.status == "done":
                stats.published += 1
                stats.published_jobs.append(job)
                remaining -= 1
                self._after_publish(job)
                self._sleep(self.cfg.daemon.pause_seconds)
            else:
                stats.failed += 1
                attempts = self.pipe.db.bump_attempt(job.id)
                first = (job.error or "невідома помилка").splitlines()[0]
                self.log(f"[{job.id}] не вдалося (спроба {attempts}): {first}")
                if attempts == cfg.daemon.max_attempts:
                    self.notifier.failed_final(job, attempts)
        return stats

    def _advance(self, job: Job) -> Job:
        cfg = self.cfg
        if cfg.pipeline.auto_transcribe and STATUS_ORDER[job.status] < STATUS_ORDER["transcribed"]:
            job = self.pipe.transcribe(job)
        if job.error:
            return job
        if cfg.pipeline.auto_describe and STATUS_ORDER[job.status] < STATUS_ORDER["described"]:
            job = self.pipe.describe(job)
        if job.error:
            return job
        if cfg.pipeline.auto_render and STATUS_ORDER[job.status] < STATUS_ORDER["rendered"]:
            job = self.pipe.render(job)
        if job.error:
            return job
        if job.status in ("rendered", "uploaded"):
            job = self.pipe.publish(job)
        return job

    # --- після публікації ---------------------------------------------------
    def _after_publish(self, job: Job) -> None:
        self.log(f"[{job.id}] опубліковано: {job.url or job.video_id}")
        self._report(job)
        self.notifier.published(job)
        if self.cfg.daemon.cleanup_video_after_upload:
            self._cleanup_video(job)

    def _cleanup_video(self, job: Job) -> None:
        video = job.video
        if video is None:
            return
        try:
            video.unlink(missing_ok=True)
        except OSError as e:
            self.log(f"[{job.id}] не вдалося видалити {video}: {e}")
            return
        self.pipe.db.update(job.id, video=None)

    def _report(self, job: Job) -> None:
        path = self.cfg.data / "reports" / f"{datetime.now().date().isoformat()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "job_id": job.id,
            "video_id": job.video_id,
            "title": job.title,
            "url": job.url,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    # --- службове -----------------------------------------------------------
    def _install_signals(self) -> None:
        def handler(_signum, _frame):
            self._stop = True
            self.log("отримано сигнал зупинки — завершую поточний крок")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # не головний потік
                pass

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop:
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(5.0, left))

    def _print_cycle(self, stats: CycleStats, *, dry_run: bool) -> None:
        parts = []
        if stats.synced:
            parts.append(f"синк: +{stats.synced}")
        if dry_run:
            parts.append(f"до обробки: {stats.planned}")
        else:
            parts.append(f"опубліковано: {stats.published}")
        if stats.failed:
            parts.append(f"невдач: {stats.failed}")
        if stats.reason:
            parts.append(stats.reason)
        self.log("цикл — " + ", ".join(parts))
