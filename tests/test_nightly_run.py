"""Нічний пакетний прогін: `run --force-describe` і `run --limit` (spec 012)."""

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
from audiotube.metadata import load_meta
from audiotube.planner import ContentPlan
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


class NightlyRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.yaml").write_text(
            "render:\n  width: 320\n  height: 180\n  fps: 10\n  segment_seconds: 5\n"
            "llm:\n  provider: openai\n  model: fake\n"
            "transcribe:\n  backend: none\n",
            encoding="utf-8",
        )
        self.assertEqual(run_cli("--root", str(self.tmp), "init")[0], 0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_audio(self, name: str) -> Path:
        audio = self.tmp / "inbox" / name
        audio.write_bytes(b"fake-audio")
        audio.with_suffix(".yaml").write_text(
            "title: Назва з sync\nprivacy: private\nnotebook: test\n", encoding="utf-8"
        )
        return audio

    def test_parser_accepts_nightly_flags(self):
        """NIGHT-AC5: прапорці доступні у CLI."""
        args = build_parser().parse_args(["run", "--force-describe", "--limit", "5"])
        self.assertTrue(args.force_describe)
        self.assertEqual(args.limit, 5)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["run", "--limit", "0"])

    def test_run_force_describe_regenerates_sync_sidecar(self):
        """NIGHT-AC1: YAML від sync перегенеровується через LLM, ручні поля збережено."""
        audio = self._add_audio("Тема.mp3")
        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe), \
                mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan) as plan:
            code, out = run_cli("--root", str(self.tmp), "run", "--force-describe", "--no-render")

        self.assertEqual(code, 0, out)
        self.assertEqual(plan.call_count, 1)
        meta = load_meta(audio)
        self.assertEqual(meta.title, "Назва від LLM")
        self.assertEqual(meta.description, "Опис від LLM.")
        self.assertEqual(meta.privacy, "private")
        self.assertEqual([c.title for c in meta.chapters], ["Вступ", "Далі"])

    def test_run_without_flag_keeps_sync_sidecar(self):
        """NIGHT-AC2: без прапорця наявний YAML не перегенеровується."""
        audio = self._add_audio("Тема.mp3")
        before = audio.with_suffix(".yaml").read_text(encoding="utf-8")
        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe), \
                mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan) as plan:
            code, out = run_cli("--root", str(self.tmp), "run", "--no-render")

        self.assertEqual(code, 0, out)
        self.assertEqual(plan.call_count, 0)
        self.assertEqual(audio.with_suffix(".yaml").read_text(encoding="utf-8"), before)

    def test_force_describe_refreshes_already_described_job(self):
        """NIGHT-AC6: статус `described` не блокує повторну LLM-генерацію з --force-describe."""
        audio = self._add_audio("Тема.mp3")
        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe):
            self.assertEqual(run_cli("--root", str(self.tmp), "run", "--no-render")[0], 0)

        with mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan) as plan:
            code, out = run_cli("--root", str(self.tmp), "run", "--force-describe", "--no-render")

        self.assertEqual(code, 0, out)
        self.assertEqual(plan.call_count, 1)
        self.assertEqual(load_meta(audio).description, "Опис від LLM.")

    def test_limit_processes_only_n_jobs(self):
        """NIGHT-AC3: --limit 2 обробляє лише дві задачі з трьох."""
        for i in range(3):
            self._add_audio(f"Тема{i}.mp3")
        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe):
            code, out = run_cli(
                "--root", str(self.tmp), "run", "--limit", "2", "--no-describe", "--no-render"
            )

        self.assertEqual(code, 0, out)
        cfg = load_config(root=self.tmp)
        db = Database(cfg.db_path)
        try:
            statuses = [job.status for job in db.list()]
        finally:
            db.close()
        self.assertEqual(statuses, ["transcribed", "transcribed", "new"])

    def test_rendered_jobs_are_never_touched(self):
        """NIGHT-AC4: відрендерена задача не потрапляє в обробку."""
        self._add_audio("Готова.mp3")
        self._add_audio("Нова.mp3")
        self.assertEqual(run_cli("--root", str(self.tmp), "scan")[0], 0)

        cfg = load_config(root=self.tmp)
        db = Database(cfg.db_path)
        try:
            rendered = db.list()[0]
            db.update(rendered.id, status="rendered")
        finally:
            db.close()

        with mock.patch("audiotube.pipeline.transcribe_audio", side_effect=fake_transcribe), \
                mock.patch("audiotube.pipeline.generate_plan", side_effect=fake_plan) as plan:
            code, out = run_cli("--root", str(self.tmp), "run", "--force-describe", "--no-render")

        self.assertEqual(code, 0, out)
        self.assertEqual(plan.call_count, 1)
        db = Database(cfg.db_path)
        try:
            self.assertEqual(db.get(rendered.id).status, "rendered")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
