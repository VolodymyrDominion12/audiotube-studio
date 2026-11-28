"""Локальний веб-інтерфейс без зовнішніх залежностей (stdlib http.server).

Запуск: `audiotube web` → http://127.0.0.1:8765

Дає те, що потрібно для ручної перевірки перед публікацією: черга задач, прев'ю,
редагування YAML-метаданих, кнопки транскрипції/рендеру/завантаження.
Кожен запит створює власне з'єднання SQLite і виконується в окремому потоці.

Сторінка `/notebooks` (spec 008) показує ноутбуки NotebookLM → перекази → відео і дає запустити
синк; справжній синк бере той самий `data/daemon.lock`, що й демон.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from audiotube import AUDIO_EXTENSIONS
from audiotube.config import Config
from audiotube.daemon import daemon_lock, next_upload_at
from audiotube.db import Database, Job, QuotaTracker
from audiotube.media import has_ffmpeg
from audiotube.metadata import MetadataError, VideoMeta, dump_sidecar, load_meta, sidecar_path
from audiotube.notebooklm import Adapter
from audiotube.pipeline import STATUS_ORDER, Pipeline
from audiotube.sync import SyncReport, SyncService

MAX_BODY = 1_000_000
PREVIEW_SECONDS = 20.0


class WebError(RuntimeError):
    pass


class NotFound(WebError):
    """404: запитаного об'єкта немає."""


class Busy(WebError):
    """409: ресурс зайнятий (напр. замок демона)."""


def _rel(cfg: Config, path: Path | None) -> str | None:
    if not path:
        return None
    try:
        return str(path.resolve().relative_to(cfg.root.resolve()))
    except ValueError:
        return None


def _size(path: Path | None) -> int | None:
    if not path:
        return None
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


def _ensure_managed(path: Path, base: Path, label: str) -> None:
    """spec 014: видаляємо лише те, що лежить у керованій теці."""
    if not _is_under(path.resolve(), base.resolve()):
        raise WebError(f"{label} поза {base.name}/: {path} — видалення заборонено")


def _unlink_file(path: Path, base: Path, label: str) -> int:
    """Видаляє файл у межах base; повертає звільнені байти (0, якщо файлу немає)."""
    _ensure_managed(path, base, label)
    if not path.exists():
        return 0
    if not path.is_file():
        raise WebError(f"{label} не є файлом: {path}")
    size = path.stat().st_size
    path.unlink()
    return size


def job_json(cfg: Config, job: Job, *, audio_deleted: bool = False) -> dict:
    preview = cfg.output / f"{job.slug}_preview.mp4"
    video = job.video
    return {
        "id": job.id,
        "slug": job.slug,
        "status": job.status,
        "title": job.title or job.audio.name,
        "audio": job.audio.name,
        "duration": job.duration,
        "error": job.error,
        "url": job.url,
        "video": _rel(cfg, video),
        "thumbnail": _rel(cfg, job.thumbnail),
        "preview": _rel(cfg, preview if preview.exists() else None),
        "subtitles": _rel(cfg, job.subtitles),
        "captions": bool(job.captions_id),
        "playlist": bool(job.playlist_id),
        "has_transcript": bool(job.transcript and job.transcript.exists()),
        # spec 014: керування локальними файлами
        "audio_exists": job.audio.is_file(),
        "audio_size": _size(job.audio),
        "audio_deleted": bool(audio_deleted),
        "video_exists": bool(video and video.is_file()),
        "video_size": _size(video),
    }


