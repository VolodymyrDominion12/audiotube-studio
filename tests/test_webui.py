import json
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path

from datetime import datetime, timedelta

from audiotube.config import load_config
from audiotube.daemon import FileLock
from audiotube.db import Database
from audiotube.notebooklm import RemoteNotebook
from audiotube.webui import WebApp, make_server
from test_sync import FakeAdapter, artifact


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


class NotebooksWebTests(unittest.TestCase):
    """Вкладка «Ноутбуки» (spec 008): БД засіяна вручну, NotebookLM — FakeAdapter."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "notebooklm:\n  delay_seconds: 0\n  max_per_run: 20\n"
            "daemon:\n  videos_per_day: 2\n  min_hours_between: 6\n",
            encoding="utf-8",
        )
        self.cfg = load_config(root=self.tmp)
        self.cfg.ensure_dirs()
        self.nb = RemoteNotebook(id="nb1abc123456", title="Docker за 20 хвилин")
        self.adapter = FakeAdapter([self.nb], {self.nb.id: [artifact()]})

        self.app = WebApp(self.cfg, "127.0.0.1", 0, adapter=self.adapter)
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

    request = WebUITests.request

    def get_json(self, path, expect=200):
        status, body, _ = self.request(path)
        self.assertEqual(status, expect, body)
        return json.loads(body)

    # --- засів БД -------------------------------------------------------------
    def seed(self):
        """nb-a: опубліковано, помилка, без задачі, зниклий; nb-b: один у черзі; nb-c: порожній."""
        db = Database(self.cfg.db_path)
        try:
            db.save_notebook("nb-a", "Альфа", "alfa", None, 3)
            db.save_notebook("nb-b", "Бета", "beta", None, 1)
            db.save_notebook("nb-c", "Гама", "gama", None, 0)

            def art(aid, nb, day, state="downloaded", job=None):
                path = self.cfg.inbox / nb / f"{aid}.m4a"
                db.save_artifact(
                    artifact_id=aid, notebook_id=nb, title=f"Переказ {aid}",
                    created_at=f"2026-10-0{day}T00:00:00+00:00", duration=600.0 + day,
                    path=path, sha256=None, size=10, state=state,
                )
                if job:
                    j = db.add_job(path, f"slug-{aid}", f"Переказ {aid}")
                    db.link_artifact_job(aid, j.id)
                    if job != "queued":
                        db.update(j.id, **job)

            art("a1", "nb-a", 1, job={"status": "done", "video_id": "VID1"})
            art("a2", "nb-a", 2, job={"error": "квота вичерпана\nдеталі трасування"})
            art("a3", "nb-a", 3)
            art("a4", "nb-a", 4, state="missing", job="queued")
            art("b1", "nb-b", 1, job="queued")
        finally:
            db.close()

    def dump(self):
        db = Database(self.cfg.db_path)
        try:
            return list(db.conn.iterdump())
        finally:
            db.close()

    # --- тести ----------------------------------------------------------------
    def test_notebooks_counters(self):
        """WEB-AC1: GET /api/notebooks — усі ноутбуки з правильними лічильниками."""
        self.seed()
        data = self.get_json("/api/notebooks")
        by_id = {n["id"]: n for n in data["notebooks"]}
        self.assertEqual(list(by_id), ["nb-a", "nb-b", "nb-c"])  # за назвою
        self.assertEqual(
            {k: by_id["nb-a"][k] for k in ("downloaded", "missing", "published", "errors", "queued")},
            {"downloaded": 4, "missing": 1, "published": 1, "errors": 1, "queued": 2},
        )
        self.assertEqual(
            {k: by_id["nb-b"][k] for k in ("downloaded", "missing", "published", "errors", "queued")},
            {"downloaded": 1, "missing": 0, "published": 0, "errors": 0, "queued": 1},
        )
        self.assertEqual(by_id["nb-c"]["downloaded"], 0)
        self.assertEqual(by_id["nb-c"]["queued"], 0)
        self.assertEqual(by_id["nb-a"]["title"], "Альфа")

    def test_notebook_detail(self):
        """WEB-AC5: перекази ноутбука зі статусом, URL і першим рядком помилки; невідомий id → 404."""
        self.seed()
        data = self.get_json("/api/notebooks/nb-a")
        self.assertEqual(data["notebook"]["title"], "Альфа")
        items = {i["id"]: i for i in data["items"]}
        self.assertEqual(list(items), ["a1", "a2", "a3", "a4"])
        self.assertEqual(items["a1"]["job_status"], "done")
        self.assertEqual(items["a1"]["url"], "https://youtu.be/VID1")
        self.assertEqual(items["a2"]["error"], "квота вичерпана")
        self.assertIsNone(items["a3"]["job_id"])
        self.assertIsNone(items["a3"]["url"])
        self.assertEqual(items["a4"]["state"], "missing")
        self.assertEqual(items["a1"]["duration"], 601.0)

        status, body, _ = self.request("/api/notebooks/nope")
        self.assertEqual(status, 404)
        self.assertIn("не знайдено", json.loads(body)["message"])

    def test_daemon_panel(self):
        """WEB-AC6: 1 публікація сьогодні з 2 → наступне вікно = остання + min_hours_between."""
        db = Database(self.cfg.db_path)
        try:
            db.record_upload(1, "VID1")
            last = db.last_upload_at()
        finally:
            db.close()
        data = self.get_json("/api/daemon")
        self.assertEqual(data["uploads_today"], 1)
        self.assertEqual(data["videos_per_day"], 2)
        self.assertEqual(data["quota"]["limit"], self.cfg.youtube.daily_quota)
        self.assertEqual(data["quota"]["remaining"], self.cfg.youtube.daily_quota)
        self.assertEqual(datetime.fromisoformat(data["next_upload_at"]), last + timedelta(hours=6))
        self.assertFalse(data["running"])

        lock = FileLock(self.cfg.data / "daemon.lock")
        self.assertTrue(lock.acquire())
        try:
            self.assertTrue(self.get_json("/api/daemon")["running"])
        finally:
            lock.release()

    def test_sync_plan_does_not_change_db(self):
        """WEB-AC2: POST синк-план показує план і не змінює БД та inbox."""
        self.seed()
        before = self.dump()
        status, body, _ = self.request("/api/sync?dry_run=1", method="POST", data={})
        self.assertEqual(status, 200, body)
        data = json.loads(body)
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["report"]["planned"], 1)
        actions = [i["action"] for i in data["report"]["items"]]
        self.assertEqual(actions.count("plan"), 1)
        # засіяних ноутбуків немає в NotebookLM → план показує 4 зниклі, але в БД їх не позначає
        self.assertEqual(data["report"]["missing"], 4)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.adapter.downloads, [])

    def test_sync_busy_lock_returns_409(self):
        """WEB-AC3: замок демона зайнятий → 409 і повідомлення, NotebookLM не чіпаємо."""
        lock = FileLock(self.cfg.data / "daemon.lock")
        self.assertTrue(lock.acquire())
        try:
            status, body, _ = self.request("/api/sync", method="POST", data={})
        finally:
            lock.release()
        self.assertEqual(status, 409)
        self.assertIn("демон", json.loads(body)["message"])
        self.assertEqual(self.adapter.entered, 0)

    def test_sync_runs_and_releases_lock(self):
        """WEB-AC7: справжній синк качає файл, реєструє задачу й звільняє замок."""
        status, body, _ = self.request("/api/sync", method="POST", data={})
        self.assertEqual(status, 200, body)
        data = json.loads(body)
        self.assertFalse(data["dry_run"])
        self.assertEqual(data["report"]["downloaded"], 1)
        self.assertEqual(len(self.adapter.downloads), 1)

        nbs = self.get_json("/api/notebooks")["notebooks"]
        self.assertEqual(len(nbs), 1)
        self.assertEqual(nbs[0]["downloaded"], 1)
        self.assertEqual(nbs[0]["queued"], 1)
        self.assertEqual(len(self.get_json("/api/state")["jobs"]), 1)

        lock = FileLock(self.cfg.data / "daemon.lock")
        self.assertTrue(lock.acquire(), "замок мав бути звільнений після синку")
        lock.release()

    def test_get_requests_do_not_change_db(self):
        """WEB-AC4/NB2-AC4: жоден GET (сторінки й API) не змінює БД."""
        self.seed()
        before = self.dump()
        for path in ("/", "/notebooks", "/notebooks/nb-a", "/api/state", "/api/notebooks",
                     "/api/notebooks/nb-a", "/api/daemon", "/api/overview", "/api/groups",
                     "/api/groups/nb-a/jobs", "/api/groups/nb-a/sync",
                     "/api/jobs?q=" + quote("Альфа")):
            status, _, _ = self.request(path)
            self.assertEqual(status, 200, path)
        status, _, _ = self.request("/api/sync")
        self.assertEqual(status, 404)  # синк лише через POST
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.adapter.entered, 0)

    def test_notebooks_page_served(self):
        """WEB-R1: сторінка /notebooks віддає HTML, головна має на неї посилання."""
        status, body, headers = self.request("/notebooks")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("Ноутбуки", body.decode("utf-8"))
        _, body, _ = self.request("/")
        self.assertIn('href="/notebooks"', body.decode("utf-8"))

    def test_notebooks_accordion_markup(self):
        """NB2-AC1/NB2-AC5: акордеон із пошуком, фільтром і лінивим завантаженням переказів."""
        status, body, headers = self.request("/notebooks")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        html = body.decode("utf-8")
        for marker in (
            'id="nb-search"',
            'id="nb-filter"',
            'class="nb-head"',
            'aria-expanded="false"',
            'aria-controls="nb-body-',
            'role="region"',
            "/api/notebooks/",
            "Показано",
            "Завантаження переказів",
        ):
            self.assertIn(marker, html)
        # Список більше не рендериться сервером як таблиця з переходами на окрему сторінку.
        self.assertNotIn("<th>Скачано</th>", html)
        self.assertNotIn('<a href="/notebooks/', html)

    def test_notebooks_deep_link_and_history(self):
        """NB2-AC2: /notebooks/<id> відкриває акордеон, URL синхронізовано з історією."""
        status, body, _ = self.request("/notebooks/nb-a")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        for marker in (
            "location.pathname",
            "history.pushState",
            'addEventListener("popstate"',
            "syncFromLocation",
        ):
            self.assertIn(marker, html)

    def test_notebook_detail_payload_for_inline(self):
        """NB2-AC3: /api/notebooks/<id> містить усе для вбудованої таблиці переказів."""
        self.seed()
        data = self.get_json("/api/notebooks/nb-a")
        self.assertEqual(data["notebook"]["title"], "Альфа")
        self.assertTrue(data["items"])
        item = data["items"][0]
        for key in ("id", "title", "created_at", "duration", "state",
                    "job_id", "job_status", "audio_deleted", "audio_exists",
                    "url", "error"):
            self.assertIn(key, item)

    # --- spec 017: групи на головній і синк ноутбука -------------------------
    def add_unassigned(self, slug="solo"):
        with Database(self.cfg.db_path) as db:
            path = self.cfg.inbox / f"{slug}.mp3"
            path.write_bytes(b"audio")
            job = db.add_job(path, slug, title="Самотня задача")
            return job.id

    def save_nb(self):
        with Database(self.cfg.db_path) as db:
            db.save_notebook(self.nb.id, self.nb.title, "docker", None, 0)

    def test_overview_is_light(self):
        """GRP-AC1: /api/overview без важких jobs; /api/state лишається сумісним."""
        data = self.get_json("/api/overview")
        for key in ("counts", "files", "quota", "config", "sync"):
            self.assertIn(key, data)
        self.assertNotIn("jobs", data)
        self.assertIn("jobs", self.get_json("/api/state"))

    def test_groups_counters(self):
        """GRP-AC2: групи ноутбуків, лічильники статусів і «Без ноутбука»."""
        self.seed()
        solo = self.add_unassigned()
        groups = {g["key"]: g for g in self.get_json("/api/groups")["groups"]}
        self.assertEqual(set(groups), {"nb-a", "nb-b", "nb-c", "__none__"})

        a = groups["nb-a"]
        self.assertEqual(a["title"], "Альфа")
        self.assertEqual(a["jobs"], 3)  # a3 — артефакт без задачі, не рахується
        self.assertEqual(
            a["by_status"],
            {"new": 2, "transcribed": 0, "described": 0,
             "rendered": 0, "uploaded": 0, "done": 1},
        )
        self.assertEqual((a["published"], a["errors"], a["ready"]), (1, 1, 0))
        self.assertTrue(a["can_sync"])

        self.assertEqual(groups["nb-b"]["jobs"], 1)
        self.assertEqual(groups["nb-b"]["by_status"]["new"], 1)
        self.assertEqual(groups["nb-c"]["jobs"], 0)
        self.assertFalse(groups["__none__"]["can_sync"])
        self.assertEqual(groups["__none__"]["jobs"], 1)
        self.assertEqual(groups["__none__"]["by_status"]["new"], 1)
        self.assertEqual(solo, self.get_json("/api/groups/__none__/jobs")["jobs"][0]["id"])

    def test_group_jobs_are_scoped(self):
        """GRP-AC3: задачі групи не змішуються; невідомий ноутбук → 404."""
        self.seed()
        self.add_unassigned()
        data = self.get_json("/api/groups/nb-a/jobs")
        self.assertEqual(data["group"]["key"], "nb-a")
        self.assertEqual(len(data["jobs"]), 3)
        self.assertEqual({j["notebook_id"] for j in data["jobs"]}, {"nb-a"})
        self.assertTrue(all(j["notebook_title"] == "Альфа" for j in data["jobs"]))

        none = self.get_json("/api/groups/__none__/jobs")
        self.assertEqual(none["group"]["key"], "__none__")
        self.assertEqual(len(none["jobs"]), 1)
        self.assertIsNone(none["jobs"][0]["notebook_id"])

        status, _, _ = self.request("/api/groups/nope/jobs")
        self.assertEqual(status, 404)

    def test_search_jobs(self):
        """GRP-AC4: пошук по всій черзі за задачею/ноутбуком і фільтр статусу."""
        self.seed()
        solo = self.add_unassigned()

        by_nb = self.get_json("/api/jobs?q=" + quote("Альфа"))
        self.assertEqual(by_nb["total"], 3)
        self.assertTrue(all(j["notebook_title"] == "Альфа" for j in by_nb["jobs"]))

        by_title = self.get_json("/api/jobs?q=" + quote("Самотня"))
        self.assertEqual([j["id"] for j in by_title["jobs"]], [solo])

        new = self.get_json("/api/jobs?q=&status=new")
        self.assertEqual(new["total"], 4)  # a2, a4, b1, solo

        status, _, _ = self.request("/api/jobs?status=secret")
        self.assertEqual(status, 400)

    def test_notebook_sync_downloads_and_renders(self):
        """GRP-AC5: синк ноутбука качає нове аудіо й доводить задачу до rendered."""
        self.save_nb()

        def fake_advance(app, pipe, job):
            return pipe.db.update(job.id, status="rendered")

        with mock.patch.object(WebApp, "_advance_job", fake_advance):
            status, body, _ = self.request(
                f"/api/groups/{self.nb.id}/sync", method="POST", data={}
            )
            self.assertEqual(status, 202, body)
            run = json.loads(body)["run"]
            deadline = time.time() + 10
            while run["status"] == "running" and time.time() < deadline:
                time.sleep(0.05)
                run = self.get_json(f"/api/groups/{self.nb.id}/sync")["run"]

        self.assertEqual(run["status"], "done", run)
        self.assertEqual(run["report"]["downloaded"], 1)
        self.assertEqual(run["total"], 1)
        self.assertEqual(run["processed"], 1)
        self.assertEqual(len(self.adapter.downloads), 1)

        jobs = self.get_json(f"/api/groups/{self.nb.id}/jobs")["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "rendered")
        self.assertEqual(jobs[0]["notebook_title"], self.nb.title)

        lock = FileLock(self.cfg.data / "daemon.lock")
        acquired = False
        for _ in range(100):
            acquired = lock.acquire()
            if acquired:
                break
            time.sleep(0.02)
        self.assertTrue(acquired, "замок мав звільнитися після синку")
        lock.release()

    def test_notebook_sync_busy_lock(self):
        """GRP-AC6: зайнятий замок демона → 409 і жодного завантаження."""
        self.save_nb()
        lock = FileLock(self.cfg.data / "daemon.lock")
        self.assertTrue(lock.acquire())
        try:
            status, body, _ = self.request(
                f"/api/groups/{self.nb.id}/sync", method="POST", data={}
            )
        finally:
            lock.release()
        self.assertEqual(status, 409)
        self.assertIn("демон", json.loads(body)["message"])
        self.assertEqual(self.adapter.downloads, [])

    def test_index_groups_markup(self):
        """GRP-AC7: головна містить пошук, групи, кнопку синку й опитування прогресу."""
        status, body, _ = self.request("/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        for marker in (
            'id="q"', 'id="st"', 'id="jobs"', 'id="counts"',
            'class="grp-head"', 'class="grp-body"', 'data-sync=',
            "/api/overview", "/api/groups/", "syncGroup", "Прогрес",
        ):
            self.assertIn(marker, html)


class FakeYT:
    """Мінімальний фейк YouTube для publish (spec 011)."""

    def __init__(self):
        self.calls = []

    def list(self, **kw):
        self.calls.append(("list", kw))
        return _Req({"items": [{"id": kw["id"], "status": {
            "privacyStatus": "private", "license": "youtube", "embeddable": True,
            "publicStatsViewable": True, "selfDeclaredMadeForKids": False}}]})

    def update(self, **kw):
        self.calls.append(("update", kw))
        return _Req({"id": kw["body"]["id"]})


class _Req:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeService:
    def __init__(self):
        self.vid = FakeYT()

    def videos(self):
        return self.vid


class WebUIPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "youtube:\n  daily_quota: 10000\n  contains_synthetic_media: true\n", encoding="utf-8"
        )
        self.cfg = load_config(root=self.tmp)
        self.cfg.ensure_dirs()
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

    def request(self, path, method="GET", data=None):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            self.base + path, data=body, method=method,
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

    def state(self):
        status, body = self.request("/api/state")
        self.assertEqual(status, 200)
        return json.loads(body)

    def add_job(self, slug, status="new", video_id=None, audio=True):
        with Database(self.cfg.db_path) as db:
            path = self.cfg.inbox / f"{slug}.mp3"
            if audio:
                path.write_bytes(b"audio")
            job = db.add_job(path, slug, title=slug)
            db.update(job.id, status=status, video_id=video_id)
            return job.id

    def test_publish_changes_privacy(self):
        """WEB2-AC1: publish з UI змінює приватність і списує квоту."""
        job_id = self.add_job("ep", status="uploaded", video_id="vid1")
        fake = FakeService()
        with mock.patch("audiotube.youtube.build_service", return_value=fake):
            status, body = self.request(f"/api/jobs/{job_id}/publish?privacy=public", method="POST", data={})
        self.assertEqual(status, 200, body)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIn("youtu.be/vid1", data["message"])
        self.assertIn("update", [c[0] for c in fake.vid.calls])
        self.assertEqual(self.state()["quota"]["used"], 51)

    def test_publish_requires_upload(self):
        """WEB2-AC2: без video_id — 400 і жодного виклику YouTube."""
        job_id = self.add_job("ep")
        fake = FakeService()
        with mock.patch("audiotube.youtube.build_service", return_value=fake):
            status, body = self.request(f"/api/jobs/{job_id}/publish", method="POST", data={})
        self.assertEqual(status, 400)
        self.assertIn("upload", json.loads(body)["message"])
        self.assertEqual(fake.vid.calls, [])

    def test_publish_rejects_bad_privacy(self):
        """WEB2-AC3: невідома приватність — 400, без виклику API."""
        job_id = self.add_job("ep", status="uploaded", video_id="vid1")
        fake = FakeService()
        with mock.patch("audiotube.youtube.build_service", return_value=fake):
            status, body = self.request(f"/api/jobs/{job_id}/publish?privacy=secret", method="POST", data={})
        self.assertEqual(status, 400)
        self.assertIn("приватність", json.loads(body)["message"])
        self.assertEqual(fake.vid.calls, [])

    def test_state_counts(self):
        """WEB2-AC4: counts рахує статуси, опубліковані й готові до публікації."""
        self.add_job("a", status="new")
        self.add_job("b", status="rendered")
        self.add_job("c", status="uploaded", video_id="vid3")
        counts = self.state()["counts"]
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["published"], 1)
        self.assertEqual(counts["ready_to_publish"], 1)
        self.assertEqual(counts["by_status"]["new"], 1)
        self.assertEqual(counts["by_status"]["done"], 0)

    def test_state_files(self):
        """WEB2-AC5: files рахує аудіо в inbox і незареєстровані."""
        self.add_job("registered", status="new")
        (self.cfg.inbox / "extra.mp3").write_bytes(b"audio")
        (self.cfg.inbox / "notes.txt").write_text("x", encoding="utf-8")
        (self.cfg.output / "ep.mp4").write_bytes(b"v")
        (self.cfg.output / "ep.mp4.part").write_bytes(b"v")
        files = self.state()["files"]
        self.assertEqual(files["inbox_audio"], 2)
        self.assertEqual(files["inbox_unregistered"], 1)
        self.assertEqual(files["output_videos"], 1)

    def test_index_has_filters_and_publish(self):
        """WEB2-AC6: головна сторінка містить фільтри й керування приватністю."""
        status, body = self.request("/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        for marker in ('id="q"', 'id="st"', "privacy", "counts"):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
