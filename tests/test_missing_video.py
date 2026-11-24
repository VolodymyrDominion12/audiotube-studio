"""Дорендер аудіо без відео: `run --missing-video` і `--no-upload` (spec 013)."""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiotube.cli import build_parser, main
from audiotube.config import load_config
from audiotube.db import Database
from audiotube.planner import ContentPlan
from audiotube.render import RenderResult
from audiotube.transcribe import TranscriptionResult
from audiotube.transcript import Segment, Transcript, write_srt


def run_cli(*args) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = main(list(args))
    return code, out.getvalue()


def fake_transcribe(audio, workdir, cfg, force=False, log=print):
    transcript = Transcript(
        language="uk",
        duration=8.0,
        segments=[Segment(0, 4, "Перший сегмент."), Segment(4, 8, "Другий сегмент.")],
        backend="fake",
        model="fake-small",
    )
    workdir.mkdir(parents=True, exist_ok=True)
    json_path = transcript.save(workdir / "transcript.json")
    srt_path = write_srt(transcript, workdir / "subtitles.srt")
    return TranscriptionResult(transcript, json_path, srt_path)


def fake_plan(transcript, cfg, fallback_title, known_playlists=None, client=None):
    return ContentPlan(
        title="Назва від LLM",
        description="Опис від LLM.",
        tags=["тег"],
        chapters=[{"time": "00:00", "title": "Вступ"}, {"time": "00:04", "title": "Далі"}],
        visual_keywords=["абстракція"],
    )


def fake_render_video(audio, bg, title, thumbnail_text, cfg, workdir, out_video, out_thumb,
                      preview_seconds=None, subtitles=None):
    out_video.parent.mkdir(parents=True, exist_ok=True)
    out_video.write_bytes(b"fake-video")
    out_thumb.write_bytes(b"fake-thumb")
    return RenderResult(out_video, out_thumb, 8.0, "fake")


class MissingVideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._write_config()
        self.assertEqual(run_cli("--root", str(self.tmp), "init")[0], 0)
        self._ffmpeg = mock.patch("audiotube.cli.require_ffmpeg")
        self._ffmpeg.start()

    def tearDown(self):
        self._ffmpeg.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, auto_upload: bool = False) -> None:
        (self.tmp / "config.yaml").write_text(
            "render:\n  width: 320\n  height: 180\n  fps: 10\n  segment_seconds: 5\n"
            "llm:\n  provider: openai\n  model: fake\n"
            "transcribe:\n  backend: none\n"
            f"youtube:\n  auto_upload: {'true' if auto_upload else 'false'}\n",
            encoding="utf-8",
        )

    def _add_audio(self, name: str) -> Path:
        audio = self.tmp / "inbox" / name
        audio.write_bytes(b"fake-audio")
        audio.with_suffix(".yaml").write_text(
            "title: Назва з sync\nprivacy: private\n", encoding="utf-8"
        )
        return audio

    def _jobs(self):
        cfg = load_config(root=self.tmp)
        db = Database(cfg.db_path)
        try:
            return db.list()
        finally:
            db.close()

    def _update_job(self, job_id: int, **fields) -> None:
        cfg = load_config(root=self.tmp)
        db = Database(cfg.db_path)
        try:
            db.update(job_id, **fields)
        finally:
            db.close()

    def test_parser_accepts_missing_video_flags(self):
        """FILL-AC1: прапорці --missing-video/--no-upload доступні, --upload і --no-upload взаємовиключні."""
        args = build_parser().parse_args(["run", "--missing-video", "--no-upload"])
        self.assertTrue(args.missing_video)
        self.assertTrue(args.no_upload)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["run", "--upload", "--no-upload"])
        with self.assertRaises(SystemExit):
            run_cli("--root", str(self.tmp), "run", "--missing-video", "--upload")

    def test_rendered_job_without_file_is_rerendered(self):
        """FILL-AC2: статус rendered, але файлу відео немає — рендер повторюється."""
        self._add_audio("Втрачене.mp3")
        self.assertEqual(run_cli("--root", str(self.tmp), "scan")[0], 0)
        job = self._jobs()[0]
        self._update_job(job.id, status="rendered", video=str(self.tmp / "output" / "zlydni.mp4"))

        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe), \
                mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan), \
                mock.patch("audiotube.pipeline.render_video", side_effect=fake_render_video), \
                mock.patch("audiotube.pipeline.choose_background") as bg:
            bg.return_value = mock.Mock(path=self.tmp / "bg.png", source="fake")
            code, out = run_cli("--root", str(self.tmp), "run", "--missing-video")

        self.assertEqual(code, 0, out)
        job = self._jobs()[0]
        self.assertEqual(job.status, "rendered")
        self.assertIsNotNone(job.video)
        self.assertTrue(job.video.exists())
        self.assertEqual(job.video.read_bytes(), b"fake-video")

    def test_existing_video_is_never_touched(self):
        """FILL-AC3: якщо файл відео вже є, задача не потрапляє в обробку."""
        self._add_audio("Готове.mp3")
        self.assertEqual(run_cli("--root", str(self.tmp), "scan")[0], 0)
        job = self._jobs()[0]
        existing = self.tmp / "output" / "hotove.mp4"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(b"original")
        self._update_job(job.id, status="described", video=str(existing))

        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe), \
                mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan), \
                mock.patch("audiotube.pipeline.render_video") as render:
            code, out = run_cli("--root", str(self.tmp), "run", "--missing-video")

        self.assertEqual(code, 0, out)
        render.assert_not_called()
        self.assertEqual(existing.read_bytes(), b"original")

    def test_missing_video_never_uploads_even_with_auto_upload(self):
        """FILL-AC4: --missing-video не публікує навіть із youtube.auto_upload: true."""
        self._write_config(auto_upload=True)
        self._add_audio("Без відео.mp3")

        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe), \
                mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan), \
                mock.patch("audiotube.pipeline.render_video", side_effect=fake_render_video), \
                mock.patch("audiotube.pipeline.choose_background") as bg, \
                mock.patch("audiotube.pipeline.Pipeline.publish") as publish:
            bg.return_value = mock.Mock(path=self.tmp / "bg.png", source="fake")
            code, out = run_cli("--root", str(self.tmp), "run", "--missing-video")

        self.assertEqual(code, 0, out)
        publish.assert_not_called()
        job = self._jobs()[0]
        self.assertEqual(job.status, "rendered")
        self.assertIsNone(job.video_id)

    def test_no_upload_overrides_auto_upload(self):
        """FILL-AC5: --no-upload вимикає публікацію навіть якщо auto_upload: true."""
        self._write_config(auto_upload=True)
        self._add_audio("Тиха.mp3")

        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe), \
                mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan), \
                mock.patch("audiotube.pipeline.render_video", side_effect=fake_render_video), \
                mock.patch("audiotube.pipeline.choose_background") as bg, \
                mock.patch("audiotube.pipeline.Pipeline.publish") as publish:
            bg.return_value = mock.Mock(path=self.tmp / "bg.png", source="fake")
            code, out = run_cli("--root", str(self.tmp), "run", "--no-upload")

        self.assertEqual(code, 0, out)
        publish.assert_not_called()
        self.assertEqual(self._jobs()[0].status, "rendered")

    def test_job_with_youtube_video_is_skipped(self):
        """FILL-AC6: задача з video_id вважається опублікованою, навіть якщо локальний файл зник."""
        self._add_audio("На ютубі.mp3")
        self.assertEqual(run_cli("--root", str(self.tmp), "scan")[0], 0)
        job = self._jobs()[0]
        self._update_job(job.id, status="done", video_id="abc123", video=str(self.tmp / "gone.mp4"))

        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe), \
                mock.patch("audiotube.pipeline.render_video") as render:
            code, out = run_cli("--root", str(self.tmp), "run", "--missing-video")

        self.assertEqual(code, 0, out)
        render.assert_not_called()

    def test_limit_applies_to_missing_video_jobs(self):
        """FILL-AC7: --limit обмежує кількість дорендерених задач."""
        for i in range(3):
            self._add_audio(f"Тема{i}.mp3")

        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe), \
                mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan), \
                mock.patch("audiotube.pipeline.render_video", side_effect=fake_render_video), \
                mock.patch("audiotube.pipeline.choose_background") as bg:
            bg.return_value = mock.Mock(path=self.tmp / "bg.png", source="fake")
            code, out = run_cli(
                "--root", str(self.tmp), "run", "--missing-video", "--limit", "2", "--no-upload"
            )

        self.assertEqual(code, 0, out)
        statuses = [job.status for job in self._jobs()]
        self.assertEqual(statuses, ["rendered", "rendered", "new"])


if __name__ == "__main__":
    unittest.main()
