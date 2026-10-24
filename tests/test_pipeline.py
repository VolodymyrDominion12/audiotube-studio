"""Інтеграційний тест: реальний FFmpeg, фейковий YouTube."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from audiotube.cli import main
from audiotube.config import load_config
from audiotube.media import duration
from audiotube.pipeline import Pipeline

def _ffmpeg_works() -> bool:
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


HAS_FFMPEG = _ffmpeg_works() and bool(shutil.which("ffprobe") or shutil.which("ffmpeg.ffprobe"))


class FakePublisher:
    def __init__(self):
        self.uploads, self.playlists, self.added = [], {}, []

    def upload(self, video, meta):
        self.uploads.append(meta.title)
        return f"vid{len(self.uploads)}"

    def set_thumbnail(self, vid, thumb):
        return True

    def ensure_playlist(self, title, description=""):
        return self.playlists.setdefault(title, f"pl{len(self.playlists) + 1}")

    def add_to_playlist(self, pid, vid):
        self.added.append((pid, vid))


@unittest.skipUnless(HAS_FFMPEG, "потрібен ffmpeg")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "render:\n  width: 640\n  height: 360\n  fps: 15\n  segment_seconds: 5\n", encoding="utf-8"
        )
        self.assertEqual(main(["--root", str(self.tmp), "init"]), 0)
        audio = self.tmp / "inbox" / "Що_таке_Docker.mp3"
        try:
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=12", str(audio)],
                check=True, capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as e:
            self.skipTest(f"ffmpeg недоступний у цьому середовищі: {e}")
        self.assertEqual(main(["--root", str(self.tmp), "meta", str(audio)]), 0)
        side = audio.with_suffix(".yaml")
        side.write_text(
            side.read_text(encoding="utf-8").replace("playlist: null", 'playlist: "DevOps за 20 хвилин"'),
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_render_and_publish_idempotent(self):
        root = ["--root", str(self.tmp)]
        self.assertEqual(main([*root, "render", "--preview", "4"]), 0)
        cfg = load_config(root=self.tmp)
        pipe = Pipeline(cfg, log=lambda *_: None)
        job = pipe.db.get(1)
        self.assertEqual(job.status, "new")  # прев'ю не змінює статус
        self.assertTrue((cfg.output / f"{job.slug}_preview.mp4").exists())

        self.assertEqual(main([*root, "render"]), 0)
        job = pipe.db.get(1)
        self.assertEqual(job.status, "rendered")
        self.assertEqual(job.title, "Що таке Docker")
        self.assertAlmostEqual(duration(job.video), 12, delta=0.3)
        self.assertTrue(job.thumbnail.exists())

        fake = FakePublisher()
        pipe._publisher = fake
        job = pipe.publish(job)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.video_id, "vid1")
        self.assertEqual(fake.added, [("pl1", "vid1")])

        # повторний запуск нічого не дублює
        pipe.publish(job)
        self.assertEqual(len(fake.uploads), 1)

        # повторний scan не додає те саме аудіо
        self.assertEqual(pipe.scan(), [])


if __name__ == "__main__":
    unittest.main()
