"""Spec 010: зміна приватності завантаженого відео — без мережі й без справжнього YouTube."""

import contextlib
import io
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiotube.cli import main
from audiotube.config import load_config
from audiotube.db import Database, QuotaTracker
from audiotube.youtube import Publisher


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeVideos:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return FakeRequest({"items": [{"id": kwargs["id"], "status": dict(self.status)}]})

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return FakeRequest({"id": kwargs["body"]["id"]})


class FakeService:
    def __init__(self, status=None):
        self.vid = FakeVideos(status or {
            "privacyStatus": "private",
            "license": "youtube",
            "embeddable": True,
            "publicStatsViewable": True,
            "selfDeclaredMadeForKids": False,
            "publishAt": "2026-12-01T10:00:00Z",
        })

    def videos(self):
        return self.vid


def run_cli(*args) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = main(list(args))
        except SystemExit as e:  # argparse: недопустиме значення --privacy
            code = int(e.code or 0)
    return code, out.getvalue()


class PublishTests(unittest.TestCase):
    def make_root(self, daily_quota: int = 10000) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "config.yaml").write_text(
            f"youtube:\n  daily_quota: {daily_quota}\n  contains_synthetic_media: true\n",
            encoding="utf-8",
        )
        return tmp

    def add_job(self, root: Path, video_id: str | None = "vid1") -> int:
        cfg = load_config(root=root)
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        self.addCleanup(db.close)
        audio = cfg.inbox / "Тема.mp3"
        audio.write_bytes(b"audio")
        job = db.add_job(audio, "tema", title="Тема")
        if video_id:
            db.update(job.id, video_id=video_id, status="done")
        return job.id

    def test_set_privacy_preserves_fields(self):
        """PUB-AC1: videos.update зберігає ліцензію, embeddable, madeForKids і позначку AI."""
        tmp = self.make_root()
        cfg = load_config(root=tmp)
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        self.addCleanup(db.close)
        quota = QuotaTracker(db, cfg.youtube.daily_quota)
        fake = FakeService()
        pub = Publisher(fake, cfg, quota, log=lambda _m: None)

        result = pub.set_privacy("vid1", "public")

        self.assertEqual(result, "public")
        update = [c for c in fake.vid.calls if c[0] == "update"][0][1]
        status = update["body"]["status"]
        self.assertEqual(update["part"], "status")
        self.assertEqual(status["privacyStatus"], "public")
        self.assertEqual(status["license"], "youtube")
        self.assertIs(status["embeddable"], True)
        self.assertIs(status["publicStatsViewable"], True)
        self.assertIs(status["selfDeclaredMadeForKids"], False)
        self.assertIs(status["containsSyntheticMedia"], True)
        self.assertNotIn("publishAt", status, "ручна публікація має скасовувати відкладений publishAt")
        self.assertEqual(quota.used(), 51)

    def test_dry_run_makes_no_calls(self):
        """PUB-AC2: --dry-run не звертається до YouTube і не списує квоту."""
        tmp = self.make_root()
        job_id = self.add_job(tmp)
        with mock.patch("audiotube.youtube.build_service") as build:
            code, out = run_cli("--root", str(tmp), "publish", str(job_id), "--dry-run")
        self.assertEqual(code, 0)
        build.assert_not_called()
        self.assertIn("dry-run", out)
        self.assertIn("vid1", out)
        db = Database(load_config(root=tmp).db_path)
        self.addCleanup(db.close)
        self.assertEqual(QuotaTracker(db, 10000).used(), 0)

    def test_job_without_video_id(self):
        """PUB-AC3: задача без video_id — повідомлення «спершу upload», код 1, без API."""
        tmp = self.make_root()
        job_id = self.add_job(tmp, video_id=None)
        with mock.patch("audiotube.youtube.build_service") as build:
            code, out = run_cli("--root", str(tmp), "publish", str(job_id))
        self.assertEqual(code, 1)
        build.assert_not_called()
        self.assertIn("upload", out)

    def test_quota_exhausted(self):
        """PUB-AC4: нестача квоти — жодного videos.update, код 1."""
        tmp = self.make_root(daily_quota=10)
        job_id = self.add_job(tmp)
        fake = FakeService()
        with mock.patch("audiotube.youtube.build_service", return_value=fake):
            code, out = run_cli("--root", str(tmp), "publish", str(job_id), "--privacy", "unlisted")
        self.assertEqual(code, 1)
        self.assertIn("квот", out.lower())
        self.assertNotIn("update", [c[0] for c in fake.vid.calls])

    def test_unknown_id_does_not_block_others(self):
        """PUB-AC5: невідомий id не заважає змінити приватність відомого відео."""
        tmp = self.make_root()
        job_id = self.add_job(tmp)
        fake = FakeService()
        with mock.patch("audiotube.youtube.build_service", return_value=fake):
            code, out = run_cli("--root", str(tmp), "publish", "999", str(job_id))
        self.assertEqual(code, 1)
        self.assertIn("999", out)
        self.assertIn("https://youtu.be/vid1", out)
        self.assertIn("update", [c[0] for c in fake.vid.calls])


class PublishStatusMappingTests(unittest.TestCase):
    def test_privacy_values(self):
        """PUB-R1: --privacy приймає лише public/unlisted/private."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "config.yaml").write_text("youtube:\n  daily_quota: 10000\n", encoding="utf-8")
        code, _out = run_cli("--root", str(tmp), "publish", "1", "--privacy", "secret")
        self.assertEqual(code, 2)  # argparse: недопустиме значення


if __name__ == "__main__":
    unittest.main()
