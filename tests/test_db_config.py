import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from audiotube.config import Config, load_config
from audiotube.db import STATUSES, Database, Job, QuotaTracker

OLD_SCHEMA = """
CREATE TABLE jobs (
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
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE quota (day TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0);
"""


class DatabaseTests(unittest.TestCase):
    def test_migration_adds_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(OLD_SCHEMA)
            conn.execute(
                "INSERT INTO jobs (audio, slug, status, created_at, updated_at) "
                "VALUES ('/tmp/a.mp3', 'a', 'rendered', 'now', 'now')"
            )
            conn.commit()
            conn.close()

            db = Database(path)
            job = db.get(1)
            db.close()

        self.assertIsNotNone(job)
        self.assertEqual(job.status, "rendered")
        self.assertIsNone(job.transcript)
        self.assertIsNone(job.subtitles)
        self.assertIsNone(job.captions_id)

    def test_add_job_and_unique_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = Database(tmp / "db.sqlite")
            first = db.add_job(tmp / "a.mp3", "same")
            second = db.add_job(tmp / "b.mp3", "same")
            self.assertNotEqual(first.slug, second.slug)
            self.assertIsNone(db.add_job(tmp / "a.mp3", "other"))
            db.close()

    def test_update_paths_and_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = Database(tmp / "db.sqlite")
            job = db.add_job(tmp / "a.mp3", "a")
            job = db.update(job.id, transcript=tmp / "t.json", status="transcribed")
            self.assertEqual(job.status, "transcribed")
            self.assertEqual(job.transcript, tmp / "t.json")
            db.close()

        self.assertIn("uploaded", STATUSES)
        self.assertEqual(STATUSES[0], "new")


class QuotaTests(unittest.TestCase):
    def test_spend_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "db.sqlite")
            quota = QuotaTracker(db, daily_limit=100)
            self.assertEqual(quota.used(), 0)
            self.assertEqual(quota.remaining(), 100)
            quota.spend(60)
            self.assertEqual(quota.used(), 60)
            self.assertFalse(quota.can_spend(50))
            self.assertTrue(quota.can_spend(40))
            db.close()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config.yaml").write_text(
            "render:\n  width: 640\n  height: 360\n  fps: 15\n"
            "llm:\n  provider: openai\n  model: gpt-test\n"
            "transcribe:\n  backend: none\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_overrides_and_defaults(self):
        cfg = load_config(root=self.root)
        self.assertEqual(cfg.render.width, 640)
        self.assertEqual(cfg.llm.provider, "openai")
        self.assertEqual(cfg.transcribe.backend, "none")
        self.assertEqual(cfg.youtube.upload_captions, True)  # значення за замовчуванням
        self.assertEqual(cfg.paths.cache, "cache")
        self.assertEqual(cfg.db_path, self.root / "data" / "audiotube.db")

    def test_ensure_dirs(self):
        cfg = load_config(root=self.root)
        cfg.ensure_dirs()
        for path in (cfg.inbox, cfg.workspace, cfg.output, cfg.data, cfg.cache):
            self.assertTrue(path.is_dir(), path)
        self.assertTrue((cfg.assets / "images" / "abstract").is_dir())

    def test_dotenv_does_not_override_env(self):
        (self.root / ".env").write_text("AUDIOTUBE_TEST_KEY=from-dotenv\n", encoding="utf-8")
        os.environ["AUDIOTUBE_TEST_KEY"] = "from-env"
        try:
            load_config(root=self.root)
            self.assertEqual(os.environ["AUDIOTUBE_TEST_KEY"], "from-env")
        finally:
            del os.environ["AUDIOTUBE_TEST_KEY"]

    def test_invalid_value(self):
        (self.root / "config.yaml").write_text("render:\n  encoder: magic\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(root=self.root)


class ModelTests(unittest.TestCase):
    def test_config_roundtrip_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.llm.provider, "ollama")
        self.assertEqual(cfg.transcribe.model, "small")
        self.assertFalse(cfg.render.burn_subtitles)
        self.assertTrue(cfg.pipeline.auto_transcribe)

    def test_job_url(self):
        job = Job(1, Path("a.mp3"), "a", "uploaded", "T", None, None, None, "vid", None, None)
        self.assertEqual(job.url, "https://youtu.be/vid")


if __name__ == "__main__":
    unittest.main()
