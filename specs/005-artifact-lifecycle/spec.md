# 005 · Версії й зникнення артефактів

**Статус:** implemented · **Версія:** 1.0 · **Дата:** 2026-11-21
**Залежить від:** 001 · **Модулі:** `sync.py`, `db.py`, `cli.py`

## 1. Проблема

Дизайн синку (docs/05, розд. 4.2) обіцяє, що синк помічає перегенеровані та видалені в NotebookLM
перекази. Зараз це не так:

- `artifacts.state` завжди `downloaded`; видалений у NotebookLM переказ ніяк не позначається.
- `sync --force` качає в той самий шлях і через `os.replace` **перезаписує** наявний файл — це
  порушує конституцію IV, а якщо відео вже на YouTube, локальний файл перестає йому відповідати.

## 2. User stories

- **US1** Як автор, я хочу бачити в `audiotube status`/звіті синку, які перекази зникли з NotebookLM, щоб вирішити, що з ними робити.
- **US2** Як автор, я хочу перекачати переказ, якщо я його перегенерував, не втративши стару версію.
- **US3** Як автор, я хочу, щоб нова версія переказу не публікувалася автоматично вдруге, якщо стару вже опубліковано.

## 3. Поза межами

Автоматична заміна відео на YouTube; видалення локальних файлів; перейменування папок
(`--rename-folders` — окрема спека за потреби).

## 4. Функціональні вимоги

| ID | Вимога |
|----|--------|
| LIFE-R1 | Якщо артефакт є в БД, але відсутній у відповіді NotebookLM для свого ноутбука, синк МУСИТЬ встановити `state='missing'` і показати це у звіті. Локальний файл і задача не змінюються. |
| LIFE-R2 | Якщо `missing`-артефакт знову з'явився — повернути `state='downloaded'`. |
| LIFE-R3 | Якщо ноутбук не прийшов у відповіді взагалі (видалено/втрачено доступ) — його артефакти теж `missing`; якщо `api.audio()` впав з помилкою — стани НЕ змінюються (помилка ≠ відсутність). |
| LIFE-R4 | `sync --force` НЕ МОЖЕ перезаписати наявний файл. Новий файл качається в `.part`; якщо sha256 збігається зі збереженим — `.part` видаляється, стан без змін. Якщо не збігається — до реалізації R5 `.part` теж видаляється, а у звіті з'являється `changed` (локальний файл, задача й БД не змінюються). |
| LIFE-R5 | ~~Зберігати нову версію поруч (`.v2.m4a`, `superseded`).~~ **Відхилено 2026-11-21** — див. §9. |
| LIFE-R6 | ~~Ставити нову версію вже опублікованого на паузу (`held`).~~ **Відхилено 2026-11-21** — див. §9. |
| LIFE-R7 | При кожному синку `notebooks.title` оновлюється з NotebookLM; slug і папка — ні. *(Уже так працює — `save_notebook` + slug з БД; бракує лише регресійного тесту.)* |
| LIFE-R8 | `audiotube sync` виводить окремі лічильники: зниклі, повернуті, змінені; `audiotube status --missing` показує зниклі перекази зі станом їхніх задач. |

## 5. Нефункціональні вимоги

Детекція `missing` не робить додаткових мережевих запитів (використовує вже отримані списки).
Детекція перегенерації можлива лише через завантаження, тому відбувається лише з `--force`.

## 6. Інтерфейси

- БД: `artifacts.state ∈ {downloaded, missing}` (колонка вже була — міграції не потрібні).
- CLI: нові рядки у звіті `sync`; `audiotube status --missing`.

## 7. Критерії приймання

| ID | Given / When / Then | Тест |
|----|---------------------|------|
| LIFE-AC1 | **Given** синкнутий артефакт **When** NotebookLM більше його не повертає **Then** `state=missing`, файл на місці | `test_sync.py::ArtifactLifecycleTests::test_removed_artifact_marked_missing`, `::test_removed_notebook_marks_its_artifacts_missing`, `::test_notebook_filter_does_not_mark_others_missing`, `::test_dry_run_reports_missing_without_writing` |
| LIFE-AC2 | **Given** `missing` **When** артефакт повернувся **Then** `state=downloaded` | `test_sync.py::ArtifactLifecycleTests::test_missing_artifact_restored` |
| LIFE-AC3 | **Given** `api.audio()` кидає помилку **Then** стани не змінені | `test_sync.py::ArtifactLifecycleTests::test_api_error_keeps_states` |
| LIFE-AC4 | **Given** той самий вміст **When** `sync --force` **Then** файл не перезаписаний (mtime той самий), `.part` прибрано | `test_sync.py::ArtifactLifecycleTests::test_force_same_content_keeps_file`, `::test_force_changed_content_never_overwrites` |
| LIFE-AC7 | **Given** ноутбук перейменовано **Then** title оновлено, папка та сама | `test_sync.py::ArtifactLifecycleTests::test_notebook_rename_updates_title` |
| LIFE-AC9 | **When** `sync` після зникнення **Then** «зникло з NotebookLM: 1»; `status --missing` показує переказ | `test_sync.py::SyncCliTests::test_missing_is_reported_by_sync_and_status` |
| LIFE-AC8 | Усі тести 001 (SYNC-AC1…14) лишаються зеленими | `tests/test_sync.py` |

## 8. Відповідність конституції

III — `missing` не спричиняє дій; IV — головна мета спеки (жодного перезапису); V — лише через `Adapter`;
VI — перегенерація лише за явним `--force`.

## 9. Рішення

**2026-11-21:** версії артефактів (LIFE-R5/R6) не реалізуємо. NotebookLM при перегенерації,
найімовірніше, створює новий `artifact_id`, тож нова версія і так скачується як окремий переказ.
Якщо `sync --force` колись покаже `changed` (той самий id, інший вміст), — повертаємося до цієї
спеки з реальним прикладом. До того часу `changed` лише інформує й нічого не змінює.
