"""Рендер відео та обкладинки.

Стратегія швидкого рендеру 20-хвилинних відео:
1. Один раз кодуємо короткий «сегмент» (за замовчуванням 60 с): картинка з повільним
   наближенням-віддаленням, або нормалізований відеолуп, + назва в кадрі.
   Сегмент безшовний: останній кадр переходить у перший.
2. Склеюємо: сегмент повторюється (`-stream_loop -1`) і копіюється без перекодування
   (`-c:v copy`), а аудіо накладається поверх. Це займає секунди, а не десятки хвилин.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from audiotube.config import Config
from audiotube.media import duration, pick_encoder, run, video_codec_args
from audiotube.visuals import Background

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def find_font(cfg: Config) -> Path:
    if cfg.render.font:
        p = cfg.path(cfg.render.font)
        if p.exists():
            return p
        raise FileNotFoundError(f"шрифт з конфігу не знайдено: {p}")
    fonts_dir = cfg.assets / "fonts"
    if fonts_dir.is_dir():
        own = sorted(fonts_dir.glob("*.[ot]tf"))
        if own:
            return own[0]
    for c in _FONT_CANDIDATES:
        if Path(c).exists():
            return Path(c)
    if shutil.which("fc-match"):
        out = subprocess.run(
            ["fc-match", "-f", "%{file}", "sans:bold:lang=uk"], capture_output=True, text=True
        ).stdout.strip()
        if out and Path(out).exists():
            return Path(out)
    raise FileNotFoundError(
        "не знайдено шрифт з кирилицею. Покладіть .ttf у assets/fonts/ або вкажіть render.font у config.yaml"
    )


@dataclass
class RenderResult:
    video: Path
    thumbnail: Path
    duration: float
    encoder: str


def _title_filter(cfg: Config, workdir: Path, title: str | None) -> str:
    """drawtext з відносними шляхами (ffmpeg запускається з cwd=workdir) — без проблем з екрануванням."""
    if not (cfg.render.overlay_title and title):
        return ""
    shutil.copyfile(find_font(cfg), workdir / "font.ttf")
    max_chars = max(20, int(cfg.render.width / (cfg.render.height * 0.045 * 0.6)))
    (workdir / "title.txt").write_text(textwrap.fill(title, width=max_chars, break_long_words=False), encoding="utf-8")
    fs = int(cfg.render.height * 0.045)
    pad = int(cfg.render.height * 0.022)
    return (
        f",drawtext=fontfile=font.ttf:textfile=title.txt:fontsize={fs}:fontcolor=white"
        f":line_spacing={fs // 4}:x=(w-text_w)/2:y=h-text_h-h*0.08"
        f":box=1:boxcolor=black@0.45:boxborderw={pad}"
    )


def build_segment(bg: Background, cfg: Config, workdir: Path, title: str | None) -> Path:
    r = cfg.render
    W, H, fps = r.width, r.height, r.fps
    encoder = pick_encoder(r.encoder)
    seg = workdir / "segment.mp4"
    overlay = _title_filter(cfg, workdir, title)
    codec = video_codec_args(encoder, r.crf) + ["-g", str(fps * 2), "-pix_fmt", "yuv420p", "-an"]
    src = str(bg.path.resolve())

    if bg.kind == "image":
        if r.zoom > 0:
            n = r.segment_seconds * fps
            # 1 -> 1+zoom -> 1 за один сегмент (косинус): стик між повторами непомітний
            z = f"1+{r.zoom}*(1-cos(2*PI*on/{n}))/2"
            vf = (
                f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
                f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps}"
                f"{overlay},format=yuv420p"
            )
        else:
            n = 2 * fps  # статичний кадр: короткого сегмента достатньо
            vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}{overlay},format=yuv420p"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loop", "1", "-framerate", str(fps), "-i", src,
               "-vf", vf, "-frames:v", str(n), *codec, seg.name]
    else:
        base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},setsar=1"
        clip_len = duration(bg.path)
        if r.loop_pingpong:
            # вперед + назад: безшовний повтор для будь-якого кліпу. Reverse тримає кадри
            # в пам'яті, тому шматок навмисно короткий (<=20 с).
            part = max(1.0, min(20.0, float(r.segment_seconds), clip_len))
            fc = (
                f"[0:v]trim=duration={part:.3f},setpts=PTS-STARTPTS,{base},split[a][b];"
                f"[b]reverse[rv];[a][rv]concat=n=2:v=1:a=0{overlay},format=yuv420p[v]"
            )
            cmd = ["ffmpeg", "-y", "-hide_banner", "-i", src, "-filter_complex", fc, "-map", "[v]", *codec, seg.name]
        else:
            vf = f"{base}{overlay},format=yuv420p"
            segment = max(1, min(r.segment_seconds, int(clip_len)))
            cmd = ["ffmpeg", "-y", "-hide_banner", "-i", src, "-t", str(segment), "-vf", vf, *codec, seg.name]

    run(cmd, "рендер сегмента", cwd=workdir)
    return seg


def mux(segment: Path, audio: Path, out: Path, cfg: Config, length: float) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-stream_loop", "-1", "-i", str(segment), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", cfg.render.audio_bitrate,
        "-t", f"{length:.3f}", "-movflags", "+faststart", str(out),
    ]
    run(cmd, "склеювання відео з аудіо")
    return out


def _background_frame(bg: Background, workdir: Path) -> Path:
    if bg.kind == "image":
        return bg.path
    frame = workdir / "frame.png"
    run(["ffmpeg", "-y", "-hide_banner", "-ss", "1", "-i", str(bg.path), "-frames:v", "1", str(frame)],
        "кадр для обкладинки")
    return frame


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font_path: Path, max_w: int, max_h: int):
    # найбільший шрифт, а для нього — найменше рядків, що вміщаються
    for size in range(150, 30, -6):
        font = ImageFont.truetype(str(font_path), size)
        for width in range(len(text), 5, -1):
            wrapped = textwrap.fill(text, width=width, break_long_words=False)
            box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=size // 5)
            if box[2] - box[0] <= max_w and box[3] - box[1] <= max_h:
                return wrapped, font
    font = ImageFont.truetype(str(font_path), 30)
    return textwrap.fill(text, width=30, break_long_words=False), font


def make_thumbnail(bg: Background, text: str, cfg: Config, workdir: Path, out: Path) -> Path:
    """Обкладинка 1280x720: фон + затемнення зліва + великий текст."""
    W, H = 1280, 720
    img = Image.open(_background_frame(bg, workdir)).convert("RGB")
    img = ImageOps.fit(img, (W, H), Image.LANCZOS)

    shade = Image.new("L", (W, H))
    sd = ImageDraw.Draw(shade)
    for x in range(W):
        sd.line([(x, 0), (x, H)], fill=int(200 * max(0.0, 1 - x / (W * 0.85))) + 40)
    img = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img, shade)

    draw = ImageDraw.Draw(img)
    margin = 70
    wrapped, font = _fit_text(draw, text, find_font(cfg), int(W * 0.72), H - 2 * margin)
    box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=font.size // 5)
    y = (H - (box[3] - box[1])) // 2 - box[1]
    draw.multiline_text((margin, y), wrapped, font=font, fill="white", spacing=font.size // 5,
                        stroke_width=max(2, font.size // 25), stroke_fill="black")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=90)
    return out


def burn_in_subtitles(video: Path, srt: Path, cfg: Config, workdir: Path) -> Path:
    """Вшиває .srt у відео (окремий прохід перекодування — повільно, але універсально)."""
    font = find_font(cfg)
    fonts_dir = workdir / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(font, fonts_dir / font.name)
    family = ImageFont.truetype(str(font), 20).getname()[0]
    shutil.copyfile(srt, workdir / "subs.srt")

    style = ",".join([
        f"FontName={family}",
        f"Fontsize={cfg.render.subtitle_font_size}",
        f"MarginV={cfg.render.subtitle_margin}",
        "PrimaryColour=&H00FFFFFF",
        "OutlineColour=&H66000000",
        "BorderStyle=1",
        "Outline=2",
        "Shadow=0",
        "Alignment=2",
    ])
    tmp = video.with_name(f"{video.stem}_subs.mp4")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-i", str(video.resolve()),
        "-vf", f"subtitles=subs.srt:fontsdir=fonts:force_style='{style}'",
        *video_codec_args(pick_encoder(cfg.render.encoder), cfg.render.crf),
        "-c:a", "copy", "-movflags", "+faststart", str(tmp.resolve()),
    ]
    run(cmd, "вшивання субтитрів", cwd=workdir)
    tmp.replace(video)
    return video


def render_video(
    audio: Path, bg: Background, title: str, thumb_text: str, cfg: Config,
    workdir: Path, out_video: Path, out_thumb: Path, preview_seconds: float | None = None,
    subtitles: Path | None = None,
) -> RenderResult:
    workdir.mkdir(parents=True, exist_ok=True)
    length = duration(audio)
    if preview_seconds:
        length = min(length, preview_seconds)
    seg = build_segment(bg, cfg, workdir, title)
    mux(seg, audio, out_video, cfg, length)
    if subtitles and cfg.render.burn_subtitles:
        burn_in_subtitles(out_video, subtitles, cfg, workdir)
    make_thumbnail(bg, thumb_text, cfg, workdir, out_thumb)
    return RenderResult(out_video, out_thumb, length, pick_encoder(cfg.render.encoder))
