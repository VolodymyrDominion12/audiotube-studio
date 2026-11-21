"""Сповіщення демона (spec 007): Telegram або notify-send.

`Notifier` знає події, тексти й обмеження частоти (стан — у таблиці `notifications`);
відправку робить транспорт `send(text) -> bool`. Транспорт ніколи не кидає винятків назовні,
а `Notifier` додатково ловить усе, щоб збій сповіщення не зупинив цикл демона (NTF-R5).

Секрети — лише з середовища (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), не з config.yaml.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone

from audiotube.config import Config
from audiotube.db import Database, Job

TELEGRAM_API = "https://api.telegram.org"
TIMEOUT_SECONDS = 10
MAX_MESSAGE = 4096
AUTH_INTERVAL = timedelta(hours=12)
ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT = "TELEGRAM_CHAT_ID"

Send = Callable[[str], bool]

_AUTH_SOURCES = {
    "notebooklm": ("NotebookLM", "notebooklm login"),
    "youtube": ("YouTube", "audiotube auth"),
}


# --- транспорти ---------------------------------------------------------------
def telegram_env(environ: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    env = os.environ if environ is None else environ
    token, chat = (env.get(ENV_TOKEN) or "").strip(), (env.get(ENV_CHAT) or "").strip()
    return (token, chat) if token and chat else None


def telegram_transport(token: str, chat_id: str, *, log=print, opener=None) -> Send:
    """POST sendMessage через stdlib urllib. Повертає False при будь-якій помилці."""

    def redact(text: str) -> str:
        return text.replace(token, "***")

    def send(text: str) -> bool:
        body = json.dumps(
            {"chat_id": chat_id, "text": text[:MAX_MESSAGE], "disable_web_page_preview": False}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        open_url = opener or urllib.request.urlopen
        try:
            with open_url(req, timeout=TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"Telegram: не вдалося надіслати сповіщення: {redact(str(e))}")
            return False
        if not data.get("ok"):
            log(f"Telegram відхилив повідомлення: {redact(str(data.get('description', data)))}")
            return False
        return True

    return send


def desktop_transport(text: str) -> bool:
    exe = shutil.which("notify-send")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "AudioTube", text], timeout=10, capture_output=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --- події --------------------------------------------------------------------
class Notifier:
    """Події демона. `send=None` — сповіщення вимкнено (усі методи нічого не роблять)."""

    def __init__(self, cfg: Config, db: Database, send: Send | None, log=print):
        self.cfg = cfg
        self.db = db
        self.send = send
        self.log = log

    @property
    def enabled(self) -> bool:
        return self.send is not None

    def _deliver(self, text: str) -> bool:
        if self.send is None:
            return False
        try:
            return bool(self.send(text))
        except Exception as e:  # noqa: BLE001 — сповіщення ніколи не валить цикл (NTF-R5)
            self.log(f"сповіщення не надіслано: {type(e).__name__}: {e}")
            return False

    def published(self, job: Job) -> bool:
        title = job.title or job.audio.name
        return self._deliver(f"✅ Опубліковано: {title}\n{job.url or job.video_id or ''}".rstrip())

    def failed_final(self, job: Job, attempts: int) -> bool:
        title = job.title or job.audio.name
        first = (job.error or "невідома помилка").splitlines()[0]
        return self._deliver(
            f"❌ Задача #{job.id} «{title}» не вдалася {attempts} раз(и) поспіль "
            f"і більше не братиметься.\nПомилка: {first}\n"
            f"Повернути в чергу: audiotube reset {job.id} --to new"
        )

    def auth_expired(self, source: str, detail: str, now: datetime | None = None) -> bool:
        """Не частіше ніж раз на 12 год на джерело; позначаємо лише успішну відправку."""
        if not self.enabled:
            return False
        now = now or datetime.now(timezone.utc)
        key = f"auth_expired:{source}"
        last = self.db.last_notified(key)
        if last is not None and now - last < AUTH_INTERVAL:
            return False
        name, fix = _AUTH_SOURCES.get(source, (source, ""))
        text = f"⚠️ Потрібна повторна авторизація {name}.\n{detail.splitlines()[0] if detail else ''}"
        if fix:
            text += f"\nВиконайте: {fix}"
        if not self._deliver(text):
            return False
        self.db.mark_notified(key, now)
        return True

    def daily_summary(self, now: datetime | None = None) -> bool:
        """Раз на добу, не раніше daemon.summary_hour (локальний час)."""
        hour = self.cfg.daemon.summary_hour
        if not self.enabled or hour is None:
            return False
        now = (now or datetime.now()).astimezone()
        if now.hour < hour:
            return False
        last = self.db.last_notified("daily_summary")
        if last is not None and last.astimezone(now.tzinfo).date() == now.date():
            return False
        c = self.db.daily_counts(self.cfg.daemon.max_attempts)
        text = (
            f"📊 Зведення за {now.date().isoformat()}: скачано {c['downloaded']}, "
            f"опубліковано {c['published']}/{self.cfg.daemon.videos_per_day}; "
            f"у черзі {c['queued']}; з помилками {c['errors']}."
        )
        if not self._deliver(text):
            return False
        self.db.mark_notified("daily_summary", now)
        return True


def build_notifier(
    cfg: Config, db: Database, log=print, environ: Mapping[str, str] | None = None
) -> Notifier:
    mode = cfg.daemon.notify
    if mode == "desktop":
        return Notifier(cfg, db, desktop_transport, log)
    if mode == "telegram":
        creds = telegram_env(environ)
        if creds is None:
            log(f"notify: telegram, але {ENV_TOKEN}/{ENV_CHAT} не задані — сповіщення вимкнено")
            return Notifier(cfg, db, None, log)
        return Notifier(cfg, db, telegram_transport(*creds, log=log), log)
    return Notifier(cfg, db, None, log)


def telegram_status(cfg: Config, environ: Mapping[str, str] | None = None) -> tuple[bool | None, str]:
    """Рядок для `doctor`: (True|False|None, пояснення). None — Telegram не використовується."""
    creds = telegram_env(environ)
    if cfg.daemon.notify != "telegram":
        return None, "вимкнено (daemon.notify)" + (", але змінні середовища задані" if creds else "")
    if creds is None:
        return False, f"задайте {ENV_TOKEN} і {ENV_CHAT} у середовищі або .env"
    return True, f"chat_id {creds[1]}; перевірка: audiotube notify --test"
