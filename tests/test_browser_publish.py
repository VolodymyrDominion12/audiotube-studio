"""Spec 016: браузерна публікація — без мережі й без справжнього Chrome.

Драйвер UI підмінюється фейком: перевіряємо оркестрацію, статуси, ідемпотентність,
dry-run/review, розбір id і план заповнення форми.
"""

import contextlib
import io
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from audiotube.browser_publish import (
    AUTH_HINT,
    BrowserAuthError,
    BrowserPublisher,
    ChromeSession,
    PlaywrightStudio,
    PublishOutcome,
    build_plan,
    video_id_from,
)
from audiotube.cli import main
from audiotube.config import load_config
from audiotube.db import Database
from audiotube.metadata import VideoMeta
from audiotube.pipeline import Pipeline

VIDEO_ID = "dQw4w9WgXcQ"


def run_cli(*args) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = main(list(args))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
    return code, out.getvalue()


class FakeDriver:
    """Драйвер-фейк: записує виклики й повертає наперед заданий результат."""

    def __init__(self, video_id: str | None = VIDEO_ID, warnings=None, fail=None):
        self.calls: list[dict] = []
        self.video_id = video_id
        self.warnings = list(warnings or [])
        self.fail = fail
        self.closed = False

    def sign_in(self, *, interactive: bool = True) -> str:
        self.calls.append({"op": "sign_in", "interactive": interactive})
        return "Тестовий канал"

    def publish(self, video, meta, *, thumbnail, captions, review, dry_run):
        self.calls.append({
            "op": "publish", "video": Path(video), "meta": meta,
            "thumbnail": thumbnail, "captions": captions,
            "review": review, "dry_run": dry_run,
        })
        if self.fail:
            raise self.fail
        return PublishOutcome(
            video_id=None if dry_run else self.video_id,
            published=not dry_run,
            thumbnail=bool(thumbnail),
            captions=bool(captions),
            warnings=list(self.warnings),
        )

    def finish(self, video_id, meta, *, thumbnail, captions):
        self.calls.append({
            "op": "finish", "video_id": video_id, "meta": meta,
            "thumbnail": thumbnail, "captions": captions,
        })
        return PublishOutcome(
            video_id=video_id, published=True,
            thumbnail=bool(thumbnail), captions=bool(captions),
            warnings=list(self.warnings),
        )

    def close(self) -> None:
        self.closed = True
        self.calls.append({"op": "close"})


class FakeSession:
    """Мінімум сесії для перевірки гілки «немає входу»."""

    def __init__(self, logged_in: bool = False):
        self.logged_in = logged_in
        self.visited = []

    def goto_studio(self):
        self.visited.append("studio")

    def is_logged_in(self) -> bool:
        return self.logged_in

    def wait_for_login(self, timeout):
        self.visited.append(("wait", timeout))

    def channel_name(self) -> str:
        return "Канал"


class BrowserTestBase(unittest.TestCase):
    def make_root(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "config.yaml").write_text(
            "youtube:\n"
            "  default_privacy: private\n"
            "  upload_captions: true\n"
            "  contains_synthetic_media: true\n"
            "  captions_language: uk\n"
            "  captions_name: Українська\n"
            "  browser:\n"
            "    profile_dir: data/browser/youtube\n"
            "description:\n"
            "  footer: \"FOOTER\"\n"
            "  default_tags: [освіта]\n",
            encoding="utf-8",
        )
        return tmp

    def make_job(
        self,
        root: Path,
        *,
        status: str = "rendered",
        video_id: str | None = None,
        with_video: bool = True,
        privacy: str | None = None,
    ) -> tuple[Pipeline, int]:
        cfg = load_config(root=root)
        cfg.ensure_dirs()
        audio = cfg.inbox / "Тема.mp3"
        audio.write_bytes(b"audio")
        meta = "title: \"Дуже довга назва\"\ndescription: \"Опис відео\"\ntags: [docker]\n"
        if privacy:
            meta += f"privacy: {privacy}\n"
        audio.with_suffix(".yaml").write_text(meta, encoding="utf-8")

        db = Database(cfg.db_path)
        self.addCleanup(db.close)
        job = db.add_job(audio, "tema", title="Тема")
        video = cfg.output / "tema.mp4"
        if with_video:
            video.write_bytes(b"video")
        subtitles = cfg.workspace / "tema" / "subtitles.srt"
        subtitles.parent.mkdir(parents=True, exist_ok=True)
        subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nПривіт\n", encoding="utf-8")
        db.update(
            job.id,
            status=status,
            video=video if with_video else cfg.output / "missing.mp4",
            subtitles=subtitles,
            video_id=video_id,
        )
        return Pipeline(cfg, log=lambda *_: None), job.id


