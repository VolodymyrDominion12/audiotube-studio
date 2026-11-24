# 003 · Публікація на YouTube

**Статус:** implemented (as-built) · **Версія:** 1.0 · **Дата:** 2026-11-21
**Модулі:** `youtube.py`, `pipeline.Pipeline.publish`, `db.QuotaTracker`
**Детальний дизайн:** [docs/02_YOUTUBE_SETUP.md](../../docs/02_YOUTUBE_SETUP.md)

## 1. Проблема

Готове відео треба завантажити з метаданими, обкладинкою й субтитрами, не перевищивши
квоту YouTube Data API (10 000 од./добу) і не завантаживши те саме двічі.

## 2. User stories

- **US1** Як автор, я хочу одноразово авторизуватися і далі завантажувати без браузера.
- **US2** Як автор, я хочу, щоб відео за замовчуванням були приватні, з позначкою AI-контенту.
- **US3** Як автор, я хочу, щоб обкладинка і субтитри ставились автоматично.

## 4. Функціональні вимоги

| ID | Вимога |
|----|--------|
| YT-R1 | OAuth (Desktop app), токен у `data/token.json`; `audiotube auth` — інтерактивно, решта — без браузера. |
| YT-R2 | Тіло відео: назва, опис (з footer), теги, категорія, мова, `privacy`, `madeForKids`, `containsSyntheticMedia`. |
| YT-R3 | `publish_at` без часового поясу — помилка. |
| YT-R4 | Перед кожним викликом API перевіряється й списується квота (`upload_cost`, `captions_cost`); при нестачі — зупинка без виклику. |
| YT-R5 | Resumable upload з експоненційним backoff на тимчасових помилках. |
| YT-R6 | Після upload: статус `uploaded` + `video_id`; далі обкладинка, субтитри (якщо `upload_captions`), плейлист (лише якщо `manage_playlists: true`) → `done`. |
| YT-R7 | Задача в статусі `done` повторно не завантажується; задача в `uploaded` догружає лише кроки, що лишились. |
| YT-R8 | Кожна публікація пишеться в таблицю `uploads` (для денного бюджету демона). |

## 6. Інтерфейси

CLI: `auth`, `upload`, `captions`, `playlists`. Конфіг: `youtube:`.

## 7. Критерії приймання

| ID | Given / When / Then | Тест |
|----|---------------------|------|
| YT-AC1 | **Given** `manage_playlists: false` **Then** плейлисти не чіпаються | `test_publish_playlist.py::test_playlist_disabled` |
| YT-AC2 | **Given** `manage_playlists: true` **Then** відео додано в плейлист | `test_publish_playlist.py::test_playlist_enabled_still_works` |
| YT-AC3 | повторна публікація не дублює відео | `test_pipeline.py::test_render_and_publish_idempotent` |
| YT-AC4 | субтитри завантажуються з транскрипту | `test_pipeline_steps.py::test_full_chain_and_captions` |
| YT-AC5 | **Given** у каналу кілька сторінок плейлистів **When** `playlists` **Then** повернуто всі | `test_youtube_playlists.py::test_collects_all_pages` |
| YT-AC6 | **Given** плейлист з такою назвою вже є **Then** він переюзується, новий не створюється | `test_youtube_playlists.py::test_ensure_playlist_reuses_existing` |

## 8. Відповідність конституції

VI — `private` і `containsSyntheticMedia` за замовчуванням, квота ніколи не перевищується;
VII — API YouTube у тестах замінено фейком; VIII — секрети в `data/` (у `.gitignore`).

## 9. Відомі ризики

OAuth-застосунок у статусі «Testing» → refresh-токен живе 7 днів; неаудитований API-проєкт
не може публікувати `public`. Див. docs/05, розд. 7.
