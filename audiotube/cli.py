"""Командний рядок AudioTube Studio.

    audiotube init                  створити папки і config.yaml
    audiotube meta <аудіо>          створити шаблон метаданих поруч з аудіо
    audiotube scan                  зареєструвати нові аудіо з inbox/
    audiotube render [id ...]       відрендерити (за замовчуванням усі нові)
    audiotube auth                  авторизація YouTube (один раз)
    audiotube upload [id ...]       завантажити на YouTube + плейлист
    audiotube run                   scan + render (+ upload, якщо youtube.auto_upload)
    audiotube status                таблиця задач і квота
    audiotube playlists             плейлисти каналу
    audiotube reset <id> [--to new] повернути задачу на попередній крок
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from audiotube import __version__
from audiotube.config import Config, load_config
from audiotube.db import STATUSES
from audiotube.media import FFmpegError, fmt_time, require_ffmpeg
from audiotube.metadata import write_sidecar_template
from audiotube.pipeline import Pipeline, resolve_audio_arg

STATUS_UA = {"new": "нове", "rendered": "відрендерено", "uploaded": "на YouTube", "done": "готово"}


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


def cmd_render(args) -> int:
    require_ffmpeg()
    pipe = Pipeline(_cfg(args))
    pipe.scan()
    jobs = pipe.select(args.ids, ("new",))
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


def cmd_run(args) -> int:
    require_ffmpeg()
    cfg = _cfg(args)
    pipe = Pipeline(cfg)
    added = pipe.scan()
    if added:
        print(f"нових аудіо: {len(added)}")
    for job in pipe.db.list(("new",)):
        pipe.render(job)
    if cfg.youtube.auto_upload or args.upload:
        for job in pipe.db.list(("rendered", "uploaded")):
            pipe.publish(job)
    return cmd_status(args)


def cmd_status(args) -> int:
    pipe = Pipeline(_cfg(args))
    jobs = pipe.db.list()
    if not jobs:
        print("задач поки немає. Покладіть аудіо в inbox/ і виконайте: audiotube scan")
    else:
        print(f"{'id':>3}  {'статус':<13} {'трив.':>7}  назва / посилання")
        print("-" * 78)
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
        fields.update(video_id=None, playlist_id=None)
    pipe.db.update(job.id, **fields)
    print(f"[{job.id}] статус -> {args.to}")
    return 0


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

    s = sub.add_parser("render", help="відрендерити відео")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--preview", type=float, metavar="СЕК", help="лише перші N секунд (швидка перевірка вигляду)")
    s.add_argument("--force", action="store_true", help="перерендерити навіть уже готові")
    s.set_defaults(func=cmd_render)

    sub.add_parser("auth", help="авторизація YouTube").set_defaults(func=cmd_auth)

    s = sub.add_parser("upload", help="завантажити на YouTube і додати в плейлист")
    s.add_argument("ids", nargs="*", type=int)
    s.set_defaults(func=cmd_upload)

    s = sub.add_parser("run", help="scan + render (+ upload)")
    s.add_argument("--upload", action="store_true", help="завантажити навіть якщо auto_upload вимкнено")
    s.set_defaults(func=cmd_run)

    sub.add_parser("status", help="стан задач").set_defaults(func=cmd_status)
    sub.add_parser("playlists", help="плейлисти каналу").set_defaults(func=cmd_playlists)

    s = sub.add_parser("reset", help="повернути задачу на крок")
    s.add_argument("id", type=int)
    s.add_argument("--to", choices=STATUSES, default="new")
    s.add_argument("--forget-youtube", action="store_true", help="забути video_id (наступний upload створить нове відео)")
    s.set_defaults(func=cmd_reset)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except FFmpegError as e:
        print(f"помилка FFmpeg: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:  # YouTubeError та інші очікувані помилки — без трейсбеку
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
