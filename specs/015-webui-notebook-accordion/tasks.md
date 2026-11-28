# 015 · Задачі

Кожна задача: тест першим (червоний) → код (зелений) → `pytest -q` для всього набору.

- [x] **T1** Тести HTML-маркерів акордеона, deep link і payload — AC: NB2-AC1, NB2-AC2, NB2-AC3, NB2-AC5 — тест: `tests/test_webui.py::test_notebooks_accordion_markup`, `::test_notebooks_deep_link_and_history`, `::test_notebook_detail_payload_for_inline`
- [x] **T2** Розмітка й CSS акордеона: toolbar (пошук, фільтр, лічильник), рядок ноутбука, тіло, стани — AC: NB2-R1, NB2-R5, NB2-R7
- [x] **T3** JS: `renderList`, `applyFilter`, `openNotebook`/`closeNotebook`, `ensureItems`, кеш, `popstate`, `runSync` → `refresh({force:true})` — AC: NB2-R2, NB2-R4, NB2-R6
- [x] **T4** Оновити `README.md`, `docs/07_COMMANDS.md`, примітку в spec 008 і реєстр `specs/README.md`, статус 015 → implemented
