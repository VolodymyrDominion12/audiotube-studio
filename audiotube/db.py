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
-- NotebookLM: ноутбук -> папка в inbox/
CREATE TABLE IF NOT EXISTS notebooks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    url TEXT,
    sources INTEGER,
    synced_at TEXT
);
-- один рядок на аудіоартефакт; ключ ідемпотентності — id у NotebookLM
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'audio',
    title TEXT,
    created_at TEXT,
    duration REAL,
    path TEXT,
    sha256 TEXT,
    bytes INTEGER,
    state TEXT NOT NULL DEFAULT 'downloaded',
    job_id INTEGER,
    downloaded_at TEXT,
    -- локальне аудіо свідомо видалено (spec 014); історія завантаження лишається
    audio_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_artifacts_notebook ON artifacts(notebook_id);
-- фактичні публікації: денний бюджет демона і «не частіше ніж раз на N годин»
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    video_id TEXT,
    day TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_day ON uploads(day);
-- коли востаннє надсилали сповіщення певного типу (spec 007: обмеження частоти)
CREATE TABLE IF NOT EXISTS notifications (
    key TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);
"""

# колонки, додані після 0.1 — для баз, створених старішою версією
_MIGRATIONS = {
    "transcript": "TEXT",
    "subtitles": "TEXT",
    "captions_id": "TEXT",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
}

# колонки artifacts, додані після першої версії (spec 014)
_ARTIFACT_MIGRATIONS = {
    "audio_deleted": "INTEGER NOT NULL DEFAULT 0",
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
    attempts: int = 0

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
            attempts=r["attempts"],
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
        self._add_columns("jobs", _MIGRATIONS)
        self._add_columns("artifacts", _ARTIFACT_MIGRATIONS)
        self.conn.commit()

    def _add_columns(self, table: str, migrations: dict[str, str]) -> None:
        existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        for column, sql_type in migrations.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

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

    def job_for_audio(self, audio: Path) -> int | None:
        r = self.conn.execute("SELECT id FROM jobs WHERE audio=?", (str(audio.resolve()),)).fetchone()
        return r["id"] if r else None

    def job_notebooks(self) -> dict[int, tuple[str, str]]:
        """Мапа job_id -> (notebook_id, назва ноутбука) для групування черги (spec 017)."""
        rows = self.conn.execute(
            """SELECT a.job_id, a.notebook_id, n.title
               FROM artifacts a JOIN notebooks n ON n.id = a.notebook_id
               WHERE a.job_id IS NOT NULL
               ORDER BY a.id"""
        ).fetchall()
        result: dict[int, tuple[str, str]] = {}
        for row in rows:
            result.setdefault(int(row["job_id"]), (row["notebook_id"], row["title"]))
        return result

    def jobs_for_notebook(self, notebook_id: str | None) -> list[Job]:
        """Задачі ноутбука; `None` — задачі без жодного привʼязаного артефакту."""
        if notebook_id is None:
            rows = self.conn.execute(
                """SELECT * FROM jobs j
                   WHERE NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.job_id = j.id)
                   ORDER BY j.id"""
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT j.* FROM jobs j
                   WHERE EXISTS (SELECT 1 FROM artifacts a
                                 WHERE a.job_id = j.id AND a.notebook_id = ?)
                   ORDER BY j.id""",
                (notebook_id,),
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    # --- NotebookLM --------------------------------------------------------
    def notebooks(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM notebooks ORDER BY title").fetchall()

    def notebook(self, notebook_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM notebooks WHERE id=?", (notebook_id,)).fetchone()

    def save_notebook(self, notebook_id: str, title: str, slug: str, url: str | None, sources: int | None) -> None:
        self.conn.execute(
            """INSERT INTO notebooks (id, title, slug, url, sources, synced_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   title=excluded.title, slug=excluded.slug, url=excluded.url,
                   sources=excluded.sources, synced_at=excluded.synced_at""",
            (notebook_id, title, slug, url, sources, _now()),
        )
        self.conn.commit()

    def artifact(self, artifact_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()

    def save_artifact(
        self,
        *,
        artifact_id: str,
        notebook_id: str,
        title: str,
        created_at: str | None,
        duration: float | None,
        path: Path,
        sha256: str | None,
        size: int | None,
        state: str = "downloaded",
    ) -> None:
        self.conn.execute(
            """INSERT INTO artifacts
                   (id, notebook_id, kind, title, created_at, duration, path, sha256, bytes,
                    state, downloaded_at, audio_deleted)
               VALUES (?, ?, 'audio', ?, ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(id) DO UPDATE SET
                   title=excluded.title, created_at=excluded.created_at,
                   duration=excluded.duration, path=excluded.path, sha256=excluded.sha256,
                   bytes=excluded.bytes, state=excluded.state,
                   downloaded_at=excluded.downloaded_at, audio_deleted=0""",
            (artifact_id, notebook_id, title, created_at, duration, str(path), sha256, size,
             state, _now()),
        )
        self.conn.commit()

    def artifacts(
        self, *, notebook_id: str | None = None, state: str | None = None
    ) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM artifacts WHERE 1=1", []
        if notebook_id is not None:
            sql += " AND notebook_id=?"
            args.append(notebook_id)
        if state is not None:
            sql += " AND state=?"
            args.append(state)
        return self.conn.execute(sql + " ORDER BY notebook_id, created_at", args).fetchall()

    def set_artifact_state(self, artifact_ids: list[str], state: str) -> None:
        self.conn.executemany(
            "UPDATE artifacts SET state=? WHERE id=?", [(state, aid) for aid in artifact_ids]
        )
        self.conn.commit()

    def link_artifact_job(self, artifact_id: str, job_id: int) -> None:
        self.conn.execute("UPDATE artifacts SET job_id=? WHERE id=?", (job_id, artifact_id))
        self.conn.commit()

    # --- локальне аудіо (spec 014) ------------------------------------------
    def set_audio_deleted(
        self, job_id: int, audio: Path | None = None, *, deleted: bool = True
    ) -> int:
        """Позначає локальне аудіо задачі видаленим/відновленим.

        Шукає артефакти і за `job_id`, і за шляхом: так працює навіть для старих баз,
        де зв'язок із задачею ще не був записаний. Повертає кількість оновлених рядків.
        """
        clause, args = "job_id=?", [job_id]
        if audio is not None:
            clause += " OR path=?"
            args.append(str(audio))
        cur = self.conn.execute(
            f"UPDATE artifacts SET audio_deleted=? WHERE {clause}",
            (1 if deleted else 0, *args),
        )
        self.conn.commit()
        return cur.rowcount

    def audio_deleted_job_ids(self) -> set[int]:
        """Id задач, чиє аудіо свідомо видалене (spec 014)."""
        rows = self.conn.execute(
            "SELECT DISTINCT job_id FROM artifacts WHERE audio_deleted=1 AND job_id IS NOT NULL"
        )
        return {int(r["job_id"]) for r in rows}

    # --- зведення для веб-UI (spec 008) -------------------------------------
    def notebook_stats(self) -> list[sqlite3.Row]:
        """Ноутбуки з лічильниками переказів; визначення лічильників — spec 008 §5."""
        return self.conn.execute(
            """SELECT n.id, n.title, n.slug, n.url, n.sources, n.synced_at,
                      COUNT(a.id) AS downloaded,
                      COALESCE(SUM(a.state = 'missing'), 0) AS missing,
                      COALESCE(SUM(a.audio_deleted), 0) AS deleted,
                      COALESCE(SUM(j.video_id IS NOT NULL), 0) AS published,
                      COALESCE(SUM(j.video_id IS NULL AND j.error IS NOT NULL), 0) AS errors,
                      COALESCE(SUM(a.id IS NOT NULL AND j.video_id IS NULL AND j.error IS NULL), 0)
                          AS queued
               FROM notebooks n
               LEFT JOIN artifacts a ON a.notebook_id = n.id
               LEFT JOIN jobs j ON j.id = a.job_id
               GROUP BY n.id
               ORDER BY n.title"""
        ).fetchall()

    def notebook_items(self, notebook_id: str) -> list[sqlite3.Row]:
        """Перекази ноутбука разом зі станом прив'язаної задачі (якщо є)."""
        return self.conn.execute(
            """SELECT a.id, a.title, a.created_at, a.duration, a.state, a.path, a.job_id,
                      a.audio_deleted, j.status AS job_status, j.video_id, j.error, j.attempts
               FROM artifacts a
               LEFT JOIN jobs j ON j.id = a.job_id
               WHERE a.notebook_id = ?
               ORDER BY a.created_at, a.id""",
            (notebook_id,),
        ).fetchall()

    # --- невдалі спроби -----------------------------------------------------
    def bump_attempt(self, job_id: int) -> int:
        self.conn.execute(
            "UPDATE jobs SET attempts = attempts + 1, updated_at=? WHERE id=?", (_now(), job_id)
        )
        self.conn.commit()
        row = self.conn.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        return int(row["attempts"]) if row else 0

    # --- фактичні публікації ------------------------------------------------
    def record_upload(self, job_id: int, video_id: str | None = None) -> None:
        now = datetime.now().astimezone()
        self.conn.execute(
            "INSERT INTO uploads (job_id, video_id, day, at) VALUES (?, ?, ?, ?)",
            (job_id, video_id, now.date().isoformat(), now.isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def uploads_count(self, day: str | None = None) -> int:
        day = day or datetime.now().date().isoformat()
        row = self.conn.execute("SELECT COUNT(*) AS n FROM uploads WHERE day=?", (day,)).fetchone()
        return int(row["n"]) if row else 0

    def last_upload_at(self) -> datetime | None:
        row = self.conn.execute("SELECT at FROM uploads ORDER BY id DESC LIMIT 1").fetchone()
        return datetime.fromisoformat(row["at"]) if row else None

    # --- сповіщення (spec 007) ------------------------------------------------
    def last_notified(self, key: str) -> datetime | None:
        row = self.conn.execute("SELECT sent_at FROM notifications WHERE key=?", (key,)).fetchone()
        return datetime.fromisoformat(row["sent_at"]) if row else None

    def mark_notified(self, key: str, at: datetime | None = None) -> None:
        at = at or datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO notifications (key, sent_at) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET sent_at=excluded.sent_at""",
            (key, at.isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def daily_counts(self, max_attempts: int) -> dict[str, int]:
        """Лічильники для денного зведення: скачано й опубліковано сьогодні, черга, помилки."""
        local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        since = local_midnight.astimezone(timezone.utc).isoformat(timespec="seconds")

        def one(sql: str, *args) -> int:
            row = self.conn.execute(sql, args).fetchone()
            return int(row[0] or 0)

        return {
            "downloaded": one("SELECT COUNT(*) FROM artifacts WHERE downloaded_at >= ?", since),
            "published": self.uploads_count(),
            "queued": one(
                "SELECT COUNT(*) FROM jobs WHERE status != 'done' AND attempts < ?", max_attempts
            ),
            "errors": one("SELECT COUNT(*) FROM jobs WHERE error IS NOT NULL"),
        }


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
