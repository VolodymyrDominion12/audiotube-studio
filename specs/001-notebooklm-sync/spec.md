# 001 · Синхронізація NotebookLM («Бібліотекар»)

**Статус:** implemented (as-built) · **Версія:** 1.0 · **Дата:** 2026-11-21
**Модулі:** `audiotube/notebooklm.py`, `audiotube/sync.py`, `audiotube/db.py`, `audiotube/cli.py`
**Детальний дизайн:** [docs/05_NOTEBOOKLM_PLAN.md](../../docs/05_NOTEBOOKLM_PLAN.md), розд. 4

## 1. Проблема

У NotebookLM десятки ноутбуків з аудіопереказами. Качати їх вручну, розкладати по папках і
стежити, що вже скачано, — довго і легко помилитися.

## 2. User stories

- **US1** Як автор, я хочу однією командою скачати всі аудіоперекази, щоб не клацати кожен вручну.
- **US2** Як автор, я хочу, щоб файли лежали в папках з назвами ноутбуків, щоб структура збігалася з NotebookLM.
- **US3** Як автор, я хочу запускати синк повторно без дублів, щоб його можна було ставити в розклад.
- **US4** Як автор, я хочу спершу побачити план без завантажень, щоб перевірити, що синк бачить правильні ноутбуки.
- **US5** Як автор, я хочу додати аудіо, скачане вручну, якщо неофіційний API зламався.

## 3. Поза межами

Видео-, слайд- та інші не-аудіо артефакти; генерація нових переказів у NotebookLM; чужі (shared)
ноутбуки за замовчуванням; плейлисти (→ [006](../006-notebook-playlists/spec.md)); версії
й зникнення артефактів (→ [005](../005-artifact-lifecycle/spec.md)).

## 4. Функціональні вимоги

| ID | Вимога |
|----|--------|
| SYNC-R1 | `audiotube sync` МУСИТЬ отримати список ноутбуків і їхніх аудіоартефактів через `Adapter`. |
| SYNC-R2 | Кожен ноутбук МУСИТЬ мати власну папку `inbox/<slug>/`; slug стабільний (зберігається в БД) і при колізії отримує суфікс з id ноутбука. |
| SYNC-R3 | Ім'я файлу: `<YYYY-MM-DD>_<slug назви>-<6 символів id>.m4a`. |
| SYNC-R4 | Поруч з аудіо МУСИТЬ з'явитися sidecar `<файл>.yaml` (title, privacy), а в папці — `notebook.json`. |
| SYNC-R5 | Завантаження йде в `.part`, перевіряється (не порожній, sha256, ffprobe) і лише потім атомарно перейменовується. |
| SYNC-R6 | Артефакт, уже записаний у БД і наявний на диску, НЕ МОЖЕ бути скачаний повторно (крім `--force`). |
| SYNC-R7 | Артефакт, що ще генерується (`ready=false`), пропускається. |
| SYNC-R8 | Якщо файл артефакту зник локально і `repair_missing: true` — перекачати. |
| SYNC-R9 | Shared-ноутбуки ігноруються, якщо `include_shared: false`. |
| SYNC-R10 | Збій одного артефакту не зупиняє решту; помилки збираються у звіт. |
| SYNC-R11 | Кожен новий файл реєструється як задача `jobs` зі статусом `new` і пов'язується з `artifacts.job_id`. |
| SYNC-R12 | `--dry-run` нічого не пише (ні файлів, ні БД) і показує повний план, ігноруючи `max_per_run`. |
| SYNC-R13 | `--limit N` / `max_per_run` обмежують кількість нових файлів за запуск. |
| SYNC-R14 | `--notebook Q` обмежує синк ноутбуками, чий id або назва містить Q. |
| SYNC-R15 | Загальні назви артефактів (напр. «Audio Overview») замінюються на «<ноутбук> — переказ <дата>». |
| SYNC-R16 | `audiotube import <шляхи> [--move]` копіює локальні аудіо в `inbox/` без дублів: окремий файл → `inbox/imported/`, тека → `inbox/<slug теки>/` зі збереженням структури; потім `scan`. |
| SYNC-R17 | Помилка автентифікації NotebookLM → код виходу 2 і підказка `notebooklm login`. |

