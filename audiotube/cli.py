"""Командний рядок AudioTube Studio.

    audiotube init                     створити папки і config.yaml
    audiotube meta <аудіо>             створити шаблон метаданих поруч з аудіо
    audiotube scan                     зареєструвати нові аудіо з inbox/
    audiotube transcribe [id ...]      транскрипція -> transcript.json + subtitles.srt
    audiotube describe [id ...]        LLM-генерація назви/опису/тегів/глав/плейлиста
    audiotube render [id ...]          відрендерити відео й обкладинку
    audiotube auth                     авторизація YouTube (один раз)
    audiotube upload [id ...]          завантажити на YouTube + субтитри + плейлист
    audiotube captions <id>            додати субтитри до вже завантаженого відео
    audiotube run                      scan + transcribe + describe + render (+ upload)
    audiotube watch                    стежити за inbox/ і обробляти нові файли
    audiotube status                   таблиця задач і квота
    audiotube playlists                плейлисти каналу
    audiotube reset <id> [--to new]    повернути задачу на попередній крок
    audiotube web                      локальний веб-інтерфейс
    audiotube doctor                   діагностика середовища
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from audiotube import __version__
from audiotube.config import Config, load_config
from audiotube.db import STATUSES
from audiotube.media import FFmpegError, ffprobe_bin, fmt_time, require_ffmpeg
from audiotube.metadata import write_sidecar_template
from audiotube.pipeline import STATUS_ORDER, Pipeline, resolve_audio_arg

STATUS_UA = {
    "new": "нове",
    "transcribed": "транскрипт",
    "described": "метадані",
    "rendered": "відрендерено",
    "uploaded": "на YouTube",
    "done": "готово",
}


def _cfg(args) -> Config:
    return load_config(args.config, root=Path(args.root) if args.root else None)


def cmd_init(args) -> int:
    cfg = _cfg(args)
    cfg.ensure_dirs()
    target = cfg.root / "config.yaml"
    example = cfg.root / "config.example.yaml"
    if not target.exists() and example.exists():
        shutil.copyfile(example, target)
        print(f"створено {target}")
    print(f"папки готові в {cfg.root}")
    print(f"  покладіть аудіо в   {cfg.inbox}")
    print(f"  фонові картинки в   {cfg.assets / 'images' / '<категорія>'}")
    print(f"  відеолупи в         {cfg.assets / 'loops' / '<категорія>'}")
    from audiotube.transcribe import resolve_backend

    try:
        print(f"  транскрипція:       {resolve_backend(cfg)}")
    except Exception as e:  # noqa: BLE001
        print(f"  транскрипція:       недоступна — {e}")
    return 0


def cmd_meta(args) -> int:
    cfg = _cfg(args)
    for a in args.audio:
        side = write_sidecar_template(resolve_audio_arg(cfg, a), cfg, overwrite=args.overwrite)
        print(f"метадані: {side}")
    return 0


def cmd_scan(args) -> int:
    pipe = Pipeline(_cfg(args))
    added = pipe.scan()
    for j in added:
        print(f"+ [{j.id}] {j.audio.name}  ->  {j.slug}")
    print(f"нових аудіо: {len(added)}")
    return 0


def cmd_transcribe(args) -> int:
    pipe = Pipeline(_cfg(args))
    pipe.scan()
    jobs = pipe.select(args.ids, ("new", "transcribed", "described", "rendered"))
    if not jobs:
        print("немає задач для транскрипції")
        return 0
    failed = 0
    for job in jobs:
        job = pipe.transcribe(job, force=args.force)
        failed += bool(job.error)
    return 1 if failed else 0


def cmd_describe(args) -> int:
    pipe = Pipeline(_cfg(args))
    pipe.scan()
    jobs = pipe.select(args.ids, ("new", "transcribed", "described"))
    if not jobs:
        print("немає задач для генерації метаданих")
        return 0
    failed = 0
    for job in jobs:
        job = pipe.describe(job, force=args.force)
        failed += bool(job.error)
    return 1 if failed else 0


def cmd_render(args) -> int:
    require_ffmpeg()
    pipe = Pipeline(_cfg(args))
    pipe.scan()
    statuses = ("new", "transcribed", "described") if not args.force else STATUSES
    jobs = pipe.select(args.ids, statuses)
    if not jobs:
        print("немає що рендерити (нові аудіо кладіть в inbox/)")
        return 0
    failed = 0
    for job in jobs:
        res = pipe.render(job, preview=args.preview, force=args.force)
        failed += bool(res.error)
    return 1 if failed else 0


def cmd_auth(args) -> int:
    from audiotube.youtube import build_service

    cfg = _cfg(args)
    yt = build_service(cfg, interactive=True)
    ch = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    name = ch[0]["snippet"]["title"] if ch else "(канал не знайдено)"
    print(f"авторизовано. Канал: {name}")
    return 0


def cmd_upload(args) -> int:
    pipe = Pipeline(_cfg(args))
    jobs = pipe.select(args.ids, ("rendered", "uploaded"))
    if not jobs:
        print("немає відрендерених відео для завантаження")
        return 0
    failed = 0
    for job in jobs:
        res = pipe.publish(job)
        failed += bool(res.error)
    print(f"квота сьогодні: використано {pipe.quota.used()} з {pipe.quota.daily_limit}")
    return 1 if failed else 0


def cmd_captions(args) -> int:
    from audiotube.pipeline import Pipeline as _Pipeline
    from audiotube.youtube import Publisher, build_service

    cfg = _cfg(args)
    pipe = _Pipeline(cfg)
    job = pipe.db.get(args.id)
    if not job:
        print(f"задачу {args.id} не знайдено")
        return 1
    if not job.video_id:
        print("відео ще не завантажено на YouTube (спершу upload)")
        return 1
    srt = pipe._subtitles_path(job, cfg.workspace / job.slug)
    if not srt or not srt.exists():
        print("немає файлу субтитрів (спершу transcribe)")
        return 1
    pub = Publisher(build_service(cfg, interactive=True), cfg, pipe.quota)
    cid = pub.set_captions(job.video_id, srt)
    if cid:
        pipe.db.update(job.id, captions_id=cid)
        print(f"субтитри додано: {srt.name} (id {cid})")
        return 0
    print("YouTube не прийняв субтитри (див. повідомлення вище)")
    return 1


def _has_transcript(pipe: Pipeline, job) -> bool:
    from audiotube.transcribe import TRANSCRIPT_NAME

    path = job.transcript or (pipe.cfg.workspace / job.slug / TRANSCRIPT_NAME)
    return path.exists()


def _process(
    pipe: Pipeline,
    *,
    transcribe: bool,
    describe: bool,
    render: bool,
    upload: bool,
    force: bool = False,
) -> None:
    """Один прохід конвеєра по всіх незавершених задачах."""
    cfg = pipe.cfg
    pipe.scan()
    jobs = pipe.db.list(("new", "transcribed", "described"))
    for job in jobs:
        if transcribe and cfg.pipeline.auto_transcribe and job.status == "new":
            job = pipe.transcribe(job, force=force)

        if (
            describe
            and cfg.pipeline.auto_describe
            and cfg.llm.provider != "none"
            and STATUS_ORDER[job.status] < STATUS_ORDER["described"]
            and _has_transcript(pipe, job)
        ):
            job = pipe.describe(job, force=force)

        if render and cfg.pipeline.auto_render and STATUS_ORDER[job.status] < STATUS_ORDER["rendered"]:
            pipe.render(job, force=force)

    if upload or cfg.youtube.auto_upload:
        for job in pipe.db.list(("rendered", "uploaded")):
            pipe.publish(job)


def cmd_run(args) -> int:
    cfg = _cfg(args)
    if not args.no_render:
        require_ffmpeg()
    pipe = Pipeline(cfg)
    added = pipe.scan()
    if added:
        print(f"нових аудіо: {len(added)}")
    _process(
        pipe,
        transcribe=not args.no_transcribe,
        describe=not args.no_describe,
        render=not args.no_render,
        upload=args.upload,
    )
    return cmd_status(args)


def cmd_watch(args) -> int:
    cfg = _cfg(args)
    if not args.no_render:
        require_ffmpeg()
    pipe = Pipeline(cfg)
    print(f"стежу за {cfg.inbox} (інтервал {args.interval} с, Ctrl+C — вихід)")
    while True:
        _process(
            pipe,
            transcribe=not args.no_transcribe,
            describe=not args.no_describe,
            render=not args.no_render,
            upload=args.upload,
        )
        if args.once:
            return cmd_status(args)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nзупинено")
            return 0


def cmd_status(args) -> int:
    pipe = Pipeline(_cfg(args))
    jobs = pipe.db.list()
    if not jobs:
        print("задач поки немає. Покладіть аудіо в inbox/ і виконайте: audiotube scan")
    else:
        print(f"{'id':>3}  {'статус':<13} {'трив.':>7}  назва / посилання")
        print("-" * 82)
        for j in jobs:
            dur = fmt_time(j.duration) if j.duration else "—"
            title = j.title or j.audio.name
            print(f"{j.id:>3}  {STATUS_UA.get(j.status, j.status):<13} {dur:>7}  {title[:48]}")
            if j.url:
                print(f"{'':>26}{j.url}")
            if j.error:
                print(f"{'':>26}! {j.error.splitlines()[0][:70]}")
    print(f"\nквота YouTube сьогодні: {pipe.quota.used()} / {pipe.quota.daily_limit}")
    return 0


def cmd_playlists(args) -> int:
    from audiotube.youtube import Publisher, build_service

    cfg = _cfg(args)
    pipe = Pipeline(cfg)
    pub = Publisher(build_service(cfg), cfg, pipe.quota)
    for p in pub.list_playlists():
        count = p.get("contentDetails", {}).get("itemCount", "?")
        print(f"{p['snippet']['title']:<50} {count:>4} відео  [{p['status']['privacyStatus']}]  {p['id']}")
    return 0


def cmd_reset(args) -> int:
    pipe = Pipeline(_cfg(args))
    job = pipe.db.get(args.id)
    if not job:
        print(f"задачу {args.id} не знайдено")
        return 1
    fields = {"status": args.to, "error": None}
    if args.to == "new" and job.video_id and not args.forget_youtube:
        print("! відео вже на YouTube; рендер можна повторити, але завантаження не повториться.")
        print("  Щоб завантажити заново (як нове відео), додайте --forget-youtube")
    if args.forget_youtube:
        fields.update(video_id=None, playlist_id=None, captions_id=None)
    pipe.db.update(job.id, **fields)
    print(f"[{job.id}] статус -> {args.to}")
    return 0


def cmd_web(args) -> int:
    from audiotube.webui import serve

    cfg = _cfg(args)
    cfg.ensure_dirs()
    return serve(cfg, host=args.host, port=args.port, open_browser=args.open)


def cmd_doctor(args) -> int:
    cfg = _cfg(args)
    problems = 0

    def line(ok: bool | None, title: str, detail: str = "") -> None:
        mark = "OK " if ok else ("!  " if ok is False else "—  ")
        print(f"{mark}{title}" + (f": {detail}" if detail else ""))

    print(f"AudioTube Studio {__version__} · {cfg.root}\n")
    line(None, "конфіг", str(cfg.root / "config.yaml"))

    ffmpeg = shutil.which("ffmpeg")
    try:
        ffprobe = ffprobe_bin()
    except FFmpegError:
        ffprobe = None
    line(bool(ffmpeg and ffprobe), "ffmpeg/ffprobe", f"{ffmpeg or 'немає'}, {ffprobe or 'немає'}")
    if not (ffmpeg and ffprobe):
        problems += 1
    elif ffmpeg:
        try:
            filters = subprocess.run(
                [ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=15
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            filters = ""
        has_text = " drawtext " in filters
        line(has_text, "фільтр drawtext", "назва на відео" if has_text else "потрібен ffmpeg з libfreetype")
        if not has_text:
            problems += 1
        line(" subtitles " in filters, "фільтр subtitles", "для вшитих субтитрів (опційно)")

    from audiotube.render import find_font

    try:
        line(True, "шрифт", str(find_font(cfg)))
    except Exception as e:  # noqa: BLE001
        line(False, "шрифт", str(e))
        problems += 1

    from audiotube.transcribe import backend_report, resolve_backend

    for name, ok, note in backend_report(cfg):
        line(ok, f"транскрипція: {name}", note)
    try:
        line(True, "обраний бекенд", resolve_backend(cfg))
    except Exception as e:  # noqa: BLE001
        line(False, "обраний бекенд", str(e).splitlines()[0])
        problems += 1

    from audiotube.llm import provider_report

    ok, note = provider_report(cfg.llm)
    line(ok, f"LLM: {cfg.llm.provider}/{cfg.llm.model}", note)
    if not ok:
        problems += 1

    secret = cfg.path(cfg.youtube.client_secret)
    token = cfg.path(cfg.youtube.token)
    line(token.exists(), "YouTube токен", "є" if token.exists() else "запустіть audiotube auth")
    line(secret.exists(), "YouTube client_secret", "є" if secret.exists() else "див. docs/02_YOUTUBE_SETUP.md")
    if not token.exists() or not secret.exists():
        problems += 1

    nvidia = shutil.which("nvidia-smi")
    gpu_ok = False
    if nvidia:
        try:
            gpu_ok = subprocess.run(
                [nvidia, "-L"], capture_output=True, text=True, timeout=10
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            gpu_ok = False
    line(gpu_ok, "NVIDIA GPU", "nvenc доступний" if gpu_ok else "CPU-рендер (libx264)")
    line(None, "квота YouTube", f"{cfg.youtube.daily_quota} од./добу, завантаження ~{cfg.youtube.upload_cost}")

    print(f"\nпапки: {cfg.inbox}, {cfg.output}, {cfg.workspace}")
    print(f"висновок: {'усе готово' if problems == 0 else f'проблем: {problems}'}")
    return 0 if problems == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="audiotube", description="Аудіорозбори -> YouTube-відео")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-c", "--config", help="шлях до config.yaml (за замовчуванням ./config.yaml)")
    p.add_argument("--root", help="корінь проєкту (за замовчуванням поточна папка)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="створити папки і config.yaml").set_defaults(func=cmd_init)

    s = sub.add_parser("meta", help="шаблон метаданих (.yaml) поруч з аудіо")
    s.add_argument("audio", nargs="+")
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=cmd_meta)

    sub.add_parser("scan", help="зареєструвати нові аудіо з inbox/").set_defaults(func=cmd_scan)

    s = sub.add_parser("transcribe", help="транскрипція аудіо -> текст + субтитри")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--force", action="store_true", help="перезаписати наявний транскрипт")
    s.set_defaults(func=cmd_transcribe)

    s = sub.add_parser("describe", aliases=["plan"], help="LLM: назва, опис, теги, глави, плейлист")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--force", action="store_true", help="перегенерувати наявні метадані")
    s.set_defaults(func=cmd_describe)

    s = sub.add_parser("render", help="відрендерити відео")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--preview", type=float, metavar="СЕК", help="лише перші N секунд (швидка перевірка вигляду)")
    s.add_argument("--force", action="store_true", help="перерендерити навіть уже готові")
    s.set_defaults(func=cmd_render)

    sub.add_parser("auth", help="авторизація YouTube").set_defaults(func=cmd_auth)

    s = sub.add_parser("upload", help="завантажити на YouTube, додати субтитри й плейлист")
    s.add_argument("ids", nargs="*", type=int)
    s.set_defaults(func=cmd_upload)

    s = sub.add_parser("captions", help="додати субтитри до вже завантаженого відео")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_captions)

    s = sub.add_parser("run", help="scan + transcribe + describe + render (+ upload)")
    s.add_argument("--upload", action="store_true", help="завантажити навіть якщо auto_upload вимкнено")
    s.add_argument("--no-transcribe", action="store_true", help="не робити транскрипцію")
    s.add_argument("--no-describe", action="store_true", help="не генерувати метадані через LLM")
    s.add_argument("--no-render", action="store_true", help="тільки підготувати дані, без відео")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("watch", help="стежити за inbox/ і обробляти нові аудіо")
    s.add_argument("--interval", type=int, default=60, help="період перевірки, секунд (типово 60)")
    s.add_argument("--upload", action="store_true", help="одразу завантажувати на YouTube")
    s.add_argument("--once", action="store_true", help="один прохід і вихід")
    s.add_argument("--no-transcribe", action="store_true")
    s.add_argument("--no-describe", action="store_true")
    s.add_argument("--no-render", action="store_true")
    s.set_defaults(func=cmd_watch)

    sub.add_parser("status", help="стан задач").set_defaults(func=cmd_status)
    sub.add_parser("playlists", help="плейлисти каналу").set_defaults(func=cmd_playlists)

    s = sub.add_parser("reset", help="повернути задачу на крок")
    s.add_argument("id", type=int)
    s.add_argument("--to", choices=STATUSES, default="new")
    s.add_argument("--forget-youtube", action="store_true", help="забути video_id (наступний upload створить нове відео)")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("web", help="локальний веб-інтерфейс")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="відкрити браузер")
    s.set_defaults(func=cmd_web)

    sub.add_parser("doctor", help="діагностика середовища").set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except FFmpegError as e:
        print(f"помилка FFmpeg: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:  # YouTubeError, TranscriptionError, LLMError тощо — без трейсбеку
        print(f"помилка: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nперервано", file=sys.stderr)
        return 130
    except ImportError as e:
        print(f"бракує залежності: {e}. Виконайте: pip install -e .", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