class BrowserPublisherTests(BrowserTestBase):
    def test_done_job_skipped(self):
        """BROW-AC1: задача done не викликає драйвер і не публікується вдруге."""
        root = self.make_root()
        pipe, job_id = self.make_job(root, status="done", video_id="old12345678")
        fake = FakeDriver()
        pub = BrowserPublisher(pipe, driver=fake, log=lambda *_: None)

        result = pub.publish(pipe.db.get(job_id))

        self.assertEqual(result.status, "done")
        self.assertEqual(fake.calls, [])

    def test_upload_sets_video_id_and_status(self):
        """BROW-AC2: браузерна публікація зберігає video_id, статус done і запис uploads."""
        root = self.make_root()
        pipe, job_id = self.make_job(root)
        fake = FakeDriver()
        logs = []
        pub = BrowserPublisher(pipe, driver=fake, log=logs.append)

        result = pub.publish(pipe.db.get(job_id))

        self.assertEqual(result.video_id, VIDEO_ID)
        self.assertEqual(result.status, "done")
        self.assertIsNone(result.error)
        self.assertEqual(pipe.db.uploads_count(), 1)
        self.assertEqual(pipe.quota.used(), 0, "браузерний режим не списує API-квоту")
        call = [c for c in fake.calls if c["op"] == "publish"][0]
        self.assertEqual(call["video"], pipe.cfg.output / "tema.mp4")
        self.assertFalse(call["review"])
        self.assertFalse(call["dry_run"])
        self.assertEqual(call["captions"].name, "subtitles.srt")
        self.assertIn("канал: Тестовий канал", "\n".join(logs))

    def test_resume_uploaded_does_not_reupload(self):
        """BROW-AC3: задача uploaded догружає лише обкладинку/субтитри, без повторного відео."""
        root = self.make_root()
        pipe, job_id = self.make_job(root, status="uploaded", video_id="old12345678")
        fake = FakeDriver(video_id="old12345678")
        pub = BrowserPublisher(pipe, driver=fake, log=lambda *_: None)

        result = pub.publish(pipe.db.get(job_id))

        self.assertEqual(result.status, "done")
        self.assertNotIn("publish", [c["op"] for c in fake.calls])
        finish = [c for c in fake.calls if c["op"] == "finish"][0]
        self.assertEqual(finish["video_id"], "old12345678")
        self.assertEqual(pipe.db.uploads_count(), 0)

    def test_dry_run_keeps_db_untouched(self):
        """BROW-AC4: dry-run заповнює форму, але не публікує й не змінює БД."""
        root = self.make_root()
        pipe, job_id = self.make_job(root)
        fake = FakeDriver()
        pub = BrowserPublisher(pipe, driver=fake, log=lambda *_: None)

        result = pub.publish(pipe.db.get(job_id), dry_run=True)

        self.assertEqual(result.status, "rendered")
        self.assertIsNone(result.video_id)
        self.assertEqual(pipe.db.uploads_count(), 0)
        call = [c for c in fake.calls if c["op"] == "publish"][0]
        self.assertTrue(call["dry_run"])

    def test_dry_run_uploaded_job_keeps_status(self):
        """BROW-AC4: dry-run для вже завантаженого відео не чіпає ні YouTube, ні БД."""
        root = self.make_root()
        pipe, job_id = self.make_job(root, status="uploaded", video_id="old12345678")
        fake = FakeDriver()
        pub = BrowserPublisher(pipe, driver=fake, log=lambda *_: None)

        result = pub.publish(pipe.db.get(job_id), dry_run=True)

        self.assertEqual(result.status, "uploaded")
        self.assertEqual(fake.calls, [])

    def test_review_flag_passed(self):
        """BROW-AC5: --review доходить до драйвера як review=True."""
        root = self.make_root()
        pipe, job_id = self.make_job(root)
        fake = FakeDriver()
        pub = BrowserPublisher(pipe, driver=fake, log=lambda *_: None)

        result = pub.publish(pipe.db.get(job_id), review=True)

        self.assertEqual(result.status, "done")
        call = [c for c in fake.calls if c["op"] == "publish"][0]
        self.assertTrue(call["review"])

    def test_warnings_do_not_fail_publish(self):
        """BROW-AC8: попередження про субтитри не роблять публікацію невдалою."""
        root = self.make_root()
        pipe, job_id = self.make_job(root)
        fake = FakeDriver(warnings=["субтитри не додано: UI змінився"])
        logs = []
        pub = BrowserPublisher(pipe, driver=fake, log=logs.append)

        result = pub.publish(pipe.db.get(job_id))

        self.assertEqual(result.status, "done")
        self.assertIsNone(result.error)
        self.assertIn("субтитри не додано", "\n".join(logs))

    def test_missing_video_fails_without_browser(self):
        """BROW-AC9: без файлу відео драйвер не викликається."""
        root = self.make_root()
        pipe, job_id = self.make_job(root, with_video=False)
        fake = FakeDriver()
        pub = BrowserPublisher(pipe, driver=fake, log=lambda *_: None)

        result = pub.publish(pipe.db.get(job_id))

        self.assertIn("відео", (result.error or "").lower())
        self.assertEqual(fake.calls, [])

    def test_cli_browser_upload(self):
        """BROW-AC13: CLI browser-upload публікує через BrowserPublisher і закриває браузер."""
        root = self.make_root()
        _, job_id = self.make_job(root)
        created = []

        class FakePublisher:
            def __init__(self, pipe, log=None, driver=None):
                self.pipe = pipe
                self.closed = False
                created.append(self)

            def publish(self, job, review=False, dry_run=False):
                self.called = (job.id, review, dry_run)
                return job

            def close(self, keep_open=False):
                self.closed = True
                self.keep_open = keep_open

        with mock.patch("audiotube.browser_publish.BrowserPublisher", FakePublisher):
            code, out = run_cli("--root", str(root), "browser-upload", str(job_id), "--review")

        self.assertEqual(code, 0, out)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].called, (job_id, True, False))
        self.assertTrue(created[0].closed)

    def test_unknown_id_fails(self):
        """BROW-AC13: невідомий id — SystemExit з повідомленням, без браузера."""
        root = self.make_root()
        load_config(root=root).ensure_dirs()
        code, _out = run_cli("--root", str(root), "browser-upload", "999")
        self.assertNotEqual(code, 0)


