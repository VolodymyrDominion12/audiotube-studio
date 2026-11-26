# AudioTube Studio

Локальний конвеєр: аудіорозбори → транскрипт і субтитри → метадані (назва, опис, теги, глави,
плейлист) → відео з фоном і обкладинкою → завантаження на YouTube.

- **Специфікації (spec-driven development):** [specs/README.md](specs/README.md) — спершу спека, потім код
- Концепція і план розвитку: [docs/01_CONCEPT.md](docs/01_CONCEPT.md)
- План інтеграції з NotebookLM (синхронізація аудіопереказів → папки → автопублікація): [docs/05_NOTEBOOKLM_PLAN.md](docs/05_NOTEBOOKLM_PLAN.md)
- Автоматика (демон, systemd, квота): [docs/06_AUTOMATION.md](docs/06_AUTOMATION.md)
- Довідник команд: [docs/07_COMMANDS.md](docs/07_COMMANDS.md)
- Аналіз коду, рішення й наступні етапи: [docs/03_ANALYSIS_AND_NEXT_STEPS.md](docs/03_ANALYSIS_AND_NEXT_STEPS.md)
- Налаштування транскрипції та LLM: [docs/04_LLM_AND_WHISPER.md](docs/04_LLM_AND_WHISPER.md)
- Налаштування доступу до YouTube: [docs/02_YOUTUBE_SETUP.md](docs/02_YOUTUBE_SETUP.md)

