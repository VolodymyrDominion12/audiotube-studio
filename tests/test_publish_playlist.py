"""Публікація без плейлистів (розкладання відкладено) — без FFmpeg і без мережі."""

import shutil
import tempfile
import unittest
from pathlib import Path

from audiotube.config import load_config
from audiotube.db import Database
from audiotube.pipeline import Pipeline


class FakePublisher:
    def __init__(self):
        self.uploads, self.playlists, self.added = [], {}, []

    def upload(self, video, meta):
        self.uploads.append(meta.title)
        return "vid1"

    def set_thumbnail(self, vid, thumb):
        return True

    def ensure_playlist(self, title, description=""):
        return self.playlists.setdefault(title, "pl1")

    def add_to_playlist(self, pid, vid):
        self.added.append((pid, vid))


class PublishPlaylistTests(unittest.TestCase):
    def make(self, manage_playlists: bool):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "config.yaml").write_text(
            "youtube:\n"
            "  upload_captions: false\n"
            f"  manage_playlists: {'true' if manage_playlists else 'false'}\n",
            encoding="utf-8",
        )
        cfg = load_config(root=tmp)
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        self.addCleanup(db.close)

        audio = cfg.inbox / "ep.mp3"
        audio.write_bytes(b"audio")
        audio.with_suffix(".yaml").write_text(
            'title: "Епізод"\nplaylist: "Docker"\n', encoding="utf-8"
        )
        job = db.add_job(audio, "ep", title="Епізод")
        video = cfg.output / "ep.mp4"
        video.write_bytes(b"video")
        db.update(job.id, status="rendered", video=str(video))

        pipe = Pipeline(cfg, db=db, log=lambda _m: None)
        fake = FakePublisher()
        pipe._publisher = fake
        return pipe, db, job, fake

    def test_playlist_disabled(self):
        pipe, db, job, fake = self.make(manage_playlists=False)
        result = pipe.publish(db.get(job.id))
        self.assertEqual(result.status, "done")
        self.assertEqual(fake.uploads, ["Епізод"])
        self.assertEqual(fake.playlists, {})
        self.assertEqual(fake.added, [])
        self.assertEqual(db.uploads_count(), 1)

    def test_playlist_enabled_still_works(self):
        pipe, db, job, fake = self.make(manage_playlists=True)
        result = pipe.publish(db.get(job.id))
        self.assertEqual(result.status, "done")
        self.assertEqual(fake.playlists, {"Docker": "pl1"})
        self.assertEqual(fake.added, [("pl1", "vid1")])
        self.assertEqual(db.uploads_count(), 1)


if __name__ == "__main__":
    unittest.main()
