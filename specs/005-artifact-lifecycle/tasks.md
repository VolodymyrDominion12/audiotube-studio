# 005 · Задачі

Тест першим → код → `pytest -q`. `[P]` — незалежні.

- [ ] **T0** Перевірити на реальному акаунті: чи змінюється `artifact_id` при перегенерації переказу. Записати відповідь у spec.md §9.
- [ ] **T1** Розширити `FakeAdapter` у тестах: видалення артефакту, видалення ноутбука, зміна вмісту, помилка `audio()`.
- [ ] **T2** LIFE-AC1/AC2 — missing-детекція і повернення — `test_sync.py::test_removed_artifact_marked_missing`, `::test_missing_artifact_restored`
- [ ] **T3** LIFE-AC3 — помилка API не змінює стани — `test_sync.py::test_api_error_keeps_states`
- [ ] **T4** [P] LIFE-AC4 — `--force` без перезапису — `test_sync.py::test_force_same_content_keeps_file`
- [ ] **T5** [P] LIFE-AC7 — регресійний тест на оновлення назви ноутбука (код уже є) — `test_sync.py::test_notebook_rename_updates_title`
- [ ] **T6** LIFE-AC5/AC6 — версії (після T0 і рішення щодо моделі даних)
- [ ] **T7** Лічильники у звіті й `status --missing` (LIFE-R8)
- [ ] **T8** Оновити docs/05 розд. 4.2, `specs/README.md`, статус → implemented
