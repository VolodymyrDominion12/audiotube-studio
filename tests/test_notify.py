"""Сповіщення (spec 007): Telegram-транспорт, події демона, CLI. HTTP завжди замоканий."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from audiotube.config import load_config
from audiotube.daemon import Daemon
from audiotube.db import Database
from audiotube.notebooklm import NotebookLMError
from audiotube.notify import Notifier, build_notifier, telegram_status, telegram_transport
from audiotube.youtube import AuthRequired
from test_daemon import FakePipe

TOKEN = "123456:SECRET-token"
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}


class FakeSend:
    """Фейковий транспорт: запам'ятовує тексти; може «падати» або повертати False."""

    def __init__(self, ok=True, raises=None):
        self.ok = ok
        self.raises = raises
        self.messages = []

    def __call__(self, text):
        if self.raises:
            raise self.raises
        self.messages.append(text)
        return self.ok

    def having(self, needle):
        return [m for m in self.messages if needle in m]


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


def tg_ok(*_args, **_kwargs):
    return FakeResponse({"ok": True, "result": {}})


def write_config(root, extra=""):
    (root / "config.yaml").write_text(
        "daemon:\n"
        "  videos_per_day: 2\n"
        "  min_hours_between: 0\n"
        "  pause_seconds: 0\n"
        "  cleanup_video_after_upload: false\n"
        "  summary_hour: null\n"
        + extra
        + "youtube:\n"
        "  upload_captions: false\n"
        "  manage_playlists: false\n",
        encoding="utf-8",
    )


