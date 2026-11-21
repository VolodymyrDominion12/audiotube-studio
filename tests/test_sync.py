"""Синхронізація NotebookLM -> inbox/<ноутбук>/ (без мережі й без FFmpeg)."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import yaml

from audiotube.cli import main
from audiotube.config import load_config
from audiotube.db import Database
from audiotube.notebooklm import NotebookLMError, RemoteArtifact, RemoteNotebook
from audiotube.pipeline import Pipeline
from audiotube.sync import SyncService, import_audio


class FakeAdapter:
    """Підміняє notebooklm-py: віддає задані ноутбуки й пише фейкові байти."""

    def __init__(self, notebooks, artifacts, fail=(), fail_audio=(), content=None):
        self._notebooks = list(notebooks)
        self._artifacts = artifacts
        self.fail = set(fail)
        self.fail_audio = set(fail_audio)  # ноутбуки, для яких audio() падає
        self.content = dict(content or {})  # artifact_id -> байти (інакше — типові)
        self.downloads = []
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def notebooks(self):
        return list(self._notebooks)

    async def audio(self, notebook_id):
        if notebook_id in self.fail_audio:
            raise NotebookLMError("тимчасова помилка API")
        return list(self._artifacts.get(notebook_id, []))

    async def download(self, notebook_id, artifact_id, dest):
        if artifact_id in self.fail:
            raise RuntimeError("мережа обірвалася")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.content.get(artifact_id, b"fake-audio-" + artifact_id.encode()))
        self.downloads.append((notebook_id, artifact_id, dest))


def artifact(aid="a1", title="Вступ", day=2):
    return RemoteArtifact(
        id=aid,
        title=title,
        created_at=datetime(2026, 10, day, tzinfo=timezone.utc),
        duration=1200.0,
        ready=True,
        status="completed",
    )


class SyncTestCase(unittest.TestCase):
    """Спільне оточення: тимчасовий корінь, БД і FakeAdapter з одним ноутбуком."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "notebooklm:\n  delay_seconds: 0\n  max_per_run: 20\n", encoding="utf-8"
        )
        self.cfg = load_config(root=self.tmp)
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.nb = RemoteNotebook(id="nb1abc123456", title="Docker за 20 хвилин")
        self.adapter = FakeAdapter([self.nb], {self.nb.id: [artifact()]})

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def service(self):
        return SyncService(self.cfg, db=self.db, adapter=self.adapter, log=lambda _m: None)


