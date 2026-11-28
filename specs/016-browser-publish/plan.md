# 016 · План реалізації

**Спека:** [spec.md](spec.md) (approved)

## 1. Підхід

Керуємо **справжнім Google Chrome**, а не вбудованим Chromium Playwright:

1. **Перший вхід** — `browser-auth` запускає `google-chrome --user-data-dir=data/browser/youtube`
   **без** `--remote-debugging-port` і без CDP. Людина входить у Google у звичайному вікні й
   закриває його; Google не бачить automation-ознак на найчутливішому кроці.
2. **Публікація** — запускаємо Chrome з `--remote-debugging-port=<вільний>` і підключаємось
   через `playwright.chromium.connect_over_cdp()`. Playwright лише керує сторінкою.
3. Профіль на диску зберігає cookies, тому наступні запуски вже залогінені.

Альтернативу `launch_persistent_context()` відкинули: Playwright додає свої прапорці запуску,
і Google частіше блокує вхід.

Підводні камені, знайдені на живому Studio:

- `ytcp-uploads-dialog` — нульової висоти; видимий елемент — вкладений `tp-yt-paper-dialog`.
- `ytcp-thumbnail-uploader input[type=file]` (`#file-loader`) має `hidden`/`display: none` (`0×0`),
  тому очікувати `state="visible"` не можна — чекаємо `visible` для `ytcp-thumbnail-uploader`
  і `attached` для `input[type=file]`; ставимо обкладинку одразу на кроці «Деталі» при створенні відео.
- Playwright через CDP відмовляється передавати файли >50 МБ («not co-located»), тому шлях
  ставимо через `DOM.setFileInputFiles` (Chrome локальний і сам читає файл).
- Посилання в діалозі до натискання «Опублікувати» — це ще чернетка; `video_id` беремо
  з рядка «Вміст» Studio після збереження.

UI-кроки інкапсульовані в `PlaywrightStudio` (реалізація протоколу `StudioDriver`).
`BrowserPublisher` працює лише з протоколом, тому тести не потребують браузера.

## 2. Зміни по модулях

| Модуль | Зміна | Вимоги |
|--------|-------|--------|
| `audiotube/config.py` | `BrowserCfg` + поле `youtube.browser` | R1, R8, R13 |
| `audiotube/browser_publish.py` (новий) | `ChromeSession`, `PlaywrightStudio`, `BrowserPublisher`, `PublishPlan`, `PublishOutcome`, `video_id_from`, `find_chrome` | R1–R15 |
| `audiotube/cli.py` | команди `browser-auth`, `browser-upload` | R1, R2, R7, R8 |
| `audiotube/webui.py` | кнопка, `POST /api/jobs/<id>/browser-upload`, `GET /api/browser`, фоновий потік | R13 |
| `config.example.yaml`, `config.yaml` | секція `youtube.browser` | R1 |
| `docs/07_COMMANDS.md`, `README.md`, `specs/README.md` | документація й реєстр | IX |

## 3. Дані

Міграцій SQLite немає: використовуємо наявні `jobs.video_id`, `jobs.status`, `jobs.error`,
`uploads`. Нові ключі конфігу мають дефолти, тож старий `config.yaml` лишається валідним.

Профіль Chrome — `data/browser/youtube/` (у `.gitignore` разом з `data/`).
Скриншоти — `data/browser/screenshots/`.

## 4. Ризики й відкат

- **Зміна DOM Studio** — селектори з фолбеками (id → name → текст uk/en), скриншот при помилці.
  Відкат: не користуватись командою; API-режим не залежить від нового модуля.
- **Google блокує вхід** — людина входить вручну; у разі блокування підказка в консолі.
- **Автоматизація UI може суперечити ToS YouTube** — режим opt-in, `--review` для контролю,
  типово `private`.
- **Playwright відсутній** — лінивий імпорт, зрозуміла помилка з `pip install playwright`.
- **Два прогони одночасно** — замок у `WebApp`, у CLI — один процес на профіль Chrome
  (Chrome сам не дасть відкрити той самий `user-data-dir` двічі).
