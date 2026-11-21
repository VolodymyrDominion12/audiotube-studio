# 007 · Задачі

Тест першим → код → `pytest -q`. `[P]` — незалежні.

- [x] **T1** Конфіг `notify` (bool-сумісність) і `summary_hour` — NTF-AC7 — `TelegramTransportTests::test_bool_notify_compat`
- [x] **T2** [P] Telegram-транспорт — NTF-AC8 — `TelegramTransportTests::test_request_shape_and_token_redacted`
- [x] **T3** `notifications` у БД + `Notifier` + демон: `published` — NTF-AC1 — `NotifyDaemonTests::test_published_sends_url`
- [x] **T4** Помилка транспорту не валить цикл — NTF-AC2 — `NotifyDaemonTests::test_telegram_down_does_not_break_cycle`
- [x] **T5** `auth_expired` NotebookLM з обмеженням 12 год — NTF-AC3 — `NotifyDaemonTests::test_auth_expired_throttled`
- [x] **T6** `AuthRequired` + `check_publish_auth` + пропуск публікацій — NTF-AC3/R7 — `NotifyDaemonTests::test_youtube_auth_expired_skips_jobs`
- [x] **T7** [P] `failed_final` — NTF-AC5 — `NotifyDaemonTests::test_failed_final_once`
- [x] **T8** [P] `daily_summary` — NTF-AC6 — `NotifyDaemonTests::test_daily_summary_once_per_day`
- [x] **T9** Без env — тихий режим і попередження `doctor` — NTF-AC4 — `::test_missing_env_runs_silently`, `NotifyCliTests::test_doctor_warns_without_env`
- [x] **T10** `audiotube notify --test [--dry-run]` — NTF-AC7 — `NotifyCliTests::test_notify_test_command`
- [x] **T11** docs/06, `config.example.yaml`, README, `specs/README.md`, статус → implemented
