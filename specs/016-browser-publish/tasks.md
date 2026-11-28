# 016 · Задачі

Кожна задача: тест першим (червоний) → код (зелений) → `pytest -q` для всього набору.

- [x] **T1** `BrowserCfg` + `youtube.browser` у конфігу — AC: BROW-AC14 — тест: `test_browser_publish.py::test_profile_dir_created_private`
- [x] **T2** [P] Чисті функції: `video_id_from`, `build_plan`, `find_chrome` — AC: BROW-AC6, BROW-AC10, BROW-AC11 — тест: `test_browser_publish.py::test_video_id_parsing`, `test_plan_uses_metadata`, `test_publish_at_downgraded_to_private`
- [x] **T3** `StudioDriver` протокол + `BrowserPublisher` (ідемпотентність, dry-run, review, помилки) — AC: BROW-AC1..AC5, AC8, AC9 — тести: `test_done_job_skipped`, `test_upload_sets_video_id_and_status`, `test_resume_uploaded_does_not_reupload`, `test_dry_run_keeps_db_untouched`, `test_review_flag_passed`, `test_warnings_do_not_fail_publish`, `test_missing_video_fails_without_browser`
- [x] **T4** [P] `ChromeSession` + `PlaywrightStudio`: запуск Chrome, CDP, логін, скриншоти — AC: BROW-AC7, BROW-AC14 — тест: `test_noninteractive_login_fails_with_hint`
- [x] **T5** CLI `browser-auth`, `browser-upload` — AC: BROW-AC13 — тест: `test_cli_browser_upload`
- [x] **T6** [P] Веб-UI: кнопка, API, фоновий потік — AC: BROW-AC12 — тест: `test_webui_browser.py::test_start_and_busy`
- [x] **T7** Docs: README, `docs/07_COMMANDS.md`, `config.example.yaml`, реєстр `specs/README.md`, статус → implemented
