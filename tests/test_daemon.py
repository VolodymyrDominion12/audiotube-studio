"""Демон: бюджет, інтервали, повтори, лок, очищення (без мережі й FFmpeg)."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiotube.config import load_config
from audiotube.daemon import Daemon, FileLock, estimate_upload_cost
from audiotube.db import Database, QuotaTracker


class FakePipe:
    """Замінює Pipeline: кроки лише змінюють статуси в БД."""

    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.quota = QuotaTracker(db, cfg.youtube.daily_quota)
        self.calls = []
        self.fail_publish = set()
        self.auth_error = None  # youtube.AuthRequired, щоб імітувати протухлий токен

    def check_publish_auth(self):
        if self.auth_error:
            raise self.auth_error

    def scan(self):
        self.calls.append("scan")
        return []

    def _set(self, job, status, **fields):
        self.calls.append(status)
        return self.db.update(job.id, status=status, error=None, **fields)

    def transcribe(self, job):
        return self._set(job, "transcribed")

    def describe(self, job):
        return self._set(job, "described")

    def render(self, job):
        video = self.cfg.output / f"{job.slug}.mp4"
        video.write_bytes(b"video")
        return self._set(job, "rendered", video=video)

    def publish(self, job):
        self.calls.append("publish")
        if job.id in self.fail_publish:
            return self.db.update(job.id, error="YouTube відмовив")
        job = self.db.update(job.id, status="done", video_id=f"vid{job.id}", error=None)
        self.db.record_upload(job.id, job.video_id)
        return job


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "daemon:\n"
            "  videos_per_day: 2\n"
            "  min_hours_between: 0\n"
            "  pause_seconds: 0\n"
            "  cleanup_video_after_upload: false\n"
            "  interval_minutes: 30\n"
            "youtube:\n"
            "  upload_captions: false\n"
            "  manage_playlists: false\n",
            encoding="utf-8",
        )
        self.cfg = load_config(root=self.tmp)
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.pipe = FakePipe(self.cfg, self.db)
        # на машині без FFmpeg демон мав би завершитися з кодом 2 — тут перевіряємо логіку
        patch = mock.patch("audiotube.daemon.has_ffmpeg", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def daemon(self):
        return Daemon(self.cfg, pipe=self.pipe, log=lambda _m: None)

    def job(self, index=1, status="new"):
        audio = self.cfg.inbox / f"ep{index}.mp3"
        audio.write_bytes(b"audio")
        job = self.db.add_job(audio, f"ep{index}", title=f"Епізод {index}")
        if status != "new":
            self.db.update(job.id, status=status)
        return self.db.get(job.id)

    def test_daily_budget(self):
        for i in range(1, 6):
            self.job(i)
        code = self.daemon().run(once=True, sync=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.db.uploads_count(), 2)
        done = [j for j in self.db.list() if j.status == "done"]
        self.assertEqual(len(done), 2)

    def test_min_hours_between_blocks_next_cycle(self):
        self.job(1)
        self.cfg.daemon.min_hours_between = 6
        self.daemon().run(once=True, sync=False)
        self.assertEqual(self.db.uploads_count(), 1)

        stats = self.daemon()._cycle(dry_run=False, max_videos=None, sync=False)
        self.assertEqual(stats.published, 0)
        self.assertIn("6", stats.reason)

    def test_max_attempts_stops_retries(self):
        job = self.job(1)
        self.pipe.fail_publish.add(job.id)
        self.cfg.daemon.max_attempts = 2
        for _ in range(3):
            self.daemon().run(once=True, sync=False, max_videos=1)
        self.assertEqual(self.db.get(job.id).attempts, 2)
        self.assertEqual(self.pipe.calls.count("publish"), 2)

    def test_cleanup_removes_local_video(self):
        job = self.job(1, status="rendered")
        video = self.cfg.output / "ep1.mp4"
        video.write_bytes(b"video")
        self.db.update(job.id, video=video)
        self.cfg.daemon.cleanup_video_after_upload = True
        self.daemon().run(once=True, sync=False)
        self.assertFalse(video.exists())
        self.assertIsNone(self.db.get(job.id).video)

    def test_dry_run_changes_nothing(self):
        self.job(1)
        stats = self.daemon()._cycle(dry_run=True, max_videos=None, sync=False)
        self.assertEqual(stats.planned, 1)
        self.assertEqual(self.pipe.calls, [])
        self.assertEqual(self.db.uploads_count(), 0)

    def test_lock_blocks_second_instance(self):
        lock = FileLock(self.cfg.data / "daemon.lock")
        self.assertTrue(lock.acquire())
        try:
            code = self.daemon().run(once=True, sync=False)
            self.assertEqual(code, 3)
        finally:
            lock.release()

    def test_quota_guard(self):
        self.cfg.youtube.daily_quota = 1000
        self.pipe.quota = QuotaTracker(self.db, 1000)
        self.job(1)
        stats = self.daemon()._cycle(dry_run=False, max_videos=None, sync=False)
        self.assertEqual(stats.published, 0)
        self.assertIn("квота", stats.reason)
        self.assertEqual(self.pipe.calls, [])

    def test_full_chain_from_new(self):
        job = self.job(1)
        self.daemon().run(once=True, sync=False)
        done = self.db.get(job.id)
        self.assertEqual(done.status, "done")
        self.assertEqual(done.video_id, "vid1")
        self.assertIn("rendered", self.pipe.calls)
        self.assertEqual(self.db.uploads_count(), 1)

    def test_cost_estimate(self):
        self.assertEqual(estimate_upload_cost(self.cfg), 1600 + 50)
        self.cfg.youtube.upload_captions = True
        self.cfg.youtube.manage_playlists = True
        self.assertEqual(estimate_upload_cost(self.cfg), 1600 + 50 + 400 + 52)


class DaemonCliTests(unittest.TestCase):
    def test_dry_run_smoke(self):
        from audiotube.cli import main

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "config.yaml").write_text(
            "daemon:\n  cleanup_video_after_upload: false\n", encoding="utf-8"
        )
        code = main(["--root", str(tmp), "daemon", "--dry-run", "--no-sync"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
