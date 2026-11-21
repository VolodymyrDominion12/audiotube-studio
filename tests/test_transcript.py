import random
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from audiotube.config import load_config
from audiotube.transcript import (
    Segment,
    SubtitleRules,
    Transcript,
    build_cues,
    clock,
    layout_cues,
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


def _chars(cue):
    return len(re.sub(r"\s", "", cue.text))


def _cps(cue):
    return _chars(cue) / (cue.end - cue.start)


class SubtitleTimingTests(unittest.TestCase):
    """Spec 009: поділ субтитрів за часом, точками поділу й швидкістю читання."""

    RULES = SubtitleRules()  # 42 символи, 2 рядки, 1–7 с, 17 cps

    def test_config_keys_and_validation(self):
        """SUB-R1…R3: ключі конфігу з дефолтами; min_cue_seconds має бути менше за max_cue_seconds."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rules = SubtitleRules.from_config(load_config(root=tmp))
        self.assertEqual(rules, SubtitleRules(max_chars=42, max_lines=2, max_seconds=7.0,
                                              min_seconds=1.0, max_cps=17.0))
        (tmp / "config.yaml").write_text(
            "transcribe:\n  max_cue_seconds: 5\n  min_cue_seconds: 0.5\n  max_cps: 20\n  max_chars: 30\n",
            encoding="utf-8",
        )
        rules = SubtitleRules.from_config(load_config(root=tmp))
        self.assertEqual((rules.max_seconds, rules.min_seconds, rules.max_cps, rules.max_chars),
                         (5.0, 0.5, 20.0, 30))
        (tmp / "config.yaml").write_text(
            "transcribe:\n  max_cue_seconds: 3\n  min_cue_seconds: 3\n", encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            load_config(root=tmp)

    def test_long_segment_is_split_by_time(self):
        """SUB-AC1: сегмент 0–20 с з текстом на 2 рядки → ≥ 3 титри по ≤ 7 с, що разом покривають 0–20 с."""
        text = "Сьогодні ми розберемо, як працює Docker і навіщо потрібні контейнери у розробці"
        self.assertLessEqual(len(text), 2 * 42)
        cues = layout_cues([Segment(0.0, 20.0, text)], self.RULES).cues
        self.assertGreaterEqual(len(cues), 3)
        for cue in cues:
            self.assertLessEqual(cue.end - cue.start, 7.0 + 1e-6)
        self.assertEqual(cues[0].start, 0.0)
        self.assertAlmostEqual(cues[-1].end, 20.0, places=2)
        for prev, cur in zip(cues, cues[1:]):
            self.assertAlmostEqual(prev.end, cur.start, places=3)
        self.assertEqual(" ".join(c.text.replace("\n", " ") for c in cues), text)

    def test_split_prefers_sentence_then_clause(self):
        """SUB-AC2: межа титру — після крапки; без крапок — після коми, а не посеред фрази."""
        rules = SubtitleRules(max_chars=36, max_lines=1)
        cues = layout_cues(
            [Segment(0.0, 8.0, "Перше речення про контейнери тут. Друге речення про образи та шари.")], rules
        ).cues
        self.assertEqual([c.text for c in cues],
                         ["Перше речення про контейнери тут.", "Друге речення про образи та шари."])

        cues = layout_cues(
            [Segment(0.0, 8.0, "Коли ми запускаємо контейнер, Docker створює ізольований процес")], rules
        ).cues
        self.assertEqual(cues[0].text, "Коли ми запускаємо контейнер,")

    def test_lines_are_balanced(self):
        """SUB-AC3: довший рядок мінімальний; кома має перевагу лише в межах 30 %."""
        def lines(text, max_chars):
            rules = SubtitleRules(max_chars=max_chars, max_lines=2)
            cues = layout_cues([Segment(0.0, 6.0, text)], rules).cues
            self.assertEqual(len(cues), 1, cues)
            return cues[0].lines

        # жадібне перенесення дало б 25/15; збалансоване — 19/21
        self.assertEqual(lines("один два три чотири п'ять шість сім вісім", 30),
                         ("один два три чотири", "п'ять шість сім вісім"))
        # кома гірша за найкращий розрив на 25/23 ≈ 9 % → обираємо кому
        self.assertEqual(lines("Контейнери легкі, а віртуальні машини важчі", 30),
                         ("Контейнери легкі,", "а віртуальні машини важчі"))
        # кома гірша на 33/23 ≈ 43 % → баланс важливіший
        self.assertEqual(lines("Добре, тепер запускаємо контейнер і логи", 34),
                         ("Добре, тепер запускаємо", "контейнер і логи"))
        # коротке — один рядок
        self.assertEqual(lines("Привіт усім", 30), ("Привіт усім",))

    def test_dense_cue_extends_into_silence(self):
        """SUB-AC4: щільний титр подовжується в тишу до cps ≤ 17, але не далі за наступний."""
        layout = layout_cues(
            [Segment(0.0, 1.0, "Дуже щільний текст на одну секунду"), Segment(3.0, 5.0, "Далі.")],
            self.RULES,
        )
        first, second = layout.cues
        self.assertGreater(first.end, 1.0)
        self.assertLessEqual(_cps(first), 17.0 + 1e-3)
        self.assertLessEqual(first.end, second.start)
        self.assertEqual(layout.dense, 0)

    def test_dense_cue_without_silence_is_counted(self):
        """SUB-AC5: тиші немає → титр не змінено, лічильник «щільних» = 1."""
        layout = layout_cues(
            [Segment(0.0, 1.0, "Дуже щільний текст на одну секунду"),
             Segment(1.0, 3.0, "Далі йде наступна фраза.")],
            self.RULES,
        )
        self.assertEqual((layout.cues[0].start, layout.cues[0].end), (0.0, 1.0))
        self.assertEqual(layout.dense, 1)

    def test_short_gaps_are_closed(self):
        """SUB-AC6: пауза 0.2 с закривається, пауза 1 с лишається."""
        cues = layout_cues([Segment(0, 2, "Перша фраза."), Segment(2.2, 4, "Друга фраза.")], self.RULES).cues
        self.assertEqual(cues[0].end, 2.2)
        cues = layout_cues([Segment(0, 2, "Перша фраза."), Segment(3, 5, "Друга фраза.")], self.RULES).cues
        self.assertEqual(cues[0].end, 2.0)

    def test_properties_on_random_transcripts(self):
        """SUB-AC7: порядок, без накладань, ліміти рядків і тривалості, жодне слово не загублене."""
        vocab = ("контейнер образ Docker мережа том шар процес ізоляція швидко дуже просто "
                 "а і чи бо, тому. далі! чому? так, ні.").split()
        rng = random.Random(7)
        for case in range(200):
            rules = SubtitleRules(max_chars=rng.choice([20, 32, 42]), max_lines=rng.choice([1, 2, 3]))
            segments, t = [], 0.0
            for _ in range(rng.randint(1, 12)):
                t += rng.choice([0.0, 0.0, 0.1, 0.25, 0.6, 2.0])
                dur = rng.uniform(0.5, 25.0)
                words = [rng.choice(vocab) for _ in range(rng.randint(1, 20))]
                segments.append(Segment(round(t, 3), round(t + dur, 3), " ".join(words)))
                t += dur
            cues = layout_cues(segments, rules, duration=t + 1.0).cues
            with self.subTest(case=case):
                for i, cue in enumerate(cues):
                    nxt = cues[i + 1] if i + 1 < len(cues) else None
                    self.assertLess(cue.start, cue.end)
                    self.assertLessEqual(len(cue.lines), rules.max_lines)
                    self.assertTrue(all(len(line) <= rules.max_chars for line in cue.lines), cue)
                    self.assertLessEqual(cue.end - cue.start, rules.max_seconds + 1e-3)
                    cut_by_next = nxt is not None and abs(cue.end - nxt.start) < 1e-3
                    self.assertTrue(cue.end - cue.start >= rules.min_seconds - 1e-3 or cut_by_next, cue)
                    if nxt:
                        self.assertLessEqual(cue.end, nxt.start + 1e-6)
                got = " ".join(" ".join(c.lines) for c in cues).split()
                want = " ".join(s.text for s in segments).split()
                self.assertEqual(got, want)

    def test_deterministic_and_fast(self):
        """SUB-AC8, §5: той самий вхід → той самий SRT; ~10 000 слів < 1 с."""
        words = ("Сьогодні ми говоримо про контейнери, образи і мережі. Це просто!").split()
        segments = [Segment(i * 4.0, i * 4.0 + 3.5, " ".join(words * 2)) for i in range(500)]
        started = time.perf_counter()
        first = to_srt(layout_cues(segments, self.RULES).cues)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(first, to_srt(layout_cues(segments, self.RULES).cues))
        self.assertGreaterEqual(sum(len(s.text.split()) for s in segments), 10_000)


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
