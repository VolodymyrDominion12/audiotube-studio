"""Локальний веб-інтерфейс без зовнішніх залежностей (stdlib http.server).

Запуск: `audiotube web` → http://127.0.0.1:8765

Дає те, що потрібно для ручної перевірки перед публікацією: черга задач, прев'ю,
редагування YAML-метаданих, кнопки транскрипції/рендеру/завантаження.
Кожен запит створює власне з'єднання SQLite і виконується в окремому потоці.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml

from audiotube.config import Config
from audiotube.db import Job
from audiotube.media import has_ffmpeg
from audiotube.metadata import MetadataError, VideoMeta, dump_sidecar, load_meta, sidecar_path
from audiotube.pipeline import Pipeline

MAX_BODY = 1_000_000
PREVIEW_SECONDS = 20.0


class WebError(RuntimeError):
    pass


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
    def __init__(self, cfg: Config, host: str = "127.0.0.1", port: int = 8765):
        self.cfg = cfg
        self.host = host
        self.port = port
        self.lock = threading.Lock()

    # --- серіалізація стану --------------------------------------------------
    def state(self) -> dict:
        with self.lock:
            pipe = Pipeline(self.cfg, log=lambda *_: None)
            try:
                jobs = [job_json(self.cfg, j) for j in pipe.db.list()]
                used = pipe.quota.used()
                limit = pipe.quota.daily_limit
            finally:
                pipe.db.close()
        return {
            "jobs": jobs,
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
    def action(self, job_id: int, action: str, force: bool = False) -> dict:
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
                elif action == "reset":
                    job = pipe.db.update(job.id, status="new", error=None)
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
            elif parts == ["api", "state"]:
                self._json(200, self.app.state())
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
        except (WebError, RuntimeError) as e:
            self._error(400, str(e))
        except Exception as e:  # noqa: BLE001 — веб-сервер не має падати через одну дію
            self._error(500, f"{type(e).__name__}: {e}")

    def do_POST(self) -> None:  # noqa: N802
        parts = self._parts()
        try:
            if parts == ["api", "scan"]:
                self._json(200, self.app.scan())
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                force = "force=1" in (urlparse(self.path).query or "")
                self._json(200, self.app.action(int(parts[2]), parts[3], force=force))
            else:
                self._error(404, "немає такого маршруту")
        except RuntimeError as e:  # WebError, YouTubeError, TranscriptionError, LLMError
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
</style>
</head>
<body>
<header>
  <h1>AudioTube Studio</h1>
  <span class="pill" id="quota">квота: …</span>
  <span class="pill" id="cfg">…</span>
  <span class="spacer"></span>
  <button class="ghost" id="toggle-log">Журнал</button>
  <button class="primary" id="scan">Сканувати inbox</button>
</header>
<main>
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
let state = {jobs: [], quota: {used: 0, limit: 0}, config: {}};
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
function render() {
  document.getElementById("quota").textContent = `квота: ${state.quota.used} / ${state.quota.limit}`;
  const c = state.config;
  document.getElementById("cfg").textContent =
    `${c.llm} · ${c.transcribe} · ffmpeg: ${c.ffmpeg ? "ok" : "немає"}`;
  const box = document.getElementById("jobs");
  if (!state.jobs.length) {
    box.innerHTML = `<div class="empty">Порожньо. Покладіть аудіо в <b>inbox/</b> і натисніть «Сканувати inbox».</div>`;
    return;
  }
  box.innerHTML = state.jobs.map(j => {
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
        <button class="primary" onclick="act(${j.id},'upload')">Опублікувати</button>
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
