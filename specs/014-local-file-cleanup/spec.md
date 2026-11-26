# 014 · Локальне очищення аудіо та відео

**Статус:** implemented · **Версія:** 1.0 · **Дата:** 2026-11-26
**Залежить від:** 001, 003, 005, 008 · **Модулі:** `db.py`, `sync.py`, `webui.py`, `daemon.py`, `cli.py`

## 1. Проблема

Після масового синку `inbox/<ноутбук>/` заповнюється аудіопереказами, а `output/` — готовими
`.mp4`; разом це з'їдає десятки гігабайт. Видаляти файли вручну небезпечно: `sync` бачить, що
файлу з відомим `artifact_id` немає, і знову його качає (`repair_missing`), а разом із ним гине
інформація, що переказ уже був скачаний / відео вже на YouTube.

Потрібне керування файлами з веб-UI: окремо видалити аудіо або відео, зберігши в SQLite історію
та зв'язок із NotebookLM і YouTube.

## 2. User stories

- **US1** Як автор, я хочу видалити аудіо конкретного переказу після публікації, щоб звільнити
  місце, не втративши задачу, метадані та посилання на YouTube.
- **US2** Як автор, я хочу, щоб наступний `sync` **не** качав це аудіо повторно, навіть якщо
  увімкнено `repair_missing`, і навіть `sync --force`.
- **US3** Як автор, я хочу окремо видаляти локальний `.mp4`, залишаючи на YouTube уже
  опубліковане відео та його `video_id`.
- **US4** Як автор, я хочу бачити в UI, що аудіо/відео немає на диску, скільки місця вони
  займають, і мати кнопку «Відновити аудіо», якщо видалив помилково.

## 3. Поза межами

- Видалення переказів у NotebookLM або відео на YouTube (лише локальні файли).
- Видалення рядків `artifacts`/`jobs`, YAML-метаданих, транскриптів, субтитрів і обкладинок.
- Автоматичні політики зберігання (напр. «видаляти аудіо через N днів»), масові операції
  та вибір кількох задач одночасно.
- Видалення файлів поза керованими теками `inbox/` і `output/`.
- Заміна або повторна публікація відео на YouTube.

## 4. Функціональні вимоги

| ID | Вимога |
|----|--------|
| FILE-R1 | Веб-UI МУСИТЬ давати для кожної задачі окремі дії «Видалити аудіо» та «Видалити відео» (з підтвердженням) і показувати, що файл видалено вручну. |
| FILE-R2 | Видалення аудіо МУСИТЬ: видалити файл `job.audio` (і `*.part` поруч) лише в межах `inbox/`; встановити `artifacts.audio_deleted=1` для всіх артефактів, пов'язаних із задачею (`job_id` або шлях аудіо); зберегти рядок `artifacts` (`sha256`, `bytes`, `downloaded_at`), `jobs` (статус, `video_id`), YAML-метадані, транскрипт і субтитри. |
| FILE-R3 | `sync` НЕ МОЖЕ завантажувати артефакт із `audio_deleted=1` — ні звичайний, ні `--force`, ні `repair_missing`. Такий артефакт МУСИТЬ потрапляти у звіт окремим лічильником `deleted` (і в dry-run, і в справжньому прогоні). |
| FILE-R4 | Дія «Відновити аудіо» МУСИТЬ знімати `audio_deleted`; наступний `sync` МУСИТЬ перекачати файл (штатний `repair`), якщо `repair_missing` увімкнено. |
| FILE-R5 | Видалення відео МУСИТЬ: видалити `jobs.video`, `output/<slug>.mp4` і `output/<slug>_preview.mp4` (лише в межах `output/`); встановити `jobs.video=NULL`; зберегти `video_id`, `playlist_id`, `captions_id`, `thumbnail`, `status`, посилання та записи `uploads`. |
| FILE-R6 | Обидві дії ідемпотентні: якщо файлу вже немає — дія успішна, прапорець/поле встановлюється, у відповіді пояснення. |
| FILE-R7 | Файл, що лежить поза своєю керованою текою, НЕ МОЖЕ бути видалений через UI: API повертає помилку, файл і БД не змінюються. |
| FILE-R8 | Автоматичні етапи, яким потрібне аудіо (`transcribe`, `describe`, `render` у `run`/`watch`/`daemon`), НЕ МОЖУТЬ вибирати задачу з `audio_deleted=1`; публікація вже відрендереного відео такої задачі не блокується. |
| FILE-R9 | `GET /api/state` МУСИТЬ віддавати для задачі `audio_exists`, `audio_size`, `audio_deleted`, `video_exists`, `video_size`; `files` — додатково `inbox_bytes` і `output_bytes`. `GET /api/notebooks` і `GET /api/notebooks/<id>` — лічильник/позначку видаленого аудіо. |
| FILE-R10 | Усі зміни стану — лише POST; GET нічого не змінює. |

## 5. Нефункціональні вимоги

- Один SQLite-запит на список задач для прапорців; розміри файлів — `stat` наявних файлів.
- Видалення не звертається до NotebookLM чи YouTube API.
- Файл спершу видаляється з диска, потім фіксується прапорець; якщо `unlink` упав — БД не
  змінюється (помилка повертається користувачу).
- Одночасний `sync` із зовнішнього процесу не блокується: у найгіршому разі файл буде
  скачано ще раз, але прапорець збережеться і наступний синк його пропустить.