def disk_stats(cfg: Config, jobs: list[Job]) -> dict:
    """WEB2-R4: скільки аудіо лежить на диску і скільки з них поза чергою."""
    registered = {job.audio.resolve() for job in jobs}
    inbox_audio = [
        p for p in cfg.inbox.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    output_videos = [p for p in cfg.output.glob("*.mp4") if p.is_file()]
    return {
        "inbox_audio": len(inbox_audio),
        "inbox_unregistered": sum(1 for p in inbox_audio if p.resolve() not in registered),
        "inbox_bytes": sum(_size(p) or 0 for p in inbox_audio),
        "output_videos": len(output_videos),
        "output_bytes": sum(_size(p) or 0 for p in output_videos),
    }


def _yaml_for(cfg: Config, job: Job) -> str:
    side = sidecar_path(job.audio)
    if side.exists():
        return side.read_text(encoding="utf-8")
    return dump_sidecar(load_meta(job.audio), header=f"Нові метадані для {job.audio.name}")


def save_meta_text(job: Job, text: str) -> None:
    """Валідує YAML перед записом, щоб не зламати наступний рендер."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise WebError(f"некоректний YAML: {e}") from e
    if not isinstance(data, dict):
        raise WebError("метадані мають бути YAML-об'єктом")
    try:
        VideoMeta.model_validate({**data, "title": data.get("title") or job.title or job.audio.stem})
    except (ValueError, MetadataError) as e:
        raise WebError(f"метадані не пройшли перевірку: {e}") from e
    sidecar_path(job.audio).write_text(text, encoding="utf-8")


class WebApp:
    def __init__(
        self,
        cfg: Config,
        host: str = "127.0.0.1",
        port: int = 8765,
        adapter: Adapter | None = None,
    ):
        self.cfg = cfg
        self.host = host
        self.port = port
        self.adapter = adapter  # None → справжній NotebookLM (build_adapter у SyncService)
        self.lock = threading.Lock()
        self._sync_state_lock = threading.Lock()
        self.sync_run: dict | None = None
        # spec 016: браузерна публікація виконується у фоні, по одній за раз
        self._browser_lock = threading.Lock()
        self.browser_thread: threading.Thread | None = None
        self.browser_state: dict = {
            "status": "idle",
            "job": None,
            "review": False,
            "message": "",
            "log": [],
            "video_id": None,
        }

    # --- браузерна публікація (spec 016) ------------------------------------
    def start_browser_upload(self, job_id: int, review: bool = False) -> dict:
        """Запускає фонову публікацію через Chrome; повторний запуск — Busy."""
        with self._browser_lock:
            if self.browser_thread is not None and self.browser_thread.is_alive():
                raise Busy("браузер уже працює — дочекайтеся завершення")
            pipe = Pipeline(self.cfg, log=lambda *_: None)
            try:
                job = pipe.db.get(job_id)
            finally:
                pipe.db.close()
            if job is None:
                raise NotFound(f"задачу {job_id} не знайдено")
            if job.status == "done":
                raise WebError("задача вже опублікована")
            if job.status not in ("rendered", "uploaded"):
                raise WebError("спершу відрендеріть відео")
            if not job.video_id and not (job.video and job.video.exists()):
                raise WebError("файл відео не знайдено")
            self.browser_state = {
                "status": "running",
                "job": job_id,
                "review": review,
                "message": "відкриваю браузер…",
                "log": ["відкриваю Chrome…"],
                "video_id": None,
            }
            self.browser_thread = threading.Thread(
                target=self._browser_upload, args=(job_id, review), daemon=True
            )
            self.browser_thread.start()
        return {"ok": True, "started": True, "job": job_id, "review": review}

    def _browser_upload(self, job_id: int, review: bool) -> None:
        from audiotube.browser_publish import BrowserPublisher

        logs: list[str] = []

        def log(message) -> None:
            logs.append(str(message))
            self.browser_state["log"] = logs[-200:]
            self.browser_state["message"] = str(message)

        pipe = Pipeline(self.cfg, log=log, interactive=True)
        try:
            job = pipe.db.get(job_id)
            if job is None:
                raise WebError(f"задачу {job_id} не знайдено")
            publisher = BrowserPublisher(pipe, log=log)
            try:
                job = publisher.publish(job, review=review)
            finally:
                publisher.close()
            self.browser_state["video_id"] = job.video_id
            self.browser_state["status"] = "done" if job.status == "done" else "error"
            self.browser_state["message"] = job.error or (
                f"готово: {job.url}" if job.video_id else "готово"
            )
            self.browser_state["log"] = logs[-200:]
        except Exception as exc:  # noqa: BLE001 — веб-сервер не має падати
            self.browser_state["status"] = "error"
            self.browser_state["message"] = str(exc)
            self.browser_state["log"] = (logs + [f"помилка: {exc}"])[-200:]
        finally:
            pipe.db.close()

    def browser_snapshot(self) -> dict:
        with self._browser_lock:
            return {"ok": True, **self.browser_state}

    # --- серіалізація стану --------------------------------------------------
    def _snapshot(self, *, with_jobs: bool) -> dict:
        with self.lock:
            pipe = Pipeline(self.cfg, log=lambda *_: None)
            try:
                rows = pipe.db.list()
                deleted = pipe.db.audio_deleted_job_ids()
                jobs = (
                    [job_json(self.cfg, j, audio_deleted=j.id in deleted) for j in rows]
                    if with_jobs else []
                )
                used = pipe.quota.used()
                limit = pipe.quota.daily_limit
                files = disk_stats(self.cfg, rows)
            finally:
                pipe.db.close()
        by_status = {status: 0 for status in STATUS_ORDER}
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        data: dict = {
            "counts": {
                "total": len(rows),
                "by_status": by_status,
                "published": sum(1 for row in rows if row.video_id),
                "ready_to_publish": by_status.get("rendered", 0),
            },
            "files": files,
            "quota": {"used": used, "limit": limit},
            "config": {
                "llm": f"{self.cfg.llm.provider}/{self.cfg.llm.model}",
                "transcribe": f"{self.cfg.transcribe.backend}/{self.cfg.transcribe.model}",
                "language": self.cfg.youtube.language,
                "auto_upload": self.cfg.youtube.auto_upload,
                "burn_subtitles": self.cfg.render.burn_subtitles,
                "ffmpeg": has_ffmpeg(),
            },
        }
        if with_jobs:
            data["jobs"] = jobs
        return data

    def state(self) -> dict:
        return self._snapshot(with_jobs=True)

    def overview(self) -> dict:
        """WEB3-R1: легкий знімок для головної сторінки — без важких карток задач."""
        data = self._snapshot(with_jobs=False)
        data["sync"] = self.sync_state()
        return data

    # --- групи черги (spec 017) ----------------------------------------------
    @staticmethod
    def _blank_group(nb) -> dict:
        return {
            "key": nb["id"] if nb else "__none__",
            "id": nb["id"] if nb else None,
            "title": nb["title"] if nb else "Без ноутбука",
            "slug": nb["slug"] if nb else None,
            "url": nb["url"] if nb else None,
            "synced_at": nb["synced_at"] if nb else None,
            "jobs": 0,
            "by_status": {status: 0 for status in STATUS_ORDER},
            "published": 0,
            "errors": 0,
            "ready": 0,
            "audio_deleted": 0,
            "can_sync": nb is not None,
        }

    @staticmethod
    def _add_job_to_group(group: dict, job: Job, deleted: set[int]) -> None:
        group["jobs"] += 1
        group["by_status"][job.status] = group["by_status"].get(job.status, 0) + 1
        if job.video_id:
            group["published"] += 1
        elif job.error:
            group["errors"] += 1
        if job.status == "rendered":
            group["ready"] += 1
        if job.id in deleted:
            group["audio_deleted"] += 1

    def _job_json_with_group(self, job: Job, mapping: dict, deleted: set[int]) -> dict:
        data = job_json(self.cfg, job, audio_deleted=job.id in deleted)
        nb_id, nb_title = mapping.get(job.id, (None, None))
        data["notebook_id"] = nb_id
        data["notebook_title"] = nb_title
        return data

    def groups(self) -> dict:
        """WEB3-R2: ноутбуки й «Без ноутбука» з лічильниками задач."""
        with Database(self.cfg.db_path) as db:
            notebooks = [dict(nb) for nb in db.notebooks()]
            jobs = db.list()
            mapping = db.job_notebooks()
            deleted = db.audio_deleted_job_ids()

        by_id = {nb["id"]: self._blank_group(nb) for nb in notebooks}
        unassigned = self._blank_group(None)
        for job in jobs:
            nb_id, _ = mapping.get(job.id, (None, None))
            group = by_id.get(nb_id) or unassigned
            self._add_job_to_group(group, job, deleted)

        # спершу ноутбуки з більшою чергою, далі порожні; «Без ноутбука» — у кінці
        result = sorted(
            by_id.values(),
            key=lambda g: (g["jobs"] == 0, -g["jobs"], (g["title"] or "").lower()),
        )
        if unassigned["jobs"]:
            result.append(unassigned)
        return {"ok": True, "groups": result}

    def group_jobs(self, key: str) -> dict:
        """WEB3-R3: задачі однієї групи; key = id ноутбука або __none__."""
        nb = None
        if key != "__none__":
            with Database(self.cfg.db_path) as db:
                row = db.notebook(key)
            if row is None:
                raise NotFound(f"ноутбук {key} не знайдено")
            nb = dict(row)

        with Database(self.cfg.db_path) as db:
            jobs = db.jobs_for_notebook(nb["id"] if nb else None)
            mapping = db.job_notebooks()
            deleted = db.audio_deleted_job_ids()

        group = self._blank_group(nb)
        for job in jobs:
            self._add_job_to_group(group, job, deleted)
        return {
            "ok": True,
            "group": group,
            "jobs": [self._job_json_with_group(job, mapping, deleted) for job in jobs],
        }

    def search_jobs(self, q: str, status: str) -> dict:
        """WEB3-R4: пошук по всій черзі без завантаження всіх груп."""
        if status and status not in STATUS_ORDER:
            raise WebError(f"невідомий статус: {status}")
        with Database(self.cfg.db_path) as db:
            jobs = db.list((status,) if status else None)
            mapping = db.job_notebooks()
            deleted = db.audio_deleted_job_ids()

        needle = (q or "").strip().lower()
        result = []
        for job in jobs:
            _, nb_title = mapping.get(job.id, (None, None))
            haystack = f"{job.title or ''} {job.audio.name} {job.slug} {nb_title or ''}".lower()
            if needle and needle not in haystack:
                continue
            result.append(self._job_json_with_group(job, mapping, deleted))
        return {"ok": True, "jobs": result, "total": len(result)}

    # --- дії -----------------------------------------------------------------
    def action(self, job_id: int, action: str, force: bool = False, privacy: str = "public") -> dict:
        logs: list[str] = []
        with self.lock:
            pipe = Pipeline(self.cfg, log=logs.append, interactive=False)
            try:
                job = pipe.db.get(job_id)
                if not job:
                    raise WebError(f"задачу {job_id} не знайдено")
                if action == "transcribe":
                    job = pipe.transcribe(job, force=force)
                elif action == "describe":
                    job = pipe.describe(job, force=force)
                elif action == "render":
                    job = pipe.render(job, force=force)
                elif action == "preview":
                    job = pipe.render(job, preview=PREVIEW_SECONDS, force=True)
                elif action == "upload":
                    job = pipe.publish(job)
                elif action == "publish":
                    if not job.video_id:
                        raise WebError("відео ще не завантажено на YouTube (спершу upload)")
                    from audiotube.youtube import PRIVACY_VALUES, Publisher, build_service

                    if privacy not in PRIVACY_VALUES:
                        raise WebError(f"некоректна приватність: {privacy}")
                    Publisher(
                        build_service(self.cfg, interactive=False), self.cfg, pipe.quota
                    ).set_privacy(job.video_id, privacy)
                    logs.append(f"приватність {privacy}: {job.url}")
                elif action == "reset":
                    job = pipe.db.update(job.id, status="new", error=None, attempts=0)
                elif action == "delete_audio":
                    job = self._delete_audio(pipe, job, logs)
                elif action == "restore_audio":
                    job = self._restore_audio(pipe, job, logs)
                elif action == "delete_video":
                    job = self._delete_video(pipe, job, logs)
                else:
                    raise WebError(f"невідома дія: {action}")
                deleted = pipe.db.audio_deleted_job_ids()
                payload = {"job": job_json(self.cfg, job, audio_deleted=job.id in deleted)}
            finally:
                pipe.db.close()
        payload["ok"] = True
        payload["log"] = logs
        payload["message"] = logs[-1] if logs else "готово"
        return payload

    # --- локальні файли (spec 014) -------------------------------------------
    def _delete_audio(self, pipe: Pipeline, job: Job, logs: list[str]) -> Job:
        """Видаляє аудіофайл, лишаючи задачу, YAML і прапорець «уже скачано»."""
        candidates = [job.audio, job.audio.with_name(job.audio.name + ".part")]
        for path in candidates:
            if path.exists():
                _ensure_managed(path, self.cfg.inbox, "аудіо")
        removed, freed = [], 0
        for path in candidates:
            if path.exists():
                freed += _unlink_file(path, self.cfg.inbox, "аудіо")
                removed.append(path.name)
        if removed:
            logs.append(f"[{job.id}] видалено аудіо: {', '.join(removed)} ({_human_size(freed)})")
        else:
            logs.append(f"[{job.id}] аудіо вже немає на диску")
        marked = pipe.db.set_audio_deleted(job.id, job.audio, deleted=True)
        if marked:
            logs.append(f"[{job.id}] синк не качатиме його повторно (позначено артефактів: {marked})")
        else:
            logs.append(f"[{job.id}] артефакту NotebookLM немає — повторного завантаження не буде")
        return pipe.db.get(job.id)

    def _restore_audio(self, pipe: Pipeline, job: Job, logs: list[str]) -> Job:
        """Знімає прапорець; саме завантаження робить наступний sync."""
        if job.audio.exists():
            logs.append(f"[{job.id}] аудіо вже на диску: {job.audio.name}")
        marked = pipe.db.set_audio_deleted(job.id, job.audio, deleted=False)
        if marked:
            logs.append(f"[{job.id}] аудіо позначено для повторного завантаження; натисніть «Синк»")
        elif job.audio.exists():
            logs.append(f"[{job.id}] прапорець знято")
        else:
            logs.append(f"[{job.id}] немає артефакту NotebookLM — відновлювати нічого")
        return pipe.db.get(job.id)

    def _delete_video(self, pipe: Pipeline, job: Job, logs: list[str]) -> Job:
        """Видаляє локальні mp4, лишаючи video_id і всю історію YouTube."""
        candidates: list[Path] = []
        if job.video:
            candidates.append(job.video)
        candidates.append(self.cfg.output / f"{job.slug}.mp4")
        candidates.append(self.cfg.output / f"{job.slug}_preview.mp4")
        unique: list[Path] = []
        for path in candidates:
            if path.resolve() not in {p.resolve() for p in unique}:
                unique.append(path)
        # спершу перевіряємо всі наявні файли, щоб не видалити частину й упасти
        for path in unique:
            if path.exists():
                _ensure_managed(path, self.cfg.output, "відео")
        removed, freed = [], 0
        for path in unique:
            if path.exists():
                freed += _unlink_file(path, self.cfg.output, "відео")
                removed.append(path.name)
        if removed:
            logs.append(f"[{job.id}] видалено відео: {', '.join(removed)} ({_human_size(freed)})")
        else:
            logs.append(f"[{job.id}] відео на диску вже немає")
        if job.video_id:
            logs.append(f"[{job.id}] на YouTube лишається: {job.url}")
        else:
            logs.append(f"[{job.id}] локальне відео можна відрендерити знову")
        return pipe.db.update(job.id, video=None)

    def scan(self) -> dict:
        logs: list[str] = []
        with self.lock:
            pipe = Pipeline(self.cfg, log=logs.append, interactive=False)
            try:
                added = pipe.scan()
                count = len(added)
            finally:
                pipe.db.close()
        return {"ok": True, "added": count, "log": logs, "message": f"нових аудіо: {count}"}

    def meta(self, job_id: int) -> dict:
        with self.lock:
            pipe = Pipeline(self.cfg, log=lambda *_: None)
            try:
                job = pipe.db.get(job_id)
                if not job:
                    raise WebError(f"задачу {job_id} не знайдено")
                return {"ok": True, "yaml": _yaml_for(self.cfg, job), "path": str(sidecar_path(job.audio))}
            finally:
                pipe.db.close()

    def save_meta(self, job_id: int, text: str) -> dict:
        with self.lock:
            pipe = Pipeline(self.cfg, log=lambda *_: None)
            try:
                job = pipe.db.get(job_id)
                if not job:
                    raise WebError(f"задачу {job_id} не знайдено")
                save_meta_text(job, text)
                meta = load_meta(job.audio)
                pipe.db.update(job.id, title=meta.title, error=None)
                deleted = pipe.db.audio_deleted_job_ids()
                return {
                    "ok": True, "message": "метадані збережено",
                    "job": job_json(self.cfg, job, audio_deleted=job.id in deleted),
                }
            finally:
                pipe.db.close()

    # --- ноутбуки (spec 008) -------------------------------------------------
    def notebooks(self) -> dict:
        """WEB-R1: ноутбуки з лічильниками."""
        with Database(self.cfg.db_path) as db:
            rows = db.notebook_stats()
        keys = ("id", "title", "slug", "url", "sources", "synced_at",
                "downloaded", "missing", "deleted", "published", "errors", "queued")
        return {"ok": True, "notebooks": [{k: r[k] for k in keys} for r in rows]}

    def notebook(self, notebook_id: str) -> dict:
        """WEB-R2: перекази одного ноутбука."""
        with Database(self.cfg.db_path) as db:
            nb = db.notebook(notebook_id)
            if nb is None:
                raise NotFound(f"ноутбук {notebook_id} не знайдено")
            rows = db.notebook_items(notebook_id)
        items = [
            {
                "id": r["id"],
                "title": r["title"],
                "created_at": r["created_at"],
                "duration": r["duration"],
                "state": r["state"],
                "job_id": r["job_id"],
                "job_status": r["job_status"],
                "attempts": r["attempts"],
                "audio_deleted": bool(r["audio_deleted"]),
                "audio_exists": bool(r["path"] and Path(r["path"]).is_file()),
                "url": f"https://youtu.be/{r['video_id']}" if r["video_id"] else None,
                "error": r["error"].splitlines()[0] if r["error"] else None,
            }
            for r in rows
        ]
        notebook = {k: nb[k] for k in ("id", "title", "slug", "url", "sources", "synced_at")}
        return {"ok": True, "notebook": notebook, "items": items}

    def daemon_state(self) -> dict:
        """WEB-R5: бюджет, квота, наступне вікно публікації, чи працює демон."""
        with Database(self.cfg.db_path) as db:
            quota = QuotaTracker(db, self.cfg.youtube.daily_quota)
            nxt = next_upload_at(self.cfg, db)
            data = {
                "uploads_today": db.uploads_count(),
                "videos_per_day": self.cfg.daemon.videos_per_day,
                "quota": {"remaining": quota.remaining(), "limit": quota.daily_limit},
                "next_upload_at": nxt.isoformat(timespec="seconds") if nxt else None,
            }
        data["running"] = daemon_lock(self.cfg).is_locked()
        data["ok"] = True
        return data

    def sync(self, dry_run: bool) -> dict:
        """WEB-R3/R4: синк з UI. Справжній — лише під замком демона, dry-run — без замка."""
        logs: list[str] = []
        lock = daemon_lock(self.cfg)
        if not dry_run and not lock.acquire():
            raise Busy("демон уже працює (замок зайнятий) — синк з UI не запущено, спробуйте пізніше")
        try:
            with Database(self.cfg.db_path) as db:
                service = SyncService(self.cfg, db=db, adapter=self.adapter, log=logs.append)
                report = service.run(dry_run=dry_run)
        finally:
            lock.release()
        return {
            "ok": True,
            "dry_run": dry_run,
            "report": report_json(report),
            "log": logs,
            "message": _sync_summary(report, dry_run),
        }

    # --- синк одного ноутбука (spec 017) ------------------------------------
    @staticmethod
    def _run_json(run: dict) -> dict:
        return {
            "notebook_id": run["notebook_id"],
            "title": run["title"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "processed": run["processed"],
            "total": run["total"],
            "remaining": run["remaining"],
            "error": run["error"],
            "message": run["message"],
            "report": run["report"],
            "jobs": list(run["jobs"]),
            "log": list(run["log"][-40:]),
        }

    def sync_state(self) -> dict | None:
        with self._sync_state_lock:
            return self._run_json(self.sync_run) if self.sync_run else None

    def _run_set(self, run: dict, **fields) -> None:
        with self._sync_state_lock:
            run.update(fields)

    def _run_log(self, run: dict, line: str) -> None:
        with self._sync_state_lock:
            run["log"].append(str(line))
            run["message"] = str(line)

    def start_notebook_sync(self, notebook_id: str) -> dict:
        """WEB3-R5: синк + рендер одного ноутбука у фоні (без публікації)."""
        with Database(self.cfg.db_path) as db:
            row = db.notebook(notebook_id)
        if row is None:
            raise NotFound(f"ноутбук {notebook_id} не знайдено")
        nb = dict(row)

        with self._sync_state_lock:
            active = self.sync_run
            if active and active["status"] == "running":
                raise Busy(f"уже виконується синк ноутбука «{active['title']}»")

        lock = daemon_lock(self.cfg)
        if not lock.acquire():
            raise Busy(
                "демон уже працює (замок зайнятий) — синк ноутбука не запущено, спробуйте пізніше"
            )
        run = {
            "notebook_id": notebook_id,
            "title": nb["title"],
            "status": "running",
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "finished_at": None,
            "processed": 0,
            "total": 0,
            "remaining": 0,
            "error": None,
            "message": "синхронізую…",
            "report": None,
            "jobs": [],
            "log": [],
        }
        with self._sync_state_lock:
            self.sync_run = run
        try:
            threading.Thread(
                target=self._notebook_sync_worker, args=(run, lock), daemon=True
            ).start()
        except Exception:
            lock.release()
            with self._sync_state_lock:
                self.sync_run = None
            raise
        with self._sync_state_lock:
            snapshot = self._run_json(run)
        return {"ok": True, "run": snapshot}

    def notebook_sync(self, notebook_id: str) -> dict:
        """WEB3-R9: стан фонового прогону для конкретного ноутбука."""
        with Database(self.cfg.db_path) as db:
            if db.notebook(notebook_id) is None:
                raise NotFound(f"ноутбук {notebook_id} не знайдено")
        with self._sync_state_lock:
            run = (
                self._run_json(self.sync_run)
                if self.sync_run and self.sync_run["notebook_id"] == notebook_id
                else None
            )
        return {"ok": True, "run": run}

    def _notebook_sync_worker(self, run: dict, lock) -> None:
        db = None
        try:
            db = Database(self.cfg.db_path)
            log = lambda line: self._run_log(run, line)  # noqa: E731 — короткий колбек
            pipe = Pipeline(self.cfg, db=db, log=log, interactive=False)
            service = SyncService(self.cfg, db=db, adapter=self.adapter, log=log)
            report = service.run(notebook=run["notebook_id"])
            self._run_set(run, report=report_json(report))
            self._run_log(run, _sync_summary(report, False))

            deleted = db.audio_deleted_job_ids()
            all_jobs = db.jobs_for_notebook(run["notebook_id"])
            pending = [
                job
                for job in all_jobs
                if job.video_id is None
                and STATUS_ORDER.get(job.status, 0) < STATUS_ORDER["rendered"]
            ]
            no_audio = [job for job in pending if job.id in deleted]
            missing = [job for job in pending if job.id not in deleted and not job.audio.exists()]
            exhausted = [
                job for job in pending if job.attempts >= self.cfg.daemon.max_attempts
            ]
            if no_audio:
                self._run_log(run, f"пропущено задач з видаленим аудіо: {len(no_audio)}")
            if missing:
                self._run_log(run, f"пропущено задач без аудіофайлу: {len(missing)}")
            if exhausted:
                self._run_log(
                    run,
                    f"пропущено задач після {self.cfg.daemon.max_attempts} спроб: {len(exhausted)}",
                )
            candidates = [
                job
                for job in pending
                if job.id not in deleted
                and job.audio.exists()
                and job.attempts < self.cfg.daemon.max_attempts
            ]
            limit = self.cfg.notebooklm.max_per_run
            selected = candidates[:limit]
            self._run_set(
                run, total=len(selected), remaining=max(0, len(candidates) - len(selected))
            )

            for job in selected:
                self._run_log(run, f"[{job.id}] {job.title or job.audio.name}: обробка…")
                job = db.update(job.id, error=None)
                job = self._advance_job(pipe, job)
                if job.error:
                    attempts = db.bump_attempt(job.id)
                    first = job.error.splitlines()[0]
                    self._run_log(run, f"[{job.id}] помилка (спроба {attempts}): {first}")
                else:
                    with self._sync_state_lock:
                        run["processed"] += 1
                        run["jobs"].append(
                            {
                                "id": job.id,
                                "title": job.title or job.audio.name,
                                "status": job.status,
                                "url": job.url,
                            }
                        )
                    self._run_log(run, f"[{job.id}] готово: {job.status}")

            with self._sync_state_lock:
                processed = run["processed"]
                remaining = run["remaining"]
            if not candidates:
                if pending:
                    message = f"синк готово; задач для рендеру немає (пропущено {len(pending)})"
                else:
                    message = "синк готово; незавершених задач немає"
            else:
                message = f"готово: відео {processed}"
                if remaining:
                    message += f"; ще {remaining} у черзі — натисніть «Синк + відео» ще раз"
            self._run_set(
                run,
                status="done",
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                message=message,
            )
        except Exception as e:  # noqa: BLE001 — фоновий прогін не має валити сервер
            self._run_set(
                run,
                status="error",
                error=f"{type(e).__name__}: {e}",
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                message=f"помилка: {e}",
            )
            self._run_log(run, f"ПОМИЛКА: {e}")
        finally:
            if db is not None:
                db.close()
            lock.release()

    def _advance_job(self, pipe: Pipeline, job: Job) -> Job:
        """Транскрипт → метадані → рендер для однієї задачі (без публікації)."""
        if STATUS_ORDER.get(job.status, 0) < STATUS_ORDER["transcribed"]:
            job = pipe.transcribe(job)
        if job.error:
            return job
        if STATUS_ORDER.get(job.status, 0) < STATUS_ORDER["described"]:
            job = pipe.describe(job)
        if job.error:
            return job
        if STATUS_ORDER.get(job.status, 0) < STATUS_ORDER["rendered"]:
            job = pipe.render(job)
        return job

    # --- медіа ---------------------------------------------------------------
    def media_path(self, rel: str) -> Path | None:
        rel = unquote(rel).lstrip("/")
        if not rel:
            return None
        root = self.cfg.root.resolve()
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        allowed = [self.cfg.output.resolve(), self.cfg.workspace.resolve()]
        if not any(_is_under(target, base) for base in allowed):
            return None
        return target if target.is_file() else None


def report_json(report: SyncReport) -> dict:
    return {
        "notebooks": report.notebooks,
        "downloaded": report.downloaded,
        "skipped": report.skipped,
        "planned": report.planned,
        "missing": report.missing,
        "restored": report.restored,
        "changed": report.changed,
        "deleted": report.deleted,
        "errors": list(report.errors),
        "items": [
            {"action": i.action, "notebook": i.notebook, "title": i.title, "message": i.message}
            for i in report.items
        ],
    }


def _sync_summary(report: SyncReport, dry_run: bool) -> str:
    head = f"план: до завантаження {report.planned}" if dry_run else f"скачано {report.downloaded}"
    parts = [head, f"пропущено {report.skipped}"]
    if report.missing:
        parts.append(f"зникли {report.missing}")
    if report.restored:
        parts.append(f"повернулися {report.restored}")
    if report.deleted:
        parts.append(f"видалено локально {report.deleted}")
    if report.errors:
        parts.append(f"помилок {len(report.errors)}")
    return ", ".join(parts)


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    app: WebApp
    server_version = "AudioTubeStudio"

    # --- утиліти -------------------------------------------------------------
    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        """Віддає файл потоком, з підтримкою Range (потрібно для перемотування відео)."""
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            raw = header.removeprefix("bytes=").split("-", 1)
            try:
                start = int(raw[0]) if raw[0] else 0
                end = int(raw[1]) if len(raw) > 1 and raw[1] else size - 1
            except ValueError:
                start, end = 0, size - 1
            if start >= size or start > end:
                self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                return
            end = min(end, size - 1)
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"ok": False, "message": message})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise WebError("занадто великий запит")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise WebError(f"некоректний JSON: {e}") from e
        return data if isinstance(data, dict) else {}

    def _parts(self) -> list[str]:
        return [p for p in urlparse(self.path).path.split("/") if p]

    # --- маршрути ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parts = self._parts()
        try:
            if not parts or parts[0] == "index.html":
                self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif parts[0] == "notebooks" and len(parts) <= 2:
                self._send(200, NOTEBOOKS_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif parts == ["api", "state"]:
                self._json(200, self.app.state())
            elif parts == ["api", "overview"]:
                self._json(200, self.app.overview())
            elif parts == ["api", "groups"]:
                self._json(200, self.app.groups())
            elif parts == ["api", "jobs"]:
                query = parse_qs(urlparse(self.path).query)
                self._json(
                    200,
                    self.app.search_jobs(
                        query.get("q", [""])[0], query.get("status", [""])[0]
                    ),
                )
            elif len(parts) == 4 and parts[:2] == ["api", "groups"] and parts[3] == "jobs":
                self._json(200, self.app.group_jobs(unquote(parts[2])))
            elif len(parts) == 4 and parts[:2] == ["api", "groups"] and parts[3] == "sync":
                self._json(200, self.app.notebook_sync(unquote(parts[2])))
            elif parts == ["api", "notebooks"]:
                self._json(200, self.app.notebooks())
            elif len(parts) == 3 and parts[:2] == ["api", "notebooks"]:
                self._json(200, self.app.notebook(unquote(parts[2])))
            elif parts == ["api", "daemon"]:
                self._json(200, self.app.daemon_state())
            elif parts == ["api", "browser"]:
                self._json(200, self.app.browser_snapshot())
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "meta":
                self._json(200, self.app.meta(int(parts[2])))
            elif len(parts) >= 2 and parts[0] == "media":
                path = self.app.media_path("/".join(parts[1:]))
                if not path:
                    self._error(404, "файл не знайдено")
                    return
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self._send_file(path, ctype)
            else:
                self._error(404, "немає такого маршруту")
        except NotFound as e:
            self._error(404, str(e))
        except (WebError, RuntimeError) as e:
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001 — веб-сервер не має падати через одну дію
            self._error(500, f"{type(e).__name__}: {e}")

    def do_POST(self) -> None:  # noqa: N802
        parts = self._parts()
        try:
            if parts == ["api", "scan"]:
                self._json(200, self.app.scan())
            elif parts == ["api", "sync"]:
                dry_run = "dry_run=1" in (urlparse(self.path).query or "")
                self._json(200, self.app.sync(dry_run=dry_run))
            elif len(parts) == 4 and parts[:2] == ["api", "groups"] and parts[3] == "sync":
                self._json(202, self.app.start_notebook_sync(unquote(parts[2])))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "browser-upload":
                query = parse_qs(urlparse(self.path).query)
                review = query.get("review", ["0"])[0] in ("1", "true")
                self._json(202, self.app.start_browser_upload(int(parts[2]), review=review))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                query = parse_qs(urlparse(self.path).query)
                force = query.get("force", ["0"])[0] in ("1", "true")
                privacy = query.get("privacy", ["public"])[0]
                self._json(
                    200,
                    self.app.action(int(parts[2]), parts[3], force=force, privacy=privacy),
                )
            else:
                self._error(404, "немає такого маршруту")
        except Busy as e:
            self._error(409, str(e))
        except NotFound as e:
            self._error(404, str(e))
        except RuntimeError as e:  # WebError, YouTubeError, TranscriptionError, LLMError, NotebookLMError
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._error(500, f"{type(e).__name__}: {e}")

    def do_PUT(self) -> None:  # noqa: N802
        parts = self._parts()
        try:
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "meta":
                data = self._body()
                self._json(200, self.app.save_meta(int(parts[2]), str(data.get("yaml", ""))))
            else:
                self._error(404, "немає такого маршруту")
        except RuntimeError as e:  # WebError, YouTubeError, TranscriptionError, LLMError
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._error(500, f"{type(e).__name__}: {e}")

    def log_message(self, fmt: str, *args) -> None:  # тихіше в консолі
        return


def make_server(app: WebApp) -> ThreadingHTTPServer:
    handler = type("AudioTubeHandler", (_Handler,), {"app": app})
    return ThreadingHTTPServer((app.host, app.port), handler)


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> int:
    app = WebApp(cfg, host, port)
    try:
        server = make_server(app)
    except OSError as e:
        print(f"не вдалося відкрити {host}:{port}: {e}. Спробуйте інший --port")
        return 1
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"AudioTube Studio: {url}  (Ctrl+C — зупинити)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nзупинено")
    finally:
        server.server_close()
    return 0


HTML = r"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AudioTube Studio</title>
<style>
  :root { --bg:#101418; --panel:#171d24; --panel2:#1f2731; --text:#e8eef5; --muted:#93a4b5;
          --accent:#4ea1ff; --ok:#41c98a; --warn:#ffb454; --err:#ff6b6b; --line:#2a3542; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  header { position:sticky; top:0; z-index:5; background:rgba(16,20,24,.96); border-bottom:1px solid var(--line);
           padding:14px 22px; display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:18px; margin:0; font-weight:650; letter-spacing:.2px; }
  .pill { padding:2px 9px; border-radius:99px; background:var(--panel2); color:var(--muted); font-size:12px; }
  .spacer { flex:1; }
  a.navlink { color:var(--accent); text-decoration:none; font-size:14px; }
  a.navlink:hover { text-decoration:underline; }
  button { background:var(--panel2); color:var(--text); border:1px solid var(--line); border-radius:8px;
           padding:7px 12px; font:inherit; font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#06121f; font-weight:600; }
  button.ghost { background:transparent; }
  button.danger { color:var(--err); }
  button.danger:hover { border-color:var(--err); }
  button:disabled { opacity:.45; cursor:wait; }
  main { padding:18px 22px 60px; max-width:1100px; margin:0 auto; }
  .empty { color:var(--muted); padding:40px 0; text-align:center; }
  .job { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:12px; }
  .job .top { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .job .title { font-weight:600; font-size:16px; }
  .job .sub { color:var(--muted); font-size:12px; margin-top:4px; }
  .badge { font-size:11px; text-transform:uppercase; letter-spacing:.4px; padding:3px 8px; border-radius:6px;
           background:#25303c; color:#b9c6d3; }
  .badge.transcribed { background:#20384a; color:#7cc4ff; }
  .badge.described { background:#2b2c4d; color:#a9a6ff; }
  .badge.rendered { background:#1f3d2f; color:#66d19e; }
  .badge.uploaded { background:#3d3320; color:#ffcd7a; }
  .badge.done { background:#1d3f38; color:#5fe0c0; }
  .err { color:var(--err); font-size:13px; margin-top:8px; white-space:pre-wrap; }
  .actions { display:flex; gap:7px; flex-wrap:wrap; margin-top:12px; }
  .files { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px; padding-top:10px;
           border-top:1px dashed var(--line); font-size:13px; }
  .files .f { color:var(--muted); }
  .files .gone { color:var(--warn); }
  .meta-box { display:flex; gap:14px; align-items:flex-start; margin-top:12px; }
  .meta-box img { width:180px; border-radius:8px; border:1px solid var(--line); }
  audio, video { max-width:100%; border-radius:8px; }
  #log { position:fixed; left:0; right:0; bottom:0; max-height:26vh; overflow:auto; background:#0b0f13;
         border-top:1px solid var(--line); font:12px/1.5 ui-monospace, monospace; padding:8px 14px; color:#a9bccd;
         white-space:pre-wrap; display:none; }
  #log.on { display:block; }
  #log b { color:#dfe9f3; }
  dialog { background:var(--panel); color:var(--text); border:1px solid var(--line); border-radius:12px;
           width:min(860px, 94vw); padding:0; }
  dialog::backdrop { background:rgba(0,0,0,.6); }
  .dlg-head { display:flex; align-items:center; gap:10px; padding:14px 16px; border-bottom:1px solid var(--line); }
  .dlg-head h2 { font-size:15px; margin:0; }
  .dlg-body { padding:14px 16px; }
  textarea { width:100%; height:52vh; background:#0d1217; color:#dbe6f1; border:1px solid var(--line);
             border-radius:8px; padding:12px; font:13px/1.5 ui-monospace, monospace; resize:vertical; }
  .dlg-foot { display:flex; gap:8px; justify-content:flex-end; padding:0 16px 16px; }
  .busy { position:fixed; inset:0; background:rgba(8,11,14,.55); display:none; align-items:center;
          justify-content:center; z-index:20; font-size:15px; }
  .busy.on { display:flex; }
  .spin { width:28px; height:28px; border:3px solid #35455a; border-top-color:var(--accent);
          border-radius:50%; animation:sp 0.8s linear infinite; margin-right:12px; }
  @keyframes sp { to { transform:rotate(360deg); } }
  .hint { color:var(--muted); font-size:12px; }
  .muted { color:var(--muted); }
  .filters { display:flex; gap:10px; margin-bottom:14px; flex-wrap:wrap; }
  .filters input[type="search"], .filters select { background:var(--panel2); color:var(--text); border:1px solid var(--line);
           border-radius:8px; padding:8px 11px; font:inherit; font-size:14px; }
  .filters input[type="search"] { flex:1; min-width:220px; }
  .filters input[type="checkbox"] { width:15px; height:15px; margin:0; padding:0; flex:none; accent-color:var(--accent); }
  .filters .hint { align-self:center; }
  .grp { background:var(--panel); border:1px solid var(--line); border-radius:12px; margin-bottom:8px; overflow:hidden; }
  .grp-row { display:flex; align-items:stretch; }
  .grp-head { flex:1; min-width:0; display:flex; gap:10px; align-items:flex-start; background:none; border:0;
              border-radius:0; padding:11px 14px; color:inherit; text-align:left; font:inherit; cursor:pointer; }
  .grp-head:hover { background:var(--panel2); }
  .grp-head:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
  .chev { color:var(--muted); font-size:11px; line-height:20px; transition:transform .15s ease; }
  .grp.open .chev { transform:rotate(90deg); }
  .grp-main { flex:1; min-width:0; }
  .grp-title { display:block; font-weight:600; font-size:14.5px; line-height:1.3;
               overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .grp-meta { display:block; margin-top:3px; color:var(--muted); font-size:12px; line-height:1.5; }
  .grp-meta b { color:var(--text); font-weight:600; font-variant-numeric:tabular-nums; }
  .grp-meta .warn b { color:var(--warn); }
  .grp-meta .bad b { color:var(--err); }
  .grp-meta .ok b { color:var(--ok); }
  .grp-syncdate { color:var(--muted); font-size:11.5px; white-space:nowrap; margin-top:3px; }
  .grp-sync { flex:none; align-self:center; margin:0 12px; font-size:12px; padding:6px 10px; }
  .grp-body { border-top:1px solid var(--line); padding:0 14px 4px; background:rgba(0,0,0,.08); }
  .grp-jobs { padding-top:12px; }
  .grp-jobs .job { margin-bottom:12px; }
  .grp-status { margin-top:12px; padding:8px 10px; border:1px solid var(--line); border-radius:8px;
                font-size:12.5px; color:var(--muted); display:none; }
  .grp-status.on { display:block; }
  .grp-status.running { border-color:var(--accent); color:var(--text); }
  .grp-status.done { border-color:var(--ok); color:var(--ok); }
  .grp-status.error { border-color:var(--err); color:var(--err); }
  .grp-status .bar { height:4px; background:var(--line); border-radius:99px; margin-top:6px; overflow:hidden; }
  .grp-status .bar i { display:block; height:100%; background:var(--accent); width:0; transition:width .3s; }
  .search-head { color:var(--muted); font-size:13px; margin:0 0 10px; }
  .job .nb-tag { color:var(--accent); }
  .job .nb-tag::before { content:" · "; color:var(--muted); }
  @media (max-width:640px) {
    .grp-row { flex-wrap:wrap; }
    .grp-sync { margin:0 14px 12px 34px; }
    .grp-title { white-space:normal; }
    .grp-syncdate { margin-left:20px; }
  }
  select.privacy { background:var(--panel2); color:var(--text); border:1px solid var(--line);
           border-radius:8px; padding:7px 10px; font:inherit; font-size:13px; }
</style>
</head>
<body>
<header>
  <h1>AudioTube Studio</h1>
  <span class="pill" id="quota">квота: …</span>
  <span class="pill" id="cfg">…</span>
  <span class="pill" id="counts">…</span>
  <span class="spacer"></span>
  <a class="navlink" href="/notebooks">Ноутбуки</a>
  <button class="ghost" id="toggle-log">Журнал</button>
  <button class="primary" id="scan">Сканувати inbox</button>
</header>
<main>
  <div class="filters">
    <input id="q" type="search" placeholder="пошук за задачею, файлом або ноутбуком">
    <select id="st">
      <option value="">усі статуси</option>
      <option value="new">нове</option>
      <option value="transcribed">транскрипт</option>
      <option value="described">метадані</option>
      <option value="rendered">відрендерено</option>
      <option value="uploaded">на YouTube</option>
      <option value="done">готово</option>
    </select>
    <label class="hint" style="display:flex;align-items:center;gap:6px">
      <input type="checkbox" id="browser-review"> перегляд перед публікацією
    </label>
    <span class="hint" id="result-info"></span>
  </div>
  <div id="jobs"><div class="empty">Завантаження…</div></div>
</main>
<div id="log"></div>
<div class="busy" id="busy"><div class="spin"></div><span id="busy-text">працюю…</span></div>

<dialog id="meta-dlg">
  <div class="dlg-head">
    <h2>Метадані</h2>
    <span class="hint" id="meta-path"></span>
    <span class="spacer" style="flex:1"></span>
    <button class="ghost" onclick="document.getElementById('meta-dlg').close()">Закрити</button>
  </div>
  <div class="dlg-body"><textarea id="meta-text" spellcheck="false"></textarea></div>
  <div class="dlg-foot">
    <span class="hint" style="margin-right:auto">YAML перевіряється перед збереженням</span>
    <button class="primary" id="meta-save">Зберегти</button>
  </div>
</dialog>

<script>

const UA = {new:"нове", transcribed:"транскрипт", described:"метадані", rendered:"відрендерено",
            uploaded:"на YouTube", done:"готово"};
let overview = {counts:{}, files:{}, quota:{used:0, limit:0}, config:{}, sync:null};
let groups = [];
let groupsByKey = new Map();
let openKey = null;
let jobsCache = new Map();
let jobsLoading = new Set();
let busyCount = 0;
let searchTimer = null;
let searchSeq = 0;
let activeRun = null;
let syncPoll = null;
let browserPoll = null;

function setBusy(on, text) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  document.getElementById("busy").classList.toggle("on", busyCount > 0);
  if (text) document.getElementById("busy-text").textContent = text;
}
function setLog(lines) {
  const el = document.getElementById("log");
  el.classList.add("on");
  el.innerHTML = (lines || []).filter(Boolean).map(l => esc(l)).join("\n");
  el.scrollTop = el.scrollHeight;
}
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtDur(s) {
  if (!s) return "—";
  s = Math.round(s); const m = Math.floor(s/60), r = s%60;
  return Math.floor(m/60) ? `${Math.floor(m/60)}:${String(m%60).padStart(2,'0')}:${String(r).padStart(2,'0')}`
                          : `${m}:${String(r).padStart(2,'0')}`;
}
function fmtSize(n) {
  if (n === null || n === undefined) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let value = n, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${i ? value.toFixed(1) : Math.round(value)} ${units[i]}`;
}
function fmtDate(iso) {
  if (!iso) return "не синкався";
  const d = String(iso).slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(d) ? `синк ${d.split("-").reverse().join(".")}` : esc(iso);
}
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}
async function api(path, opts = {}) {
  const res = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  let data;
  try { data = await res.json(); } catch { throw new Error(`HTTP ${res.status}`); }
  if (!res.ok || data.ok === false) throw new Error(data.message || `HTTP ${res.status}`);
  return data;
}
function isFiltering() {
  return !!(document.getElementById("q").value.trim() || document.getElementById("st").value);
}
function renderHeader() {
  const q = overview.quota || {used: 0, limit: 0};
  const c = overview.config || {};
  const cnt = overview.counts || {};
  const f = overview.files || {};
  document.getElementById("quota").textContent = `квота: ${q.used} / ${q.limit}`;
  document.getElementById("cfg").textContent =
    `${c.llm || "—"} · ${c.transcribe || "—"} · ffmpeg: ${c.ffmpeg ? "ok" : "немає"}`;
  document.getElementById("counts").textContent =
    `${cnt.total || 0} ${plural(cnt.total || 0, "задача", "задачі", "задач")} · ` +
    `на YouTube ${cnt.published || 0} · готових ${cnt.ready_to_publish || 0} · ` +
    `inbox ${f.inbox_audio || 0} (поза чергою ${f.inbox_unregistered || 0}) · аудіо ${fmtSize(f.inbox_bytes || 0)} · ` +
    `mp4 ${f.output_videos || 0} · відео ${fmtSize(f.output_bytes || 0)}`;
}

function groupMeta(g) {
  if (!g.jobs) return `<span class="muted">порожньо</span>`;
  const bits = [`<b>${g.jobs}</b> ${plural(g.jobs, "задача", "задачі", "задач")}`];
  const st = g.by_status || {};
  if (st.new) bits.push(`<span class="warn"><b>${st.new}</b> нових</span>`);
  if (g.ready) bits.push(`<b>${g.ready}</b> відео`);
  if (g.published) bits.push(`<span class="ok"><b>${g.published}</b> на YouTube</span>`);
  if (g.errors) bits.push(`<span class="bad"><b>${g.errors}</b> ${plural(g.errors, "помилка", "помилки", "помилок")}</span>`);
  if (g.audio_deleted) bits.push(`<span class="warn"><b>${g.audio_deleted}</b> без аудіо</span>`);
  return bits.join(" · ");
}
function groupHtml(g) {
  const key = esc(g.key);
  const safe = key.replace(/[^a-zA-Z0-9_-]/g, "_");
  const sync = g.can_sync
    ? `<button class="grp-sync" type="button" data-sync="${key}">Синк + відео</button>`
    : `<span class="hint" style="align-self:center;margin:0 12px">без NotebookLM</span>`;
  const syncDate = g.can_sync ? fmtDate(g.synced_at) : "";
  return `<div class="grp" data-key="${key}">
    <div class="grp-row">
      <button class="grp-head" type="button" aria-expanded="false" aria-controls="grp-body-${safe}">
        <span class="chev" aria-hidden="true">▶</span>
        <span class="grp-main">
          <span class="grp-title">${esc(g.title)}</span>
          <span class="grp-meta">${groupMeta(g)}</span>
        </span>
        <span class="grp-syncdate" title="${esc(g.synced_at || "")}">${syncDate}</span>
      </button>
      ${sync}
    </div>
    <div class="grp-body" id="grp-body-${safe}" role="region" hidden></div>
  </div>`;
}
function rowForGroup(key) {
  return Array.from(document.querySelectorAll(".grp")).find(r => r.dataset.key === key) || null;
}
function ensureGroupBody(row) {
  const body = row.querySelector(".grp-body");
  if (!body) return null;
  if (!body.dataset.ready) {
    body.innerHTML = `<div class="grp-status" role="status" aria-label="Прогрес синку"></div>
      <div class="grp-jobs"><div class="empty">Завантаження задач…</div></div>`;
    body.dataset.ready = "1";
  }
  return body;
}
function setGroupOpen(row, open) {
  row.classList.toggle("open", open);
  const head = row.querySelector(".grp-head");
  if (head) head.setAttribute("aria-expanded", open ? "true" : "false");
  const body = row.querySelector(".grp-body");
  if (body) {
    body.hidden = !open;
    if (open) ensureGroupBody(row);
  }
}
function openGroup(key, {push = true} = {}) {
  if (!groupsByKey.has(key)) return;
  if (openKey === key) {
    const same = rowForGroup(key);
    if (same) setGroupOpen(same, true);
    loadGroupJobs(key);
    return;
  }
  if (openKey) {
    const prev = rowForGroup(openKey);
    if (prev) setGroupOpen(prev, false);
  }
  openKey = key;
  const row = rowForGroup(key);
  if (row) setGroupOpen(row, true);
  if (push) history.pushState({group: key}, "", `/?group=${encodeURIComponent(key)}`);
  if (row) loadGroupJobs(key);
  if (activeRun && activeRun.notebook_id === key) showSyncStatus(key, activeRun);
}
function closeGroup({push = true} = {}) {
  if (!openKey) return;
  const row = rowForGroup(openKey);
  if (row) setGroupOpen(row, false);
  openKey = null;
  if (push) history.pushState({}, "", "/");
}
function toggleGroup(key) {
  if (openKey === key) closeGroup();
  else openGroup(key);
}
function renderJobsInto(box, jobs, key) {
  if (!jobs.length) {
    box.innerHTML = key === "__none__"
      ? `<div class="empty">Задач без ноутбука немає.</div>`
      : `<div class="empty">Тут ще немає задач. Натисніть «Синк + відео».</div>`;
    return;
  }
  box.innerHTML = jobs.map(j => jobCard(j)).join("");
}
async function loadGroupJobs(key, {force = false} = {}) {
  const row = rowForGroup(key);
  if (!row) return;
  const body = ensureGroupBody(row);
  const box = body ? body.querySelector(".grp-jobs") : null;
  if (!box) return;
  if (!force && jobsCache.has(key)) {
    renderJobsInto(box, jobsCache.get(key), key);
    return;
  }
  if (jobsLoading.has(key)) return;
  jobsLoading.add(key);
  if (!jobsCache.has(key)) box.innerHTML = `<div class="empty">Завантаження задач…</div>`;
  try {
    const data = await api(`/api/groups/${encodeURIComponent(key)}/jobs`);
    jobsCache.set(key, data.jobs);
    if (openKey === key) {
      const current = rowForGroup(key);
      const currentBox = current ? current.querySelector(".grp-jobs") : null;
      if (currentBox) renderJobsInto(currentBox, data.jobs, key);
    }
  } catch (e) {
    if (openKey === key) {
      const current = rowForGroup(key);
      const currentBox = current ? current.querySelector(".grp-jobs") : null;
      if (currentBox) currentBox.innerHTML = `<div class="empty bad">Помилка: ${esc(e.message)}</div>`;
    }
  } finally {
    jobsLoading.delete(key);
  }
}
function jobCard(j, showGroup = false) {
  const badge = `<span class="badge ${j.status}">${UA[j.status] || j.status}</span>`;
  const yt = j.url ? ` · <a href="${esc(j.url)}" target="_blank" style="color:var(--accent)">${esc(j.url)}</a>` : "";
  const flags = [j.has_transcript ? "текст" : "", j.subtitles ? "srt" : "", j.captions ? "субтитри YT" : "",
                 j.playlist ? "плейлист" : ""].filter(Boolean).join(" · ");
  const nb = showGroup && j.notebook_title ? `<span class="nb-tag">${esc(j.notebook_title)}</span>` : "";
  const thumb = j.thumbnail ? `<img src="/media/${encodeURI(j.thumbnail)}" alt="">` : "";
  const preview = j.preview ? `<video controls preload="metadata" src="/media/${encodeURI(j.preview)}"></video>` : "";
  const audioInfo = j.audio_exists
    ? `аудіо ${fmtSize(j.audio_size)}`
    : (j.audio_deleted ? `<span class="gone">аудіо видалено (було скачано)</span>`
                       : `<span class="gone">аудіо відсутнє</span>`);
  const videoInfo = j.video_exists
    ? `відео ${fmtSize(j.video_size)}`
    : (j.url ? `<span class="gone">відео видалено · на YouTube є</span>` : "відео не відрендерено");
  const files = `<div class="files">
    <span class="f">${audioInfo}</span>
    ${j.audio_exists ? `<button class="ghost danger" onclick="deleteAudio(${j.id})">Видалити аудіо</button>` : ""}
    ${j.audio_deleted ? `<button class="ghost" onclick="restoreAudio(${j.id})">Відновити аудіо</button>` : ""}
    <span class="f">${videoInfo}</span>
    ${j.video_exists ? `<button class="ghost danger" onclick="deleteVideo(${j.id})">Видалити відео</button>` : ""}
  </div>`;
  const noAudio = j.audio_exists ? "" : `disabled title="немає аудіо — відновіть його"`;
  return `<div class="job">
    <div class="top">${badge}<span class="title">${esc(j.title)}</span>
      <span class="hint">${fmtDur(j.duration)}</span></div>
    <div class="sub">#${j.id} · ${esc(j.audio)}${nb} · ${esc(j.status)}${yt}${flags ? " · " + flags : ""}</div>
    ${j.error ? `<div class="err">${esc(j.error)}</div>` : ""}
    <div class="meta-box">${thumb}<div>${preview}</div></div>
    ${files}
    <div class="actions">
      <button ${noAudio} onclick="act(${j.id},'transcribe')">Транскрипт</button>
      <button onclick="act(${j.id},'describe')">Метадані LLM</button>
      <button class="ghost" onclick="editMeta(${j.id})">Редагувати</button>
      <button class="ghost" ${noAudio} onclick="act(${j.id},'preview')">Прев'ю 20с</button>
      <button ${noAudio} onclick="act(${j.id},'render',${j.video_exists ? "false" : "true"})">Рендер</button>
      ${j.url
        ? `<select class="privacy" id="privacy-${j.id}">
             <option value="public">public</option>
             <option value="unlisted">unlisted</option>
             <option value="private">private</option>
           </select>
           <button onclick="publishJob(${j.id})">Змінити приватність</button>`
        : `<button class="primary" onclick="act(${j.id},'upload')">Опублікувати</button>
           ${j.video_exists ? `<button class="ghost" onclick="browserUpload(${j.id})">Через браузер</button>` : ""}`}
      <button class="ghost" onclick="act(${j.id},'reset')">Скинути</button>
    </div>
  </div>`;
}
function renderGroups() {
  if (isFiltering()) { runSearch(); return; }
  const box = document.getElementById("jobs");
  document.getElementById("result-info").textContent = "";
  if (!groups.length) {
    box.innerHTML = `<div class="empty">Ще немає ноутбуків. Натисніть «Сканувати inbox» або синхронізуйте з NotebookLM.</div>`;
    return;
  }
  box.innerHTML = groups.map(groupHtml).join("");
  if (openKey) {
    const row = rowForGroup(openKey);
    if (row) {
      setGroupOpen(row, true);
      loadGroupJobs(openKey);
    }
  }
}
async function loadGroups({force = false} = {}) {
  try {
    const data = await api("/api/groups");
    groups = data.groups || [];
    groupsByKey = new Map(groups.map(g => [g.key, g]));
    if (force) jobsCache.clear();
    renderGroups();
    maybeResumeSync();
  } catch (e) {
    document.getElementById("jobs").innerHTML = `<div class="empty bad">Помилка: ${esc(e.message)}</div>`;
  }
}
async function loadOverview() {
  try {
    const data = await api("/api/overview");
    overview = data;
    activeRun = data.sync || null;
    renderHeader();
  } catch (e) {
    setLog([`Помилка: ${e.message}`]);
  }
}
function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (isFiltering()) runSearch();
    else loadGroups();
  }, 250);
}
async function runSearch() {
  const q = document.getElementById("q").value.trim();
  const st = document.getElementById("st").value;
  const seq = ++searchSeq;
  const box = document.getElementById("jobs");
  const info = document.getElementById("result-info");
  box.innerHTML = `<div class="empty">Пошук…</div>`;
  try {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (st) params.set("status", st);
    const data = await api(`/api/jobs?${params.toString()}`);
    if (seq !== searchSeq) return;
    if (!data.jobs.length) {
      box.innerHTML = `<div class="empty">Нічого не знайдено.</div>`;
      info.textContent = "Знайдено 0";
      return;
    }
    info.textContent = `Знайдено ${data.total}`;
    box.innerHTML = data.jobs.map(j => jobCard(j, true)).join("");
  } catch (e) {
    if (seq === searchSeq) {
      box.innerHTML = `<div class="empty bad">Помилка: ${esc(e.message)}</div>`;
      info.textContent = "";
    }
  }
}
async function afterChange() {
  jobsCache.clear();
  await loadOverview();
  await loadGroups();
}
async function act(id, action, force = false) {
  setBusy(true, action === "render" ? "рендер… це може тривати кілька хвилин"
              : action === "describe" ? "LLM генерує метадані…"
              : action === "upload" ? "завантаження на YouTube…"
              : action === "delete_audio" ? "видаляю аудіо…"
              : action === "delete_video" ? "видаляю відео…"
              : action === "restore_audio" ? "відновлюю позначку…" : "працюю…");
  try {
    const data = await api(`/api/jobs/${id}/${action}${force ? "?force=1" : ""}`, {method: "POST"});
    setLog(data.log && data.log.length ? data.log : [data.message]);
  } catch (e) {
    setLog([`Помилка: ${e.message}`]);
  } finally {
    setBusy(false);
    await afterChange();
  }
}
async function deleteAudio(id) {
  if (!confirm("Видалити аудіофайл? Задача, метадані й відео на YouTube лишаться. " +
               "Синк не качатиме це аудіо повторно; можна відновити кнопкою «Відновити аудіо».")) return;
  await act(id, "delete_audio");
}
async function deleteVideo(id) {
  if (!confirm("Видалити локальні mp4 (відео і прев'ю)? Якщо відео вже на YouTube, воно там лишиться; " +
               "файл можна відрендерити знову.")) return;
  await act(id, "delete_video");
}
async function restoreAudio(id) { await act(id, "restore_audio"); }
async function publishJob(id) {
  const sel = document.getElementById(`privacy-${id}`);
  const privacy = sel ? sel.value : "public";
  setBusy(true, `зміна приватності → ${privacy}…`);
  try {
    const data = await api(`/api/jobs/${id}/publish?privacy=${encodeURIComponent(privacy)}`, {method: "POST"});
    setLog(data.log && data.log.length ? data.log : [data.message]);
  } catch (e) {
    setLog([`Помилка: ${e.message}`]);
  } finally {
    setBusy(false);
    await afterChange();
  }
}
async function browserUpload(id) {
  const box = document.getElementById("browser-review");
  const review = box ? box.checked : false;
  try {
    await api(`/api/jobs/${id}/browser-upload${review ? "?review=1" : ""}`, {method: "POST"});
    setLog(["браузер відкрито. Заповнюю поля у YouTube Studio…",
            review ? "перевірте відео й натисніть «Опублікувати» у вікні браузера"
                   : "публікую автоматично…"]);
    pollBrowser();
  } catch (e) { setLog([`Помилка: ${e.message}`]); }
}
function pollBrowser() {
  if (browserPoll) return;
  browserPoll = setInterval(async () => {
    try {
      const s = await api("/api/browser");
      setLog(s.log && s.log.length ? s.log : [s.message || "працюю…"]);
      if (s.status !== "running") {
        clearInterval(browserPoll); browserPoll = null;
        await afterChange();
      }
    } catch (e) {
      clearInterval(browserPoll); browserPoll = null;
      setLog([`Помилка: ${e.message}`]);
    }
  }, 2000);
}
async function editMeta(id) {
  try {
    const data = await api(`/api/jobs/${id}/meta`);
    document.getElementById("meta-text").value = data.yaml;
    document.getElementById("meta-path").textContent = data.path.split("/").slice(-1)[0];
    document.getElementById("meta-save").onclick = () => saveMeta(id);
    document.getElementById("meta-dlg").showModal();
  } catch (e) { setLog([`Помилка: ${e.message}`]); }
}
async function saveMeta(id) {
  const yamlText = document.getElementById("meta-text").value;
  try {
    const data = await api(`/api/jobs/${id}/meta`, {method: "PUT", body: JSON.stringify({yaml: yamlText})});
    document.getElementById("meta-dlg").close();
    setLog([data.message]);
  } catch (e) { setLog([`Помилка: ${e.message}`]); }
  await afterChange();
}
function setSyncButtonsDisabled(disabled, runningKey = null) {
  document.querySelectorAll(".grp-sync").forEach(btn => {
    btn.disabled = disabled;
    if (disabled && runningKey && btn.dataset.sync === runningKey) btn.textContent = "Синк…";
    else btn.textContent = "Синк + відео";
  });
}
function showSyncStatus(key, run, {forceOpen = false} = {}) {
  const row = rowForGroup(key);
  if (!row) return;
  if (openKey !== key) {
    if (!forceOpen) return;
    openGroup(key, {push: false});
  }
  const body = ensureGroupBody(row);
  if (!body) return;
  const status = body.querySelector(".grp-status");
  if (!status) return;
  status.classList.add("on");
  status.classList.remove("running", "done", "error");
  status.classList.add(run.status);
  let line = run.message || (run.status === "running" ? "синхронізую…" : "");
  if (run.status === "error" && run.error) line = `Помилка: ${run.error}`;
  if (run.total) line += ` · ${run.processed || 0}/${run.total}`;
  if (run.status === "done" && run.remaining) line += ` · залишилось ${run.remaining}`;
  let progress = "";
  if (run.status === "running" && run.total) {
    const pct = Math.max(0, Math.min(100, Math.round((run.processed || 0) / run.total * 100)));
    progress = `<div class="bar"><i style="width:${pct}%"></i></div>`;
  }
  status.innerHTML = `<div>${esc(line)}</div>${progress}`;
}
function stopSyncPolling() {
  if (syncPoll) { clearInterval(syncPoll); syncPoll = null; }
}
function startSyncPolling(key) {
  stopSyncPolling();
  syncPoll = setInterval(async () => {
    try {
      const data = await api(`/api/groups/${encodeURIComponent(key)}/sync`);
      if (!data.run) {
        stopSyncPolling();
        setSyncButtonsDisabled(false);
        return;
      }
      activeRun = data.run;
      showSyncStatus(key, activeRun);
      if (activeRun.status === "running") return;
      stopSyncPolling();
      setSyncButtonsDisabled(false);
      const lines = [activeRun.message || "синк завершено"];
      if (activeRun.report) {
        if (activeRun.report.errors && activeRun.report.errors.length) {
          lines.push(...activeRun.report.errors.map(e => "помилка: " + e));
        }
      }
      if (activeRun.log && activeRun.log.length) lines.push(...activeRun.log.slice(-8));
      setLog(lines);
      jobsCache.delete(key);
      await loadOverview();
      await loadGroups();
      showSyncStatus(key, activeRun);
    } catch (e) {
      stopSyncPolling();
      setSyncButtonsDisabled(false);
      setLog([`Помилка: ${e.message}`]);
    }
  }, 1500);
}
async function syncGroup(key) {
  if (activeRun && activeRun.status === "running") {
    setLog(["інший синк уже виконується — дочекайтеся завершення"]);
    return;
  }
  const g = groupsByKey.get(key);
  if (!g || !g.can_sync) return;
  if (!confirm(`Синхронізувати «${g.title}»?\n\n` +
               "Завантажаться нові аудіо цього ноутбука, потім відрендеряться відео " +
               "для незавершених задач. Публікації на YouTube не буде. Це може тривати.")) return;
  openGroup(key, {push: true});
  setSyncButtonsDisabled(true, key);
  try {
    const data = await api(`/api/groups/${encodeURIComponent(key)}/sync`, {method: "POST"});
    activeRun = data.run;
    showSyncStatus(key, activeRun, {forceOpen: true});
    startSyncPolling(key);
  } catch (e) {
    setLog([`Помилка: ${e.message}`]);
    setSyncButtonsDisabled(false);
  }
}
function maybeResumeSync() {
  const run = overview.sync;
  if (!run || run.status !== "running" || syncPoll) return;
  if (!groupsByKey.has(run.notebook_id)) return;
  activeRun = run;
  openGroup(run.notebook_id, {push: false});
  setSyncButtonsDisabled(true, run.notebook_id);
  showSyncStatus(run.notebook_id, run, {forceOpen: true});
  startSyncPolling(run.notebook_id);
}
function syncFromUrl() {
  const key = new URLSearchParams(location.search).get("group");
  if (!key) { closeGroup({push: false}); return; }
  if (groupsByKey.has(key) && openKey !== key) openGroup(key, {push: false});
}
document.getElementById("jobs").addEventListener("click", e => {
  const sync = e.target.closest(".grp-sync");
  if (sync) { syncGroup(sync.dataset.sync); return; }
  const head = e.target.closest(".grp-head");
  if (head) {
    const row = head.closest(".grp");
    if (row) toggleGroup(row.dataset.key);
  }
});
document.getElementById("q").addEventListener("input", scheduleSearch);
document.getElementById("st").addEventListener("change", () => {
  if (isFiltering()) runSearch();
  else loadGroups();
});
window.addEventListener("popstate", syncFromUrl);
document.getElementById("scan").onclick = async () => {
  setBusy(true, "сканую inbox…");
  try {
    const data = await api("/api/scan", {method: "POST"});
    setLog(data.log && data.log.length ? data.log : [data.message]);
  } catch (e) { setLog([`Помилка: ${e.message}`]); }
  finally { setBusy(false); await afterChange(); }
};
document.getElementById("toggle-log").onclick = () => document.getElementById("log").classList.toggle("on");
async function init() {
  const fromUrl = new URLSearchParams(location.search).get("group");
  if (fromUrl) openKey = fromUrl;
  await Promise.all([loadOverview(), loadGroups()]);
  if (overview.sync) activeRun = overview.sync;
  maybeResumeSync();
}
init();
setInterval(loadOverview, 15000);

</script>
</body>
</html>
"""


NOTEBOOKS_HTML = r"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ноутбуки · AudioTube Studio</title>
<style>
  :root { --bg:#101418; --panel:#171d24; --panel2:#1f2731; --text:#e8eef5; --muted:#93a4b5;
          --accent:#4ea1ff; --ok:#41c98a; --warn:#ffb454; --err:#ff6b6b; --line:#2a3542; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  header { position:sticky; top:0; z-index:5; background:rgba(16,20,24,.96); border-bottom:1px solid var(--line);
           padding:14px 22px; display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:18px; margin:0; font-weight:650; }
  h2 { font-size:16px; margin:22px 0 10px; }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .spacer { flex:1; }
  main { padding:18px 22px 60px; max-width:1100px; margin:0 auto; }
  button { background:var(--panel2); color:var(--text); border:1px solid var(--line); border-radius:8px;
           padding:7px 12px; font:inherit; font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button:disabled { opacity:.45; cursor:wait; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#06121f; font-weight:600; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px;
           display:flex; gap:22px; flex-wrap:wrap; }
  .stat .k { color:var(--muted); font-size:12px; }
  .stat .v { font-size:17px; font-weight:600; font-variant-numeric:tabular-nums; }
  .table-wrap { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line);
          border-radius:12px; overflow:hidden; }
  th, td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); font-size:14px; }
  th { color:var(--muted); font-weight:500; font-size:12px; }
  td.n { text-align:right; font-variant-numeric:tabular-nums; }
  tr:last-child td { border-bottom:none; }
  .ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--err); } .muted { color:var(--muted); }
  .empty { color:var(--muted); padding:40px 0; text-align:center; }
  .toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:14px 0 10px; }
  .toolbar input[type="search"], .toolbar select { background:var(--panel); color:var(--text);
    border:1px solid var(--line); border-radius:8px; padding:8px 12px; font:inherit; font-size:14px; }
  .toolbar input[type="search"] { flex:1; min-width:200px; }
  .toolbar input[type="search"]:focus, .toolbar select:focus { outline:none; border-color:var(--accent); }
  .toolbar input:disabled, .toolbar select:disabled { opacity:.5; }
  .nb-list { display:flex; flex-direction:column; gap:8px; }
  .nb { background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .nb.hidden { display:none; }
  .nb-head { width:100%; display:flex; gap:10px; align-items:flex-start; background:none; border:0;
             border-radius:0; padding:11px 14px; color:inherit; text-align:left; font:inherit; cursor:pointer; }
  .nb-head:hover { background:var(--panel2); }
  .nb-head:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
  .chev { color:var(--muted); font-size:11px; line-height:20px; transition:transform .15s ease; }
  .nb.open .chev { transform:rotate(90deg); }
  .nb-main { flex:1; min-width:0; }
  .nb-title { display:block; font-weight:600; font-size:14.5px; line-height:1.3;
              overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .nb-meta { display:block; margin-top:3px; color:var(--muted); font-size:12px; line-height:1.5; }
  .nb-meta .cnt b { color:var(--text); font-weight:600; font-variant-numeric:tabular-nums; }
  .nb-meta .cnt.ok b { color:var(--ok); }
  .nb-meta .cnt.warn b { color:var(--warn); }
  .nb-meta .cnt.bad b { color:var(--err); }
  .nb-sync { color:var(--muted); font-size:11.5px; white-space:nowrap; margin-top:3px; }
  .nb-body { border-top:1px solid var(--line); padding:0 14px 14px; background:rgba(0,0,0,.08); }
  .nb-body-head { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-top:12px;
                  font-size:13px; color:var(--muted); }
  .nb-body .table-wrap { margin-top:12px; }
  .nb-body table { background:transparent; }
  .nb-body .empty { padding:22px 0; }
  @media (max-width:640px) {
    .nb-head { flex-wrap:wrap; }
    .nb-title { white-space:normal; }
    .nb-sync { margin-left:20px; }
  }
  #log { margin-top:18px; background:#0b0f13; border:1px solid var(--line); border-radius:10px;
         font:12px/1.5 ui-monospace, monospace; padding:10px 14px; color:#a9bccd; white-space:pre-wrap; display:none; }
  #log.on { display:block; }
  @media (max-width:640px) { header, main { padding-left:16px; padding-right:16px; } }
</style>
</head>
<body>
<header>
  <h1><a href="/notebooks" style="color:inherit">Ноутбуки</a></h1>
  <a href="/">← Черга задач</a>
  <span class="spacer"></span>
  <button id="sync-plan">Синк (план)</button>
  <button class="primary" id="sync-run">Синк</button>
</header>
<main>
  <div class="panel" id="daemon">Завантаження…</div>
  <div class="toolbar">
    <input id="nb-search" type="search" placeholder="Пошук ноутбука за назвою…" autocomplete="off"
           aria-label="Пошук ноутбука">
    <select id="nb-filter" aria-label="Фільтр ноутбуків за станом">
      <option value="all">Усі ноутбуки</option>
      <option value="queued">З чергою</option>
      <option value="errors">З помилками</option>
      <option value="published">З публікаціями</option>
      <option value="empty">Без переказів</option>
    </select>
    <span class="muted" id="nb-count"></span>
  </div>
  <div id="content"><div class="empty">Завантаження…</div></div>
  <div id="log"></div>
</main>
<script>
const UA = {new:"нове", transcribed:"транскрипт", described:"метадані", rendered:"відрендерено",
            uploaded:"на YouTube", done:"готово"};
let notebooks = [];
let byId = new Map();
let openId = null;
let itemCache = new Map();
let loadingItems = new Set();
let query = "";
let filter = "all";
let firstLoad = true;

function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtDur(s) {
  if (!s) return "—";
  s = Math.round(s); const m = Math.floor(s/60), r = s%60;
  return Math.floor(m/60) ? `${Math.floor(m/60)}:${String(m%60).padStart(2,'0')}:${String(r).padStart(2,'0')}`
                          : `${m}:${String(r).padStart(2,'0')}`;
}
function fmtTime(iso) {
  if (!iso) return "зараз";
  const d = new Date(iso);
  return d.toLocaleString("uk-UA", {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"});
}
function fmtDate(iso) {
  if (!iso) return "не синкався";
  const d = String(iso).slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(d) ? `синк ${d.split("-").reverse().join(".")}` : esc(iso);
}
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}
async function api(path, opts = {}) {
  const res = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  let data;
  try { data = await res.json(); } catch { throw new Error(`HTTP ${res.status}`); }
  if (!res.ok || data.ok === false) throw new Error(data.message || `HTTP ${res.status}`);
  return data;
}
function setLog(lines) {
  const el = document.getElementById("log");
  el.classList.add("on");
  el.textContent = lines.join("\n");
}
function stat(k, v, cls = "") { return `<div class="stat"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`; }

async function loadDaemon() {
  const el = document.getElementById("daemon");
  try {
    const d = await api("/api/daemon");
    el.innerHTML =
      stat("Демон", d.running ? "працює" : "не запущено", d.running ? "ok" : "muted") +
      stat("Опубліковано сьогодні", `${d.uploads_today} / ${d.videos_per_day}`) +
      stat("Квота YouTube", `${d.quota.remaining} / ${d.quota.limit}`, d.quota.remaining < 1700 ? "warn" : "") +
      stat("Наступне вікно", esc(fmtTime(d.next_upload_at)));
  } catch (e) { el.innerHTML = `<span class="bad">Помилка: ${esc(e.message)}</span>`; }
}
function counters(n) {
  const bits = [`<span class="cnt"><b>${n.downloaded}</b> ${plural(n.downloaded, "переказ", "перекази", "переказів")}</span>`];
  if (n.queued) bits.push(`<span class="cnt warn"><b>${n.queued}</b> у черзі</span>`);
  if (n.published) bits.push(`<span class="cnt ok"><b>${n.published}</b> на YouTube</span>`);
  if (n.errors) bits.push(`<span class="cnt bad"><b>${n.errors}</b> ${plural(n.errors, "помилка", "помилки", "помилок")}</span>`);
  if (n.missing) bits.push(`<span class="cnt warn"><b>${n.missing}</b> ${plural(n.missing, "зник", "зникли", "зникли")}</span>`);
  if (n.deleted) bits.push(`<span class="cnt warn"><b>${n.deleted}</b> без аудіо</span>`);
  return bits.join(" · ");
}
function rowHtml(n) {
  const id = esc(n.id);
  return `<div class="nb" data-id="${id}">
    <button class="nb-head" type="button" id="nb-head-${id}" aria-expanded="false"
            aria-controls="nb-body-${id}">
      <span class="chev" aria-hidden="true">▶</span>
      <span class="nb-main">
        <span class="nb-title">${esc(n.title)}</span>
        <span class="nb-meta">${counters(n)}</span>
      </span>
      <span class="nb-sync" title="${esc(n.synced_at || "")}">${fmtDate(n.synced_at)}</span>
    </button>
    <div class="nb-body" id="nb-body-${id}" role="region" aria-labelledby="nb-head-${id}" hidden></div>
  </div>`;
}
function itemsHtml(data) {
  const nb = data.notebook || {};
  const link = nb.url
    ? `<a href="${esc(nb.url)}" target="_blank" rel="noreferrer">відкрити в NotebookLM</a>` : "";
  const count = `<b>${data.items.length}</b> ${plural(data.items.length, "переказ", "перекази", "переказів")}`;
  if (!data.items.length) {
    return `<div class="nb-body-head"><span>${count}</span><span class="spacer"></span>${link}</div>
      <div class="empty">Переказів ще немає.</div>`;
  }
  const rows = data.items.map(i => {
    const status = i.job_status ? (UA[i.job_status] || i.job_status) : "без задачі";
    const audio = i.audio_deleted
      ? `<span class="warn">аудіо видалено</span>`
      : (i.audio_exists ? "" : `<span class="warn">аудіо відсутнє</span>`);
    const state = [i.state === "missing" ? `<span class="warn">зник з NotebookLM</span>` : "", audio]
      .filter(Boolean).join(" · ");
    const yt = i.url
      ? `<a href="${esc(i.url)}" target="_blank" rel="noreferrer">${esc(i.url.replace("https://", ""))}</a>`
      : "—";
    return `<tr>
      <td>${esc(i.title)}<div class="muted" style="font-size:12px">${esc((i.created_at || "").slice(0, 10))}${state ? " · " + state : ""}</div></td>
      <td class="n">${fmtDur(i.duration)}</td>
      <td>${esc(status)}${i.job_id ? ` <span class="muted">#${i.job_id}</span>` : ""}</td>
      <td>${yt}</td>
      <td class="bad">${esc(i.error || "")}</td>
    </tr>`;
  }).join("");
  return `<div class="nb-body-head"><span>${count}</span><span class="spacer"></span>${link}</div>
    <div class="table-wrap"><table>
      <tr><th>Переказ</th><th>Тривалість</th><th>Задача</th><th>YouTube</th><th>Помилка</th></tr>
      ${rows}</table></div>`;
}
function rowFor(id) {
  return Array.from(document.querySelectorAll(".nb")).find(r => r.dataset.id === id) || null;
}
function setExpanded(row, open) {
  row.classList.toggle("open", open);
  const head = row.querySelector(".nb-head");
  if (head) head.setAttribute("aria-expanded", open ? "true" : "false");
  const body = row.querySelector(".nb-body");
  if (body) body.hidden = !open;
}
function updateTitle() {
  const nb = openId ? byId.get(openId) : null;
  document.title = nb ? `${nb.title} · Ноутбуки · AudioTube Studio` : "Ноутбуки · AudioTube Studio";
}
function matchesFilter(n) {
  if (filter === "queued") return n.queued > 0;
  if (filter === "errors") return n.errors > 0;
  if (filter === "published") return n.published > 0;
  if (filter === "empty") return n.downloaded === 0;
  return true;
}
function applyFilter() {
  const q = query.trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll(".nb").forEach(row => {
    const n = byId.get(row.dataset.id);
    if (!n) return;
    const ok = matchesFilter(n) && (!q || String(n.title || "").toLowerCase().includes(q));
    row.classList.toggle("hidden", !ok);
    if (ok) shown++;
  });
  const el = document.getElementById("nb-count");
  if (el) el.textContent = notebooks.length ? `Показано ${shown} з ${notebooks.length}` : "";
}
function renderList() {
  const box = document.getElementById("content");
  const search = document.getElementById("nb-search");
  const select = document.getElementById("nb-filter");
  const has = notebooks.length > 0;
  if (search) search.disabled = !has;
  if (select) select.disabled = !has;
  if (!has) {
    box.innerHTML = `<div class="empty">Ще немає ноутбуків. Натисніть «Синк (план)», щоб побачити, що є в NotebookLM.</div>`;
    if (document.getElementById("nb-count")) document.getElementById("nb-count").textContent = "";
    return;
  }
  box.innerHTML = `<div class="nb-list" id="nb-list">${notebooks.map(rowHtml).join("")}</div>`;
  const row = openId ? rowFor(openId) : null;
  if (row) {
    setExpanded(row, true);
    ensureItems(openId);
  }
  applyFilter();
  updateTitle();
}
async function ensureItems(id, {force = false} = {}) {
  const row = rowFor(id);
  if (!row) return;
  const body = row.querySelector(".nb-body");
  if (!body) return;
  if (!force && itemCache.has(id)) {
    body.innerHTML = itemsHtml(itemCache.get(id));
    return;
  }
  if (loadingItems.has(id)) return;
  loadingItems.add(id);
  body.innerHTML = `<div class="empty">Завантаження переказів…</div>`;
  try {
    const data = await api(`/api/notebooks/${encodeURIComponent(id)}`);
    itemCache.set(id, data);
    if (openId === id) body.innerHTML = itemsHtml(data);
  } catch (e) {
    if (openId === id) {
      body.innerHTML = `<div class="empty bad">Помилка: ${esc(e.message)}<br>
        <button type="button" data-retry="${esc(id)}">Повторити</button></div>`;
    }
  } finally {
    loadingItems.delete(id);
  }
}
function notebookFromPath() {
  const parts = location.pathname.split("/").filter(Boolean);
  return parts[0] === "notebooks" && parts.length > 1 ? decodeURIComponent(parts[1]) : null;
}
function openNotebook(id, {push = true} = {}) {
  if (!byId.has(id) || openId === id) return;
  const prev = openId ? rowFor(openId) : null;
  if (prev) setExpanded(prev, false);
  openId = id;
  const row = rowFor(id);
  if (row) setExpanded(row, true);
  if (push) history.pushState({notebook: id}, "", `/notebooks/${encodeURIComponent(id)}`);
  ensureItems(id);
  updateTitle();
}
function closeNotebook({push = true} = {}) {
  if (!openId) return;
  const row = rowFor(openId);
  if (row) setExpanded(row, false);
  openId = null;
  if (push) history.pushState({}, "", "/notebooks");
  updateTitle();
}
function toggleNotebook(id) {
  if (openId === id) closeNotebook();
  else openNotebook(id);
}
function syncFromLocation() {
  const id = notebookFromPath();
  if (id === openId) return;
  if (id && byId.has(id)) openNotebook(id, {push: false});
  else if (!id) closeNotebook({push: false});
}
async function refresh({force = false} = {}) {
  if (force) itemCache.clear();
  loadDaemon();
  const box = document.getElementById("content");
  try {
    const data = await api("/api/notebooks");
    notebooks = data.notebooks;
    byId = new Map(notebooks.map(n => [n.id, n]));
    const fromUrl = notebookFromPath();
    if (fromUrl && byId.has(fromUrl)) {
      openId = fromUrl;
    } else {
      if (openId && !byId.has(openId)) openId = null;
      if (fromUrl) {
        if (firstLoad) setLog([`Ноутбук ${fromUrl} не знайдено — можливо, його видалено.`]);
        history.replaceState({}, "", "/notebooks");
      }
    }
    firstLoad = false;
    renderList();
  } catch (e) {
    box.innerHTML = `<div class="empty bad">Помилка: ${esc(e.message)}</div>`;
  }
}
async function runSync(dryRun) {
  const buttons = [document.getElementById("sync-plan"), document.getElementById("sync-run")];
  buttons.forEach(b => b.disabled = true);
  setLog([dryRun ? "будую план синку…" : "синхронізую з NotebookLM…"]);
  try {
    const data = await api(`/api/sync${dryRun ? "?dry_run=1" : ""}`, {method: "POST"});
    const items = data.report.items.map(i => `${i.action.padEnd(9)} ${i.notebook} — ${i.title}${i.message ? " (" + i.message + ")" : ""}`);
    setLog([data.message, ...items, ...data.report.errors.map(e => "помилка: " + e)]);
  } catch (e) { setLog([`Помилка: ${e.message}`]); }
  finally { buttons.forEach(b => b.disabled = false); refresh({force: true}); }
}
document.getElementById("content").addEventListener("click", e => {
  const head = e.target.closest(".nb-head");
  if (head) {
    const row = head.closest(".nb");
    if (row) toggleNotebook(row.dataset.id);
    return;
  }
  const retry = e.target.closest("[data-retry]");
  if (retry) ensureItems(retry.dataset.retry, {force: true});
});
document.getElementById("nb-search").addEventListener("input", e => {
  query = e.target.value;
  applyFilter();
});
document.getElementById("nb-filter").addEventListener("change", e => {
  filter = e.target.value;
  applyFilter();
});
window.addEventListener("popstate", syncFromLocation);
document.getElementById("sync-plan").onclick = () => runSync(true);
document.getElementById("sync-run").onclick = () => {
  if (confirm("Запустити справжній синк? Нові перекази буде скачано (не більше max_per_run).")) runSync(false);
};
refresh();
setInterval(loadDaemon, 30000);
</script>
</body>
</html>
"""
