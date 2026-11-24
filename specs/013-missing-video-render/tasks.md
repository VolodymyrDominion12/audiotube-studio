# Задачі · 013 · Дорендер аудіо без відео

Кожна задача — тест-першим: спершу тест на критерій приймання, потім код.

- [x] **T1** Тести FILL-AC1…AC7 у `tests/test_missing_video.py` (фейкові transcribe/LLM/render).
- [x] **T2** `cli._has_video()` і режим `_process(missing_video=True, no_upload=True)`: вибір за
  фактичним файлом/`video_id`, транскрипт за потреби, рендер із `force`, жодної публікації.
- [x] **T3** CLI: `run --missing-video`, `run --no-upload` (взаємовиключний з `--upload`),
  помилка на `--missing-video --upload`.
- [x] **T4** Документація: `README.md`, `docs/07_COMMANDS.md`, реєстр `specs/README.md`.
- [x] **T5** `.venv/bin/python -m pytest -q` — зелене (188 тестів).
