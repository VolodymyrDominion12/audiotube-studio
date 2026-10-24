# AudioTube Studio

Локальний конвеєр: аудіорозбори → транскрипт і субтитри → метадані (назва, опис, теги, глави,
плейлист) → відео з фоном і обкладинкою → завантаження на YouTube.

- Концепція і план розвитку: [docs/01_CONCEPT.md](docs/01_CONCEPT.md)
- Аналіз коду, рішення й наступні етапи: [docs/03_ANALYSIS_AND_NEXT_STEPS.md](docs/03_ANALYSIS_AND_NEXT_STEPS.md)
- Налаштування транскрипції та LLM: [docs/04_LLM_AND_WHISPER.md](docs/04_LLM_AND_WHISPER.md)
- Налаштування доступу до YouTube: [docs/02_YOUTUBE_SETUP.md](docs/02_YOUTUBE_SETUP.md)

**Статус:** Фаза 1 (MVP) завершена; Фаза 2 реалізована — транскрипція, LLM-метадані, субтитри
YouTube, веб-інтерфейс, watch-folder. Залишились зовнішні джерела фонів (Pexels/Pixabay) і
тематичні слайди — див. [roadmap](docs/03_ANALYSIS_AND_NEXT_STEPS.md#7-що-робити-далі-за-пріоритетом).

## Що потрібно

- Python 3.10+ (перевірено на 3.14);
- FFmpeg з `ffprobe` (і з `drawtext`/`libass` — для написів і вшитих субтитрів);
- опційно: `faster-whisper` для локальної транскрипції та [Ollama](https://ollama.com) для LLM.

## Встановлення

```bash
sudo apt install ffmpeg
cd ~/PycharmProjects/audiotube-studio

# варіант з uv (як у цьому репозиторії)
uv pip install -e ".[transcribe]"

# або з pip
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[transcribe]"

audiotube init        # створить папки і config.yaml
audiotube doctor      # перевірить ffmpeg, шрифти, транскрипцію, LLM, токен
```

`[transcribe]` ставить `faster-whisper` (локальна транскрипція). Якщо транскрипція не потрібна,
достатньо `pip install -e .`.

## Швидкий старт

```bash
# 1. Покладіть аудіо
cp ~/Downloads/docker_za_20_khvylyn.mp3 inbox/

# 2. Транскрипт + субтитри (локально)
audiotube transcribe
#    workspace/<slug>/transcript.json, workspace/<slug>/subtitles.srt

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

audiotube status
```

Однією командою: `audiotube run` робить `scan → transcribe → describe → render`
(і upload, якщо `youtube.auto_upload: true` або додати `--upload`).

Пропустити окремі кроки: `audiotube run --no-transcribe`, `--no-describe`, `--no-render`.

## Веб-інтерфейс

```bash
audiotube web                 # http://127.0.0.1:8765
audiotube web --port 9000 --open
```

Черга задач, прев'ю відео, кнопки «Транскрипт / Метадані LLM / Рендер / Опублікувати»,
редагування YAML-метаданих із валідацією перед збереженням. Працює локально, без зовнішніх
залежностей; кожна дія виконується у своєму з'єднанні SQLite.

## Watch-folder

```bash
audiotube watch --interval 60
audiotube watch --once --no-render        # один прохід, без відео
audiotube watch --interval 300 --upload   # повна автоматика
```

## Метадані

`describe` створює `inbox/<аудіо>.yaml`, який можна редагувати вручну. Можна й почати з шаблону:

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
python -m unittest discover -s tests          # 86 тестів
```

Тести, що потребують FFmpeg, автоматично пропускаються, якщо його немає в `PATH`.
Без мережі й без реального YouTube: LLM, транскрипція та публікація підмінюються фейками.

## Структура

```
audiotube/
  cli.py         команди (transcribe, describe, render, web, watch, doctor, …)
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
  db.py          SQLite (data/audiotube.db)
tests/           86 тестів, без мережі
docs/            концепція, аудит, інструкції
```
