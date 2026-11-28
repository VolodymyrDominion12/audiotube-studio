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
    BrowserError,
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
        """BROW-AC4: dry-run не публікує, не змінює БД і не показує стару помилку."""
        root = self.make_root()
        pipe, job_id = self.make_job(root)
        pipe.db.update(job_id, error="стара помилка")
        fake = FakeDriver()
        pub = BrowserPublisher(pipe, driver=fake, log=lambda *_: None)

        result = pub.publish(pipe.db.get(job_id), dry_run=True)

        self.assertEqual(result.status, "rendered")
        self.assertIsNone(result.video_id)
        self.assertIsNone(result.error, "успішний dry-run не має повертати стару помилку")
        self.assertEqual(pipe.db.get(job_id).error, "стара помилка", "dry-run не пише в БД")
        self.assertEqual(pipe.db.uploads_count(), 0)
        call = [c for c in fake.calls if c["op"] == "publish"][0]
        self.assertTrue(call["dry_run"])

    def test_dry_run_failure_does_not_write_db(self):
        """BROW-AC4: невдача dry-run не змінює БД, лише повертає помилку."""
        root = self.make_root()
        pipe, job_id = self.make_job(root)
        fake = FakeDriver(fail=BrowserError("немає доступу до Studio"))
        pub = BrowserPublisher(pipe, driver=fake, log=lambda *_: None)

        result = pub.publish(pipe.db.get(job_id), dry_run=True)

        self.assertIn("немає доступу", result.error or "")
        self.assertEqual(pipe.db.get(job_id).status, "rendered")
        self.assertIsNone(pipe.db.get(job_id).error)

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

    def test_thumbnail_uploaded_during_video_creation(self):
        """BROW-AC15: обкладинка завантажується на кроці деталей під час створення відео."""
        root = self.make_root()
        cfg = load_config(root=root)
        cfg.ensure_dirs()
        video = cfg.output / "tema.mp4"
        video.write_bytes(b"video")
        thumb = cfg.output / "tema.jpg"
        thumb.write_bytes(b"jpg")
        meta = VideoMeta(title="Назва", description="Опис")

        events: list[str] = []

        class RecordingStudio(PlaywrightStudio):
            def _open_upload_dialog(self):
                events.append("open")

            def _select_file(self, v):
                events.append("select_file")

            def _wait_details(self):
                events.append("wait_details")

            def _fill_details(self, plan):
                events.append("fill_details")
                return super()._fill_details(plan)

            def _fill_editable(self, locator, text):
                pass

            def _set_made_for_kids(self, made_for_kids):
                pass

            def _show_more(self):
                pass

            def _fill_tags(self, tags):
                pass

            def _set_synthetic_disclosure(self):
                pass

            def _upload_thumbnail_in_scope(self, path, *, scope):
                events.append(f"dialog_thumb:{path.name}")
                return True

            def _advance_to_visibility(self, privacy):
                events.append("advance")

            def _select_visibility(self, plan):
                events.append("visibility")

            def _wait_upload_complete(self, timeout=None):
                events.append("wait_upload")
                return True

            def _click_done(self):
                events.append("done")

            def _wait_video_id(self, plan, timeout, *, require_close=False):
                events.append("wait_id")
                return VIDEO_ID

            def _set_thumbnail(self, video_id, path, warnings):
                events.append("edit_thumb")
                return True

        class DummyLocator:
            def __init__(self):
                self.first = self

            def count(self):
                return 1

            def wait_for(self, **kwargs):
                pass

        class DummyPage:
            def locator(self, _sel):
                return DummyLocator()

            def is_closed(self):
                return False

        session = FakeSession(logged_in=True)
        session.page = DummyPage()
        studio = RecordingStudio(session, cfg, log=lambda *_: None)

        out = studio.publish(
            video, meta, thumbnail=thumb, captions=None, review=False, dry_run=False
        )

        self.assertTrue(out.published)
        self.assertTrue(out.thumbnail)
        self.assertIn("dialog_thumb:tema.jpg", events)
        self.assertLess(events.index("dialog_thumb:tema.jpg"), events.index("advance"))
        self.assertNotIn("edit_thumb", events, "не дублювати завантаження обкладинки на /edit")

    def test_thumbnail_falls_back_to_edit_if_dialog_upload_fails(self):
        """BROW-AC15: якщо в діалозі обкладинку не вдалося поставити, _finish_assets ставить її на /edit."""
        root = self.make_root()
        cfg = load_config(root=root)
        cfg.ensure_dirs()
        video = cfg.output / "tema.mp4"
        video.write_bytes(b"video")
        thumb = cfg.output / "tema.jpg"
        thumb.write_bytes(b"jpg")
        meta = VideoMeta(title="Назва")

        events: list[str] = []

        class FallbackStudio(PlaywrightStudio):
            def _open_upload_dialog(self): pass
            def _select_file(self, v): pass
            def _wait_details(self): pass
            def _fill_editable(self, locator, text): pass
            def _set_made_for_kids(self, made_for_kids): pass
            def _show_more(self): pass
            def _fill_tags(self, tags): pass
            def _set_synthetic_disclosure(self): pass
            def _advance_to_visibility(self, privacy): pass
            def _select_visibility(self, plan): pass
            def _wait_upload_complete(self, timeout=None): return True
            def _click_done(self): pass
            def _wait_video_id(self, plan, timeout, *, require_close=False): return VIDEO_ID

            def _upload_thumbnail_in_scope(self, path, *, scope):
                events.append("dialog_thumb_fail")
                return False

            def _set_thumbnail(self, video_id, path, warnings):
                events.append("edit_thumb")
                return True

        class DummyLocator:
            def __init__(self):
                self.first = self

            def count(self):
                return 1

            def wait_for(self, **kwargs):
                pass

        class DummyPage:
            def locator(self, _sel):
                return DummyLocator()

            def is_closed(self):
                return False

        session = FakeSession(logged_in=True)
        session.page = DummyPage()
        studio = FallbackStudio(session, cfg, log=lambda *_: None)

        out = studio.publish(
            video, meta, thumbnail=thumb, captions=None, review=False, dry_run=False
        )

        self.assertTrue(out.thumbnail)
        self.assertEqual(events, ["dialog_thumb_fail", "edit_thumb"])

    def test_thumbnail_upload_waits_for_attached_not_visible(self):
        """BROW-AC15: прихований input[type=file] очікується як attached, а не visible."""
        root = self.make_root()
        cfg = load_config(root=root)
        cfg.ensure_dirs()
        thumb = cfg.output / "tema.jpg"
        thumb.write_bytes(b"jpg")

        uploaded_files: list[str] = []

        class UploaderBox:
            def __init__(self):
                self.first = self

            def wait_for(self, *, state="visible", timeout=0):
                if state != "visible":
                    raise AssertionError(f"unexpected uploader state={state}")

        class HiddenFileInput:
            def __init__(self):
                self.first = self

            def wait_for(self, *, state="visible", timeout=0):
                if state == "visible":
                    raise TimeoutError("input[type=file] is hidden")

            def set_input_files(self, path):
                uploaded_files.append(path)

        class FakePreview:
            def __init__(self):
                self.first = self

            def count(self):
                return 1

            def is_visible(self):
                return True

        class FakeScope:
            def locator(self, selector):
                if selector == "ytcp-thumbnail-uploader":
                    return UploaderBox()
                if "input" in selector:
                    return HiddenFileInput()
                return FakePreview()

        session = FakeSession(logged_in=True)
        session.page = FakeScope()
        studio = PlaywrightStudio(session, cfg, log=lambda *_: None)

        ok = studio._upload_thumbnail_in_scope(thumb, scope=FakeScope())

        self.assertTrue(ok)
        self.assertEqual(uploaded_files, [str(thumb)])

    def test_browser_publisher_finds_output_thumbnail_fallback(self):
        """BROW-AC15: якщо job.thumbnail не записано в БД, береться output/<slug>.jpg."""
        root = self.make_root()
        pipe, job_id = self.make_job(root)
        pipe.db.update(job_id, thumbnail=None)
        fallback = pipe.cfg.output / "tema.jpg"
        fallback.write_bytes(b"jpg")
        fake = FakeDriver()
        pub = BrowserPublisher(pipe, driver=fake, log=lambda *_: None)

        pub.publish(pipe.db.get(job_id))

        call = [c for c in fake.calls if c["op"] == "publish"][0]
        self.assertEqual(call["thumbnail"], fallback)

    def test_closed_page_aborts_wait_loops(self):
        """BROW-AC16: закрите вікно негайно перериває очікування без зависання."""
        root = self.make_root()
        cfg = load_config(root=root)

        class ClosedLocator:
            def __init__(self):
                self.first = self

            def wait_for(self, **kwargs):
                pass

            def is_disabled(self):
                return True

            def count(self):
                return 0

        class ClosedPage:
            def is_closed(self):
                return True

            def locator(self, _sel):
                return ClosedLocator()

        session = FakeSession(logged_in=True)
        session.page = ClosedPage()
        studio = PlaywrightStudio(session, cfg, log=lambda *_: None)

        with self.assertRaises(BrowserError):
            studio._wait_upload_complete(timeout=60)
        with self.assertRaises(BrowserError):
            studio._next()
        with self.assertRaises(BrowserError):
            studio._click_done()

    def test_wait_upload_complete_ignores_background_table_progress(self):
        """BROW-AC16: прогрес береться лише з діалогу завантаження, не зі старої чернетки в таблиці."""
        root = self.make_root()
        cfg = load_config(root=root)

        class ProgressLocator:
            def __init__(self, text):
                self._text = text
                self.first = self

            def count(self):
                return 1 if self._text is not None else 0

            def inner_text(self, timeout=None):
                return self._text or ""

        class StudioPageWithStaleDraft:
            def is_closed(self):
                return False

            def locator(self, selector):
                if "ytcp-uploads-dialog" in selector:
                    return ProgressLocator("Перевірки виконано. Проблем не виявлено.")
                return ProgressLocator("Завантаження: 44%")

        session = FakeSession(logged_in=True)
        session.page = StudioPageWithStaleDraft()
        studio = PlaywrightStudio(session, cfg, log=lambda *_: None)

        with mock.patch("audiotube.browser_publish.time.sleep"):
            done = studio._wait_upload_complete(timeout=0.05)

        self.assertTrue(done)


if __name__ == "__main__":
    unittest.main()
