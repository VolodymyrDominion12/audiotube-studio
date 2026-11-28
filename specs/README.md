# Специфікації (Spec-Driven Development)

Тут живуть специфікації AudioTube Studio. Правило одне: **спершу спека, потім код**.
Код, що розходиться зі спекою, — це баг або в коді, або в спеці; виправляємо одне з двох
в тому самому PR.

## Як влаштовано

```
specs/
  constitution.md          # незмінні принципи проєкту — їх перевіряє кожна спека
  _templates/              # шаблони spec.md / plan.md / tasks.md
  NNN-назва/
    spec.md                # ЩО і НАВІЩО: user stories, вимоги, критерії приймання
    plan.md                # ЯК: модулі, дані, CLI, ризики (для draft-фіч)
    tasks.md               # кроки реалізації, кожен — з тестом
```

- `spec.md` не описує реалізацію: лише поведінку, яку видно ззовні (CLI, файли, БД, YouTube).
- Кожна вимога має id (`SYNC-R3`), кожен критерій приймання — id (`SYNC-AC2`).
- Тести посилаються на критерій у docstring: `"""SYNC-AC2: повторний sync нічого не качає."""`.
  Так `grep -r SYNC-AC2` показує, чим вимога покрита.
- Відкриті питання позначаємо `[NEEDS CLARIFICATION: …]`. Спека з такими мітками не може
  отримати статус `approved`.

## Життєвий цикл

```
draft → approved → in-progress → implemented
                                     │
                     (зміна поведінки) └─▶ нова версія спеки, а не тихе редагування
```

1. **draft** — пишемо `spec.md` за шаблоном, перевіряємо на відповідність `constitution.md`.
2. **approved** — немає `NEEDS CLARIFICATION`, власник погодив. Пишемо `plan.md` і `tasks.md`.
3. **in-progress** — задачі з `tasks.md` закриваються тест-першими: спершу тест на AC, потім код.
4. **implemented** — усі AC покриті тестами, `pytest` зелений, docs/ оновлені.

## Реєстр

| # | Спека | Статус | Модулі |
|---|-------|--------|--------|
| 001 | [Синхронізація NotebookLM («Бібліотекар»)](001-notebooklm-sync/spec.md) | implemented | `notebooklm.py`, `sync.py` |
| 002 | [Конвеєр аудіо → відео](002-video-pipeline/spec.md) | implemented | `transcribe.py`, `planner.py`, `render.py`, `visuals.py` |
| 003 | [Публікація на YouTube](003-youtube-publish/spec.md) | implemented | `youtube.py`, `db.QuotaTracker` |
| 004 | [Демон публікацій («Видавець»)](004-publish-daemon/spec.md) | implemented | `daemon.py`, `deploy/systemd/` |
| 005 | [Версії й зникнення артефактів](005-artifact-lifecycle/spec.md) | implemented | `sync.py`, `db.py` |
| 006 | [Ноутбук → плейлист](006-notebook-playlists/spec.md) | draft (відкладено) | `youtube.py`, `sync.py` |
| 007 | [Сповіщення в Telegram](007-telegram-notify/spec.md) | implemented | `notify.py`, `daemon.py` |
| 008 | [Вкладка «Ноутбуки» у веб-UI](008-webui-notebooks/spec.md) | implemented | `webui.py`, `db.py` |
| 009 | [Поділ субтитрів за часом і швидкістю читання](009-subtitle-timing/spec.md) | implemented | `transcript.py`, `subtitles.py` |
| 010 | [Публікація завантаженого відео](010-publish-command/spec.md) | implemented | `youtube.py`, `cli.py` |
| 011 | [Веб-інтерфейс: публікація, лічильники, фільтри](011-webui-publish-filters/spec.md) | implemented | `webui.py` |
| 012 | [Нічний пакетний прогін](012-nightly-run/spec.md) | implemented | `cli.py`, `pipeline.py` |
| 013 | [Дорендер аудіо без відео](013-missing-video-render/spec.md) | implemented | `cli.py` |
| 014 | [Локальне очищення аудіо та відео](014-local-file-cleanup/spec.md) | implemented | `webui.py`, `db.py`, `sync.py` |
| 015 | [«Ноутбуки»: акордеон із лінивим завантаженням переказів](015-webui-notebook-accordion/spec.md) | implemented | `webui.py` |
| 016 | [Публікація через справжній браузер](016-browser-publish/spec.md) | implemented | `browser_publish.py`, `cli.py`, `webui.py` |
| 017 | [Головна сторінка: групи ноутбуків, ліниві задачі, синк ноутбука](017-webui-notebook-groups/spec.md) | implemented | `webui.py`, `db.py` |

Спеки 001–004 написані **після** коду (reverse-engineered): вони фіксують поточну поведінку
як контракт, щоб подальші зміни не ламали її непомітно.

## Робота з Claude Code

- Нова фіча: «напиши `specs/NNN-…/spec.md` за шаблоном для …» → рев'ю → «напиши plan.md і
  tasks.md» → «реалізуй задачі з tasks.md по одній, тест першим».
- Зміна поведінки: спершу правка спеки (новий AC), потім тест, потім код.
