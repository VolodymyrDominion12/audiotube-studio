# Довідник команд

**Дата:** 2026-11-23 · **Статус:** актуально для 0.3.0
**Пов'язане:** [README](../README.md) · [Автоматика](06_AUTOMATION.md) · [YouTube](02_YOUTUBE_SETUP.md) · [LLM і Whisper](04_LLM_AND_WHISPER.md)

Усі команди виконуються з кореня проєкту. Конфіг — `config.yaml` (типово поруч), ключі — `.env`.

```bash
cd ~/PycharmProjects/audiotube-studio
source .venv/bin/activate        # далі просто `audiotube`; без активації — `.venv/bin/audiotube`
audiotube doctor                 # перевірка середовища, конфігу, токена
```

## 1. Огляд

| Команда | Що робить |
|---|---|
| `init` | створює папки і `config.yaml` з прикладу |
| `doctor` | діагностика: ffmpeg, шрифти, транскрипція, LLM, NotebookLM, токен, квота |
| `sync` | забирає нові аудіоперекази з усіх ноутбуків NotebookLM у `inbox/<ноутбук>/` |
| `import` | додає вже скачані вручну файли (аварійний шлях без API) |
| `meta` | створює шаблон YAML-метаданих поруч з аудіо |
| `scan` | реєструє нові аудіо з `inbox/` у базі |
| `transcribe` | Whisper → `transcript.json` + `subtitles.srt` |
| `subtitles` | перебудовує SRT з наявного транскрипту (spec 009) |
| `describe` (`plan`) | LLM → назва, опис, теги, глави, плейлист |
| `render` | FFmpeg → `output/<slug>.mp4` + обкладинка `.jpg` |
| `auth` | одноразова авторизація YouTube (браузер) |
| `upload` | завантаження на YouTube + обкладинка + субтитри + плейлист |
| `publish` | зміна приватності вже завантаженого відео (spec 010) |
| `captions` | додає субтитри до вже завантаженого відео |
| `run` | усі кроки для всієї черги |
| `watch` | стежить за `inbox/` і обробляє нове |
| `daemon` | автоматика: sync → рендер → публікація в межах квоти й бюджету |
| `notify` | перевірка Telegram-сповіщень |
| `status` | черга задач і витрата квоти |
| `playlists` | плейлисти каналу (назва, кількість відео, приватність) |
| `reset` | відкочує задачу на попередній крок |
| `web` | локальний веб-інтерфейс на `127.0.0.1:8765` |

## 2. NotebookLM → `inbox/`

```bash
audiotube sync --dry-run            # план: скільки ноутбуків і скільки нових переказів
audiotube sync --limit 5            # максимум 5 нових файлів за запуск
audiotube sync                      # до notebooklm.max_per_run (типово 20)
audiotube sync --notebook Go        # лише ноутбук, у назві/id якого є «Go»
audiotube sync --force              # перевірити наявні: порівняти хеш, не перезаписує
```

Що лягає на диск:

```
inbox/<slug ноутбука>/
  <дата>_<назва>-<id>.m4a     # аудіо
  <дата>_<назва>-<id>.yaml    # title, notebook, notebook_id, artifact_id, source, privacy
  notebook.json               # id, назва, URL ноутбука, час синку
```

Ключ ідемпотентності — `artifact_id` у SQLite, не ім'я файлу. Повторний `sync` нічого не дублює;
файл видалили — перекачає (`repair_missing`); артефакт зник із NotebookLM — позначить `missing`,
локальний файл лишиться. Нові аудіо реєструються як задачі автоматично, `scan` не потрібен.

```bash
# один файл усередину без NotebookLM
audiotube import ~/Downloads/переказ.m4a
audiotube import ~/Downloads/ноутбук/ --move
```

## 3. Один файл: покроково

```bash
ID=185

audiotube transcribe $ID            # ~6,5 хв на 16 хв аудіо (CPU, модель small)
audiotube subtitles $ID             # перебудувати SRT за правилами spec 009 (без Whisper)
audiotube describe --force $ID      # LLM ~20 хв на CPU; --force потрібен, бо sync уже створив YAML
audiotube render $ID                # ~1 хв на 16-хв відео
audiotube upload $ID                # приватно (youtube.default_privacy)
audiotube publish $ID               # зробити публічним
```

Швидкі перевірки:

```bash
audiotube render $ID --preview 20   # перші 20 с у output/<slug>_preview.mp4
audiotube render $ID --force        # перерендерити готове
audiotube describe --force $ID      # перегенерувати метадані (ручні mode/category/privacy збережуться)
audiotube subtitles --dry-run       # що зміниться в SRT, нічого не пише
```

