# 004 · Демон публікацій («Видавець»)

**Статус:** implemented (as-built) · **Версія:** 1.0 · **Дата:** 2026-11-21
**Залежить від:** 001, 002, 003 · **Модулі:** `daemon.py`, `deploy/systemd/`
**Детальний дизайн:** [docs/06_AUTOMATION.md](../../docs/06_AUTOMATION.md)

## 1. Проблема

Навіть з готовими командами хтось має щодня запускати `sync → run → upload` і стежити за
квотою. Потрібен фоновий процес «поставив і забув», що публікує рівномірно й безпечно.

## 2. User stories

- **US1** Як автор, я хочу, щоб нові перекази з NotebookLM самі ставали відео на YouTube.
- **US2** Як автор, я хочу обмежити кількість відео на добу й інтервал між ними, щоб канал не виглядав як спам.
- **US3** Як автор, я хочу, щоб збійна задача не блокувала чергу вічно.

## 4. Функціональні вимоги

| ID | Вимога |
|----|--------|
| DMN-R1 | Цикл: (опційно) `sync` → `scan` → перевірки бюджету → обробка задач до `done`. |
| DMN-R2 | Не більше `videos_per_day` публікацій за добу; рахуються й ручні `upload` (таблиця `uploads`). |
| DMN-R3 | Не частіше ніж раз на `min_hours_between` годин. |
| DMN-R4 | Задача береться, лише якщо квоти вистачає на `upload_cost` + обкладинка (+ `captions_cost`, якщо `upload_captions`; + плейлист, якщо `manage_playlists`) з урахуванням `quota_reserve`. |
| DMN-R5 | Порядок черги: `oldest` (FIFO) або `newest`. |
| DMN-R6 | Невдача → `attempts + 1`; задачі з `attempts >= max_attempts` не беруться. |
| DMN-R7 | Після успіху: опційно видалити `.mp4` (`cleanup_video_after_upload`), опційно `notify-send`. |
| DMN-R8 | Другий екземпляр не стартує (`flock` на `data/daemon.lock`, exit 3). |
| DMN-R9 | `SIGTERM` зупиняє після поточного кроку. |
| DMN-R10 | `--dry-run` нічого не змінює; `--once` — один цикл. |
| DMN-R11 | Збій синку не валить цикл — публікація з наявної черги продовжується. |

## 6. Інтерфейси

CLI: `audiotube daemon [--once] [--dry-run] [--max N] [--no-sync]`.
Конфіг: `daemon:`. Systemd: `audiotube-daemon.service`, `audiotube-once.{service,timer}`.

## 7. Критерії приймання

| ID | Given / When / Then | Тест |
|----|---------------------|------|
| DMN-AC1 | **Given** `videos_per_day=1` і 2 задачі **Then** опубліковано 1 | `test_daemon.py::test_daily_budget` |
| DMN-AC2 | **Given** публікація щойно **Then** наступний цикл чекає | `test_daemon.py::test_min_hours_between_blocks_next_cycle` |
| DMN-AC3 | **Given** задача падає `max_attempts` разів **Then** більше не береться | `test_daemon.py::test_max_attempts_stops_retries` |
| DMN-AC4 | **Given** мало квоти **Then** задача не береться | `test_daemon.py::test_quota_guard` |
| DMN-AC5 | після публікації `.mp4` видалено | `test_daemon.py::test_cleanup_removes_local_video` |
| DMN-AC6 | `--dry-run` нічого не змінює | `test_daemon.py::test_dry_run_changes_nothing` |
| DMN-AC7 | другий екземпляр блокується | `test_daemon.py::test_lock_blocks_second_instance` |
| DMN-AC8 | задача `new` проходить увесь ланцюг до `done` | `test_daemon.py::test_full_chain_from_new` |

## 8. Відповідність конституції

I — детермінований цикл; II — бюджет і спроби в БД; VI — денний ліміт, інтервал, квота, `--dry-run`.
