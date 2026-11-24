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


def job_json(cfg: Config, job: Job) -> dict:
    preview = cfg.output / f"{job.slug}_preview.mp4"
    return {
        "id": job.id,
        "slug": job.slug,
        "status": job.status,
        "title": job.title or job.audio.name,
        "audio": job.audio.name,
        "duration": job.duration,
        "error": job.error,
        "url": job.url,
        "video": _rel(cfg, job.video),
        "thumbnail": _rel(cfg, job.thumbnail),
        "preview": _rel(cfg, preview if preview.exists() else None),
        "subtitles": _rel(cfg, job.subtitles),
        "captions": bool(job.captions_id),
        "playlist": bool(job.playlist_id),
        "has_transcript": bool(job.transcript and job.transcript.exists()),
    }


def disk_stats(cfg: Config, jobs: list[Job]) -> dict:
    """WEB2-R4: скільки аудіо лежить на диску і скільки з них поза чергою."""
    registered = {job.audio.resolve() for job in jobs}
    inbox_audio = [
        p for p in cfg.inbox.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    return {
        "inbox_audio": len(inbox_audio),
        "inbox_unregistered": sum(1 for p in inbox_audio if p.resolve() not in registered),
        "output_videos": sum(1 for p in cfg.output.glob("*.mp4") if p.is_file()),
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

    # --- серіалізація стану --------------------------------------------------
    def state(self) -> dict:
        with self.lock:
            pipe = Pipeline(self.cfg, log=lambda *_: None)
            try:
                rows = pipe.db.list()
                jobs = [job_json(self.cfg, j) for j in rows]
                used = pipe.quota.used()
                limit = pipe.quota.daily_limit
                files = disk_stats(self.cfg, rows)
            finally:
                pipe.db.close()
        by_status = {status: 0 for status in STATUS_ORDER}
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        return {
            "jobs": jobs,
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
                else:
                    raise WebError(f"невідома дія: {action}")
                payload = {"job": job_json(self.cfg, job)}
            finally:
                pipe.db.close()
        payload["ok"] = True
        payload["log"] = logs
        payload["message"] = logs[-1] if logs else "готово"
        return payload

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
                return {"ok": True, "message": "метадані збережено", "job": job_json(self.cfg, job)}
            finally:
                pipe.db.close()

    # --- ноутбуки (spec 008) -------------------------------------------------
    def notebooks(self) -> dict:
        """WEB-R1: ноутбуки з лічильниками."""
        with Database(self.cfg.db_path) as db:
            rows = db.notebook_stats()
        keys = ("id", "title", "slug", "url", "sources", "synced_at",
                "downloaded", "missing", "published", "errors", "queued")
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
            elif parts == ["api", "notebooks"]:
                self._json(200, self.app.notebooks())
            elif len(parts) == 3 and parts[:2] == ["api", "notebooks"]:
                self._json(200, self.app.notebook(unquote(parts[2])))
            elif parts == ["api", "daemon"]:
                self._json(200, self.app.daemon_state())
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
  .filters { display:flex; gap:10px; margin-bottom:14px; flex-wrap:wrap; }
  .filters input, .filters select { background:var(--panel2); color:var(--text); border:1px solid var(--line);
           border-radius:8px; padding:8px 11px; font:inherit; font-size:14px; }
  .filters input { flex:1; min-width:220px; }
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
    <input id="q" type="search" placeholder="пошук за назвою, файлом або slug" oninput="render()">
    <select id="st" onchange="render()">
      <option value="">усі статуси</option>
      <option value="new">нове</option>
      <option value="transcribed">транскрипт</option>
      <option value="described">метадані</option>
      <option value="rendered">відрендерено</option>
      <option value="uploaded">на YouTube</option>
      <option value="done">готово</option>
    </select>
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
let state = {jobs: [], quota: {used: 0, limit: 0}, config: {}, counts: {}, files: {}};
let busyCount = 0;

function setBusy(on, text) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  document.getElementById("busy").classList.toggle("on", busyCount > 0);
  if (text) document.getElementById("busy-text").textContent = text;
}
function setLog(lines) {
  const el = document.getElementById("log");
  el.classList.add("on");
  el.innerHTML = lines.map(l => esc(l)).join("\n");
  el.scrollTop = el.scrollHeight;
}
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtDur(s) {
  if (!s) return "—";
  s = Math.round(s); const m = Math.floor(s/60), r = s%60;
  return Math.floor(m/60) ? `${Math.floor(m/60)}:${String(m%60).padStart(2,'0')}:${String(r).padStart(2,'0')}`
                          : `${m}:${String(r).padStart(2,'0')}`;
}
async function api(path, opts = {}) {
  const res = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  let data;
  try { data = await res.json(); } catch { throw new Error(`HTTP ${res.status}`); }
  if (!res.ok || data.ok === false) throw new Error(data.message || `HTTP ${res.status}`);
  return data;
}
function refresh() {
  return api("/api/state").then(s => { state = s; render(); }).catch(e => {
    document.getElementById("jobs").innerHTML = `<div class="empty">Помилка: ${esc(e.message)}</div>`;
  });
}
function renderCounts() {
  const c = state.counts || {total: state.jobs.length, published: 0, ready_to_publish: 0};
  const f = state.files || {inbox_audio: 0, inbox_unregistered: 0, output_videos: 0};
  document.getElementById("counts").textContent =
    `${c.total} задач · на YouTube ${c.published} · готових ${c.ready_to_publish} · ` +
    `inbox ${f.inbox_audio} (поза чергою ${f.inbox_unregistered}) · mp4 ${f.output_videos}`;
}
function visibleJobs() {
  const q = (document.getElementById("q").value || "").trim().toLowerCase();
  const st = document.getElementById("st").value;
  return state.jobs.filter(j =>
    (!st || j.status === st) &&
    (!q || `${j.title} ${j.audio} ${j.slug}`.toLowerCase().includes(q)));
}
function render() {
  document.getElementById("quota").textContent = `квота: ${state.quota.used} / ${state.quota.limit}`;
  const c = state.config;
  document.getElementById("cfg").textContent =
    `${c.llm} · ${c.transcribe} · ffmpeg: ${c.ffmpeg ? "ok" : "немає"}`;
  renderCounts();
  const box = document.getElementById("jobs");
  if (!state.jobs.length) {
    box.innerHTML = `<div class="empty">Порожньо. Покладіть аудіо в <b>inbox/</b> і натисніть «Сканувати inbox».</div>`;
    return;
  }
  const jobs = visibleJobs();
  if (!jobs.length) {
    box.innerHTML = `<div class="empty">Нічого не знайдено за фільтром.</div>`;
    return;
  }
  box.innerHTML = jobs.map(j => {
    const badge = `<span class="badge ${j.status}">${UA[j.status] || j.status}</span>`;
    const yt = j.url ? ` · <a href="${j.url}" target="_blank" style="color:var(--accent)">${j.url}</a>` : "";
    const flags = [j.has_transcript ? "текст" : "", j.subtitles ? "srt" : "", j.captions ? "субтитри YT" : "",
                   j.playlist ? "плейлист" : ""].filter(Boolean).join(" · ");
    const thumb = j.thumbnail ? `<img src="/media/${encodeURI(j.thumbnail)}" alt="">` : "";
    const preview = j.preview ? `<video controls preload="metadata" src="/media/${encodeURI(j.preview)}"></video>` : "";
    return `<div class="job">
      <div class="top">${badge}<span class="title">${esc(j.title)}</span>
        <span class="hint">${fmtDur(j.duration)}</span></div>
      <div class="sub">#${j.id} · ${esc(j.audio)} · ${esc(j.status)}${yt}${flags ? " · " + flags : ""}</div>
      ${j.error ? `<div class="err">${esc(j.error)}</div>` : ""}
      <div class="meta-box">${thumb}<div>${preview}</div></div>
      <div class="actions">
        <button onclick="act(${j.id},'transcribe')">Транскрипт</button>
        <button onclick="act(${j.id},'describe')">Метадані LLM</button>
        <button class="ghost" onclick="editMeta(${j.id})">Редагувати</button>
        <button class="ghost" onclick="act(${j.id},'preview')">Прев'ю 20с</button>
        <button onclick="act(${j.id},'render')">Рендер</button>
        ${j.url
          ? `<select class="privacy" id="privacy-${j.id}">
               <option value="public">public</option>
               <option value="unlisted">unlisted</option>
               <option value="private">private</option>
             </select>
             <button onclick="publishJob(${j.id})">Змінити приватність</button>`
          : `<button class="primary" onclick="act(${j.id},'upload')">Опублікувати</button>`}
        <button class="ghost" onclick="act(${j.id},'reset')">Скинути</button>
      </div>
    </div>`;
  }).join("");
}
async function act(id, action, force = false) {
  setBusy(true, action === "render" ? "рендер… це може тривати кілька хвилин"
              : action === "describe" ? "LLM генерує метадані…"
              : action === "upload" ? "завантаження на YouTube…" : "працюю…");
  try {
    const data = await api(`/api/jobs/${id}/${action}${force ? "?force=1" : ""}`, {method: "POST"});
    setLog(data.log && data.log.length ? data.log : [data.message]);
  } catch (e) {
    setLog([`Помилка: ${e.message}`]);
  } finally {
    setBusy(false);
    await refresh();
  }
}
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
    await refresh();
  }
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
  await refresh();
}
document.getElementById("scan").onclick = async () => {
  setBusy(true, "сканую inbox…");
  try {
    const data = await api("/api/scan", {method: "POST"});
    setLog(data.log && data.log.length ? data.log : [data.message]);
  } catch (e) { setLog([`Помилка: ${e.message}`]); }
  finally { setBusy(false); await refresh(); }
};
document.getElementById("toggle-log").onclick = () => document.getElementById("log").classList.toggle("on");
refresh();
setInterval(refresh, 15000);
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
  <div id="content"><div class="empty">Завантаження…</div></div>
  <div id="log"></div>
