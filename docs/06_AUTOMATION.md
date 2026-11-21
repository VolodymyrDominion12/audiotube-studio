# Автоматика: `audiotube daemon`

**Дата:** 2026-11-20 · **Статус:** реалізовано

Демон замикає цикл: **NotebookLM → папки-ноутбуки → транскрипт → метадані → відео → YouTube**,
не виходячи за квоту й не завантажуючи те саме двічі.

```
кожні interval_minutes:
  sync (нові аудіо з NotebookLM)  →  scan
  є бюджет на сьогодні?           →  ні: чекати
  пройшло min_hours_between?      →  ні: чекати
  є квота YouTube?                →  ні: чекати до 10:00 за Києвом
  взяти найстарішу задачу:
      transcribe → describe → render → upload
      успіх: +1 до денного бюджету, .mp4 видалити (за бажанням)
      невдача: +1 спроба; після max_attempts задачу більше не чіпати
```

> `youtube.auto_upload` стосується лише `audiotube run`; демон публікує завжди — інакше він
> не виконував би своєї роботи. Обмежувачі — `videos_per_day`, `min_hours_between` і квота.

## 1. Швидкий старт

```bash
cd ~/PycharmProjects/audiotube-studio
audiotube daemon --dry-run        # що буде зроблено, без побічних ефектів
audiotube daemon --once --max 1   # один цикл, максимум одне відео
audiotube daemon                  # безперервно (Ctrl+C — зупинка)
```

Для першого разу краще `--once --max 1`, перевірити відео в YouTube Studio, і лише потім
вмикати systemd.

## 2. systemd (рекомендовано)

Два варіанти на вибір — **не вмикайте обидва**.

### Варіант А: постійний сервіс

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/audiotube-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now audiotube-daemon
systemctl --user status audiotube-daemon
journalctl --user -u audiotube-daemon -f
```

### Варіант Б: timer раз на 30 хвилин

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/audiotube-once.service deploy/systemd/audiotube-once.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now audiotube-once.timer
journalctl --user -u audiotube-once -f
```

Timer кращий, якщо комп'ютер не працює цілодобово: пропущений запуск не «наздоганяє» десяток
відео одразу, а просто робить один цикл.

> Щоб сервіс працював і після виходу з сесії: `sudo loginctl enable-linger $USER`.

### Cron (якщо не хочете systemd)

```cron
*/30 * * * * cd $HOME/PycharmProjects/audiotube-studio && NOTEBOOKLM_HOME=$HOME/.notebooklm .venv/bin/audiotube daemon --once >> data/daemon.log 2>&1
```

Файл-лок не дасть двом запускам перетнутися.

## 3. Налаштування

Секція `daemon:` у `config.yaml`:

| Ключ | Типово | Що робить |
|------|--------|-----------|
| `interval_minutes` | 30 | як часто перевіряти чергу |
| `videos_per_day` | 2 | денний бюджет (квота дозволяє ~4) |
| `min_hours_between` | 6 | не частіше ніж раз на N годин |
| `max_attempts` | 3 | після N невдач задача заморожується |
| `order` | oldest | `oldest` — спершу старі, `newest` — свіжі |
| `sync_before_run` | true | чи кликати NotebookLM перед обробкою |
| `pause_seconds` | 10 | пауза між відео в одному циклі |
| `cleanup_video_after_upload` | true | видаляти `.mp4` після успіху (аудіо лишається) |
| `notify` | false | `false`, `desktop` (`notify-send`; старе `true`) або `telegram` — див. розд. 3.1 |
| `summary_hour` | 21 | не раніше цієї години — денне зведення; `null` вимикає |
| `quota_reserve` | 0 | скільки одиниць квоти не витрачати |

Бюджет рахується за таблицею `uploads` у SQLite, тому **ручні** `audiotube upload` теж
враховуються: демон не «перевиконає план», якщо ви вдень завантажили щось руками.

### 3.1. Сповіщення в Telegram

