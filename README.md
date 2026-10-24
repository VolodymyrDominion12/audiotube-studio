# AudioTube Studio

Локальний конвеєр: аудіорозбори → YouTube-відео з фоном, назвою в кадрі, обкладинкою, описом з розділами → завантаження на канал і розкладання по плейлистах.

- Концепція і план розвитку: [docs/01_CONCEPT.md](docs/01_CONCEPT.md)
- Налаштування доступу до YouTube: [docs/02_YOUTUBE_SETUP.md](docs/02_YOUTUBE_SETUP.md)

**Статус:** Фаза 1 (MVP) — рендер + публікація + плейлисти.

## Встановлення

```bash
sudo apt install ffmpeg            # якщо ще немає
cd ~/PycharmProjects/audiotube-studio
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
audiotube init                     # створить папки і config.yaml
```

## Як користуватися

```bash
# 1. Покладіть аудіо в inbox/
cp ~/Downloads/docker_za_20_khvylyn.mp3 inbox/

# 2. (необов'язково) створіть файл метаданих і заповніть назву, опис, плейлист, розділи
audiotube meta inbox/docker_za_20_khvylyn.mp3
#    -> inbox/docker_za_20_khvylyn.yaml

# 3. Швидко гляньте, як виглядатиме (перші 20 секунд)
audiotube render --preview 20      # -> output/<slug>_preview.mp4

# 4. Повний рендер
audiotube render                   # -> output/<slug>.mp4 + output/<slug>.jpg (обкладинка)

# 5. Один раз авторизуйтесь у YouTube (див. docs/02_YOUTUBE_SETUP.md)
audiotube auth

# 6. Завантаження + плейлист
audiotube upload

audiotube status                   # що в якому стані + використана квота
```

`audiotube run` робить scan + render усього нового (і upload, якщо `youtube.auto_upload: true` або з `--upload`).

## Приклад метаданих (`inbox/<аудіо>.yaml`)

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
thumbnail_text: "Docker за 20 хв"
privacy: private
publish_at: 2026-10-25T18:00:00+03:00
```

Без цього файлу назва береться з імені аудіо, а решта — з `config.yaml`.

## Фони

| Режим | Звідки | Як рендериться |
|-------|--------|----------------|
| `image` | `assets/images/<категорія>/` | картинка з повільним наближенням-віддаленням |
| `loop`  | `assets/loops/<категорія>/` | кліп повторюється всю тривалість (`loop_pingpong: true` — вперед-назад, без стику) |
| — | нічого не знайдено | генерується абстрактний градієнт, у кожної теми свій відтінок |

Якщо в імені файлу фону є слово з тегів або назви (напр. `docker-whale.jpg`), його оберуть першим.
Безкоштовні фони з дозволом на комерційне використання: [Pexels](https://www.pexels.com/videos/), [Pixabay](https://pixabay.com/videos/).

## Як влаштований рендер

Замість кодування всіх 20 хвилин кадр за кадром кодується один безшовний сегмент (60 с), який потім повторюється без перекодування, а аудіо накладається поверх. 20-хвилинне відео 1080p рендериться приблизно за 2 хвилини на CPU (з NVIDIA — швидше, `encoder: auto` визначить сам).

## Структура

```
audiotube/
  cli.py        команди
  pipeline.py   оркестратор і статуси: new -> rendered -> uploaded -> done
  render.py     FFmpeg-рендер + обкладинка
  visuals.py    вибір/генерація фону
  metadata.py   назва, опис, теги, розділи
  youtube.py    OAuth, завантаження, плейлисти, облік квоти
  db.py         SQLite (data/audiotube.db)
tests/          python -m unittest discover -s tests
```
