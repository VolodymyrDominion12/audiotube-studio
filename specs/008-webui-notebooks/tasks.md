# 008 · Задачі

Тест першим → код → `pytest -q`. `[P]` — незалежні.

- [x] **T1** `db.notebook_stats()` + `GET /api/notebooks` — WEB-AC1 — `NotebooksWebTests::test_notebooks_counters`
- [x] **T2** `db.notebook_items()` + `GET /api/notebooks/<id>` — WEB-AC5 — `NotebooksWebTests::test_notebook_detail`
- [x] **T3** [P] `next_upload_at`, `FileLock.is_locked` + `GET /api/daemon` — WEB-AC6 — `NotebooksWebTests::test_daemon_panel`
- [x] **T4** `POST /api/sync?dry_run=1` — WEB-AC2 — `NotebooksWebTests::test_sync_plan_does_not_change_db`
- [x] **T5** `POST /api/sync` під `daemon.lock` — WEB-AC3, WEB-AC7 — `::test_sync_busy_lock_returns_409`, `::test_sync_runs_and_releases_lock`
- [x] **T6** WEB-AC4 — GET не змінює БД — `NotebooksWebTests::test_get_requests_do_not_change_db`
- [x] **T7** HTML-сторінка `/notebooks` + посилання з головної — `NotebooksWebTests::test_notebooks_page_served`
- [x] **T8** docs, `specs/README.md`, статус → implemented
