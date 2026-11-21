# AudioTube Studio — правила для Claude Code

Проєкт ведеться за spec-driven development. Деталі: `specs/README.md`, принципи: `specs/constitution.md`.

- Перед зміною поведінки знайди спеку в `specs/NNN-*/spec.md`. Немає спеки — спершу напиши її за `specs/_templates/`.
- Реалізуй задачі з `tasks.md` по одній: спершу тест на критерій приймання (id у docstring, напр. `"""SYNC-AC2: …"""`), потім код.
- Спеки з `[NEEDS CLARIFICATION]` не реалізуй — спершу спитай користувача.
- Тести: `.venv/bin/python -m pytest -q` (без мережі, FFmpeg, Whisper і LLM).
- Код, що зачіпає NotebookLM, — лише через `Adapter` в `audiotube/notebooklm.py`.
- Мова повідомлень CLI, документації та спек — українська.
