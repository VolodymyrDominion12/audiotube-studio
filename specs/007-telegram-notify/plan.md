# 007 · План реалізації

**Спека:** [spec.md](spec.md)

## 1. Підхід

Новий модуль `notify.py` з класом `Notifier`. Він знає події (`published`, `failed_final`, `auth_expired`,
`daily_summary`), тексти й обмеження частоти, а відправку робить через транспорт: функцію `send(text) -> bool`.
Є три транспорти: `telegram` (urllib), `desktop` (notify-send) і `null`. Тести підставляють фейковий транспорт
або мокають `urllib.request.urlopen`.

Демон отримує `Notifier` у конструкторі (за замовчуванням — `build_notifier(cfg, db, log)`) і викликає його
в чотирьох точках циклу. Перевірку токена YouTube виконує `Pipeline.check_publish_auth()`, щоб демон
не імпортував google-бібліотеки напряму і щоб `FakePipe` у тестах міг її підмінити.

## 2. Зміни по модулях

| Модуль | Зміна | Вимоги |
|--------|-------|--------|
| `audiotube/notify.py` | `Notifier`, `telegram_transport`, `desktop_transport`, `build_notifier`, `telegram_env` | R2–R5 |
| `audiotube/config.py` | `daemon.notify: Literal["none","desktop","telegram"]` з валідатором bool; `summary_hour` | R1, R3 |
| `audiotube/db.py` | таблиця `notifications`, `last_notified`, `mark_notified`, `daily_counts` | R4, R3 |
| `audiotube/youtube.py` | `AuthRequired(YouTubeError)` у `get_credentials(interactive=False)` | R7 |
| `audiotube/pipeline.py` | `check_publish_auth()` | R7 |
| `audiotube/daemon.py` | виклики `Notifier`; пропуск публікацій при `AuthRequired`; прибрати `_notify` | R3, R7, R8 |
| `audiotube/cli.py` | `notify --test [--dry-run]`; рядок Telegram у `doctor` | R6, AC4 |
| docs, `config.example.yaml` | новий ключ і змінні середовища | — |

## 3. Дані

`CREATE TABLE IF NOT EXISTS notifications (key TEXT PRIMARY KEY, sent_at TEXT NOT NULL)` — у `_SCHEMA`,
тож старі бази отримують її автоматично. Конфіг: `daemon.notify` (типово `none`), `daemon.summary_hour` (21).

## 4. Ризики й відкат

- Telegram заблоковано мережею → лише рядок у журналі (R5).
- Відкат: `notify: false`. Перевірку токена YouTube (R7) поверне на стару поведінку прибирання виклику в `_cycle`.
