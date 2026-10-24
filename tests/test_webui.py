import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from audiotube.config import load_config
from audiotube.webui import WebApp, make_server


class WebUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text("render:\n  width: 320\n  height: 180\n", encoding="utf-8")
        self.cfg = load_config(root=self.tmp)
        self.cfg.ensure_dirs()
        (self.tmp / "inbox" / "test.mp3").write_bytes(b"not really audio")

        self.app = WebApp(self.cfg, "127.0.0.1", 0)
        self.server = make_server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def request(self, path, method="GET", data=None, headers=None):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            self.base + path, data=body, method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read(), dict(e.headers)
            finally:
                e.close()

    def state(self):
        status, body, _ = self.request("/api/state")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_index_and_state(self):
        status, body, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("AudioTube Studio", body.decode("utf-8"))
        self.assertIn("text/html", headers["Content-Type"])

        state = self.state()
        self.assertEqual(state["jobs"], [])
        self.assertEqual(state["quota"]["used"], 0)
        self.assertIn("llm", state["config"])

    def test_scan_and_actions(self):
        status, body, _ = self.request("/api/scan", method="POST", data={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["added"], 1)

        state = self.state()
        self.assertEqual(len(state["jobs"]), 1)
        job = state["jobs"][0]
        self.assertEqual(job["status"], "new")
        self.assertEqual(job["title"], "Test")

        status, body, _ = self.request("/api/jobs/1/reset", method="POST", data={})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        status, body, _ = self.request("/api/jobs/999/reset", method="POST", data={})
        self.assertEqual(status, 400)
        self.assertIn("не знайдено", json.loads(body)["message"])

    def test_metadata_edit_validates_yaml(self):
        self.request("/api/scan", method="POST", data={})

        status, body, _ = self.request("/api/jobs/1/meta")
        self.assertEqual(status, 200)
        self.assertIn("title", json.loads(body)["yaml"])

        status, body, _ = self.request(
            "/api/jobs/1/meta", method="PUT", data={"yaml": "title: [unclosed"}
        )
        self.assertEqual(status, 400)

        status, body, _ = self.request(
            "/api/jobs/1/meta", method="PUT", data={"yaml": "title: T\nchapters: ['1.2 погано']\n"}
        )
        self.assertEqual(status, 400)

        good = "title: \"Нова назва\"\ndescription: \"Опис\"\ntags: [docker]\nchapters:\n  - \"00:00 Вступ\"\n"
        status, body, _ = self.request("/api/jobs/1/meta", method="PUT", data={"yaml": good})
        self.assertEqual(status, 200)
        side = self.tmp / "inbox" / "test.yaml"
        self.assertTrue(side.exists())
        self.assertEqual(self.state()["jobs"][0]["title"], "Нова назва")

    def test_media_files_and_range(self):
        out = self.tmp / "output" / "clip.mp4"
        out.write_bytes(b"0123456789")

        status, body, headers = self.request("/media/output/clip.mp4")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"0123456789")
        self.assertEqual(headers["Content-Type"], "video/mp4")

        status, body, headers = self.request("/media/output/clip.mp4", headers={"Range": "bytes=2-5"})
        self.assertEqual(status, 206)
        self.assertEqual(body, b"2345")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")

        status, _, _ = self.request("/media/output/nope.mp4")
        self.assertEqual(status, 404)

    def test_media_path_safety(self):
        self.assertIsNone(self.app.media_path("../config.yaml"))
        self.assertIsNone(self.app.media_path("output/../config.yaml"))
        self.assertIsNone(self.app.media_path("config.yaml"))
        self.assertIsNone(self.app.media_path(""))
        # файл у workspace дозволений
        work = self.tmp / "workspace" / "x"
        work.mkdir(parents=True)
        (work / "plan.json").write_text("{}", encoding="utf-8")
        self.assertIsNotNone(self.app.media_path("workspace/x/plan.json"))


if __name__ == "__main__":
    unittest.main()
