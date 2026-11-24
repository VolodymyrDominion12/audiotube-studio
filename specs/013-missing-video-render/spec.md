# 013 · Дорендер аудіо без відео

**Статус:** implemented · **Версія:** 1.0 · **Дата:** 2026-11-24
**Залежить від:** 002, 012 · **Модулі:** `cli.py`

## 1. Проблема

Після масового `sync` у черзі буває 200+ аудіо, а відрендереної частини може не бути.
`audiotube run` бере задачі за **статусом** (`new`, `transcribed`, `described`), тому не бачить
задачу, у якої статус `rendered`, але файл відео зник, і не дає явної гарантії «нічого не
заливати на YouTube». Потрібен режим, який обходить усі аудіо, дивиться на **фактичну
наявність відео** і доводить кожне без відео до рендеру, не торкаючись публікації.

## 2. User stories

- **US1** Як автор, я хочу однією командою дорендерити весь беклог аудіо, у якого ще немає
  відео, щоб уранці мати готові `output/<slug>.mp4`.
- **US2** Як автор, я хочу, щоб цей режим **ніколи** не завантажував нічого на YouTube, навіть
  якщо в конфігу ввімкнено `auto_upload`.
- **US3** Як автор, я хочу, щоб задачі з наявним відео не перерендерювались і не перезаписувались.

## 3. Поза межами

- Автоматичне планування запуску (cron/systemd) — окремо, поза застосунком.
- Повторне завантаження або заміна вже опублікованого відео на YouTube.
- Видалення старих файлів, паралельна обробка, зміна статусів назад.

## 4. Функціональні вимоги

| ID | Вимога |
|----|--------|
| FILL-R1 | `audiotube run --missing-video` МУСИТЬ сканувати `inbox/` і брати кожну задачу, у якої немає ні `video_id`, ні наявного локального файлу відео; статус задачі не має значення. |
| FILL-R2 | Для кожної вибраної задачі МУСИТЬ виконуватись ланцюг `transcribe → describe → render` з урахуванням `pipeline.auto_*`, наявного транскрипту й YAML. |
| FILL-R3 | `--missing-video` НЕ МОЖЕ завантажувати на YouTube: `youtube.auto_upload` ігнорується, а поєднання з `--upload` — помилка. |
| FILL-R4 | `--no-upload` МУСИТЬ вимикати публікацію для звичайного `run`, навіть якщо `youtube.auto_upload: true`; з `--upload` не поєднується. |
| FILL-R5 | Задачі з наявним локальним файлом відео або з `video_id` НЕ МОЖУТЬ рендеритись повторно й перезаписувати файл. |
| FILL-R6 | `--limit N` МУСИТЬ обмежувати кількість вибраних задач; порядок — за id (найстаріші). |
| FILL-R7 | Повторний запуск ідемпотентний: щойно відрендерені задачі вже мають відео і більше не вибираються; перерваний прогін продовжується з БД. |
| FILL-R8 | Статус задачі не відкочується назад; задача зі статусом `rendered`/`done` без файлу лишається в тому самому статусі після дорендеру. |
| FILL-R9 | Наприкінці `run` друкує стан черги (`status`), як і раніше. |

## 5. Нефункціональні вимоги

- Один процес, послідовна обробка; жодних паралельних Whisper/Ollama/FFmpeg.
- Вибір задачі — за фактичним файлом (`Path.is_file()`), а не за статусом.
- Жодних зовнішніх записів: режим не звертається до YouTube API.

## 6. Інтерфейси

CLI:

```bash
audiotube run --missing-video                 # усі аудіо без відео → рендер, без публікації
audiotube run --missing-video --limit 10      # те саме, але 10 задач за запуск
audiotube run --missing-video --force-describe  # + перегенерувати метадані LLM
audiotube run --no-upload                     # звичайний run без публікації
```

Файли й БД не змінюються: `output/<slug>.mp4`, `output/<slug>.jpg`, `workspace/<slug>/`,
`inbox/<аудіо>.yaml`, `data/audiotube.db`.

## 7. Критерії приймання

| ID | Given / When / Then | Тест |
|----|---------------------|------|
| FILL-AC1 | прапорці доступні; `--upload` і `--no-upload` взаємовиключні; `--missing-video --upload` — помилка | `test_missing_video.py::test_parser_accepts_missing_video_flags` |
| FILL-AC2 | **Given** статус `rendered`, файлу відео немає **When** `run --missing-video` **Then** відео відрендерено | `test_missing_video.py::test_rendered_job_without_file_is_rerendered` |
| FILL-AC3 | **Given** наявний файл відео **When** `run --missing-video` **Then** задача не чіпається | `test_missing_video.py::test_existing_video_is_never_touched` |
| FILL-AC4 | **Given** `auto_upload: true` **When** `run --missing-video` **Then** публікації немає | `test_missing_video.py::test_missing_video_never_uploads_even_with_auto_upload` |
| FILL-AC5 | **Given** `auto_upload: true` **When** `run --no-upload` **Then** публікації немає | `test_missing_video.py::test_no_upload_overrides_auto_upload` |
| FILL-AC6 | **Given** `video_id` заповнений **When** `run --missing-video` **Then** задача пропущена | `test_missing_video.py::test_job_with_youtube_video_is_skipped` |
| FILL-AC7 | **Given** 3 задачі без відео **When** `--limit 2` **Then** відрендерено 2 | `test_missing_video.py::test_limit_applies_to_missing_video_jobs` |

## 8. Відповідність конституції

I — LLM лише в `describe`, вибір і рендер — детермінований код; II — стан у SQLite, прогін
відновлюється; III — повторний запуск нічого не дублює; IV — наявне відео не перезаписується;
VI — жодних записів у YouTube, режим явно це гарантує; VII — тести без мережі, FFmpeg, Whisper
і LLM (усе замоковано); IX — повідомлення й документація українською.

## 9. Відкриті питання

Немає.
