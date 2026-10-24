# Транскрипція та LLM: налаштування

Коротко: для повністю локального конвеєра потрібні **FFmpeg**, **faster-whisper** (або whisper.cpp)
і **Ollama**. Усе інше — опційне.

```bash
sudo apt install ffmpeg                     # якщо ще немає
uv pip install -e ".[transcribe]"           # або: pip install -e ".[transcribe]"
ollama pull qwen2.5:7b
```

Перевірка: `audiotube doctor` — покаже, що знайдено, а що ні.

---

## 1. Транскрипція

### 1.1 faster-whisper (рекомендовано)

Локально, без інтернету після завантаження моделі, добре працює на CPU.

```bash
uv pip install faster-whisper "av<16"
```

> **Важливо:** faster-whisper 1.2.x несумісний з PyAV ≥ 16 (`metadata_errors`). Якщо бачите
> `TypeError: open() got an unexpected keyword argument 'metadata_errors'` — виконайте
> `pip install "av<16"`. У `pyproject.toml` ця сумісність уже врахована в extra `transcribe`.

Конфіг:

```yaml
transcribe:
  backend: faster-whisper
  model: small          # tiny | base | small | medium | large-v3
  language: uk
  device: auto          # cpu | cuda
  compute_type: int8    # int8 для CPU, float16 для GPU
  vad: true
  model_dir: null       # null = ~/.cache/huggingface
```

Моделі завантажуються автоматично при першому запуску (~75 МБ для `tiny`, ~500 МБ для `small`,
~1.5 ГБ для `medium`, ~3 ГБ для `large-v3`).

| Модель | Швидкість на CPU | Якість української | Коли брати |
|--------|------------------|--------------------|------------|
| tiny/base | дуже швидко | низька | чернетки, тести |
| small | швидко | прийнятна | **типово для CPU** |
| medium | повільно | добра | якщо є час або GPU |
| large-v3 | дуже повільно | найкраща | GPU |

`model_dir` варто вказати, якщо домашня папка на іншому диску або обмежена за місцем.

### 1.2 whisper.cpp

Підходить, якщо не хочеться тягнути Python-залежності або є готова ggml-модель.

```yaml
transcribe:
  backend: whisper-cli
  command: whisper-cli                  # або повний шлях
  model_path: /models/ggml-medium.bin   # шлях до ggml-моделі
  language: uk
```

Команда викликається як `whisper-cli -m <model> -f <audio> -l uk -oj -of <prefix>`; очікується
`<prefix>.json` у форматі whisper.cpp.

### 1.3 OpenAI-сумісний API

Працює з OpenAI, Groq, локальним llama.cpp-сервером тощо.

```yaml
transcribe:
  backend: openai
  base_url: https://api.openai.com/v1   # або http://127.0.0.1:8080/v1
  model: whisper-1
  language: uk
  api_key_env: OPENAI_API_KEY
```

Ключ можна покласти в `.env` (файл у `.gitignore`):

```bash
cp .env.example .env
# OPENAI_API_KEY=sk-...
```

> Хмарна транскрипція означає, що **аудіо залишає ваш комп'ютер**. Для конфіденційних матеріалів
> використовуйте локальні бекенди.

### 1.4 Що з'являється на диску

```
workspace/<slug>/transcript.json   # мова, тривалість, сегменти з таймкодами
workspace/<slug>/subtitles.srt     # готові субтитри (2 рядки × 42 символи за замовчуванням)
```

Повторний запуск не перезаписує наявний транскрипт. Примусово: `audiotube transcribe --force`.

---

## 2. LLM для метаданих

### 2.1 Ollama (локально, типово)

```bash
ollama serve                 # якщо не працює як служба
ollama pull qwen2.5:7b
```

```yaml
llm:
  provider: ollama
  model: qwen2.5:7b
  base_url: null             # http://127.0.0.1:11434
  temperature: 0.4
```

Підійдуть й інші моделі з підтримкою української: `qwen2.5:14b` (краще, повільніше),
`llama3.1:8b`, `gemma2:9b`. Перевірити наявні: `ollama list`.

### 2.2 OpenAI-сумісний API

```yaml
llm:
  provider: openai
  model: <id моделі у вашому акаунті>
  base_url: null             # https://api.openai.com/v1
  api_key_env: OPENAI_API_KEY
```

Підходить і локальний сервер із OpenAI-сумісним `/chat/completions` (llama.cpp, vLLM, LM Studio):
вкажіть `base_url: http://127.0.0.1:.../v1`; ключ тоді не обов'язковий.

### 2.3 Anthropic

```yaml
llm:
  provider: anthropic
  model: <id моделі>
  api_key_env: ANTHROPIC_API_KEY
```

### 2.4 Вимкнено

```yaml
llm:
  provider: none
```

Тоді `run`/`watch` пропускають крок метаданих, а рендер бере назву/опис із YAML поруч з аудіо
(або з імені файлу). Це нормальний режим для ручної роботи.

---

## 3. Плейлисти

LLM пропонує назву плейлиста. Щоб він обирав із наявних, перелічіть їх у конфігу:

```yaml
plan:
  known_playlists:
    - "DevOps за 20 хвилин"
    - "Фізика за 20 хвилин"
  default_playlist: "Розбори за 20 хвилин"
```

Якщо плейлиста немає на каналі, він створиться автоматично (`youtube.playlist_privacy`).
Подивитися наявні: `audiotube playlists` (потрібен токен).

---

## 4. Типові проблеми

| Симптом | Причина | Рішення |
|---------|---------|---------|
| `metadata_errors` при транскрипції | PyAV ≥ 16 | `pip install "av<16"` |
| `немає доступного бекенда транскрипції` | нічого не встановлено | `pip install -e ".[transcribe]"` |
| `Ollama недоступний` | сервіс не запущений | `ollama serve` |
| `немає дозволу / read-only` при завантаженні моделі | `model_dir` у недоступному місці | вкажіть `transcribe.model_dir` у робочій папці |
| `No such filter: drawtext` | FFmpeg без libfreetype | перевстановіть FFmpeg з повною збіркою; `audiotube doctor` покаже |
| `ffprobe не знайдено` | snap ставить його як `ffmpeg.ffprobe` | застосунок шукає `ffmpeg.ffprobe` автоматично; за потреби `sudo ln -s /snap/bin/ffmpeg.ffprobe /usr/local/bin/ffprobe` |
| Субтитри не додаються на YouTube | канал/квота/регіон | дивіться повідомлення; `audiotube captions <id>` для повтору |
| Відео стало приватним | API-проєкт без аудиту | подайте проєкт на аудит або публікуйте вручну |

---

## 5. Конфіденційність і вартість

- **Локальний шлях** (faster-whisper + Ollama): аудіо й транскрипт не залишають комп'ютер.
- **Хмарний LLM**: у запит іде транскрипт (до `plan.max_transcript_chars` символів). Для 20-хв
  розмови це ~15–25 тис. символів.
- **Хмарна транскрипція**: у запит іде весь аудіофайл.
- Ключі зберігайте в `.env` або змінних оточення; `.env` уже в `.gitignore`.
- Квота YouTube: завантаження ~1600 + субтитри 400 + обкладинка 50 + плейлист ~52 одиниць.