</main>
<script>
const UA = {new:"нове", transcribed:"транскрипт", described:"метадані", rendered:"відрендерено",
            uploaded:"на YouTube", done:"готово"};
const parts = location.pathname.split("/").filter(Boolean);
const nbId = parts.length > 1 ? decodeURIComponent(parts[1]) : null;

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
async function loadList() {
  const data = await api("/api/notebooks");
  if (!data.notebooks.length)
    return `<div class="empty">Ще немає ноутбуків. Натисніть «Синк (план)», щоб побачити, що є в NotebookLM.</div>`;
  const rows = data.notebooks.map(n => `<tr>
      <td><a href="/notebooks/${encodeURIComponent(n.id)}">${esc(n.title)}</a></td>
      <td class="n">${n.downloaded}</td>
      <td class="n">${n.queued}</td>
      <td class="n ok">${n.published}</td>
      <td class="n ${n.errors ? "bad" : ""}">${n.errors}</td>
      <td class="n ${n.missing ? "warn" : ""}">${n.missing}</td>
      <td class="muted">${esc((n.synced_at || "").slice(0, 16).replace("T", " "))}</td>
    </tr>`).join("");
  return `<h2>Ноутбуки NotebookLM</h2><div class="table-wrap"><table>
    <tr><th>Ноутбук</th><th>Скачано</th><th>У черзі</th><th>Опубліковано</th><th>Помилки</th><th>Зникли</th><th>Синк</th></tr>
    ${rows}</table></div>`;
}
async function loadOne(id) {
  const data = await api(`/api/notebooks/${encodeURIComponent(id)}`);
  const nb = data.notebook;
  const link = nb.url ? ` · <a href="${esc(nb.url)}" target="_blank">відкрити в NotebookLM</a>` : "";
  if (!data.items.length) return `<h2>${esc(nb.title)}${link}</h2><div class="empty">Переказів ще немає.</div>`;
  const rows = data.items.map(i => {
    const status = i.job_status ? (UA[i.job_status] || i.job_status) : "без задачі";
    const state = i.state === "missing" ? `<span class="warn">зник з NotebookLM</span>` : "";
    const yt = i.url ? `<a href="${esc(i.url)}" target="_blank">${esc(i.url.replace("https://", ""))}</a>` : "—";
    return `<tr>
      <td>${esc(i.title)}<div class="muted" style="font-size:12px">${esc((i.created_at || "").slice(0, 10))} ${state}</div></td>
      <td class="n">${fmtDur(i.duration)}</td>
      <td>${esc(status)}${i.job_id ? ` <span class="muted">#${i.job_id}</span>` : ""}</td>
      <td>${yt}</td>
      <td class="bad">${esc(i.error || "")}</td>
    </tr>`;
  }).join("");
  return `<h2>${esc(nb.title)}${link}</h2><div class="table-wrap"><table>
    <tr><th>Переказ</th><th>Тривалість</th><th>Задача</th><th>YouTube</th><th>Помилка</th></tr>
    ${rows}</table></div>`;
}
async function refresh() {
  loadDaemon();
  const box = document.getElementById("content");
  try { box.innerHTML = nbId ? await loadOne(nbId) : await loadList(); }
  catch (e) { box.innerHTML = `<div class="empty bad">Помилка: ${esc(e.message)}</div>`; }
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
  finally { buttons.forEach(b => b.disabled = false); refresh(); }
}
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
