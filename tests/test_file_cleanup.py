"""Локальне очищення аудіо та відео (spec 014): без мережі, FFmpeg і Whisper."""

import contextlib
import io
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from audiotube.cli import _process, main
from audiotube.config import load_config
from audiotube.daemon import Daemon
from audiotube.db import Database
from audiotube.notebooklm import RemoteNotebook
from audiotube.pipeline import Pipeline
from audiotube.sync import SyncService
from audiotube.webui import WebApp, make_server
from test_sync import FakeAdapter, artifact


class FileCleanupTests(unittest.TestCase):
    """Задача з уже скачаним аудіо, відрендереним відео й публікацією на YouTube."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "notebooklm:\n  delay_seconds: 0\n  max_per_run: 20\n"
            "daemon:\n  videos_per_day: 2\n  min_hours_between: 0\n  pause_seconds: 0\n",
            encoding="utf-8",
        )
        self.cfg = load_config(root=self.tmp)
        self.cfg.ensure_dirs()

        self.nb = RemoteNotebook(id="nb1abc123456", title="Docker за 20 хвилин")
        self.adapter = FakeAdapter([self.nb], {self.nb.id: [artifact()]})
        self.db = Database(self.cfg.db_path)
        SyncService(self.cfg, db=self.db, adapter=self.adapter, log=lambda _m: None).run()

        self.row = self.db.artifact("a1")
        self.audio = Path(self.row["path"])
        self.job_id = self.row["job_id"]
        self.job = self.db.get(self.job_id)
        self.sidecar = self.audio.with_suffix(".yaml")

        self.video = self.cfg.output / f"{self.job.slug}.mp4"
        self.preview = self.cfg.output / f"{self.job.slug}_preview.mp4"
        self.thumb = self.cfg.output / f"{self.job.slug}.jpg"
        for path, data in ((self.video, b"video"), (self.preview, b"preview"), (self.thumb, b"jpg")):
            path.write_bytes(data)
        self.db.update(
            self.job_id, status="uploaded", video=self.video, thumbnail=self.thumb,
            video_id="VID1", playlist_id="PL1", captions_id="CAP1",
        )
        self.db.record_upload(self.job_id, "VID1")

        self.app = WebApp(self.cfg, "127.0.0.1", 0, adapter=self.adapter)
        self.server = make_server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- утиліти -------------------------------------------------------------
    def request(self, path, method="GET", data=None):
        import urllib.error
        import urllib.request

        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            self.base + path, data=body, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            finally:
                e.close()

    def action(self, name, job_id=None, expect=200):
        status, data = self.request(f"/api/jobs/{job_id or self.job_id}/{name}", method="POST", data={})
        self.assertEqual(status, expect, data)
        return data

    def state_job(self):
        status, data = self.request("/api/state")
        self.assertEqual(status, 200, data)
        return next(j for j in data["jobs"] if j["id"] == self.job_id), data

    def sync(self, dry_run=False):
        status, data = self.request(f"/api/sync{'?dry_run=1' if dry_run else ''}", method="POST", data={})
        self.assertEqual(status, 200, data)
        return data

    # --- критерії -------------------------------------------------------------
    def test_delete_audio_keeps_history(self):
        """FILE-AC1: аудіо зникає, але історія, метадані й YouTube лишаються."""
        data = self.action("delete_audio")

        self.assertFalse(self.audio.exists())
        row = self.db.artifact("a1")
        self.assertEqual(row["audio_deleted"], 1)
        self.assertIsNotNone(row["sha256"])
        self.assertIsNotNone(row["downloaded_at"])
        self.assertEqual(row["bytes"], len(b"fake-audio-a1"))

        job = self.db.get(self.job_id)
        self.assertEqual(job.video_id, "VID1")
        self.assertEqual(job.status, "uploaded")
        self.assertTrue(self.sidecar.exists(), "YAML-метадані мають лишитися")

        self.assertFalse(data["job"]["audio_exists"])
        self.assertTrue(data["job"]["audio_deleted"])

    def test_sync_does_not_redownload_deleted_audio(self):
        """FILE-AC2: після видалення sync не качає аудіо повторно."""
        self.action("delete_audio")
        self.assertEqual(len(self.adapter.downloads), 1)

        data = self.sync()
        self.assertEqual(data["report"]["downloaded"], 0)
        self.assertEqual(data["report"]["deleted"], 1)
        self.assertEqual(len(self.adapter.downloads), 1)
        self.assertFalse(self.audio.exists())
        self.assertIn("видалено локально", data["message"])

    def test_force_skips_deleted_audio(self):
        """FILE-AC2: --force теж не чіпає свідомо видалене аудіо."""
        self.action("delete_audio")
        report = SyncService(
            self.cfg, db=self.db, adapter=self.adapter, log=lambda _m: None
        ).run(force=True)
        self.assertEqual(report.downloaded, 0)
        self.assertEqual(report.deleted, 1)
        self.assertEqual(len(self.adapter.downloads), 1)
        self.assertFalse(self.audio.exists())

    def test_restore_downloads_again(self):
        """FILE-AC3: «Відновити аудіо» знімає прапорець, і sync качає файл знову."""
        self.action("delete_audio")
        data = self.action("restore_audio")
        self.assertFalse(data["job"]["audio_deleted"])
        self.assertEqual(self.db.artifact("a1")["audio_deleted"], 0)

        data = self.sync()
        self.assertEqual(data["report"]["downloaded"], 1)
        self.assertTrue(self.audio.exists())
        self.assertEqual(len(self.adapter.downloads), 2)

    def test_delete_video_keeps_youtube_info(self):
        """FILE-AC4: видаляємо лише локальні mp4, історія YouTube без змін."""
        data = self.action("delete_video")

        self.assertFalse(self.video.exists())
        self.assertFalse(self.preview.exists())
        self.assertTrue(self.thumb.exists(), "обкладинка не є відео й лишається")

        job = self.db.get(self.job_id)
        self.assertIsNone(job.video)
        self.assertEqual(job.video_id, "VID1")
        self.assertEqual(job.playlist_id, "PL1")
        self.assertEqual(job.captions_id, "CAP1")
        self.assertEqual(job.status, "uploaded")
        self.assertEqual(self.db.uploads_count(), 1)
        self.assertIn("VID1", data["job"]["url"])
        self.assertFalse(data["job"]["video_exists"])

    def test_never_deletes_outside_managed_dirs(self):
        """FILE-AC5: файли поза inbox/ і output/ не видаляються."""
        external_audio = self.tmp / "external.mp3"
        external_audio.write_bytes(b"audio")
        external_job = self.db.add_job(external_audio, "external")
        status, data = self.request(f"/api/jobs/{external_job.id}/delete_audio", method="POST", data={})
        self.assertEqual(status, 400, data)
        self.assertTrue(external_audio.exists())

        external_video = self.tmp / "external.mp4"
        external_video.write_bytes(b"video")
        self.db.update(self.job_id, video=external_video)
        status, data = self.request(f"/api/jobs/{self.job_id}/delete_video", method="POST", data={})
        self.assertEqual(status, 400, data)
        self.assertTrue(external_video.exists())
        self.assertTrue(self.video.exists(), "часткового видалення бути не має")
        self.assertEqual(self.db.get(self.job_id).video, external_video)

    def test_delete_is_idempotent(self):
        """FILE-AC6: повторне видалення не падає й не змінює історію."""
        self.action("delete_audio")
        data = self.action("delete_audio")
        self.assertEqual(self.db.artifact("a1")["audio_deleted"], 1)
        self.assertFalse(data["job"]["audio_exists"])

        self.action("delete_video")
        data = self.action("delete_video")
        self.assertIsNone(self.db.get(self.job_id).video)
        self.assertEqual(self.db.get(self.job_id).video_id, "VID1")

    def test_state_and_page_expose_file_actions(self):
        """FILE-AC7: API віддає наявність/розміри, сторінка — кнопки й підсумки."""
        self.action("delete_audio")
        self.action("delete_video")

        job, state = self.state_job()
        self.assertFalse(job["audio_exists"])
        self.assertTrue(job["audio_deleted"])
        self.assertIsNone(job["audio_size"])
        self.assertFalse(job["video_exists"])
        self.assertIsNone(job["video_size"])
        self.assertIn("inbox_bytes", state["files"])
        self.assertIn("output_bytes", state["files"])
        self.assertGreaterEqual(state["files"]["output_bytes"], 0)

        status, data = self.request("/api/notebooks")
        self.assertEqual(status, 200, data)
        nb = next(n for n in data["notebooks"] if n["id"] == self.nb.id)
        self.assertEqual(nb["deleted"], 1)

        status, data = self.request(f"/api/notebooks/{self.nb.id}")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["items"][0]["audio_deleted"])
        self.assertFalse(data["items"][0]["audio_exists"])

        import urllib.request

        with urllib.request.urlopen(self.base + "/", timeout=15) as resp:
            html = resp.read().decode("utf-8")
        for marker in ("delete_audio", "delete_video", "restore_audio", "fmtSize"):
            self.assertIn(marker, html)

    def test_dry_run_reports_deleted(self):
        """FILE-AC8: dry-run показує видалене аудіо, але нічого не змінює."""
        self.action("delete_audio")
        data = self.sync(dry_run=True)
        self.assertEqual(data["report"]["deleted"], 1)
        self.assertEqual(data["report"]["downloaded"], 0)
        self.assertFalse(self.audio.exists())
        self.assertEqual(self.db.artifact("a1")["audio_deleted"], 1)
        self.assertEqual(len(self.adapter.downloads), 1)

    def test_daemon_skips_audio_less_unfinished_job(self):
        """FILE-AC9: демон не бере незавершену задачу без аудіо, але публікує готове відео."""
        self.action("delete_audio")
        self.db.update(self.job_id, status="new", video=None, video_id=None)
        daemon = Daemon(self.cfg, log=lambda _m: None)
        try:
            stats = daemon._cycle_body(dry_run=True, max_videos=None, sync=False)
            self.assertEqual(stats.planned, 0)
            self.assertIn("немає задач", stats.reason)

            self.video.write_bytes(b"video")
            self.db.update(self.job_id, status="rendered", video=self.video)
            stats = daemon._cycle_body(dry_run=True, max_videos=None, sync=False)
            self.assertEqual(stats.planned, 1)
        finally:
            daemon._close()

    def test_run_process_skips_audio_less_job(self):
        """FILE-AC9: `run/watch` не намагається транскрибувати задачу без аудіо."""
        self.action("delete_audio")
        self.db.update(self.job_id, status="new", video=None, video_id=None)
        pipe = Pipeline(self.cfg, log=lambda _m: None)
        try:
            _process(pipe, transcribe=True, describe=True, render=True, upload=False)
        finally:
            pipe.db.close()
        job = self.db.get(self.job_id)
        self.assertEqual(job.status, "new")
        self.assertIsNone(job.error)

    def test_cli_sync_reports_deleted(self):
        """FILE-AC2: `audiotube sync` друкує окремий рядок і лічильник видаленого."""
        self.action("delete_audio")
        out = io.StringIO()
        with mock.patch("audiotube.cli.build_adapter", return_value=self.adapter):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--root", str(self.tmp), "sync"])
        self.assertEqual(code, 0, out.getvalue())
        text = out.getvalue()
        self.assertIn("видалено локально: 1", text)
        self.assertIn("не качаємо повторно", text)


if __name__ == "__main__":
    unittest.main()
