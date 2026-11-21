# 002 · Конвеєр аудіо → відео

**Статус:** implemented (as-built) · **Версія:** 1.0 · **Дата:** 2026-11-21
**Модулі:** `pipeline.py`, `transcribe.py`, `transcript.py`, `planner.py`, `llm.py`, `metadata.py`, `visuals.py`, `render.py`
**Детальний дизайн:** [docs/01_CONCEPT.md](../../docs/01_CONCEPT.md), [docs/04_LLM_AND_WHISPER.md](../../docs/04_LLM_AND_WHISPER.md)

## 1. Проблема

Аудіо саме по собі не можна залити на YouTube: потрібні відеоряд, обкладинка, назва, опис,
теги, глави й субтитри. Робити це вручну для кожного переказу — години.

## 2. User stories

- **US1** Як автор, я хочу, щоб з аудіо автоматично вийшло 1080p-відео з фоном і назвою.
- **US2** Як автор, я хочу отримати транскрипт і субтитри українською.
- **US3** Як автор, я хочу, щоб LLM запропонувала назву, опис, теги й глави, а я міг їх відредагувати в YAML.
- **US4** Як автор, я хочу швидке прев'ю на 20 с, щоб перевірити кадр до повного рендеру.

## 3. Поза межами

Зовнішні стоки фонів (Pexels/Pixabay), слайди по главах, аудіовізуалізатор, пресети стилів —
roadmap у [docs/03](../../docs/03_ANALYSIS_AND_NEXT_STEPS.md#7-що-робити-далі-за-пріоритетом).

## 4. Функціональні вимоги

| ID | Вимога |
|----|--------|
| PIPE-R1 | Статуси задачі йдуть лише вперед: `new → transcribed → described → rendered → uploaded → done`. |
| PIPE-R2 | `transcribe` пише `workspace/<slug>/transcript.json` і `subtitles.srt`; бекенд: faster-whisper / whisper.cpp / OpenAI-сумісний / none. |
| PIPE-R3 | Порожній транскрипт — помилка, а не успіх. |
| PIPE-R4 | `describe` генерує через LLM назву, опис, теги, глави, `visual_keywords` і пише sidecar YAML. |
| PIPE-R5 | Невалідний JSON від LLM нормалізується; за потреби — `llm.retries` спроб виправлення. |
| PIPE-R6 | Глави: відсортовані, без дублів, перша з 0:00, не за межами тривалості. |
| PIPE-R7 | `describe --force` зберігає ручні поля (privacy, background, mode, category, …). |
| PIPE-R8 | `render` створює `output/<slug>.mp4` + `.jpg`; фон — з `assets/` за категорією/ключовими словами, або згенерований абстрактний. |
| PIPE-R9 | Рендер працює, навіть якщо транскрипція/опис не вдалися (необов'язкові кроки). |
| PIPE-R10 | Повторний `render` без `--force` нічого не робить. |

## 6. Інтерфейси

CLI: `scan`, `transcribe`, `describe [--force]`, `render [--preview S] [--force]`, `run`, `watch`.
Конфіг: `render:`, `visuals:`, `transcribe:`, `llm:`, `plan:`, `pipeline:`.

## 7. Критерії приймання

| ID | Given / When / Then | Тест |
|----|---------------------|------|
| PIPE-AC1 | **Given** аудіо **When** повний ланцюг **Then** відео, обкладинка, субтитри | `test_pipeline_steps.py::test_full_chain_and_captions` |
| PIPE-AC2 | статус ніколи не відкочується | `test_pipeline_steps.py::test_status_never_goes_backwards` |
| PIPE-AC3 | `describe --force` зберігає ручні поля | `test_pipeline_steps.py::test_describe_force_preserves_manual_settings`, `test_planner.py::test_merge_preserves_manual_fields` |
| PIPE-AC4 | рендер іде попри збій необов'язкових кроків | `test_pipeline_steps.py::test_render_works_when_optional_steps_fail` |
| PIPE-AC5 | рендер і публікація ідемпотентні | `test_pipeline.py::test_render_and_publish_idempotent` |
| PIPE-AC6 | транскрипт + SRT пишуться і не перегенеровуються | `test_transcribe.py::test_writes_transcript_and_srt_and_is_idempotent` |
| PIPE-AC7 | порожній транскрипт — помилка | `test_transcribe.py::test_empty_transcript_is_an_error` |
| PIPE-AC8 | «брудний» JSON від LLM нормалізується, ретрай при невалідному | `test_planner.py::test_messy_llm_json_is_normalised`, `::test_retries_on_invalid_json` |
| PIPE-AC9 | глави нормалізуються | `test_planner.py::test_sort_dedup_and_zero_start`, `::test_clamps_beyond_duration` |

## 8. Відповідність конституції

I — LLM лише в `describe`; II — статуси в `jobs`; III — ідемпотентні кроки; VII — LLM/Whisper
у тестах замінені фейками; VIII — Whisper і Ollama локальні за замовчуванням.
