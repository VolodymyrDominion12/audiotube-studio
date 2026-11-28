# 017 · Задачі

Кожна задача: тест першим (червоний) → код (зелений) → `pytest -q` для всього набору.

- [x] **T1** `Database.job_notebooks()` і `jobs_for_notebook()` + тести групування — AC: GRP-AC2, GRP-AC3
- [x] **T2** `GET /api/overview` (без `jobs`) і збережений `/api/state` — AC: GRP-AC1
- [x] **T3** `GET /api/groups`, `/api/groups/<key>/jobs`, `/api/jobs?q=&status=` — AC: GRP-AC2, GRP-AC3, GRP-AC4
- [x] **T4** Фоновий `POST/GET /api/groups/<key>/sync` + замок + `max_per_run` — AC: GRP-AC5, GRP-AC6
- [x] **T5** Головна сторінка: акордеон, ліниві картки, пошук, прогрес — AC: GRP-AC7
- [x] **T6** Оновити README, docs/07_COMMANDS.md і реєстр, статус 017 → implemented