class BrowserPlanTests(BrowserTestBase):
    def test_video_id_parsing(self):
        """BROW-AC6: id читається з URL Studio, watch і youtu.be, сміття — None."""
        self.assertEqual(video_id_from(f"https://studio.youtube.com/video/{VIDEO_ID}/edit"), VIDEO_ID)
        self.assertEqual(video_id_from(f"https://youtu.be/{VIDEO_ID}?si=abc"), VIDEO_ID)
        self.assertEqual(video_id_from(f"https://www.youtube.com/watch?v={VIDEO_ID}&t=1"), VIDEO_ID)
        self.assertEqual(video_id_from("https://studio.youtube.com/"), None)
        self.assertEqual(video_id_from(""), None)
        self.assertEqual(video_id_from("https://youtu.be/short"), None)

    def test_plan_uses_metadata(self):
        """BROW-AC10: назва ≤100, footer у описі, приватність з meta."""
        root = self.make_root()
        cfg = load_config(root=root)
        meta = VideoMeta(
            title="Я" * 150,
            description="Опис",
            tags=["docker", "devops"],
            privacy="unlisted",
        )

        plan = build_plan(meta, cfg)

        self.assertLessEqual(len(plan.title), 100)
        self.assertIn("FOOTER", plan.description)
        self.assertIn("Опис", plan.description)
        self.assertEqual(plan.privacy, "unlisted")
        self.assertIn("освіта", plan.tags)
        self.assertTrue(plan.synthetic)
        self.assertFalse(plan.made_for_kids)

    def test_publish_at_downgraded_to_private(self):
        """BROW-AC11: publish_at у браузерному режимі ігнорується з попередженням."""
        root = self.make_root()
        cfg = load_config(root=root)
        meta = VideoMeta(
            title="Тема",
            publish_at=datetime(2026, 12, 1, 18, 0, tzinfo=timezone.utc),
            privacy="public",
        )

        plan = build_plan(meta, cfg)

        self.assertEqual(plan.privacy, "private")
        self.assertTrue(any("publish_at" in w for w in plan.warnings))

    def test_profile_dir_created_private(self):
        """BROW-AC14: профіль Chrome створюється під data/ з правами 0700."""
        root = self.make_root()
        cfg = load_config(root=root)
        cfg.ensure_dirs()
        session = ChromeSession(cfg, log=lambda *_: None)

        path = session.profile_path()

        self.assertTrue(path.is_dir())
        self.assertTrue(str(path).endswith("data/browser/youtube"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_noninteractive_login_fails_with_hint(self):
        """BROW-AC7: без входу й без інтерактиву — BrowserAuthError з підказкою."""
        root = self.make_root()
        cfg = load_config(root=root)
        studio = PlaywrightStudio(FakeSession(logged_in=False), cfg, log=lambda *_: None)

        with self.assertRaises(BrowserAuthError) as ctx:
            studio.sign_in(interactive=False)

        self.assertIn(AUTH_HINT, str(ctx.exception))
        self.assertIn("browser-auth", str(ctx.exception))

    def test_logged_in_skips_wait(self):
        """BROW-AC3: якщо сесія вже є, чекати логін не треба."""
        root = self.make_root()
        cfg = load_config(root=root)
        session = FakeSession(logged_in=True)
        studio = PlaywrightStudio(session, cfg, log=lambda *_: None)

        name = studio.sign_in(interactive=True)

        self.assertEqual(name, "Канал")
        self.assertNotIn(("wait", cfg.youtube.browser.login_timeout), session.visited)


if __name__ == "__main__":
    unittest.main()