**Статус:** Фаза 1 (MVP) завершена; Фаза 2 реалізована — транскрипція, LLM-метадані, субтитри
YouTube, веб-інтерфейс, watch-folder. Додано синхронізацію з NotebookLM (`sync`/`import`) і
демон з денним бюджетом публікацій (`daemon`). Залишились зовнішні джерела фонів (Pexels/Pixabay),
тематичні слайди й розклад публікацій — див. [roadmap](docs/03_ANALYSIS_AND_NEXT_STEPS.md#7-що-робити-далі-за-пріоритетом).

## Що потрібно

- Python 3.10+ (перевірено на 3.14);
- FFmpeg з `ffprobe` (і з `drawtext`/`libass` — для написів і вшитих субтитрів);
- опційно: `faster-whisper` для локальної транскрипції, [Ollama](https://ollama.com) для LLM
  і `notebooklm-py` із входом у Google — для завантаження аудіо з NotebookLM.

## Встановлення

```bash
sudo apt install ffmpeg
cd ~/PycharmProjects/audiotube-studio

# варіант з uv (як у цьому репозиторії)
uv pip install -e ".[transcribe,notebooklm]"

# або з pip
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[transcribe,notebooklm]"

audiotube init        # створить папки і config.yaml
audiotube doctor      # перевірить ffmpeg, шрифти, транскрипцію, LLM, токен
```

`[transcribe]` ставить `faster-whisper` (локальна транскрипція), `[notebooklm]` — клієнт
NotebookLM. Якщо ні те, ні те не потрібно, достатньо `pip install -e .`.

## Швидкий старт

```bash
# 1. Покладіть аудіо
cp ~/Downloads/docker_za_20_khvylyn.mp3 inbox/

# 2. Транскрипт + субтитри (локально)
audiotube transcribe
#    workspace/<slug>/transcript.json, workspace/<slug>/subtitles.srt
#    правила нарізки субтитрів і `audiotube subtitles` — docs/04, розд. 1.5

# 3. Метадані через LLM (потрібна Ollama або API-ключ)
audiotube describe
#    inbox/<аудіо>.yaml — перевірте й відредагуйте за потреби

# 4. Швидке прев'ю (перші 20 с)
audiotube render --preview 20
#    output/<slug>_preview.mp4

# 5. Повний рендер
audiotube render
#    output/<slug>.mp4 + output/<slug>.jpg

# 6. Один раз — авторизація YouTube
audiotube auth

# 7. Завантаження + обкладинка + субтитри + плейлист
audiotube upload

# 8. Зробити відео публічним (типово завантажується приватним)
audiotube publish <id>

audiotube status
```

Однією командою: `audiotube run` робить `scan → transcribe → describe → render`
(і upload, якщо `youtube.auto_upload: true` або додати `--upload`).

Пропустити окремі кроки: `audiotube run --no-transcribe`, `--no-describe`, `--no-render`.

`sync` уже створює YAML-заготовку, тому для повного опису, тегів і глав потрібен
`audiotube run --force-describe`. Обмежити нічний прогін: `--limit N` (наприклад,
`audiotube run --force-describe --limit 10`).

Дорендерити всі аудіо, у яких ще немає відео, і **нічого не заливати на YouTube**:

```bash
audiotube run --missing-video --limit 10    # 10 задач за запуск; без --limit — уся черга
audiotube run --no-upload                   # звичайний run без публікації
```

`--missing-video` дивиться на фактичний файл `output/<slug>.mp4` (або `video_id`), а не на
статус, тож дорендерить і задачу, у якої відео зникло. Задачі з наявним відео не перезаписуються.

## Веб-інтерфейс

```bash
audiotube web                 # http://127.0.0.1:8765
audiotube web --port 9000 --open
```

Черга задач, прев'ю відео, кнопки «Транскрипт / Метадані LLM / Рендер / Опублікувати»,
редагування YAML-метаданих із валідацією перед збереженням. Працює локально, без зовнішніх
залежностей; кожна дія виконується у своєму з'єднанні SQLite.

Зверху — лічильники: скільки задач, скільки вже на YouTube, скільки готових до публікації,
скільки аудіо в `inbox/` поза чергою і скільки `.mp4` у `output/`, а також сумарний розмір
аудіо й відео. Нижче — фільтри за статусом і пошук за назвою/файлом. Для відео, що вже на
YouTube, є вибір приватності й кнопка «Змінити приватність» (те саме, що `audiotube publish`).

У кожній картці є керування файлами: **«Видалити аудіо»** прибирає `inbox/<ноутбук>/*.m4a`
(або `.mp3` тощо), **«Видалити відео»** — `output/<slug>.mp4` і прев'ю. Історія лишається в БД:
аудіо позначене як «видалено (було скачано)», тому `sync` більше не качає його повторно навіть
з `--force`; `video_id`, посилання на YouTube, плейлист, субтитри й обкладинка зберігаються.
Кнопка **«Відновити аудіо»** знімає позначку — наступний синк скачає файл знову. YAML-метадані
й транскрипти не видаляються. Поза `inbox/` та `output/` UI нічого не видаляє.

Сторінка **«Ноутбуки»** (`/notebooks`) показує ланцюжок NotebookLM → переказ → відео: лічильники
«скачано / аудіо видалено / у черзі / опубліковано / помилки / зникли» по кожному ноутбуку,
перекази зі статусом задачі й посиланням на YouTube, а також панель демона (публікації сьогодні
з бюджету, залишок квоти, наступне вікно). Кнопка «Синк (план)» — це `sync --dry-run`, «Синк» —
справжній запуск з `max_per_run`. Він бере той самий `data/daemon.lock`, що й демон: поки демон
працює, синк з UI не запуститься. `sync` показує окремий лічильник «видалено локально» і не рахує
такі файли серед пропущених.

## Watch-folder

```bash
audiotube watch --interval 60
audiotube watch --once --no-render        # один прохід, без відео
audiotube watch --interval 300 --upload   # повна автоматика
```

## Автоматика

```bash
audiotube daemon --dry-run        # що буде зроблено (без побічних ефектів)
audiotube daemon --once --max 1   # один цикл, максимум одне відео
audiotube daemon                  # безперервно: sync → рендер → upload
```

Демон тримає денний бюджет (`videos_per_day`, типово 2), мінімальний інтервал між відео,
не виходить за квоту YouTube, після 3 невдач заморожує задачу й бере файловий лок, щоб два
екземпляри не дублювали завантаження. Може надсилати сповіщення в Telegram (публікації, заморожені
задачі, протухла авторизація, денне зведення): `daemon.notify: telegram` + `TELEGRAM_BOT_TOKEN` і
`TELEGRAM_CHAT_ID` у `.env`, перевірка — `audiotube notify --test`. Готові systemd-юніти — у `deploy/systemd/`, інструкції —
[docs/06_AUTOMATION.md](docs/06_AUTOMATION.md).

## NotebookLM

```bash
uv tool install "notebooklm-py[browser]"   # окремий CLI для входу в Google
notebooklm login                            # один раз, у браузері
notebooklm auth check --test --json

audiotube sync --dry-run                    # план: що буде завантажено
audiotube sync --limit 5                    # перші 5 нових аудіо
audiotube sync                              # усе (за один запуск — до max_per_run)
audiotube import ~/Downloads/notebook/      # аварійний шлях: вручну скачані файли
```

Файли лягають у `inbox/<ноутбук>/` разом із YAML поруч, далі — звичайний `audiotube run`.
Повторний `sync` нічого не дублює: ключ ідемпотентності — id артефакту в NotebookLM.
Відео за замовчуванням приватні, плейлисти не створюються (`youtube.manage_playlists: false`
у прикладі конфігу) — розкласти по плейлистах можна вручну в Studio. Деталі й ризики:
[docs/05_NOTEBOOKLM_PLAN.md](docs/05_NOTEBOOKLM_PLAN.md).

## Метадані

`describe` створює `<аудіо>.yaml` поруч з файлом (у т.ч. в підпапках ноутбуків), який можна
редагувати вручну. Можна й почати з шаблону:

```bash
audiotube meta inbox/docker.mp3    # -> inbox/docker.yaml
```

```yaml
title: "Docker за 20 хвилин: контейнери без магії"
description: |
  Що таке контейнер, чим він відрізняється від віртуальної машини,
  і як запустити свій перший сервіс.
tags: [docker, devops, контейнери]
playlist: "DevOps за 20 хвилин"     # створиться, якщо його немає
chapters:
  - "00:00 Вступ"
  - "02:40 Контейнер vs віртуальна машина"
  - "09:15 Образи і шари"
mode: loop                           # image | loop
category: tech                       # assets/loops/tech/ або assets/images/tech/
visual_keywords: [docker, containers, whale]
thumbnail_text: "Docker за 20 хв"
privacy: private
publish_at: 2026-10-25T18:00:00+03:00
```

Пріоритет джерел: YAML поруч з аудіо → назва з імені файлу. Поля, яких немає, беруться з `config.yaml`.
`describe --force` перегенерує контент, зберігаючи ручні `mode/category/background/privacy/publish_at`.

## Фони

| Режим | Звідки | Як рендериться |
|-------|--------|----------------|
| `image` | `assets/images/<категорія>/` | картинка з повільним наближенням-віддаленням |
| `loop`  | `assets/loops/<категорія>/` | кліп повторюється всю тривалість (`loop_pingpong: true` — вперед-назад, без стику) |
| — | нічого не знайдено | абстрактний градієнт, у кожної теми свій відтінок |

Якщо в імені фону є слово з тегів, назви або `visual_keywords` (напр. `docker-whale.jpg`), його
оберуть першим. Безкоштовні фони з дозволом на комерційне використання:
[Pexels](https://www.pexels.com/videos/), [Pixabay](https://pixabay.com/videos/).

## Як влаштований рендер

Кодується один короткий сегмент (за замовчуванням 60 с) — картинка з zoom/pan або нормалізований
відеолуп плюс назва в кадрі. Потім сегмент повторюється через `-stream_loop -1` **без перекодування**
(`-c:v copy`), а аудіо накладається поверх. 20-хвилинне відео 1080p рендериться приблизно за
2 хвилини на CPU (з NVENC — швидше; `encoder: auto` визначить сам).

`render.burn_subtitles: true` додає окремий повільний прохід вшивання `.srt` (libass, власний шрифт).
Для YouTube субтитри завантажуються окремо (`youtube.upload_captions: true`) — це швидко й не
псує якість відео.

## Статуси задач

```
new → transcribed → described → rendered → uploaded → done
```

- `new` — аудіо зареєстровано;
- `transcribed` — є `transcript.json` і `subtitles.srt`;
- `described` — є YAML-метадані;
- `rendered` — є `.mp4` і обкладинка;
- `uploaded` — є `video_id`;
- `done` — додано в плейлист (і субтитри, якщо вдалося).

Повторний запуск ніколи не знижує статус і не дублює завантаження. Скинути крок:
`audiotube reset <id> --to described` (або `--to new --forget-youtube`).

## Обмеження YouTube

- **Квота:** 10 000 одиниць/добу. Завантаження ~1600, субтитри ~400, обкладинка ~50, плейлист ~52 —
  тобто **~4 відео на добу**. `audiotube status` показує витрачене; квота скидається опівночі
  за тихоокеанським часом (~10:00 за Києвом).
- **Аудит API-проєкту:** без нього завантажені через API відео стають приватними. Варіанти —
  публікувати вручну в YouTube Studio або подати проєкт на аудит.
- **Позначка AI-контенту:** `youtube.contains_synthetic_media: true` виставляє відповідне поле.
- **Обкладинки:** власні працюють лише на верифікованому каналі (підтвердження телефоном).

Деталі — [docs/02_YOUTUBE_SETUP.md](docs/02_YOUTUBE_SETUP.md).

## Конфігурація

`config.yaml` створюється командою `audiotube init` з `config.example.yaml`. Основні секції:

| Секція | Що налаштовує |
|--------|----------------|
| `paths` | inbox, workspace, output, assets, data, cache |
| `render` | роздільність, fps, енкодер, crf, довжина сегмента, zoom, назва в кадрі, вшиті субтитри |
| `visuals` | режим і категорія фону за замовчуванням |
| `transcribe` | бекенд, модель, мова, device, compute_type, SRT-ліміти |
| `llm` | провайдер, модель, base_url, ключі, temperature, ретраї |
| `plan` | відомі плейлисти, кількість глав, ліміт транскрипту для LLM |
| `pipeline` | які кроки виконує `run`/`watch` |
| `youtube` | приватність, категорія, мова, субтитри, квота, auto_upload |
| `description` | футер опису і теги за замовчуванням |

Ключі API зручно тримати в `.env` (див. `.env.example`); файл у `.gitignore`.

## Тести

```bash
python -m pytest -q                            # 169 тестів (або unittest discover -s tests)
```

Тести, що потребують FFmpeg, автоматично пропускаються, якщо його немає в `PATH`.
Без мережі й без реального YouTube: LLM, транскрипція та публікація підмінюються фейками.

## Структура

```
audiotube/
  cli.py         команди (transcribe, describe, render, publish, web, watch, doctor, …)
  pipeline.py    оркестратор і статуси
  transcript.py  модель транскрипту, SRT
  transcribe.py  бекенди транскрипції
  llm.py         клієнти Ollama / OpenAI / Anthropic
  planner.py     промпт, схема і валідація метаданих
  render.py      FFmpeg-рендер, обкладинка, вшиті субтитри
  visuals.py     вибір/генерація фону
  metadata.py    YAML-метадані, ліміти YouTube
  youtube.py     OAuth, завантаження, субтитри, плейлисти, квота
  webui.py       локальний веб-інтерфейс
  notebooklm.py  адаптер до NotebookLM (неофіційний клієнт)
  sync.py        синхронізація ноутбуків -> inbox/<ноутбук>/
  daemon.py      автоматичний цикл: бюджет, повтори, лок, очищення
  notify.py      сповіщення демона: Telegram / notify-send
  db.py          SQLite (data/audiotube.db)
tests/           181 тест; синк, демон і публікація — без мережі й FFmpeg
deploy/systemd/  готові unit-файли для автозапуску
docs/            концепція, аудит, інструкції
```