## 6. Інтерфейси

**БД (міграція через `Database._migrate`):**

```sql
ALTER TABLE artifacts ADD COLUMN audio_deleted INTEGER NOT NULL DEFAULT 0;
```

`state` артефакту (`downloaded`/`missing`) і далі описує лише наявність у NotebookLM;
`audio_deleted` — локальну історію, тому прапорці не конфліктують.

**Веб-API:**

```
POST /api/jobs/<id>/delete_audio     # файл + прапорець; відповідь {ok, job, log, message}
POST /api/jobs/<id>/delete_video     # локальні mp4 + jobs.video=NULL
POST /api/jobs/<id>/restore_audio    # audio_deleted=0, файл не качає
GET  /api/state                      # job.audio_exists/audio_size/audio_deleted/video_*; files.*_bytes
GET  /api/notebooks[/<id>]           # deleted, audio_deleted
```

**CLI:** `audiotube sync` друкує `видалено локально: N` і рядок `×` для кожного пропущеного
артефакту. Нових команд немає.

## 7. Критерії приймання

| ID | Given / When / Then | Тест |
|----|---------------------|------|
| FILE-AC1 | **Given** синкнуте аудіо з відео **When** POST `delete_audio` **Then** файлу немає, `audio_deleted=1`, `sha256`/`downloaded_at`/`video_id`/YAML збережені | `test_file_cleanup.py::FileCleanupTests::test_delete_audio_keeps_history` |
| FILE-AC2 | **Given** аудіо видалено **When** `sync` (і `sync --force`) **Then** файл не качається, звіт `deleted=1` | `test_file_cleanup.py::FileCleanupTests::test_sync_does_not_redownload_deleted_audio`, `::test_force_skips_deleted_audio` |
| FILE-AC3 | **Given** аудіо видалено **When** `restore_audio` + `sync` **Then** прапорець знято, файл скачано знову | `test_file_cleanup.py::FileCleanupTests::test_restore_downloads_again` |
| FILE-AC4 | **Given** відрендерене відео з `video_id` **When** POST `delete_video` **Then** mp4 і preview зникли, `video=None`, `video_id`/`status`/`uploads` без змін | `test_file_cleanup.py::FileCleanupTests::test_delete_video_keeps_youtube_info` |
| FILE-AC5 | **Given** файл поза `inbox/`/`output/` **When** відповідна дія **Then** 400, файл на місці, БД без змін | `test_file_cleanup.py::FileCleanupTests::test_never_deletes_outside_managed_dirs` |
| FILE-AC6 | **When** повторити видалення **Then** успіх, файлів немає, прапорці ті самі | `test_file_cleanup.py::FileCleanupTests::test_delete_is_idempotent` |
| FILE-AC7 | **When** GET `/api/state` і `/` **Then** є `audio_exists`/`audio_size`/`audio_deleted`/`video_*`, розміри тек, кнопки видалення й відновлення | `test_file_cleanup.py::FileCleanupTests::test_state_and_page_expose_file_actions` |
| FILE-AC8 | **Given** аудіо видалено **When** `sync --dry-run` **Then** `deleted=1`, БД і диска не змінено | `test_file_cleanup.py::FileCleanupTests::test_dry_run_reports_deleted` |
| FILE-AC9 | **Given** задача з видаленим аудіо **When** демон планує цикл **Then** незавершену задачу не бере, а відрендерену — бере для публікації | `test_file_cleanup.py::FileCleanupTests::test_daemon_skips_audio_less_unfinished_job` |
| FILE-AC10 | **Given** стара БД без колонки **When** відкрити `Database` **Then** `audio_deleted` додано зі значенням 0 | `test_db_config.py::DatabaseTests::test_migration_adds_artifact_audio_deleted` |

## 8. Відповідність конституції

I — жодних LLM у видаленні, це детермінований код; II — прапорці й історія в SQLite, після
рестарту поведінка та сама; III — повторне видалення/синк ідемпотентні; IV — файли видаляються
лише явною дією з підтвердженням, метадані й історія лишаються, поза керованими теками нічого
не чіпаємо; V — NotebookLM не звертаємось; VI — жодних записів у YouTube; VII — тести без
мережі, FFmpeg, Whisper і LLM; IX — UI, повідомлення й документація українською.

## 9. Рішення

- **2026-11-26:** прапорець `artifacts.audio_deleted`, а не нове значення `state`. `state`
  відповідає на питання «чи є переказ у NotebookLM», і видалення локального файлу не має
  робити артефакт `missing` (він же не зник із NotebookLM). Прапорець ортогональний: переказ
  може бути `missing` і одночасно видаленим локально.
- **2026-11-26:** YAML-метадані не видаляємо — вони малі, потрібні для повторного рендеру й
  публікації, і саме вони зберігають «що це був за переказ» без аудіо.
- **2026-11-26:** «Відновити аудіо» лише знімає прапорець; завантаження робить звичайний
  `sync` (окремої кнопки синку вже достатньо, і так само працює `repair_missing`).
- **2026-11-26:** видалення не бере `data/daemon.lock`: інакше при постійно запущеному демоні
  кнопка була б завжди недоступна. Гонка з синком безпечна (див. §5).
- **2026-11-26:** «Видалити відео» прибирає і `*_preview.mp4`, бо це теж локальне відео;
  обкладинка (`*.jpg`) лишається.