Файли кожного кроку:

| Крок | Результат |
|---|---|
| `transcribe` | `workspace/<slug>/transcript.json`, `workspace/<slug>/subtitles.srt` |
| `describe` | `<аудіо>.yaml` поруч з файлом в `inbox/` |
| `render` | `output/<slug>.mp4`, `output/<slug>.jpg` |
| `upload` | `video_id` у базі; статус `uploaded` → `done` |

Статуси: `new → transcribed → described → rendered → uploaded → done`. Повторний запуск кроку
нічого не дублює і не знижує статус.

## 4. Пакетно

```bash
audiotube run                       # transcribe → describe → render для всієї черги
audiotube run --upload              # + завантаження (навіть якщо auto_upload: false)
audiotube run --no-transcribe       # пропустити крок (так само --no-describe, --no-render)

audiotube run --force-describe      # перегенерувати метадані, навіть якщо YAML уже є
audiotube run --limit 10            # лише перші 10 незавершених задач
audiotube run --force-describe --limit 10   # нічний прогін беклогу

audiotube run --missing-video       # усі аудіо без output/<slug>.mp4 → транскрипт → метадані → рендер
audiotube run --missing-video --limit 10    # те саме, але 10 задач за запуск
audiotube run --no-upload           # не публікувати навіть якщо auto_upload: true

audiotube watch --interval 300      # реагувати на нові файли в inbox/
audiotube watch --once              # один прохід і вихід
```

> `--missing-video` вибирає задачі за **фактичною наявністю відео** (локальний файл або
> `video_id` на YouTube), а не за статусом: задача зі статусом `rendered`, у якої файл зник,
> буде дорендерена. Режим **ніколи не публікує** — `auto_upload` ігнорується, а поєднання з
> `--upload` є помилкою. Задачі з наявним відео не перезаписуються. `--limit N` обмежує запуск,
> повторний запуск продовжує з місця зупинки.

> `sync` створює YAML-заготовку з назвою, тому звичайний `run` вважає метадані готовими й **не
> кличе LLM**. Для опису, тегів і глав потрібен `--force-describe` (ручні `privacy`, `mode`,
> `background` при цьому зберігаються). `--limit N` обробляє перші N задач за id; перерваний
> прогін продовжується з того самого місця — просто запустіть команду ще раз.

> `run` і `watch` беруть **усю** чергу. Для 200 задач це дні безперервного CPU — для потоку
> використовуйте демон.

## 5. Публікація і приватність

```bash
audiotube publish 185                       # public (типово)
audiotube publish 185 --privacy unlisted
audiotube publish 185 --privacy private     # зняти з публікації
audiotube publish 7 8 9 --privacy public    # кілька одразу
audiotube publish 185 --dry-run             # показати, нічого не змінювати
```

Команда зберігає ліцензію, `embeddable`, `publicStatsViewable`, `selfDeclaredMadeForKids` і
позначку AI-контенту (`containsSyntheticMedia`), скасовує відкладений `publishAt`.
Квота: 1 одиниця (читання статусу) + 50 (`videos.update`) на відео.

**Відкладена публікація** — без окремої команди, у YAML перед `upload`:

```yaml
privacy: private
publish_at: 2026-11-25T18:00:00+02:00   # без часового поясу — помилка
```

`upload` поставить `private` + `publishAt`, і YouTube відкриє відео сам.

## 6. Автоматика

```bash
audiotube daemon --dry-run          # що буде зроблено, без побічних ефектів
audiotube daemon --once --max 1     # один цикл, максимум одне відео
audiotube daemon                    # безперервно: sync → transcribe → describe → render → upload
audiotube daemon --no-sync          # без звернень до NotebookLM
```

Обмежувачі — у `config.yaml`, секція `daemon:`: `videos_per_day`, `min_hours_between`,
`max_attempts`, `interval_minutes`, `notify`. Демон публікує завжди (інакше не виконував би
роботи) і тримає файловий лок, щоб два екземпляри не дублювали завантаження.

systemd (деталі — [06_AUTOMATION.md](06_AUTOMATION.md)):

```bash
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now audiotube-daemon.service    # варіант А — постійний сервіс
# АБО
systemctl --user enable --now audiotube-once.timer        # варіант Б — періодичний запуск
journalctl --user -u audiotube-daemon -f
```

Обидва варіанти одночасно не вмикайте.

## 7. Стан, квота, обслуговування

