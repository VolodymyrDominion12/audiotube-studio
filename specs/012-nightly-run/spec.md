# 012 · Нічний пакетний прогін

**Статус:** implemented · **Версія:** 1.0 · **Дата:** 2026-11-24
**Залежить від:** 002 · **Модулі:** `cli.py`, `pipeline.py`

## 1. Проблема

Після `sync` у черзі буває 200+ аудіо. `audiotube run` проходить усю чергу за один запуск,
але `sync` уже створює YAML-заготовку з назвою, тому `describe` вважає метадані готовими й
не кличе LLM. Крім того, немає способу обмежити нічний прогін кількістю задач: запуск на
200 файлів триває кілька діб і його незручно планувати.

## 2. User stories

- **US1** Як автор, я хочу однією командою доробити беклог за ніч і продовжити вранці з того
  самого місця, не переробляючи готове.
- **US2** Як автор, я хочу змусити LLM перегенерувати метадані для аудіо, де YAML створив
  `sync`, але опису, тегів і глав ще немає.
- **US3** Як автор, я хочу обмежити один запуск, щоб він не молотив CPU днями й уміщався в ніч.

## 3. Поза межами

- Автоматичне планування «з 01:00 до 08:00» всередині застосунку — для цього достатньо cron,
  systemd timer або `timeout`.
- Паралельна обробка кількох задач: Whisper і Ollama і так забирають усі ядра й пам'ять.
- Видалення або перерендер уже готових відео.

## 4. Функціональні вимоги

| ID | Вимога |
|----|--------|
| NIGHT-R1 | `audiotube run --force-describe` МУСИТЬ генерувати метадані через LLM навіть тоді, коли sidecar YAML уже існує. |
| NIGHT-R2 | `--force-describe` зберігає ручні поля YAML згідно з PIPE-R7 (privacy, mode, category, background тощо). |
| NIGHT-R3 | `audiotube run --limit N` МУСИТЬ обробити щонайбільше N задач черги за один запуск; N ≥ 1. |
| NIGHT-R4 | Без нових прапорців `run` поводиться як раніше: наявний валідний YAML не перегенеровується. |
| NIGHT-R5 | Обробляються лише задачі зі статусом `new`, `transcribed`, `described`; `rendered`, `uploaded`, `done` не чіпаються навіть із `--force-describe`. |
| NIGHT-R6 | Перерваний прогін відновлюється з БД: повторний запуск не дублює транскрипти, метадані чи відео. |
| NIGHT-R7 | `--limit` діє на кроки конвеєра; блок `--upload`/`auto_upload` лишається окремим. |
| NIGHT-R8 | Наприкінці `run` друкує стан черги (`status`), як і раніше. |

## 5. Нефункціональні вимоги

- Один процес, послідовна обробка: без паралельних Whisper/Ollama.
- Кожен крок ідемпотентний; статус у SQLite — єдине джерело істини.
- `--limit` не змінює порядок черги: беруться перші N задач за id (найстаріші).

## 6. Інтерфейси

CLI:

```bash
audiotube run                          # як раніше
audiotube run --force-describe         # + LLM-метадані попри наявний YAML
audiotube run --limit 10               # щонайбільше 10 задач за запуск
audiotube run --force-describe --limit 10
```

Файли й БД не змінюються: `workspace/<slug>/`, `inbox/<аудіо>.yaml`, `output/<slug>.mp4`.

## 7. Критерії приймання

| ID | Given / When / Then | Тест |
|----|---------------------|------|
| NIGHT-AC1 | **Given** YAML від `sync` без опису **When** `run --force-describe` **Then** LLM викликано і YAML має опис/глави | `test_nightly_run.py::test_run_force_describe_regenerates_sync_sidecar` |
| NIGHT-AC2 | **Given** такий самий YAML **When** `run` без прапорця **Then** LLM не викликається | `test_nightly_run.py::test_run_without_flag_keeps_sync_sidecar` |
| NIGHT-AC3 | **Given** 3 задачі **When** `--limit 2` **Then** оброблено 2 | `test_nightly_run.py::test_limit_processes_only_n_jobs` |
| NIGHT-AC4 | **Given** відрендерена задача **When** `run --force-describe` **Then** її не чіпають | `test_nightly_run.py::test_rendered_jobs_are_never_touched` |
| NIGHT-AC5 | прапорці доступні у CLI | `test_nightly_run.py::test_parser_accepts_nightly_flags` |
| NIGHT-AC6 | **Given** задача вже `described` **When** `run --force-describe` **Then** LLM викликано повторно | `test_nightly_run.py::test_force_describe_refreshes_already_described_job` |

## 8. Відповідність конституції

I — LLM лише на кроці `describe`, решта — детермінований код; II — стан у SQLite, прогін
відновлюється; III — повторний запуск не дублює кроки; IV — наявні файли не перезаписуються
без явного `--force-describe`/`--force`; VI — жодних зовнішніх записів, лише локальні
Whisper/Ollama та файли; VII — тести без мережі, Whisper і LLM замокані; VIII — локальні
бекенди; IX — повідомлення українською.

## 9. Відкриті питання

Немає.