class TelegramTransportTests(unittest.TestCase):
    def test_request_shape_and_token_redacted(self):
        """NTF-AC8: POST на sendMessage з chat_id/text, таймаут ≤ 10 с; токен не потрапляє в журнал."""
        calls = []

        def opener(req, timeout):
            calls.append((req, timeout))
            return tg_ok()

        logs = []
        send = telegram_transport(TOKEN, "42", log=logs.append, opener=opener)
        self.assertTrue(send("привіт " + "я" * 5000))
        req, timeout = calls[0]
        self.assertEqual(req.full_url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(req.get_method(), "POST")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["chat_id"], "42")
        self.assertTrue(body["text"].startswith("привіт"))
        self.assertLessEqual(len(body["text"]), 4096)
        self.assertLessEqual(timeout, 10)

        def broken(req, timeout):
            raise urllib.error.URLError(f"no route to api.telegram.org/bot{TOKEN}")

        send = telegram_transport(TOKEN, "42", log=logs.append, opener=broken)
        self.assertFalse(send("x"))
        self.assertTrue(logs)
        self.assertFalse([line for line in logs if TOKEN in line], logs)

        refused = telegram_transport(
            TOKEN, "42", log=logs.append,
            opener=lambda req, timeout: FakeResponse({"ok": False, "description": "chat not found"}),
        )
        self.assertFalse(refused("x"))

    def test_bool_notify_compat(self):
        """NTF-AC7 (R1): `true` = desktop, `false` = none; невідоме значення — помилка конфігу."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cases = {"true": "desktop", "false": "none", "telegram": "telegram", "desktop": "desktop"}
        for raw, expected in cases.items():
            (tmp / "config.yaml").write_text(f"daemon:\n  notify: {raw}\n", encoding="utf-8")
            self.assertEqual(load_config(root=tmp).daemon.notify, expected, raw)
        (tmp / "config.yaml").write_text("daemon:\n  notify: sms\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(root=tmp)
        self.assertEqual(load_config(root=Path(tempfile.mkdtemp())).daemon.summary_hour, 21)


class NotifyDaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        write_config(self.tmp, "  notify: telegram\n")
        self.cfg = load_config(root=self.tmp)
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.pipe = FakePipe(self.cfg, self.db)
        self.send = FakeSend()
        self.logs = []
        patch = mock.patch("audiotube.daemon.has_ffmpeg", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def notifier(self, send=None):
        return Notifier(self.cfg, self.db, send=send or self.send, log=self.logs.append)

    def daemon(self, send=None):
        return Daemon(self.cfg, pipe=self.pipe, log=self.logs.append, notifier=self.notifier(send))

    def job(self, index=1):
        audio = self.cfg.inbox / f"ep{index}.mp3"
        audio.write_bytes(b"audio")
        return self.db.add_job(audio, f"ep{index}", title=f"Епізод {index}")

    def cycle(self, **kw):
        kw.setdefault("sync", False)
        return self.daemon(kw.pop("send", None))._cycle(dry_run=kw.pop("dry_run", False), max_videos=None, **kw)

    def test_published_sends_url(self):
        """NTF-AC1: публікація → рівно 1 повідомлення з назвою і URL."""
        self.job(1)
        self.assertEqual(self.daemon().run(once=True, sync=False), 0)
        self.assertEqual(len(self.send.messages), 1)
        self.assertIn("https://youtu.be/vid1", self.send.messages[0])
        self.assertIn("Епізод 1", self.send.messages[0])

    def test_telegram_down_does_not_break_cycle(self):
        """NTF-AC2: транспорт падає чи мережі немає — цикл усе одно завершується успішно."""
        self.job(1)
        stats = self.cycle(send=FakeSend(raises=RuntimeError("telegram недоступний")))
        self.assertEqual(stats.published, 1)

        def broken(req, timeout):
            raise urllib.error.URLError("timed out")

        self.job(2)
        real = telegram_transport(TOKEN, "42", log=self.logs.append, opener=broken)
        stats = self.cycle(send=real)
        self.assertEqual(stats.published, 1)
        self.assertEqual(self.db.get(2).status, "done")

    def test_auth_expired_throttled(self):
        """NTF-AC3: сесія NotebookLM протухла 3 цикли поспіль → 1 повідомлення; після 12 год — ще одне."""
        self.cfg.notebooklm.enabled = True
        failing = mock.patch(
            "audiotube.daemon.SyncService",
            side_effect=NotebookLMError("не вдалося підключитися до NotebookLM: сесія прострочена"),
        )
        with failing:
            for _ in range(3):  # кожен цикл — новий Daemon і Notifier: обмеження живе в БД
                self.cycle(sync=True)
            self.assertEqual(len(self.send.having("NotebookLM")), 1)
            self.assertIn("notebooklm login", self.send.messages[0])

            self.db.mark_notified(
                "auth_expired:notebooklm", datetime.now(timezone.utc) - timedelta(hours=13)
            )
            self.cycle(sync=True)
        self.assertEqual(len(self.send.having("NotebookLM")), 2)

    def test_youtube_auth_expired_skips_jobs(self):
        """NTF-AC3, NTF-R7: токен YouTube протух → 1 повідомлення за 3 цикли, задачі не чіпаються."""
        job = self.job(1)
        self.pipe.auth_error = AuthRequired("немає дійсного токена YouTube. Виконайте: audiotube auth")
        for _ in range(3):
            stats = self.cycle()
            self.assertEqual(stats.published, 0)
            self.assertIn("YouTube", stats.reason)
        self.assertEqual(len(self.send.having("YouTube")), 1)
        self.assertIn("audiotube auth", self.send.messages[0])
        self.assertEqual(self.db.get(job.id).attempts, 0)
        self.assertEqual(self.pipe.calls, [])

    def test_failed_final_once(self):
        """NTF-AC5: задача досягла max_attempts → рівно одне повідомлення failed_final."""
        job = self.job(1)
        self.pipe.fail_publish.add(job.id)
        self.cfg.daemon.max_attempts = 2
        for _ in range(3):
            self.cycle()
        final = self.send.having("більше не братиметься")
        self.assertEqual(len(final), 1, self.send.messages)
        self.assertIn("YouTube відмовив", final[0])
        self.assertIn(f"audiotube reset {job.id} --to new", final[0])

    def test_daily_summary_once_per_day(self):
        """NTF-AC6: після summary_hour зведення надсилається один раз на добу, з лічильниками."""
        self.cfg.daemon.summary_hour = 0
        self.job(1)
        self.job(2)
        self.pipe.fail_publish.add(2)
        self.cycle()
        self.cycle()
        summaries = self.send.having("Зведення")
        self.assertEqual(len(summaries), 1, self.send.messages)
        self.assertIn("опубліковано 1/2", summaries[0])
        self.assertIn("у черзі 1", summaries[0])
        self.assertIn("з помилками 1", summaries[0])

        # ще не настала година зведення → нічого
        self.cfg.daemon.summary_hour = 21
        early = datetime.now().astimezone().replace(hour=9) + timedelta(days=1)
        self.assertFalse(self.notifier().daily_summary(now=early))
        # вимкнено
        self.cfg.daemon.summary_hour = None
        self.assertFalse(self.notifier().daily_summary(now=early.replace(hour=23)))

    def test_dry_run_sends_nothing(self):
        """NTF-R8: daemon --dry-run нічого не надсилає."""
        self.cfg.daemon.summary_hour = 0
        self.cfg.notebooklm.enabled = True
        self.job(1)
        with mock.patch("audiotube.daemon.SyncService", side_effect=NotebookLMError("протухло")):
            self.cycle(sync=True, dry_run=True)
        self.assertEqual(self.send.messages, [])

    def test_failed_send_is_not_marked(self):
        """NTF-R5: невдала відправка не позначається — наступний цикл спробує знову."""
        notifier = self.notifier(FakeSend(ok=False))
        self.assertFalse(notifier.auth_expired("youtube", "протухло"))
        self.assertIsNone(self.db.last_notified("auth_expired:youtube"))
        self.assertTrue(self.notifier().auth_expired("youtube", "протухло"))

    def test_missing_env_runs_silently(self):
        """NTF-AC4: notify: telegram, але env порожні → демон публікує, сповіщень немає, є попередження."""
        self.job(1)
        with mock.patch.dict(os.environ):
            for key in ENV:
                os.environ.pop(key, None)
            with mock.patch("audiotube.notify.urllib.request.urlopen") as urlopen:
                daemon = Daemon(self.cfg, pipe=self.pipe, log=self.logs.append)
                self.assertEqual(daemon.run(once=True, sync=False), 0)
        urlopen.assert_not_called()
        self.assertEqual(self.db.get(1).status, "done")
        self.assertTrue([line for line in self.logs if "TELEGRAM_BOT_TOKEN" in line], self.logs)

    def test_build_notifier_uses_env(self):
        """NTF-R2: токен і chat_id беруться лише з середовища."""
        with mock.patch("audiotube.notify.urllib.request.urlopen", side_effect=tg_ok) as urlopen:
            notifier = build_notifier(self.cfg, self.db, log=self.logs.append, environ=ENV)
            self.assertTrue(notifier.auth_expired("youtube", "протухло"))
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn(TOKEN, urlopen.call_args.args[0].full_url)


class NotifyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        write_config(self.tmp, "  notify: telegram\n")

    def run_cli(self, *args):
        from audiotube.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--root", str(self.tmp), *args])
        return code, out.getvalue()

    def test_notify_test_command(self):
        """NTF-AC7 (R6): notify --test шле 1 запит; --dry-run — жодного; без env — код 1."""
        with mock.patch.dict(os.environ, ENV), \
                mock.patch("audiotube.notify.urllib.request.urlopen", side_effect=tg_ok) as urlopen:
            code, out = self.run_cli("notify", "--test")
            self.assertEqual(code, 0, out)
            self.assertEqual(urlopen.call_count, 1)
            self.assertNotIn(TOKEN, out)

            code, out = self.run_cli("notify", "--test", "--dry-run")
            self.assertEqual(code, 0)
            self.assertEqual(urlopen.call_count, 1)
            self.assertIn("AudioTube", out)

        with mock.patch.dict(os.environ):
            for key in ENV:
                os.environ.pop(key, None)
            code, out = self.run_cli("notify", "--test")
        self.assertEqual(code, 1)
        self.assertIn("TELEGRAM_BOT_TOKEN", out)

    def test_doctor_warns_without_env(self):
        """NTF-AC4: doctor попереджає, якщо notify: telegram, а змінних середовища немає."""
        cfg = load_config(root=self.tmp)
        ok, detail = telegram_status(cfg, environ={})
        self.assertFalse(ok)
        self.assertIn("TELEGRAM_BOT_TOKEN", detail)

        ok, detail = telegram_status(cfg, environ=ENV)
        self.assertTrue(ok)
        self.assertNotIn(TOKEN, detail)

        cfg.daemon.notify = "none"
        ok, _ = telegram_status(cfg, environ={})
        self.assertIsNone(ok)  # не використовується — не проблема


if __name__ == "__main__":
    unittest.main()
