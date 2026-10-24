import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiotube.config import Config
from audiotube.transcribe import (
    TranscriptionError,
    _multipart,
    _run_whisper_cli,
    backend_report,
    resolve_backend,
    transcribe_audio,
)
from audiotube.transcript import Segment, Transcript


class BackendSelectionTests(unittest.TestCase):
    def test_none_backend(self):
        cfg = Config()
        cfg.transcribe.backend = "none"
        with self.assertRaises(TranscriptionError):
            resolve_backend(cfg)

    def test_explicit_backend(self):
        cfg = Config()
        cfg.transcribe.backend = "whisper-cli"
        self.assertEqual(resolve_backend(cfg), "whisper-cli")

    def test_auto_without_anything(self):
        cfg = Config()
        cfg.transcribe.backend = "auto"
        with mock.patch("audiotube.transcribe._faster_whisper_available", return_value=False), \
             mock.patch("audiotube.transcribe._whisper_cli_command", return_value=None), \
             mock.patch("audiotube.transcribe._openai_backend_ready", return_value=False):
            with self.assertRaises(TranscriptionError) as ctx:
                resolve_backend(cfg)
        self.assertIn("faster-whisper", str(ctx.exception))

    def test_backend_report_shape(self):
        cfg = Config()
        report = backend_report(cfg)
        self.assertEqual([name for name, _, _ in report], ["faster-whisper", "whisper-cli", "openai"])


class TranscribeFlowTests(unittest.TestCase):
    def test_writes_transcript_and_srt_and_is_idempotent(self):
        cfg = Config()
        cfg.transcribe.backend = "openai"
        cfg.transcribe.base_url = "http://localhost:9/v1"
        fake = Transcript(
            language="uk", duration=12.0,
            segments=[Segment(0, 5, "Привіт, це тест."), Segment(5, 12, "Друге речення.")],
            model="small",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            audio = tmp / "a.mp3"
            audio.write_bytes(b"fake")
            workdir = tmp / "work"
            with mock.patch("audiotube.transcribe._run_openai", return_value=fake) as run:
                result = transcribe_audio(audio, workdir, cfg, log=lambda *_: None)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(result.transcript.backend, "openai")
                self.assertTrue(result.json_path.exists())
                self.assertTrue(result.srt_path.exists())
                srt = result.srt_path.read_text(encoding="utf-8")
                self.assertIn("Привіт", srt)
                self.assertIn("00:00:05,000", srt)

                cached = transcribe_audio(audio, workdir, cfg, log=lambda *_: None)
                self.assertEqual(run.call_count, 1)  # кеш: бекенд не викликався
                self.assertEqual(cached.transcript.text, fake.text)

                forced = transcribe_audio(audio, workdir, cfg, force=True, log=lambda *_: None)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(forced.transcript.text, fake.text)

    def test_empty_transcript_is_an_error(self):
        cfg = Config()
        cfg.transcribe.backend = "openai"
        cfg.transcribe.base_url = "http://localhost:9/v1"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            audio = tmp / "a.mp3"
            audio.write_bytes(b"fake")
            with mock.patch(
                "audiotube.transcribe._run_openai",
                return_value=Transcript(language="uk", duration=0, segments=[]),
            ):
                with self.assertRaises(TranscriptionError):
                    transcribe_audio(audio, tmp / "w", cfg, log=lambda *_: None)


class WhisperCppParsingTests(unittest.TestCase):
    def test_parses_offsets(self):
        cfg = Config()
        cfg.transcribe.backend = "whisper-cli"
        cfg.transcribe.command = sys.executable
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            model = tmp / "ggml-small.bin"
            model.write_bytes(b"model")
            audio = tmp / "a.mp3"
            audio.write_bytes(b"fake")
            cfg.transcribe.model_path = str(model)

            def fake_run(cmd, **kwargs):
                prefix = Path(cmd[cmd.index("-of") + 1])
                Path(f"{prefix}.json").write_text(json.dumps({
                    "transcription": [
                        {"offsets": {"from": 0, "to": 1500}, "text": " перше"},
                        {"offsets": {"from": 1500, "to": 3000}, "text": " друге"},
                    ],
                    "result": {"language": "uk"},
                }), encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch("audiotube.transcribe.subprocess.run", side_effect=fake_run):
                transcript = _run_whisper_cli(audio, tmp, cfg, log=lambda *_: None)

        self.assertEqual(transcript.language, "uk")
        self.assertAlmostEqual(transcript.duration, 3.0)
        self.assertEqual([s.text for s in transcript.segments], ["перше", "друге"])

    def test_missing_model_path(self):
        cfg = Config()
        cfg.transcribe.backend = "whisper-cli"
        cfg.transcribe.command = sys.executable
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TranscriptionError) as ctx:
                _run_whisper_cli(Path(tmp) / "a.mp3", Path(tmp), cfg, log=lambda *_: None)
        self.assertIn("model_path", str(ctx.exception))


class MultipartTests(unittest.TestCase):
    def test_body_contains_fields_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "voice.mp3"
            audio.write_bytes(b"12345")
            body, content_type = _multipart({"model": "small", "language": "uk"}, {"file": audio})
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="model"', body)
        self.assertIn(b"small", body)
        self.assertIn(b'filename="voice.mp3"', body)
        self.assertIn(b"12345", body)


if __name__ == "__main__":
    unittest.main()
