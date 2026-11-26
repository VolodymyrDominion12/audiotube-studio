# 014 · Задачі

Кожна задача: тест першим (червоний) → код (зелений) → `pytest -q` для всього набору.
Задачі з `[P]` незалежні й можуть іти паралельно.

- [x] **T1** Міграція `artifacts.audio_deleted` + `set_audio_deleted`/`audio_deleted_job_ids`, `save_artifact` скидає прапорець — AC: FILE-AC10 — тест: `tests/test_db_config.py::DatabaseTests::test_migration_adds_artifact_audio_deleted`
- [x] **T2** `sync` пропускає `audio_deleted=1` (dry-run, `--force`), `SyncReport.deleted`, CLI-звіт — AC: FILE-AC2, FILE-AC8 — тест: `tests/test_file_cleanup.py::FileCleanupTests::test_sync_does_not_redownload_deleted_audio`, `::test_force_skips_deleted_audio`, `::test_dry_run_reports_deleted`
- [x] **T3** [P] Дії `delete_audio`/`restore_audio` у `WebApp` + безпека шляхів — AC: FILE-AC1, FILE-AC3, FILE-AC5, FILE-AC6 — тест: `::test_delete_audio_keeps_history`, `::test_restore_downloads_again`, `::test_never_deletes_outside_managed_dirs`, `::test_delete_is_idempotent`
- [x] **T4** [P] Дія `delete_video` (mp4 + preview, `jobs.video=NULL`) — AC: FILE-AC4 — тест: `::test_delete_video_keeps_youtube_info`
- [x] **T5** [P] Поля `job_json`/`files`/`notebook_*` + кнопки й розміри в HTML — AC: FILE-AC7 — тест: `::test_state_and_page_expose_file_actions`
- [x] **T6** Демон і `run/watch` не беруть незавершені задачі без аудіо — AC: FILE-AC9 — тест: `::test_daemon_skips_audio_less_unfinished_job`
- [x] **T7** Оновити docs/ (05, 07), README, `specs/README.md`, статус спеки → implemented
