"""Spec 016: кнопка й API браузерної публікації у веб-UI (без Chrome)."""

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
import urllib.error
import urllib.request

from audiotube.config import load_config
from audiotube.db import Database
from audiotube.webui import Busy, WebApp, make_server


class BrowserWebUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "youtube:\n  browser:\n    profile_dir: data/browser/youtube\n",
            encoding="utf-8",
        )
        self.cfg = load_config(root=self.tmp)
        self.cfg.ensure_dirs()
        audio = self.cfg.inbox / "Тема.mp3"
        audio.write_bytes(b"audio")
        audio.with_suffix(".yaml").write_text('title: "Тема"\n', encoding="utf-8")
        video = self.cfg.output / "tema.mp4"
        video.write_bytes(b"video")
        db = Database(self.cfg.db_path)
        job = db.add_job(audio, "tema", title="Тема")
        db.update(job.id, status="rendered", video=video)
        db.close()
        self.job_id = job.id

        self.app = WebApp(self.cfg, "127.0.0.1", 0)
        self.server = make_server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def request(self, path, method="GET", data=None):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()

    def test_index_has_browser_button(self):
        """BROW-AC12: у веб-UI є кнопка браузерної публікації й режим перегляду."""
        status, body = self.request("/")
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("Через браузер", html)
        self.assertIn('id="browser-review"', html)
        self.assertIn("/api/browser", html)

    def test_index_resumes_browser_poll_on_load(self):
        """BROW-AC16: після перезавантаження сторінки веб-UI підхоплює активний браузерний прогін."""
        status, body = self.request("/")
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("maybeResumeBrowser", html)

    def test_start_and_busy(self):
        """BROW-AC12: фоновий запуск, повторний — 409, стан доступний через /api/browser."""
        started = threading.Event()
        release = threading.Event()

        class BlockingPublisher:
            instances = []

            def __init__(self, pipe, log=None, driver=None):
                self.pipe = pipe
                self.log = log
                self.closed = False
                BlockingPublisher.instances.append(self)

            def publish(self, job, review=False, dry_run=False):
                self.review = review
                self.log("фейкова публікація почалась")
                started.set()
                release.wait(10)
                self.log("фейкова публікація завершена")
                return self.pipe.db.update(
                    job.id, video_id="dQw4w9WgXcQ", status="done", error=None
                )

            def close(self):
                self.closed = True

        with mock.patch("audiotube.browser_publish.BrowserPublisher", BlockingPublisher):
            status, body = self.request(
                f"/api/jobs/{self.job_id}/browser-upload?review=1", method="POST", data={}
            )
            self.assertEqual(status, 202)
            self.assertTrue(json.loads(body)["started"])
            self.assertTrue(started.wait(5))

            status, body = self.request(
                f"/api/jobs/{self.job_id}/browser-upload", method="POST", data={}
            )
            self.assertEqual(status, 409)
            self.assertIn("браузер уже працює", json.loads(body)["message"])

            release.set()
            self.app.browser_thread.join(timeout=10)
            self.assertFalse(self.app.browser_thread.is_alive())

        self.assertTrue(BlockingPublisher.instances[0].closed)
        self.assertTrue(BlockingPublisher.instances[0].review)

        status, body = self.request("/api/browser")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["video_id"], "dQw4w9WgXcQ")
        self.assertIn("фейкова публікація завершена", "\n".join(state["log"]))

        status, body = self.request("/api/state")
        jobs = json.loads(body)["jobs"]
        self.assertEqual(jobs[0]["status"], "done")
        self.assertEqual(jobs[0]["url"], "https://youtu.be/dQw4w9WgXcQ")

    def test_unknown_job(self):
        status, body = self.request("/api/jobs/999/browser-upload", method="POST", data={})
        self.assertEqual(status, 404)
        self.assertIn("не знайдено", json.loads(body)["message"])

    def test_done_job_rejected(self):
        db = Database(self.cfg.db_path)
        db.update(self.job_id, status="done", video_id="dQw4w9WgXcQ")
        db.close()
        status, body = self.request(
            f"/api/jobs/{self.job_id}/browser-upload", method="POST", data={}
        )
        self.assertEqual(status, 400)
        self.assertIn("вже опублікована", json.loads(body)["message"])


class BrowserBusyUnitTests(unittest.TestCase):
    def test_busy_when_thread_alive(self):
        """BROW-AC12: Busy піднімається, якщо попередній прогін ще живий."""
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "config.yaml").write_text("", encoding="utf-8")
            cfg = load_config(root=tmp)
            cfg.ensure_dirs()
            app = WebApp(cfg, "127.0.0.1", 0)
            gate = threading.Event()

            def alive():
                gate.wait(5)

            app.browser_thread = threading.Thread(target=alive, daemon=True)
            app.browser_thread.start()
            with self.assertRaises(Busy):
                app.start_browser_upload(1)
            gate.set()
            app.browser_thread.join(timeout=5)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
