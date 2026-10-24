import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from audiotube.config import Config
from audiotube.metadata import (
    Chapter,
    VideoMeta,
    dump_sidecar,
    load_meta,
    merge_generated_meta,
    write_sidecar,
)
from audiotube.planner import (
    ContentPlan,
    PlanError,
    build_messages,
    extract_json,
    generate_plan,
    parse_plan,
    parse_timecode,
    plan_to_meta,
    sanitize_chapters,
)
from audiotube.transcript import Segment, Transcript


class ExtractJsonTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(extract_json('{"title": "x"}'), {"title": "x"})

    def test_fenced(self):
        self.assertEqual(extract_json('```json\n{"title": "x"}\n```'), {"title": "x"})

    def test_text_around(self):
        self.assertEqual(extract_json('Ось результат:\n{"title": "x"}\nГотово.'), {"title": "x"})

    def test_invalid(self):
        with self.assertRaises(PlanError):
            extract_json("зовсім не json")


class TimecodeTests(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse_timecode("00:00"), 0)
        self.assertEqual(parse_timecode("02:40"), 160)
        self.assertEqual(parse_timecode("1:02:03"), 3723)
        self.assertAlmostEqual(parse_timecode("2.40"), 2.4)
        self.assertIsNone(parse_timecode("--"))


class ChapterTests(unittest.TestCase):
    def test_sort_dedup_and_zero_start(self):
        chapters = [
            Chapter(time="05:00", title="B"),
            Chapter(time="00:00", title="A"),
            Chapter(time="05:02", title="дубль"),
            Chapter(time="03:00", title="C"),
        ]
        result = sanitize_chapters(chapters, duration=600)
        self.assertEqual([(c.time, c.title) for c in result], [("00:00", "A"), ("03:00", "C"), ("05:00", "B")])

    def test_prepends_intro_when_late_start(self):
        result = sanitize_chapters([Chapter(time="02:00", title="Пізно")], duration=600)
        self.assertEqual(result[0].time, "00:00")
        self.assertEqual(result[0].title, "Вступ")

    def test_clamps_beyond_duration(self):
        result = sanitize_chapters([Chapter(time="00:00", title="A"), Chapter(time="99:00", title="B")], 600)
        self.assertEqual(result[-1].time, "09:59")


class ParsePlanTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_messy_llm_json_is_normalised(self):
        raw = """
        ```json
        {"title": "  «Docker за 20 хвилин»  ",
         "description": "Опис.",
         "tags": "#docker, devops, devops",
         "chapters": ["05:00 Основи", "02:10 Перший крок", "00:00 Вступ"],
         "playlist": null,
         "visual_keywords": ["docker", "containers"],
         "thumbnail_text": "Docker за 20 хв"}
        ```
        """
        plan = parse_plan(raw, self.cfg, duration=1200, fallback_title="Docker")
        self.assertEqual(plan.title, "Docker за 20 хвилин")
        self.assertEqual(plan.tags, ["docker", "devops"])
        self.assertEqual([c.time for c in plan.chapters], ["00:00", "02:10", "05:00"])
        self.assertEqual(plan.thumbnail_text, "Docker за 20 хв")
        self.assertIsNone(plan.playlist)

    def test_default_playlist_is_applied(self):
        cfg = Config()
        cfg.plan.default_playlist = "Технології за 20 хвилин"
        plan = parse_plan('{"title": "T", "playlist": ""}', cfg, duration=100, fallback_title="T")
        self.assertEqual(plan.playlist, "Технології за 20 хвилин")

    def test_schema_error(self):
        with self.assertRaises(PlanError):
            parse_plan('{"title": 42, "chapters": "not a list"}', self.cfg, 10, "T")

    def test_plan_to_meta(self):
        plan = ContentPlan(
            title="T", description="D", tags=["a"],
            chapters=[{"time": "00:00", "title": "Вступ"}, {"time": "03:00", "title": "Далі"}],
            playlist="P", thumbnail_text="Текст",
        )
        meta = plan_to_meta(plan)
        self.assertEqual(meta.playlist, "P")
        self.assertEqual(len(meta.chapters), 2)
        desc = meta.youtube_description()
        self.assertIn("00:00 Вступ", desc)
        self.assertIn("03:00 Далі", desc)


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.messages = []

    def complete(self, messages, json_mode=True):
        self.calls += 1
        self.messages.append(messages)
        return self.replies.pop(0)


class GeneratePlanTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.llm.retries = 1
        self.transcript = Transcript(
            language="uk", duration=120,
            segments=[Segment(0, 40, "Вступ до Docker"), Segment(40, 120, "Контейнери та образи")],
        )

    def test_retries_on_invalid_json(self):
        client = FakeClient([
            "не json",
            '{"title": "Docker", "chapters": ["00:00 Вступ", "01:40 Контейнери"]}',
        ])
        plan = generate_plan(self.transcript, self.cfg, "Docker", client=client)
        self.assertEqual(plan.title, "Docker")
        self.assertEqual(client.calls, 2)
        self.assertIn("виправлений", client.messages[1][-1]["content"])

    def test_prompt_contains_context(self):
        client = FakeClient(['{"title": "Docker", "chapters": ["00:00 Вступ"]}'])
        generate_plan(self.transcript, self.cfg, "Docker", known_playlists=["DevOps за 20 хвилин"], client=client)
        user = client.messages[0][1]["content"]
        self.assertIn("DevOps за 20 хвилин", user)
        self.assertIn("[00:00]", user)
        self.assertIn("[02:00] (кінець)", user)

    def test_build_messages_without_playlists(self):
        messages = build_messages(self.transcript, self.cfg, "Docker", known_playlists=[])
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("(немає)", messages[1]["content"])


class SidecarTests(unittest.TestCase):
    def test_dump_and_load_roundtrip(self):
        meta = ContentPlan(
            title="Docker: контейнери без магії",
            description="Перший рядок.\n\nДругий абзац.",
            tags=["docker", "devops"],
            chapters=[{"time": "00:00", "title": "Вступ"}, {"time": "03:00", "title": "Основи"}],
            playlist="DevOps за 20 хвилин",
            thumbnail_text="Docker за 20 хв",
        )
        video_meta = plan_to_meta(meta)
        video_meta.mode = "loop"
        video_meta.category = "tech"
        video_meta.privacy = "unlisted"
        video_meta.publish_at = datetime.fromisoformat("2026-10-25T18:00:00+03:00")
        text = dump_sidecar(video_meta, header="Згенеровано тестом")
        self.assertIn("# Згенеровано тестом", text)
        self.assertIn("description: |-", text)

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "docker.mp3"
            audio.write_bytes(b"")
            write_sidecar(audio, video_meta, header="перевірка")
            loaded = load_meta(audio)
        self.assertEqual(loaded.title, video_meta.title)
        self.assertEqual(loaded.description, video_meta.description)
        self.assertEqual(loaded.tags, video_meta.tags)
        self.assertEqual([c.time for c in loaded.chapters], ["00:00", "03:00"])
        self.assertEqual(loaded.playlist, "DevOps за 20 хвилин")
        self.assertEqual(loaded.mode, "loop")
        self.assertEqual(loaded.category, "tech")
        self.assertEqual(loaded.privacy, "unlisted")
        self.assertEqual(loaded.publish_at, video_meta.publish_at)

    def test_merge_preserves_manual_fields(self):
        old_meta = VideoMeta(
            title="Стара назва", description="Старий опис", mode="loop", category="tech",
            privacy="public", thumbnail_text="Ручний текст",
        )
        generated = plan_to_meta(ContentPlan(title="Нова назва", description="Новий опис", thumbnail_text="Новий"))
        merged = merge_generated_meta(old_meta, generated)
        self.assertEqual(merged.title, "Нова назва")
        self.assertEqual(merged.description, "Новий опис")
        self.assertEqual(merged.mode, "loop")
        self.assertEqual(merged.category, "tech")
        self.assertEqual(merged.privacy, "public")
        self.assertEqual(merged.thumbnail_text, "Ручний текст")

    def test_visual_keywords_survive_sidecar(self):
        meta = plan_to_meta(ContentPlan(title="T", visual_keywords=["docker", "whale"]))
        self.assertEqual(meta.visual_keywords, ["docker", "whale"])
        text = dump_sidecar(meta)
        self.assertIn("visual_keywords:", text)
        self.assertIn("whale", text)


if __name__ == "__main__":
    unittest.main()
