import unittest
from datetime import datetime, timedelta, timezone

from audiotube.config import Config
from audiotube.metadata import VideoMeta, slugify
from audiotube.youtube import video_body


class MetadataTests(unittest.TestCase):
    def test_slugify_ukrainian(self):
        self.assertEqual(slugify("Що таке Docker за 20 хвилин?"), "shcho-take-docker-za-20-khvylyn")
        self.assertEqual(slugify("!!!"), "video")

    def test_chapters_short_form_and_zero_start(self):
        m = VideoMeta(title="T", chapters=["01:10 Основи", "05:00 Практика"])
        desc = m.youtube_description("футер")
        self.assertIn("00:00 Вступ\n01:10 Основи\n05:00 Практика", desc)
        self.assertTrue(desc.endswith("футер"))

    def test_bad_chapter_time(self):
        with self.assertRaises(ValueError):
            VideoMeta(title="T", chapters=["1.10 Основи"])

    def test_title_and_tags_limits(self):
        m = VideoMeta(title="<" + "а" * 150 + ">", tags=["x" * 100] * 3 + [f"tag{i}" for i in range(200)])
        self.assertEqual(len(m.youtube_title()), 100)
        self.assertNotIn("<", m.youtube_title())
        tags = m.youtube_tags(["освіта"])
        self.assertLessEqual(sum(len(t) for t in tags) + len(tags), 500)
        self.assertEqual(len([t for t in tags if t == "x" * 100]), 1)  # дублікати прибрано

    def test_video_body_schedule(self):
        when = datetime.now(timezone(timedelta(hours=3))) + timedelta(days=2)
        body = video_body(VideoMeta(title="T", privacy="public", publish_at=when), Config())
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertIn("publishAt", body["status"])
        self.assertTrue(body["status"]["containsSyntheticMedia"])
        self.assertEqual(body["snippet"]["categoryId"], "27")


if __name__ == "__main__":
    unittest.main()
