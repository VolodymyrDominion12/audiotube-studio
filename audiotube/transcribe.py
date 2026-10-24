"""Транскрипція аудіо у текст із таймкодами.

Бекенди (вибір через `transcribe.backend`):
- `faster-whisper` — локально, найкраща якість/швидкість на CPU та GPU (pip install faster-whisper);
- `whisper-cli` — whisper.cpp, якщо вже є ggml-модель (швидко на CPU без Python-залежностей);
- `openai` — будь-який OpenAI-сумісний `/audio/transcriptions` (хмара або локальний сервер);
- `auto` — перший доступний із перелічених.
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from audiotube.config import Config
from audiotube.transcript import Segment, Transcript, write_srt

TRANSCRIPT_NAME = "transcript.json"
SUBTITLES_NAME = "subtitles.srt"
WHISPER_CPP_COMMANDS = ("whisper-cli", "whisper-cpp", "whisper")
BACKENDS = ("faster-whisper", "whisper-cli", "openai")


class TranscriptionError(RuntimeError):
    pass


@dataclass
class TranscriptionResult:
    transcript: Transcript
    json_path: Path
    srt_path: Path

    @property
    def backend(self) -> str:
        return self.transcript.backend


# --- вибір бекенда -----------------------------------------------------------
def _faster_whisper_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def _whisper_cli_command(cfg: Config) -> str | None:
    if cfg.transcribe.command:
        found = shutil.which(cfg.transcribe.command)
        return found or (cfg.transcribe.command if Path(cfg.transcribe.command).exists() else None)
    for name in WHISPER_CPP_COMMANDS:
        found = shutil.which(name)
        if found:
            return found
    return None


def _openai_backend_ready(cfg: Config) -> bool:
    import os

    return bool(os.environ.get(cfg.transcribe.api_key_env)) or bool(cfg.transcribe.base_url)


def resolve_backend(cfg: Config) -> str:
    """Повертає назву бекенда або кидає зрозумілу помилку з підказкою."""
    wanted = cfg.transcribe.backend
    if wanted == "none":
        raise TranscriptionError("транскрипцію вимкнено (transcribe.backend: none)")
    if wanted != "auto":
        return wanted
    if _faster_whisper_available():
        return "faster-whisper"
    if _whisper_cli_command(cfg):
        return "whisper-cli"
    if _openai_backend_ready(cfg):
        return "openai"
    raise TranscriptionError(
        "немає доступного бекенда транскрипції. Варіанти:\n"
        "  • pip install faster-whisper   (локально, рекомендовано)\n"
        "  • встановити whisper.cpp і вказати transcribe.command/model_path\n"
        "  • задати OPENAI_API_KEY (або transcribe.base_url) для хмарного API"
    )


def _faster_whisper_note() -> tuple[bool, str]:
    if not _faster_whisper_available():
        return False, "pip install -e '.[transcribe]'"
    try:
        import av

        major = int(str(av.__version__).split(".")[0])
    except Exception:  # noqa: BLE001 — відсутність av не має валити діагностику
        return True, "встановлено (PyAV не перевірено)"
    if major >= 16:
        return False, f"PyAV {av.__version__} несумісний з faster-whisper: pip install 'av<16'"
    return True, f"встановлено (PyAV {av.__version__})"


def backend_report(cfg: Config) -> list[tuple[str, bool, str]]:
    """Стан бекендів для `audiotube doctor`."""
    cli = _whisper_cli_command(cfg)
    fw_ok, fw_note = _faster_whisper_note()
    return [
        ("faster-whisper", fw_ok, fw_note),
        (
            "whisper-cli",
            bool(cli and cfg.transcribe.model_path),
            f"знайдено: {cli}" if cli else "встановіть whisper.cpp і transcribe.model_path",
        ),
        (
            "openai",
            _openai_backend_ready(cfg),
            f"ключ {cfg.transcribe.api_key_env} / transcribe.base_url",
        ),
    ]


# --- спільне -----------------------------------------------------------------
def transcribe_audio(
    audio: Path, workdir: Path, cfg: Config, force: bool = False, log=print
) -> TranscriptionResult:
    """Транскрибує аудіо і зберігає transcript.json + subtitles.srt (ідемпотентно)."""
    workdir.mkdir(parents=True, exist_ok=True)
    json_path = workdir / TRANSCRIPT_NAME
    srt_path = workdir / SUBTITLES_NAME
    if not force and json_path.exists() and srt_path.exists():
        log(f"    транскрипт уже є: {json_path.name}")
        return TranscriptionResult(Transcript.load(json_path), json_path, srt_path)

    backend = resolve_backend(cfg)
    log(f"    транскрипція ({backend}, модель {cfg.transcribe.model})…")
    if backend == "faster-whisper":
        transcript = _run_faster_whisper(audio, cfg, log)
    elif backend == "whisper-cli":
        transcript = _run_whisper_cli(audio, workdir, cfg, log)
    elif backend == "openai":
        transcript = _run_openai(audio, cfg, log)
    else:  # pragma: no cover — захист від друкарської помилки в конфігу
        raise TranscriptionError(f"невідомий бекенд транскрипції: {backend}")

    if not transcript.segments:
        raise TranscriptionError("транскрипція не дала жодного сегмента (можливо, файл без мовлення)")
    transcript.backend = backend
    transcript.save(json_path)
    write_srt(transcript, srt_path, max_chars=cfg.transcribe.max_chars, max_lines=cfg.transcribe.max_lines)
    log(f"    готово: {len(transcript.segments)} сегментів, {len(transcript.text)} символів")
    return TranscriptionResult(transcript, json_path, srt_path)


# --- faster-whisper ----------------------------------------------------------
def _run_faster_whisper(audio: Path, cfg: Config, log=print) -> Transcript:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover — залежність перевіряється resolve_backend
        raise TranscriptionError("faster-whisper не встановлено: pip install faster-whisper") from e

    t = cfg.transcribe
    model_dir = cfg.path(t.model_dir) if t.model_dir else None
    device = t.device or "auto"
    compute = t.compute_type or "auto"
    if device == "cpu" and compute == "float16":
        compute = "int8"
    try:
        model = WhisperModel(
            t.model, device=device, compute_type=compute,
            download_root=str(model_dir) if model_dir else None,
        )
        segments_iter, info = model.transcribe(
            str(audio), language=t.language or None, beam_size=t.beam_size, vad_filter=t.vad,
        )
        segments = [
            Segment(float(s.start), float(s.end), (s.text or "").strip())
            for s in segments_iter
            if (s.text or "").strip()
        ]
    except Exception as e:  # noqa: BLE001 — помилки ctranslate2/ffmpeg/завантаження моделі
        if "metadata_errors" in str(e):
            raise TranscriptionError(
                "faster-whisper несумісний з встановленим PyAV. Виконайте: pip install 'av<16'"
            ) from e
        raise TranscriptionError(f"faster-whisper: {e}") from e
    duration = float(getattr(info, "duration", 0) or (segments[-1].end if segments else 0))
    return Transcript(
        language=getattr(info, "language", None) or t.language or "uk",
        duration=duration, segments=segments, model=t.model,
    )


# --- whisper.cpp -------------------------------------------------------------
def _run_whisper_cli(audio: Path, workdir: Path, cfg: Config, log=print) -> Transcript:
    t = cfg.transcribe
    command = _whisper_cli_command(cfg)
    if not command:
        raise TranscriptionError("whisper.cpp не знайдено: вкажіть transcribe.command")
    model_path = cfg.path(t.model_path) if t.model_path else None
    if not model_path or not model_path.exists():
        raise TranscriptionError(
            "для whisper.cpp потрібен шлях до ggml-моделі: transcribe.model_path у config.yaml"
        )
    prefix = workdir / "whispercpp"
    cmd = [
        command, "-m", str(model_path), "-f", str(audio),
        "-l", t.language or "auto", "-oj", "-of", str(prefix), "-nt",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=t.timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TranscriptionError(f"whisper.cpp: {e}") from e
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise TranscriptionError(f"whisper.cpp завершився з помилкою:\n{tail}")

    json_path = Path(f"{prefix}.json")
    if not json_path.exists():
        raise TranscriptionError(f"whisper.cpp не створив {json_path.name}")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    segments = []
    for item in data.get("transcription", []):
        offsets = item.get("offsets") or {}
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        segments.append(Segment(float(offsets.get("from", 0)) / 1000, float(offsets.get("to", 0)) / 1000, text))
    duration = max((s.end for s in segments), default=0.0)
    language = (data.get("result") or {}).get("language") or t.language or "uk"
    return Transcript(language=language, duration=duration, segments=segments, model=model_path.name)


# --- OpenAI-сумісний API -----------------------------------------------------
def _run_openai(audio: Path, cfg: Config, log=print) -> Transcript:
    import os

    t = cfg.transcribe
    base = (t.base_url or "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/audio/transcriptions"
    key = os.environ.get(t.api_key_env, "")
    if not key and "api.openai.com" in base:
        raise TranscriptionError(f"не задано {t.api_key_env} (або transcribe.base_url для локального сервера)")

    fields = {
        "model": t.model,
        "language": t.language or "uk",
        "response_format": "verbose_json",
    }
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    log(f"    запит до {url}…")
    body, content_type = _multipart(fields, {"file": audio})
    request = urllib.request.Request(
        url, data=body, headers={**headers, "Content-Type": content_type}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=t.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        e.close()
        raise TranscriptionError(f"API транскрипції відповів {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TranscriptionError(f"немає з'єднання з API транскрипції: {e}") from e

    segments = [
        Segment(float(s.get("start", 0)), float(s.get("end", 0)), str(s.get("text", "")).strip())
        for s in payload.get("segments") or []
        if str(s.get("text", "")).strip()
    ]
    duration = float(payload.get("duration") or (segments[-1].end if segments else 0))
    return Transcript(
        language=payload.get("language") or t.language or "uk",
        duration=duration, segments=segments, model=t.model,
    )


def _multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = f"----audiotube{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, path in files.items():
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode()
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
