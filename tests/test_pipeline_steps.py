"""Фаза 2: транскрипт -> метадані -> рендер -> публікація (реальний FFmpeg, фейкові LLM/YouTube)."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiotube.config import load_config
from audiotube.media import duration
from audiotube.metadata import load_meta
from audiotube.pipeline import Pipeline
from audiotube.planner import ContentPlan
from audiotube.transcribe import TranscriptionResult, TranscriptionError
from audiotube.transcript import Segment, Transcript, write_srt

def _ffmpeg_works() -> bool:
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


HAS_FFMPEG = _ffmpeg_works() and bool(shutil.which("ffprobe") or shutil.which("ffmpeg.ffprobe"))


class FakePublisher:
    def __init__(self):
        self.uploads, self.playlists, self.added, self.captions = [], {}, [], []

    def upload(self, video, meta):
        self.uploads.append(meta.title)
        return f"vid{len(self.uploads)}"

    def set_thumbnail(self, vid, thumb):
        return True

    def set_captions(self, vid, srt):
        self.captions.append((vid, Path(srt).name))
        return "cap1"

    def ensure_playlist(self, title, description=""):
        return self.playlists.setdefault(title, f"pl{len(self.playlists) + 1}")

    def add_to_playlist(self, pid, vid):
        self.added.append((pid, vid))


def fake_plan(transcript, cfg, fallback_title, known_playlists=None, client=None):
    return ContentPlan(
        title="Docker за 8 хвилин",
        description="Короткий опис відео.",
        tags=["docker", "devops"],
        chapters=[{"time": "00:00", "title": "Вступ"}, {"time": "00:04", "title": "Основи"}],
        playlist="DevOps за 20 хвилин",
        visual_keywords=["docker", "containers"],
        thumbnail_text="Docker",
    )


def make_fake_transcribe(workdir: Path):
    def fake(audio, workdir_, cfg, force=False, log=print):
        tr = Transcript(
            language="uk", duration=8.0,
            segments=[Segment(0, 4, "Що таке Docker."), Segment(4, 8, "Навіщо потрібні контейнери.")],
            backend="fake", model="fake-small",
        )
        workdir_.mkdir(parents=True, exist_ok=True)
        json_path = tr.save(workdir_ / "transcript.json")
        srt_path = write_srt(tr, workdir_ / "subtitles.srt")
        return TranscriptionResult(tr, json_path, srt_path)

    return fake


@unittest.skipUnless(HAS_FFMPEG, "потрібен ffmpeg")
class Phase2PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "render:\n  width: 480\n  height: 270\n  fps: 12\n  segment_seconds: 5\n"
            "llm:\n  provider: openai\n  model: fake\n"
            "youtube:\n  upload_captions: true\n",
            encoding="utf-8",
        )
        from audiotube.cli import main

        self.assertEqual(main(["--root", str(self.tmp), "init"]), 0)
        self.audio = self.tmp / "inbox" / "Docker.mp3"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=8", str(self.audio)],
            check=True,
        )
        self.cfg = load_config(root=self.tmp)
        self.pipe = Pipeline(self.cfg, log=lambda *_: None)

    def tearDown(self):
        self.pipe.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_transcribe(self):
        return mock.patch("audiotube.pipeline.transcribe_audio", side_effect=make_fake_transcribe(self.tmp))

    def test_full_chain_and_captions(self):
        jobs = self.pipe.scan()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.status, "new")

        with self._fake_transcribe():
            job = self.pipe.transcribe(job)
        self.assertEqual(job.status, "transcribed")
        self.assertTrue(job.transcript.exists())
        self.assertTrue(job.subtitles.exists())
        self.assertAlmostEqual(job.duration, 8.0)

        with mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan):
            job = self.pipe.describe(job)
        self.assertEqual(job.status, "described")
        self.assertEqual(job.title, "Docker за 8 хвилин")
        side = self.audio.with_suffix(".yaml")
        self.assertTrue(side.exists())
        meta = load_meta(self.audio)
        self.assertEqual(meta.playlist, "DevOps за 20 хвилин")
        self.assertEqual([c.time for c in meta.chapters], ["00:00", "00:04"])
        self.assertEqual(meta.visual_keywords, ["docker", "containers"])
        self.assertIn("Згенеровано автоматично", side.read_text(encoding="utf-8"))

        job = self.pipe.render(job)
        self.assertEqual(job.status, "rendered")
        self.assertTrue(job.video.exists())
        self.assertTrue(job.thumbnail.exists())
        self.assertAlmostEqual(duration(job.video), 8, delta=0.4)

        fake = FakePublisher()
        self.pipe._publisher = fake
        job = self.pipe.publish(job)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.video_id, "vid1")
        self.assertEqual(fake.captions, [("vid1", "subtitles.srt")])
        self.assertEqual(fake.added, [("pl1", "vid1")])
        self.assertEqual(job.captions_id, "cap1")
        self.assertEqual(job.playlist_id, "pl1")

        # повторна публікація нічого не дублює
        self.pipe.publish(job)
        self.assertEqual(len(fake.uploads), 1)
        self.assertEqual(len(fake.captions), 1)

    def test_describe_force_preserves_manual_settings(self):
        job = self.pipe.scan()[0]
        with self._fake_transcribe():
            job = self.pipe.transcribe(job)
        with mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan):
            job = self.pipe.describe(job)

        side = self.audio.with_suffix(".yaml")
        text = side.read_text(encoding="utf-8")
        text = text.replace("mode: null", "mode: loop").replace("category: null", "category: tech")
        text = text.replace("privacy: null", "privacy: unlisted")
        side.write_text(text, encoding="utf-8")

        def other_plan(transcript, cfg, fallback_title, known_playlists=None, client=None):
            return ContentPlan(title="Нова назва", description="Новий опис", chapters=["00:00 Вступ"])

        with mock.patch("audiotube.pipeline.generate_plan", side_effect=other_plan):
            job = self.pipe.describe(job, force=True)

        meta = load_meta(self.audio)
        self.assertEqual(meta.title, "Нова назва")
        self.assertEqual(meta.description, "Новий опис")
        self.assertEqual(meta.mode, "loop")
        self.assertEqual(meta.category, "tech")
        self.assertEqual(meta.privacy, "unlisted")
        self.assertEqual(job.status, "described")

    def test_describe_requires_or_creates_transcript(self):
        job = self.pipe.scan()[0]
        with self._fake_transcribe() as fake, mock.patch(
            "audiotube.pipeline.generate_plan", side_effect=fake_plan
        ):
            job = self.pipe.describe(job)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(job.status, "described")

    def test_render_works_when_optional_steps_fail(self):
        job = self.pipe.scan()[0]
        with mock.patch(
            "audiotube.pipeline.transcribe_audio",
            side_effect=TranscriptionError("немає бекенда"),
        ):
            job = self.pipe.transcribe(job)
        self.assertEqual(job.status, "new")
        self.assertIn("немає бекенда", job.error)

        job = self.pipe.render(job)
        self.assertEqual(job.status, "rendered")
        self.assertTrue(job.video.exists())

    def test_status_never_goes_backwards(self):
        job = self.pipe.scan()[0]
        with self._fake_transcribe():
            job = self.pipe.transcribe(job)
        job = self.pipe.db.update(job.id, status="uploaded")
        with self._fake_transcribe():
            job = self.pipe.transcribe(job, force=True)
        self.assertEqual(job.status, "uploaded")

    def test_subtitles_are_generated_from_transcript(self):
        job = self.pipe.scan()[0]
        with self._fake_transcribe():
            job = self.pipe.transcribe(job)
        job.subtitles = None
        srt = self.pipe._subtitles_path(job, self.cfg.workspace / job.slug)
        self.assertIsNotNone(srt)
        self.assertTrue(srt.exists())
        self.assertIn("Docker", srt.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
