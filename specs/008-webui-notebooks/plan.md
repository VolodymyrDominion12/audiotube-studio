# 008 · План реалізації

**Спека:** [spec.md](spec.md)

## 1. Підхід

Розширюємо наявний `webui.py` (stdlib `http.server`, без нових залежностей). Нова HTML-сторінка
`/notebooks` (і `/notebooks/<id>` — та сама сторінка, JS читає id з адреси) тягне дані з JSON-API.
Лічильники рахує один SQL-запит у `db.py`, а не Python-цикл по задачах.

Синк з UI відкриває власне з'єднання з БД і бере `FileLock(data/daemon.lock)` з `daemon.py`.
Внутрішній `WebApp.lock` на час синку не тримаємо, щоб довгий синк не блокував решту UI.
Альтернатива — окремий `sync.lock` — відкинута рішенням у spec §9.

## 2. Зміни по модулях

| Модуль | Зміна | Вимоги |
|--------|-------|--------|
| `audiotube/db.py` | `notebook_stats()`, `notebook_items(notebook_id)` | WEB-R1, WEB-R2 |
| `audiotube/daemon.py` | `FileLock.is_locked(path)`, `next_upload_at(cfg, db)` | WEB-R4, WEB-R5 |
| `audiotube/webui.py` | `WebApp(adapter=…)`, `notebooks()`, `notebook()`, `daemon_state()`, `sync()`; маршрути; `NOTEBOOKS_HTML`; посилання в шапці | WEB-R1…R6 |
| `tests/test_webui.py` | `NotebooksWebTests` на `FakeAdapter` | WEB-AC1…AC7 |

## 3. Дані

Без міграцій і нових ключів конфігу.

## 4. Ризики й відкат

- Довгий синк тримає HTTP-запит відкритим. Прийнятно для локального UI: кнопка показує «працюю…».
- Відкат: прибрати маршрути `/notebooks`, `/api/notebooks*`, `/api/sync`, `/api/daemon`. Решта UI не змінюється.