class SyncTests(SyncTestCase):
    def test_downloads_and_registers(self):
        report = self.service().run()
        self.assertEqual(report.downloaded, 1)
        self.assertEqual(report.errors, [])

        row = self.db.notebook(self.nb.id)
        self.assertIsNotNone(row)
        folder = self.cfg.inbox / row["slug"]
        audio = folder / "2026-10-02_vstup-a1.m4a"
        self.assertTrue(audio.exists(), list(folder.iterdir()))
        self.assertFalse(list(folder.glob("*.part")))

        meta = json.loads((folder / "notebook.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["id"], self.nb.id)
        self.assertEqual(meta["title"], "Docker за 20 хвилин")

        side = yaml.safe_load(audio.with_suffix(".yaml").read_text(encoding="utf-8"))
        self.assertEqual(side["title"], "Вступ")
        self.assertEqual(side["notebook"], "Docker за 20 хвилин")
        self.assertEqual(side["artifact_id"], "a1")
        self.assertEqual(side["privacy"], "private")
        self.assertNotIn("playlist", side)  # розкладання по плейлистах відкладено

        job = self.db.get(self.db.job_for_audio(audio))
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "new")
        self.assertEqual(self.db.artifact("a1")["job_id"], job.id)

    def test_second_run_is_noop(self):
        self.service().run()
        report = self.service().run()
        self.assertEqual(report.downloaded, 0)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(len(self.adapter.downloads), 1)

    def test_dry_run_writes_nothing(self):
        report = self.service().run(dry_run=True)
        self.assertEqual(report.planned, 1)
        self.assertEqual(report.downloaded, 0)
        self.assertIsNone(self.db.artifact("a1"))
        self.assertEqual(list(self.cfg.inbox.rglob("*.m4a")), [])

    def test_dry_run_ignores_max_per_run(self):
        self.cfg.notebooklm.max_per_run = 1
        self.adapter = FakeAdapter(
            [self.nb], {self.nb.id: [artifact("a1", "Перший", 1), artifact("a2", "Другий", 2)]}
        )
        report = self.service().run(dry_run=True)
        self.assertEqual(report.planned, 2)

    def test_limit_stops_after_n(self):
        self.adapter = FakeAdapter(
            [self.nb],
            {self.nb.id: [artifact("a1", "Перший", 1), artifact("a2", "Другий", 2),
                          artifact("a3", "Третій", 3)]},
        )
        report = self.service().run(limit=1)
        self.assertEqual(report.downloaded, 1)
        self.assertEqual(len(self.adapter.downloads), 1)

    def test_one_failure_does_not_stop_others(self):
        self.adapter = FakeAdapter(
            [self.nb],
            {self.nb.id: [artifact("bad", "Зламаний", 1), artifact("ok", "Робочий", 2)]},
            fail={"bad"},
        )
        report = self.service().run()
        self.assertEqual(report.downloaded, 1)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(len(list(self.cfg.inbox.rglob("*.part"))), 0)

    def test_missing_file_is_repaired(self):
        self.service().run()
        row = self.db.artifact("a1")
        Path(row["path"]).unlink()
        report = self.service().run()
        self.assertEqual(report.downloaded, 1)
        actions = [i.action for i in report.items]
        self.assertIn("repair", actions)
        self.assertTrue(Path(row["path"]).exists())

    def test_not_ready_artifact_is_skipped(self):
        self.adapter = FakeAdapter(
            [self.nb],
            {self.nb.id: [RemoteArtifact(id="a1", title="В процесі", ready=False, status="processing")]},
        )
        report = self.service().run()
        self.assertEqual(report.downloaded, 0)
        self.assertEqual(report.skipped, 1)

    def test_slug_collision_gets_id_suffix(self):
        second = RemoteNotebook(id="nb2def456", title="Docker за 20 хвилин")
        self.adapter = FakeAdapter(
            [self.nb, second],
            {self.nb.id: [artifact("a1")], second.id: []},
        )
        self.service().run()
        first = self.db.notebook(self.nb.id)["slug"]
        other = self.db.notebook(second.id)["slug"]
        self.assertNotEqual(first, other)

    def test_generic_title_is_replaced(self):
        self.adapter = FakeAdapter(
            [self.nb], {self.nb.id: [artifact("a1", "Audio Overview")]}
        )
        self.service().run()
        row = self.db.artifact("a1")
        self.assertIn("Docker за 20 хвилин", row["title"])

    def test_shared_notebooks_ignored_by_default(self):
        shared = RemoteNotebook(id="nbX", title="Чужий", shared=True)
        self.adapter = FakeAdapter([self.nb, shared], {self.nb.id: [], shared.id: [artifact("s1")]})
        report = self.service().run()
        self.assertEqual(report.notebooks, 1)
        self.assertIsNone(self.db.artifact("s1"))


class ArtifactLifecycleTests(SyncTestCase):
    """Спека 005: зниклі артефакти, безпечний --force, перейменування ноутбука."""

    def test_removed_artifact_marked_missing(self):
        """LIFE-AC1: артефакт зник із NotebookLM -> state=missing, файл і задача на місці."""
        self.service().run()
        path = Path(self.db.artifact("a1")["path"])
        job_id = self.db.job_for_audio(path)
        self.adapter._artifacts[self.nb.id] = []

        report = self.service().run()

        self.assertEqual(report.missing, 1)
        self.assertEqual(self.db.artifact("a1")["state"], "missing")
        self.assertTrue(path.exists())
        self.assertIsNotNone(self.db.get(job_id))

    def test_removed_notebook_marks_its_artifacts_missing(self):
        """LIFE-R3: ноутбук зник цілком -> його артефакти missing."""
        self.service().run()
        self.adapter._notebooks = []
        report = self.service().run()
        self.assertEqual(report.missing, 1)
        self.assertEqual(self.db.artifact("a1")["state"], "missing")

    def test_notebook_filter_does_not_mark_others_missing(self):
        """LIFE-R3: --notebook не робить відфільтровані ноутбуки «зниклими»."""
        self.service().run()
        self.service().run(notebook="інший")
        self.assertEqual(self.db.artifact("a1")["state"], "downloaded")

    def test_missing_artifact_restored(self):
        """LIFE-AC2: артефакт повернувся -> state=downloaded без повторного завантаження."""
        self.service().run()
        arts = self.adapter._artifacts[self.nb.id]
        self.adapter._artifacts[self.nb.id] = []
        self.service().run()
        self.adapter._artifacts[self.nb.id] = arts

        report = self.service().run()

        self.assertEqual(report.restored, 1)
        self.assertEqual(self.db.artifact("a1")["state"], "downloaded")
        self.assertEqual(len(self.adapter.downloads), 1)

    def test_api_error_keeps_states(self):
        """LIFE-AC3: помилка audio() — це не «зник», стани не змінюються."""
        self.service().run()
        self.adapter.fail_audio = {self.nb.id}
        report = self.service().run()
        self.assertEqual(report.missing, 0)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(self.db.artifact("a1")["state"], "downloaded")

    def test_dry_run_reports_missing_without_writing(self):
        """LIFE-R1 + SYNC-R12: dry-run показує зниклі, але не пише в БД."""
        self.service().run()
        self.adapter._artifacts[self.nb.id] = []
        report = self.service().run(dry_run=True)
        self.assertEqual(report.missing, 1)
        self.assertEqual(self.db.artifact("a1")["state"], "downloaded")

    def test_force_same_content_keeps_file(self):
        """LIFE-AC4: --force з тим самим вмістом не перезаписує файл, .part прибрано."""
        self.service().run()
        path = Path(self.db.artifact("a1")["path"])
        before = path.stat().st_mtime_ns

        report = self.service().run(force=True)

        self.assertEqual(report.downloaded, 0)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(list(self.cfg.inbox.rglob("*.part")), [])

    def test_force_changed_content_never_overwrites(self):
        """LIFE-R4: інший вміст під тим самим id не перезаписує файл і не створює задачу."""
        self.service().run()
        path = Path(self.db.artifact("a1")["path"])
        original = path.read_bytes()
        jobs_before = len(self.db.list())
        self.adapter.content["a1"] = b"regenerated-audio"

        report = self.service().run(force=True)

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(report.changed, 1)
        self.assertEqual(len(self.db.list()), jobs_before)
        self.assertEqual(list(self.cfg.inbox.rglob("*.part")), [])
        self.assertEqual(sorted(p.name for p in path.parent.glob("*.m4a")), [path.name])

    def test_notebook_rename_updates_title(self):
        """LIFE-AC7: перейменування ноутбука оновлює title, slug і папка ті самі."""
        self.service().run()
        slug = self.db.notebook(self.nb.id)["slug"]
        self.adapter._notebooks = [RemoteNotebook(id=self.nb.id, title="Docker: повний курс")]

        self.service().run()

        row = self.db.notebook(self.nb.id)
        self.assertEqual(row["title"], "Docker: повний курс")
        self.assertEqual(row["slug"], slug)
        self.assertEqual(self.db.artifact("a1")["state"], "downloaded")


class SyncCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "notebooklm:\n  delay_seconds: 0\n", encoding="utf-8"
        )
        self.nb = RemoteNotebook(id="nb1abc123456", title="Docker за 20 хвилин")
        self.adapter = FakeAdapter([self.nb], {self.nb.id: [artifact("a1")]})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sync_command(self):
        with mock.patch("audiotube.cli.build_adapter", return_value=self.adapter):
            code = main(["--root", str(self.tmp), "sync", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.adapter.downloads, [])

        with mock.patch("audiotube.cli.build_adapter", return_value=self.adapter):
            code = main(["--root", str(self.tmp), "sync"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.adapter.downloads), 1)

    def test_missing_is_reported_by_sync_and_status(self):
        """LIFE-R8: sync і status --missing показують зниклі перекази."""
        with mock.patch("audiotube.cli.build_adapter", return_value=self.adapter):
            main(["--root", str(self.tmp), "sync"])
        self.adapter._artifacts[self.nb.id] = []

        out = io.StringIO()
        with mock.patch("audiotube.cli.build_adapter", return_value=self.adapter), \
                contextlib.redirect_stdout(out):
            code = main(["--root", str(self.tmp), "sync"])
        self.assertEqual(code, 0)
        self.assertIn("зникло з NotebookLM: 1", out.getvalue())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--root", str(self.tmp), "status", "--missing"])
        self.assertEqual(code, 0)
        self.assertIn("Docker за 20 хвилин", out.getvalue())
        self.assertIn("Вступ", out.getvalue())

    def test_auth_error_exits_2(self):
        class Broken(FakeAdapter):
            async def notebooks(self):
                raise NotebookLMError("сесія прострочена")

        with mock.patch("audiotube.cli.build_adapter", return_value=Broken([], {})):
            code = main(["--root", str(self.tmp), "sync"])
        self.assertEqual(code, 2)


class ScanRecursiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load_config(root=self.tmp)
        self.cfg.ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_sees_notebook_subfolders(self):
        (self.cfg.inbox / "docker").mkdir()
        (self.cfg.inbox / "top.mp3").write_bytes(b"a")
        (self.cfg.inbox / "docker" / "one.m4a").write_bytes(b"b")
        (self.cfg.inbox / "docker" / "two.m4a.part").write_bytes(b"c")
        pipe = Pipeline(self.cfg)
        try:
            jobs = pipe.scan()
        finally:
            pipe.db.close()
        names = sorted(j.audio.name for j in jobs)
        self.assertEqual(names, ["one.m4a", "top.mp3"])


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load_config(root=self.tmp)
        self.cfg.ensure_dirs()
        self.src = self.tmp / "downloads" / "Docker NB"
        (self.src / "nested").mkdir(parents=True)
        (self.src / "one.m4a").write_bytes(b"1")
        (self.src / "nested" / "two.mp3").write_bytes(b"2")
        (self.src / "readme.txt").write_text("не аудіо", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_import_copies_audio_and_skips_duplicates(self):
        copied, skipped = import_audio(self.cfg, [str(self.src)])
        self.assertEqual(len(copied), 2)
        self.assertEqual(skipped, [])
        self.assertTrue((self.cfg.inbox / "docker-nb" / "one.m4a").exists())
        self.assertTrue((self.cfg.inbox / "docker-nb" / "nested" / "two.mp3").exists())

        copied2, skipped2 = import_audio(self.cfg, [str(self.src)])
        self.assertEqual(copied2, [])
        self.assertEqual(len(skipped2), 2)


if __name__ == "__main__":
    unittest.main()
