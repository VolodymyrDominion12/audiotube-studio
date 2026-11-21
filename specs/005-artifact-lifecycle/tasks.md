# 005 · Задачі

Тест першим → код → `pytest -q`. `[P]` — незалежні.

- [x] **T0** ~~Перевірити на реальному акаунті~~ — замінено рішенням (spec §9): версії відкладено до реального випадку `changed`.
- [x] **T1** Розширити `FakeAdapter` у тестах: видалення артефакту, видалення ноутбука, зміна вмісту, помилка `audio()`.
- [x] **T2** LIFE-AC1/AC2 — missing-детекція і повернення — `ArtifactLifecycleTests::test_removed_artifact_marked_missing`, `::test_missing_artifact_restored`
- [x] **T3** LIFE-AC3 — помилка API не змінює стани — `ArtifactLifecycleTests::test_api_error_keeps_states`
- [x] **T4** [P] LIFE-AC4 — `--force` без перезапису — `ArtifactLifecycleTests::test_force_same_content_keeps_file`, `::test_force_changed_content_never_overwrites`
- [x] **T5** [P] LIFE-AC7 — регресійний тест на оновлення назви ноутбука (код уже є) — `ArtifactLifecycleTests::test_notebook_rename_updates_title`
- [x] **T6** ~~LIFE-AC5/AC6 — версії~~ — відхилено (spec §9)
- [x] **T7** Лічильники у звіті й `status --missing` (LIFE-R8) — `SyncCliTests::test_missing_is_reported_by_sync_and_status`
- [x] **T8** Оновити docs/05 розд. 4.2, `specs/README.md`, статус → implemented