```bash
audiotube status                    # id, статус, назва; рядок квоти
audiotube playlists                 # плейлисти каналу: назва, кількість відео, приватність
audiotube reset 185 --to described  # відкотити крок
audiotube reset 185 --to new --forget-youtube   # забути video_id (upload створить нове відео)
audiotube captions 185              # додати субтитри до вже завантаженого
audiotube meta inbox/файл.m4a       # шаблон YAML для ручного редагування
audiotube notify --test             # перевірити Telegram
audiotube web                       # http://127.0.0.1:8765
```

### Веб-інтерфейс

```bash
audiotube web --port 9000 --open    # інший порт і одразу відкрити браузер
```

Локальна сторінка без збірки й залежностей: лічильники (задачі, на YouTube, готові до публікації,
аудіо в `inbox/` поза чергою, `.mp4` у `output/`, сумарні розміри аудіо й відео), фільтр за
статусом і пошук, картки задач із прев'ю, кнопки кроків, редагування YAML і зміна приватності
вже завантажених відео. Кнопки «Видалити аудіо» / «Видалити відео» звільняють місце, зберігаючи
історію: аудіо позначається в БД (`artifacts.audio_deleted`) і `sync` не качає його повторно,
а `video_id` та посилання на YouTube лишаються; «Відновити аудіо» знімає позначку. Сторінка
`/notebooks` — синк і статистика по ноутбуках. Той самий сервер віддає API:

```
GET  /api/state                                   # задачі, квота, counts, files
POST /api/scan
POST /api/sync?dry_run=1
POST /api/jobs/<id>/{transcribe,describe,render,preview,upload,reset}
POST /api/jobs/<id>/{delete_audio,delete_video,restore_audio}
POST /api/jobs/<id>/publish?privacy=public
GET/PUT /api/jobs/<id>/meta
GET  /api/notebooks, /api/daemon
```

### Квота YouTube

| Дія | Одиниць |
|---|---|
| `videos.insert` (upload) | 1 600 |
| `captions.insert` | 400 |
| обкладинка | 50 |
| `videos.update` (publish) | 50 |
| `playlists.*` | 1–50 |

Разом ≈ **2 100 на відео** → не більше **4 відео на добу** з 10 000. Скидається опівночі за
тихоокеанським часом (~10:00 за Києвом). `audiotube status` показує фактичну витрату.

## 8. Типові помилки

| Повідомлення | Що робити |
|---|---|
| `YouTube токен: запустіть audiotube auth` | `audiotube auth` (потрібен браузер) |
| `invalid_grant` / `AuthRequired` | видалити `data/token.json` і повторити `audiotube auth` |
| `403: access_denied` у браузері | додати акаунт у **Audience → Test users** або **Publish app** ([02_YOUTUBE_SETUP.md](02_YOUTUBE_SETUP.md)) |
| `Storage file not found: ~/.notebooklm/...` | `notebooklm login` (клієнт ставиться як `uv tool install "notebooklm-py[browser]"`) |
| `Ollama: немає з'єднання — timed out` | підняти `llm.timeout` (на CPU 7B — 900 с) і контекст (`num_ctx ≥ 16384`) |
| `недостатньо квоти для …` | чекати скидання квоти; `daemon.quota_reserve` лишає запас |
| `завантаження: HTTP 403` | проєкт без аудиту: відео можна лише приватним |

## 9. Рецепти

```bash
# новий переказ → опубліковане відео, один файл
audiotube sync --limit 1 && audiotube status
ID=$(audiotube status | awk '/нове/ {print $1; exit}')
audiotube transcribe $ID && audiotube describe --force $ID && audiotube render $ID
audiotube upload $ID && audiotube publish $ID

# розібрати весь беклог у межах квоти
audiotube run && audiotube daemon --once --max 4

# нічний прогін беклогу: метадані LLM + відео, по 10 задач за запуск
# (перервали/вимкнули — вранці та сама команда продовжить з місця зупинки)
nohup nice -n 10 .venv/bin/audiotube run --force-describe --limit 10 \
  >> data/nightly-$(date +%F).log 2>&1 &
tail -f data/nightly-$(date +%F).log
audiotube status

# перегенерувати метадані для задач, де вже є транскрипт
for id in $(audiotube status | awk '/описано|відрендерено/ {print $1}'); do
  audiotube describe --force "$id"
done

# опублікувати всі завантажені, але ще приватні відео
audiotube status | awk '/завантажено/ {print $1}' | xargs -r audiotube publish --privacy public
```

Останній рецепт витрачає 51 одиницю за відео — 10 відео це ~510 одиниць, безпечно в межах добової квоти.