1. Створіть бота в [@BotFather](https://t.me/BotFather) і скопіюйте токен.
2. Напишіть боту будь-що, потім відкрийте `https://api.telegram.org/bot<токен>/getUpdates` і візьміть
   `message.chat.id`.
3. Покладіть обидва значення в `.env` у корені проєкту (він у `.gitignore`), **не** в `config.yaml`:

   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```

4. Перевірте: `audiotube notify --test` (або `--dry-run`, щоб лише побачити текст), потім
   `daemon.notify: telegram` і `audiotube doctor`.

Що приходить:

| Подія | Коли |
|-------|------|
| ✅ опубліковано | після кожної публікації: назва + посилання |
| ❌ задача заморожена | задача щойно досягла `max_attempts`; у тексті — команда `audiotube reset` |
| ⚠️ потрібна авторизація | сесія NotebookLM або токен YouTube протухли; не частіше ніж раз на 12 год |
| 📊 зведення | раз на добу після `summary_hour`: скачано, опубліковано, у черзі, з помилками |

Якщо токен YouTube протух, демон пропускає публікації циклу й **не** витрачає спроби задач, тож
після `audiotube auth` черга продовжиться з того самого місця. Недоступний Telegram ніколи не
зупиняє демон: буде лише рядок у журналі.

## 4. Квота і ліміти

Одне відео з субтитрами й обкладинкою коштує ~2 100 одиниць; ліміт — 10 000/добу.
Звідси максимум ~4 відео, а `videos_per_day: 2` лишає запас на ретраї та ручні дії.
Квота скидається опівночі за тихоокеанським часом (≈10:00 за Києвом), тому «сьогодні» в
демона — це локальна дата, а «квота» — окремий лічильник, який дивиться на Лос-Анджелес.

Поки API-проєкт не пройшов аудит Google, усі відео через API лишаються **приватними** —
це очікувано для першого етапу. Деталі: [02_YOUTUBE_SETUP.md](02_YOUTUBE_SETUP.md).

## 5. Обслуговування

```bash
audiotube status                       # задачі + витрачена квота
audiotube daemon --dry-run             # що демон зробив би зараз
tail -f data/reports/$(date +%F).jsonl # журнал успішних публікацій
systemctl --user stop audiotube-daemon # пауза
systemctl --user start audiotube-daemon
```

Зупинка м'яка: демон дочіпує поточний крок (транскрипт/рендер/upload) і виходить. Якщо вбити
його посеред `render`, наступний запуск просто переробить крок — статус у БД не зміниться.

**Диск.** 50 відео по 20 хв ≈ 15–25 ГБ. `cleanup_video_after_upload: true` видаляє mp4 після
успіху, лишаючи аудіо, `.srt` і обкладинку; за потреби відео можна перерендерити з аудіо.

## 6. Типові проблеми

| Симптом | Причина | Що робити |
|---------|---------|-----------|
| `ffmpeg/ffprobe не знайдено` | не встановлено FFmpeg | `sudo apt install ffmpeg` |
| `демон уже працює` (exit 3) | інший екземпляр тримає лок | `systemctl --user status audiotube-daemon` |
| `синк пропущено: сесія прострочена` | кукі NotebookLM | `notebooklm login` (або `auth refresh`) |
| `квота: лишилось ...` | денний ліміт YouTube | чекати ~10:00 за Києвом |
| відео приватні, хоч `default_privacy: public` | API-проєкт без аудиту | подати на аудит або публікувати вручну |
| задача «застрягла» після 3 спроб | битий файл / немає LLM | `audiotube status`, потім `audiotube reset <id> --to new` |
| OAuth падає щотижня | застосунок у режимі Testing | перевести OAuth-клієнт у Production |

## 7. Що далі

- ~~Telegram-сповіщення~~ — зроблено (spec 007, розд. 3.1). Двосторонній бот з кнопками — окрема спека.
- Розклад публікацій (`publish_at`) — коли з'явиться аудит API.
- ~~Вкладка «Ноутбуки» у веб-інтерфейсі~~ — зроблено (spec 008, `audiotube web` → `/notebooks`).
