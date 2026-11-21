# Налаштування доступу до YouTube

Робиться один раз, займає ~15 хвилин. Інтерфейс Google Cloud Console час від часу змінюється, тож назви пунктів можуть трохи відрізнятися.

## 1. Проєкт і API

1. Відкрийте https://console.cloud.google.com/ → у перемикачі проєктів угорі (поруч із логотипом Google Cloud) натисніть **New project** → назва, напр. `audiotube` → **Create**.
2. Переконайтеся, що в перемикачі вибрано саме цей проєкт.
3. **APIs & Services → Library** → знайдіть **YouTube Data API v3** → **Enable**.

> Беріть окремий проєкт, а не наявний (напр. від Apps Script). Якщо в проєкті OAuth уже колись налаштовували, майстра з вибором типу користувачів не буде — одразу відкриється **Overview** з метриками.

## 2. Google Auth Platform (екран згоди OAuth)

Колишній «OAuth consent screen» тепер називається **Google Auth Platform** (меню **APIs & Services → OAuth consent screen** веде туди ж). Ліве меню: Overview, Branding, Audience, Clients, Data Access, Verification Center, Settings.

1. Відкрийте **Google Auth Platform** → у новому проєкті натисніть **Get started**.
2. Пройдіть майстер:
   - **App Information** — назва застосунку і ваша пошта для підтримки;
   - **Audience** — тип користувачів **External**;
   - **Contact Information** — ваша пошта;
   - **Finish** — погодьтеся з умовами → **Create**.
3. **Audience → Test users → Add users** — додайте пошту Google-акаунта, на якому ваш YouTube-канал.

Де що шукати пізніше:

| Що | Де в Google Auth Platform |
|---|---|
| Тип користувачів (External/Internal) | **Audience** → *User type* |
| Тестові користувачі | **Audience** → *Test users* |
| Перехід у Production | **Audience** → *Publishing status* → **Publish app** |
| Назва застосунку, пошта | **Branding** |
| OAuth-клієнти | **Clients** |

> Поки застосунок у режимі **Testing**, токен діє лише 7 днів — потім треба знову виконати `audiotube auth`.
> Щоб цього уникнути, натисніть **Audience → Publish app** (перехід у Production). Для особистого використання проходити верифікацію не обов'язково: при вході Google покаже попередження «застосунок не перевірено» — натисніть «Додатково → Перейти».

## 3. OAuth-клієнт

1. **Google Auth Platform → Clients → Create client** (або **APIs & Services → Credentials → Create credentials → OAuth client ID**).
2. Application type: **Desktop app**, назва — будь-яка → **Create**.
3. У вікні з ідентифікатором натисніть **Download JSON** і збережіть файл як `data/client_secret.json` у папці проєкту.
   Якщо вікно вже закрили — **Clients** → ваш клієнт → **Download JSON** (кнопка завантаження біля секрету).

## 4. Авторизація

```bash
audiotube auth
```

Відкриється браузер → оберіть акаунт з каналом → дозвольте доступ. Токен збережеться в `data/token.json`.
Якщо на акаунті кілька каналів (Brand Account) — оберіть саме той канал, куди потрібно публікувати.

## 5. Важливі обмеження

**Відео з неперевіреного API-проєкту стають приватними.** Google блокує публічні завантаження через API для проєктів, які не пройшли аудит. Варіанти:
- завантажувати як `private` і перемикати на публічні вручну в YouTube Studio (на старті цього достатньо);
- подати заявку на аудит: форма *YouTube API Services — Audit and Quota Extension* (посилання є в документації YouTube Data API, розділ «Quota and Compliance Audits»).

**Квота.** 10 000 одиниць на добу. Одне відео «коштує» приблизно:
завантаження 1600 (`youtube.upload_cost`) + субтитри 400 (`youtube.captions_cost`) +
обкладинка 50 + плейлист ~52 ≈ **2100 одиниць**, тобто ~4 відео на добу.
`audiotube status` показує фактичне використання; квота скидається опівночі за тихоокеанським
часом (~10:00 за Києвом). Якщо квоти не вистачає, завантаження переноситься на наступний день:
помилка зберігається в задачі, а `audiotube upload` можна повторити пізніше.

**Власні обкладинки** працюють лише на верифікованому каналі: https://www.youtube.com/verify (підтвердження телефоном). Без цього відео завантажиться, а обкладинку YouTube обере сам.

**Субтитри.** Якщо `youtube.upload_captions: true` (типово), після завантаження відео
`audiotube upload` додає `subtitles.srt` через `captions.insert` (мова — `youtube.captions_language`,
назва — `youtube.captions_name`). Субтитри створюються командою `audiotube transcribe` або
автоматично з наявного транскрипту. Додати субтитри до вже завантаженого відео:
`audiotube captions <id>`. Повторне додавання не дублюється (`captions_id` у базі).

**Позначка AI-контенту.** `youtube.contains_synthetic_media: true` у конфігу виставляє відповідну позначку при завантаженні.

## Безпека

`data/client_secret.json` і `data/token.json` — це доступ до вашого каналу. Не комітьте їх у git (вони вже в `.gitignore`).
