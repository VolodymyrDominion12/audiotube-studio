"""Стан задач і облік квоти YouTube у SQLite."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# статуси відео по черзі; кожен означає, що відповідний крок успішно завершено
STATUSES = ("new", "transcribed", "described", "rendered", "uploaded", "done")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audio TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'new',
    title TEXT,
    video TEXT,
    thumbnail TEXT,
    duration REAL,
    video_id TEXT,
    playlist_id TEXT,
    transcript TEXT,
    subtitles TEXT,
    captions_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota (
    day TEXT PRIMARY KEY,
    used INTEGER NOT NULL DEFAULT 0
);
"""

# колонки, додані після 0.1 — для баз, створених старішою версією
_MIGRATIONS = {
    "transcript": "TEXT",
    "subtitles": "TEXT",
    "captions_id": "TEXT",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: int
    audio: Path
    slug: str
    status: str
    title: str | None
    video: Path | None
    thumbnail: Path | None
    duration: float | None
    video_id: str | None
    playlist_id: str | None
    error: str | None
    transcript: Path | None = None
    subtitles: Path | None = None
    captions_id: str | None = None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Job":
        return cls(
            id=r["id"], audio=Path(r["audio"]), slug=r["slug"], status=r["status"], title=r["title"],
            video=Path(r["video"]) if r["video"] else None,
            thumbnail=Path(r["thumbnail"]) if r["thumbnail"] else None,
            duration=r["duration"], video_id=r["video_id"],
            playlist_id=r["playlist_id"], error=r["error"],
            transcript=Path(r["transcript"]) if r["transcript"] else None,
            subtitles=Path(r["subtitles"]) if r["subtitles"] else None,
            captions_id=r["captions_id"],
        )

    @property
    def url(self) -> str | None:
        return f"https://youtu.be/{self.video_id}" if self.video_id else None


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Додає колонки, яких немає у базі, створеній попередньою версією."""
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for column, sql_type in _MIGRATIONS.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {sql_type}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- jobs -------------------------------------------------------------
    def add_job(self, audio: Path, slug: str, title: str | None = None) -> Job | None:
        """Реєструє аудіо. Повертає None, якщо воно вже є в базі."""
        audio = audio.resolve()
        if self.conn.execute("SELECT 1 FROM jobs WHERE audio=?", (str(audio),)).fetchone():
            return None
        base, n = slug, 2
        while self.conn.execute("SELECT 1 FROM jobs WHERE slug=?", (slug,)).fetchone():
            slug, n = f"{base}-{n}", n + 1
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO jobs (audio, slug, status, title, created_at, updated_at) VALUES (?, ?, 'new', ?, ?, ?)",
            (str(audio), slug, title, now, now),
        )
        self.conn.commit()
        return self.get(cur.lastrowid)

    def get(self, job_id: int) -> Job | None:
        r = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(r) if r else None

    def list(self, statuses: tuple[str, ...] | None = None) -> list[Job]:
        if statuses:
            q = f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id"
            rows = self.conn.execute(q, statuses).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [Job.from_row(r) for r in rows]

    def update(self, job_id: int, **fields) -> Job:
        fields = {k: (str(v) if isinstance(v, Path) else v) for k, v in fields.items()}
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
        self.conn.commit()
        return self.get(job_id)

    def delete(self, job_id: int) -> None:
        self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        self.conn.commit()


class QuotaTracker:
    """Квота YouTube Data API скидається опівночі за тихоокеанським часом."""

    def __init__(self, db: Database, daily_limit: int):
        self.db = db
        self.daily_limit = daily_limit

    @staticmethod
    def today() -> str:
        return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()

    def used(self) -> int:
        r = self.db.conn.execute("SELECT used FROM quota WHERE day=?", (self.today(),)).fetchone()
        return r["used"] if r else 0

    def remaining(self) -> int:
        return self.daily_limit - self.used()

    def can_spend(self, units: int) -> bool:
        return self.remaining() >= units

    def spend(self, units: int) -> None:
        self.db.conn.execute(
            "INSERT INTO quota (day, used) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET used = used + ?",
            (self.today(), units, units),
        )
        self.db.conn.commit()
