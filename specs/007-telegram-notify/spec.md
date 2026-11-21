# 007 · Сповіщення в Telegram

**Статус:** implemented · **Версія:** 0.2 · **Дата:** 2026-11-21
**Залежить від:** 004 · **Модулі:** новий `notify.py`, `daemon.py`, `config.py`, `db.py`, `youtube.py`, `cli.py`

## 1. Проблема

Демон працює у фоні; про публікації та збої зараз можна дізнатися лише з `journalctl` або
`notify-send` (тільки якщо сидиш за цим комп'ютером). Прострочена сесія NotebookLM чи OAuth
YouTube може тихо зупинити конвеєр на дні.

## 2. User stories

- **US1** Як автор, я хочу отримати в Telegram посилання на кожне опубліковане відео.
- **US2** Як автор, я хочу одразу дізнатися, що протухла автентифікація (NotebookLM / YouTube).
- **US3** Як автор, я хочу коротке денне зведення: скачано, опубліковано, у черзі, помилки.

## 3. Поза межами

Двосторонній бот (кнопки, команди), кілька чатів, форматування Markdown/HTML у повідомленнях.

## 4. Функціональні вимоги

| ID | Вимога |
|----|--------|
| NTF-R1 | `daemon.notify` приймає `false | desktop | telegram` (`true` = `desktop` для сумісності). `desktop` — `notify-send`, як і раніше. |
| NTF-R2 | Токен бота й chat_id — лише зі змінних середовища (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), не з `config.yaml`. Токен ніколи не потрапляє в журнал. |
| NTF-R3 | Події: `published` (назва + URL), `failed_final` (задача щойно досягла `max_attempts`), `auth_expired` (NotebookLM/YouTube), `daily_summary` (раз на добу, не раніше `daemon.summary_hour`). |
| NTF-R4 | `auth_expired` надсилається не частіше ніж раз на 12 год на кожне джерело (без спаму кожен цикл). Час останньої відправки зберігається в SQLite, тож рестарт демона не скидає обмеження. |
| NTF-R5 | Помилка відправки ніколи не валить цикл демона; таймаут HTTP ≤ 10 с. Невдала відправка не позначається як надіслана. |
| NTF-R6 | `audiotube notify --test` надсилає тестове повідомлення в Telegram (незалежно від `daemon.notify`); `--dry-run` лише друкує текст. |
| NTF-R7 | Перед публікацією демон перевіряє токен YouTube без браузера. Протух → `auth_expired(youtube)`, цикл публікацій пропускається, спроби задач **не** витрачаються. |
| NTF-R8 | `daemon --dry-run` нічого не надсилає. |

## 5. Деталі

- `auth_expired(notebooklm)` — синк упав з `NotebookLMError` (крім «пакет не встановлено»).
- `auth_expired(youtube)` — `get_credentials(interactive=False)` не дав дійсного токена (`youtube.AuthRequired`).
- `daily_summary` — за поточну добу: скачано (`artifacts.downloaded_at`), опубліковано (`uploads`),
  у черзі (не `done`, `attempts < max_attempts`), помилки (`error IS NOT NULL`).
- `summary_hour: 21` за замовчуванням; `null` вимикає зведення.
- Новий стан: таблиця `notifications(key, sent_at)`; ключі `auth_expired:<джерело>`, `daily_summary`.
- Транспорт Telegram: `POST https://api.telegram.org/bot<token>/sendMessage` (stdlib `urllib`), простий текст ≤ 4096 символів.

## 7. Критерії приймання

| ID | Given / When / Then | Тест |
|----|---------------------|------|
| NTF-AC1 | **Given** `notify: telegram` **When** публікація **Then** 1 повідомлення з URL (HTTP замокано) | `NotifyDaemonTests::test_published_sends_url` |
| NTF-AC2 | **Given** Telegram недоступний **Then** цикл завершується успішно | `NotifyDaemonTests::test_telegram_down_does_not_break_cycle` |
| NTF-AC3 | **Given** auth протухла 3 цикли поспіль **Then** 1 повідомлення | `NotifyDaemonTests::test_auth_expired_throttled` (NotebookLM), `::test_youtube_auth_expired_skips_jobs` (YouTube) |
| NTF-AC4 | **Given** немає env-змінних **Then** `doctor` попереджає, демон працює без сповіщень | `NotifyCliTests::test_doctor_warns_without_env`, `NotifyDaemonTests::test_missing_env_runs_silently` |
| NTF-AC5 | **Given** задача падає вдруге при `max_attempts: 2` **Then** рівно 1 `failed_final` | `NotifyDaemonTests::test_failed_final_once` |
| NTF-AC6 | **Given** `summary_hour` минув **When** 2 цикли **Then** 1 зведення з лічильниками | `NotifyDaemonTests::test_daily_summary_once_per_day` |
| NTF-AC7 | `notify --test` шле 1 запит; `--dry-run` — жодного; `true` у конфігу = `desktop` | `NotifyCliTests::test_notify_test_command`, `TelegramTransportTests::test_bool_notify_compat` |
| NTF-AC8 | Транспорт: правильний URL/тіло, таймаут ≤ 10 с, токен не в тексті помилки | `TelegramTransportTests::test_request_shape_and_token_redacted` |

## 8. Відповідність конституції

I — звичайний код без LLM; II — обмеження частоти в SQLite; VI — `--dry-run` у `notify` і демоні;
VII — HTTP мокається; VIII — секрети лише з env; IX — повідомлення українською; без нових залежностей (stdlib `urllib`).

## 9. Рішення

- ~~Чи потрібні інтерактивні кнопки (напр. «опублікувати як public»)?~~ **Рішення (2026-11-21):** ні. 007 — лише
  односторонні сповіщення; двосторонній бот з кнопками — окрема майбутня спека.
- NTF-R7 трохи змінює поведінку 004: протухлий токен YouTube більше не «спалює» спроби задач.
