import tempfile
import unittest
from pathlib import Path

from audiotube.transcript import (
    Segment,
    Transcript,
    build_cues,
    clock,
    srt_timestamp,
    to_srt,
    transcript_for_prompt,
    wrap_text,
    write_srt,
)


class TimestampTests(unittest.TestCase):
    def test_clock(self):
        self.assertEqual(clock(0), "00:00")
        self.assertEqual(clock(59.6), "01:00")
        self.assertEqual(clock(3723), "1:02:03")

    def test_srt_timestamp(self):
        self.assertEqual(srt_timestamp(0), "00:00:00,000")
        self.assertEqual(srt_timestamp(3723.456), "01:02:03,456")


class WrappingTests(unittest.TestCase):
    def test_wrap_text(self):
        self.assertEqual(wrap_text("один два три чотири", 10), ["один два", "три чотири"])

    def test_long_word_is_split(self):
        lines = wrap_text("abcdefghijkl", 5)
        self.assertEqual(lines, ["abcde", "fghij", "kl"])
        self.assertTrue(all(len(line) <= 5 for line in lines))

    def test_empty(self):
        self.assertEqual(wrap_text("   ", 20), [])


class CueTests(unittest.TestCase):
    def test_cues_are_ordered_and_do_not_overlap(self):
        segments = [
            Segment(0.0, 4.0, "Перше речення тут. Друге речення трохи довше."),
            Segment(4.0, 6.5, "Коротка фраза."),
            Segment(6.5, 9.0, "І ще одна фраза для перевірки."),
        ]
        cues = build_cues(segments, max_chars=20, max_lines=2)
        self.assertTrue(cues)
        for cue in cues:
            self.assertLess(cue.start, cue.end)
            self.assertLessEqual(len(cue.lines), 2)
            self.assertTrue(all(len(line) <= 20 for line in cue.lines))
        for previous, current in zip(cues, cues[1:]):
            self.assertLessEqual(previous.end, current.start + 0.001)
        self.assertGreaterEqual(cues[0].start, 0)
        self.assertLessEqual(cues[-1].end, 9.5)

    def test_empty_segments_are_skipped(self):
        cues = build_cues([Segment(0, 2, ""), Segment(2, 3, "   ")], max_chars=10)
        self.assertEqual(cues, [])

    def test_srt_format(self):
        cues = build_cues([Segment(0, 2, "Привіт")], max_chars=40)
        srt = to_srt(cues)
        self.assertTrue(srt.startswith("1\n00:00:00,000 --> 00:00:02,000\nПривіт"))
        self.assertTrue(srt.endswith("\n"))

    def test_write_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subs.srt"
            transcript = Transcript(language="uk", duration=3, segments=[Segment(0, 3, "Тест")])
            write_srt(transcript, path)
            self.assertIn("Тест", path.read_text(encoding="utf-8"))


class TranscriptModelTests(unittest.TestCase):
    def test_roundtrip(self):
        transcript = Transcript(
            language="uk", duration=12.5,
            segments=[Segment(0.0, 5.0, "Привіт"), Segment(5.0, 12.5, "Світ")],
            backend="faster-whisper", model="small",
        )
        self.assertEqual(transcript.text, "Привіт Світ")
        with tempfile.TemporaryDirectory() as tmp:
            path = transcript.save(Path(tmp) / "transcript.json")
            loaded = Transcript.load(path)
        self.assertEqual(loaded.language, "uk")
        self.assertAlmostEqual(loaded.duration, 12.5)
        self.assertEqual(loaded.segments, transcript.segments)


class PromptTests(unittest.TestCase):
    def test_prompt_has_timestamps_and_blocks(self):
        transcript = Transcript(
            language="uk", duration=200,
            segments=[Segment(0, 30, "початок"), Segment(30, 60, "далі"), Segment(100, 130, "пізніше")],
        )
        text = transcript_for_prompt(transcript, block_seconds=45)
        self.assertIn("[00:00] початок далі", text)
        self.assertIn("[01:40] пізніше", text)
        self.assertIn("(кінець)", text)

    def test_prompt_truncates(self):
        transcript = Transcript(
            language="uk", duration=1000,
            segments=[Segment(i, i + 1, "довгий текст " * 5) for i in range(0, 1000, 10)],
        )
        text = transcript_for_prompt(transcript, max_chars=500)
        self.assertLessEqual(len(text), 540)
        self.assertIn("скорочено", text)


if __name__ == "__main__":
    unittest.main()