## 5. Нефункціональні вимоги

- Завантаження послідовні, з паузою `delay_seconds` (конституція VI).
- Уся залежність від `notebooklm-py` — в `notebooklm.py` (конституція V).

## 6. Інтерфейси

- CLI: `audiotube sync [--dry-run] [--notebook Q] [--limit N] [--force]`, `audiotube import`.
- Конфіг: секція `notebooklm:` (`enabled`, `profile`, `home`, `include_shared`, `delay_seconds`,
  `max_per_run`, `repair_missing`, `filename_max_len`).
- БД: `notebooks(id, title, slug, url, …)`, `artifacts(id, notebook_id, path, sha256, bytes, duration, state, job_id, …)`.

## 7. Критерії приймання

| ID | Given / When / Then | Тест |
|----|---------------------|------|
| SYNC-AC1 | **Given** 2 ноутбуки з аудіо **When** `sync` **Then** файли в папках ноутбуків, задачі зареєстровані | `test_sync.py::test_downloads_and_registers` |
| SYNC-AC2 | **Given** успішний синк **When** повторний `sync` **Then** 0 нових завантажень | `test_sync.py::test_second_run_is_noop` |
| SYNC-AC3 | **When** `sync --dry-run` **Then** диск і БД не змінені | `test_sync.py::test_dry_run_writes_nothing` |
| SYNC-AC4 | **Given** `max_per_run=1` **When** `--dry-run` **Then** план показує всі артефакти | `test_sync.py::test_dry_run_ignores_max_per_run` |
| SYNC-AC5 | **When** `--limit N` **Then** скачано рівно N | `test_sync.py::test_limit_stops_after_n` |
| SYNC-AC6 | **Given** один артефакт падає **Then** інші скачані, помилка у звіті, `.part` прибрано | `test_sync.py::test_one_failure_does_not_stop_others` |
| SYNC-AC7 | **Given** файл видалено локально **When** `sync` **Then** перекачано | `test_sync.py::test_missing_file_is_repaired` |
| SYNC-AC8 | **Given** артефакт не готовий **Then** пропущено | `test_sync.py::test_not_ready_artifact_is_skipped` |
| SYNC-AC9 | **Given** два ноутбуки «Docker» **Then** різні папки, друга з id-суфіксом | `test_sync.py::test_slug_collision_gets_id_suffix` |
| SYNC-AC10 | **Given** назва «Audio Overview» **Then** назва з ноутбука й дати | `test_sync.py::test_generic_title_is_replaced` |
| SYNC-AC11 | **Given** shared-ноутбук **Then** ігнорується | `test_sync.py::test_shared_notebooks_ignored_by_default` |
| SYNC-AC12 | **Given** прострочена сесія **Then** exit 2 | `test_sync.py::test_auth_error_exits_2` |
| SYNC-AC13 | **Given** файли в підпапках inbox **When** `scan` **Then** задачі створені | `test_sync.py::test_scan_sees_notebook_subfolders` |
| SYNC-AC14 | **When** `import` двічі **Then** другий раз — пропуск | `test_sync.py::test_import_copies_audio_and_skips_duplicates` |

## 8. Відповідність конституції

I — без LLM; III — ключ `artifact_id`; IV — `.part` + `os.replace`; V — `Adapter`/`FakeAdapter`;
VI — паузи, `max_per_run`, `--dry-run`; VII — тести на `FakeAdapter`.

## 9. Відомі прогалини (as-built vs дизайн)

Дизайн у docs/05 (розд. 4.2) передбачає стани `superseded` (той самий id, інший хеш) і `missing`
(артефакт зник з NotebookLM), а також `--rename-folders`. Код їх **не реалізує** — винесено в
[005](../005-artifact-lifecycle/spec.md).
