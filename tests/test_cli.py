"""CLI-команди: run/watch/status/reset/doctor (без мережі; ffmpeg — якщо є)."""

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from audiotube.cli import main
from audiotube.config import load_config

def _ffmpeg_works() -> bool:
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


HAS_FFMPEG = _ffmpeg_works() and bool(shutil.which("ffprobe") or shutil.which("ffmpeg.ffprobe"))


def run_cli(*args) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = main(list(args))
    return code, out.getvalue()


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "render:\n  width: 320\n  height: 180\n  fps: 10\n  segment_seconds: 5\n"
            "llm:\n  provider: none\n"
            "transcribe:\n  backend: none\n",
            encoding="utf-8",
        )
        self.assertEqual(run_cli("--root", str(self.tmp), "init")[0], 0)
        self.audio = self.tmp / "inbox" / "Тема.mp3"
        if HAS_FFMPEG:
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=6", str(self.audio)],
                check=True,
            )
        else:
            self.audio.write_bytes(b"fake-audio")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_help_lists_new_commands(self):
        parser_out = io.StringIO()
        with contextlib.redirect_stdout(parser_out):
            with self.assertRaises(SystemExit):
                main(["--help"])
        text = parser_out.getvalue()
        for command in ("transcribe", "describe", "web", "watch", "doctor"):
            self.assertIn(command, text)

    def test_status_and_scan(self):
        code, out = run_cli("--root", str(self.tmp), "scan")
        self.assertEqual(code, 0)
        self.assertIn("нових аудіо: 1", out)
        code, out = run_cli("--root", str(self.tmp), "status")
        self.assertEqual(code, 0)
        self.assertIn("Тема", out)

    def test_run_without_optional_backends_still_renders(self):
        if not HAS_FFMPEG:
            self.skipTest("потрібен ffmpeg")
        code, out = run_cli("--root", str(self.tmp), "run")
        self.assertEqual(code, 0, out)
        cfg = load_config(root=self.tmp)
        from audiotube.db import Database

        db = Database(cfg.db_path)
        job = db.get(1)
        db.close()
        self.assertEqual(job.status, "rendered")
        self.assertTrue(job.video.exists())
        self.assertIn("ПОМИЛКА транскрипція", out)

    def test_watch_once_without_render(self):
        code, out = run_cli("--root", str(self.tmp), "watch", "--once", "--no-render")
        self.assertEqual(code, 0, out)
        self.assertIn("стежу за", out)

    def test_reset(self):
        run_cli("--root", str(self.tmp), "scan")
        code, out = run_cli("--root", str(self.tmp), "reset", "1", "--to", "described")
        self.assertEqual(code, 0, out)
        self.assertIn("-> described", out)
        code, out = run_cli("--root", str(self.tmp), "reset", "999")
        self.assertEqual(code, 1)
        self.assertIn("не знайдено", out)

    def test_captions_without_upload(self):
        run_cli("--root", str(self.tmp), "scan")
        code, out = run_cli("--root", str(self.tmp), "captions", "1")
        self.assertEqual(code, 1)
        self.assertIn("не завантажено", out)

    def test_doctor_reports_problems(self):
        code, out = run_cli("--root", str(self.tmp), "doctor")
        self.assertIn("AudioTube Studio", out)
        self.assertIn("транскрипція", out)
        self.assertIn("LLM", out)
        self.assertIsInstance(code, int)


if __name__ == "__main__":
    unittest.main()
