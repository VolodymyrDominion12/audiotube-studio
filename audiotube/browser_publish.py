"""Публікація через справжній браузер: YouTube Studio UI замість Data API (spec 016).

Керуємо окремим процесом Google Chrome (профіль у `data/browser/youtube`), підключаємось
до нього через CDP. Перший запуск — ручний вхід людини; далі сесія живе в профілі.

Модуль ліниво імпортує Playwright, тому решта програми працює без нього. Уся залежність
від DOM Studio — у `PlaywrightStudio`; `BrowserPublisher` працює з протоколом `StudioDriver`,
тому тести не потребують браузера й мережі.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from audiotube.config import Config
from audiotube.metadata import VideoMeta

if TYPE_CHECKING:  # pragma: no cover — лише для типів
    from audiotube.db import Job
    from audiotube.pipeline import Pipeline

STUDIO_URL = "https://studio.youtube.com/"
UPLOAD_URL = "https://www.youtube.com/upload"
PRIVACY_VALUES = ("public", "unlisted", "private")
PRIVACY_RADIO = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}
PRIVACY_TEXT = {
    "public": ("Публічний", "Public"),
    "unlisted": ("За посиланням", "Unlisted"),
    "private": ("Приватний", "Private"),
}

VIDEO_ID_PATTERNS = (
    r"/video/([A-Za-z0-9_-]{11})(?:[/?#]|$)",
    r"[?&]v=([A-Za-z0-9_-]{11})(?:[&#]|$)",
    r"youtu\.be/([A-Za-z0-9_-]{11})(?:[/?#]|$)",
    r"/shorts/([A-Za-z0-9_-]{11})(?:[/?#]|$)",
)

AUTH_HINT = (
    "увійдіть у YouTube у вікні браузера або виконайте: audiotube browser-auth. "
    "Якщо Google каже «браузер небезпечний», закрийте вікно програми й увійдіть у цей самий "
    "профіль звичайним Chrome: google-chrome --user-data-dir=<профіль>, потім повторіть команду"
)

CHROME_NAMES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
)


class BrowserError(RuntimeError):
    """Помилка браузерної публікації."""


class BrowserAuthError(BrowserError):
    """Немає входу в Google, а інтерактивний логін заборонено."""


class VideoIdUnknown(BrowserError):
    """Відео, ймовірно, опубліковано, але id прочитати не вдалося."""


# --- чисті функції -----------------------------------------------------------


def video_id_from(text: str | None) -> str | None:
    """Витягує 11-символьний id відео з URL Studio, watch-посилання або youtu.be."""
    if not text:
        return None
    for pattern in VIDEO_ID_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def find_chrome(cfg: Config) -> str | None:
    """Шлях до Chrome: конфіг → $CHROME_PATH → типові назви в PATH."""
    configured = cfg.youtube.browser.chrome
    if configured:
        expanded = Path(configured).expanduser()
        if expanded.is_file():
            return str(expanded)
        return shutil.which(configured)
    from_env = os.environ.get("CHROME_PATH")
    if from_env and Path(from_env).expanduser().is_file():
        return str(Path(from_env).expanduser())
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


@dataclass
class PublishPlan:
    """Все, що треба заповнити у формі Studio; чиста логіка без браузера."""

    title: str
    description: str
    tags: list[str]
    privacy: str
    made_for_kids: bool
    synthetic: bool
    thumbnail: Path | None = None
    captions: Path | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class PublishOutcome:
    video_id: str | None = None
    published: bool = False
    thumbnail: bool = False
    captions: bool = False
    warnings: list[str] = field(default_factory=list)


def build_plan(
    meta: VideoMeta,
    cfg: Config,
    *,
    thumbnail: Path | None = None,
    captions: Path | None = None,
) -> PublishPlan:
    """Перетворює метадані у значення полів Studio (spec 016, BROW-R2/R9)."""
    warnings: list[str] = []
    privacy = meta.privacy or cfg.youtube.default_privacy
    if privacy not in PRIVACY_VALUES:
        privacy = "private"
    if meta.publish_at:
        warnings.append(
            "publish_at не підтримується у браузерному режимі: "
            "відео буде збережено як private, заплануйте вихід вручну в Studio"
        )
        privacy = "private"
    if captions and not cfg.youtube.upload_captions:
        captions = None
    return PublishPlan(
        title=meta.youtube_title(),
        description=meta.youtube_description(cfg.description.footer),
        tags=meta.youtube_tags(cfg.description.default_tags),
        privacy=privacy,
        made_for_kids=cfg.youtube.made_for_kids,
        synthetic=cfg.youtube.contains_synthetic_media,
        thumbnail=thumbnail,
        captions=captions,
        warnings=warnings,
    )


# --- протокол драйвера --------------------------------------------------------


class StudioDriver(Protocol):
    """Мінімум, потрібний оркестратору; у тестах — FakeDriver без браузера."""

    def sign_in(self, *, interactive: bool = True) -> str: ...

    def publish(
        self,
        video: Path,
        meta: VideoMeta,
        *,
        thumbnail: Path | None,
        captions: Path | None,
        review: bool,
        dry_run: bool,
    ) -> PublishOutcome: ...

    def finish(
        self,
        video_id: str,
        meta: VideoMeta,
        *,
        thumbnail: Path | None,
        captions: Path | None,
    ) -> PublishOutcome: ...

    def close(self) -> None: ...


# --- сесія Chrome -------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_devtools(port: int, proc: subprocess.Popen, timeout: float = 40.0) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise BrowserError("Chrome завершився, не відкривши порт налагодження")
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:  # noqa: BLE001 — порт ще не готовий
            time.sleep(0.2)
    raise BrowserError(f"Chrome не відкрив порт налагодження {port} за {timeout:.0f} с")


class ChromeSession:
    """Запуск справжнього Chrome із виділеним профілем + CDP-підключення Playwright."""

    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self.log = log
        self._playwright = None
        self._browser = None
        self._page = None
        self._proc: subprocess.Popen | None = None
        self._port: int | None = None
        self._log_file = None
        self._log_path: Path | None = None
        self._log_offset = 0

    # --- профіль ---------------------------------------------------------
    def profile_path(self) -> Path:
        path = self.cfg.path(self.cfg.youtube.browser.profile_dir)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(stat.S_IRWXU)
        except OSError:  # pragma: no cover — екзотична ФС
            pass
        return path

    def screenshots_dir(self) -> Path:
        path = self.cfg.path("data/browser/screenshots")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- життєвий цикл ----------------------------------------------------
    def open(self) -> "ChromeSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover — залежність є в проєкті
            raise BrowserError(
                "браузерна публікація потребує Playwright: pip install playwright"
            ) from exc

        executable = find_chrome(self.cfg)
        if not executable:
            raise BrowserError(
                "не знайдено Google Chrome. Установіть його або вкажіть шлях у "
                "youtube.browser.chrome"
            )
        profile = self.profile_path()
        for extra in ([], ["--no-sandbox"]):
            try:
                self._launch(executable, profile, extra)
                return self
            except Exception as exc:  # noqa: BLE001 — пробуємо обхід пісочниці
                sandbox = self._sandbox_problem()
                self.close()
                if extra or not sandbox:
                    if isinstance(exc, BrowserError):
                        raise
                    raise BrowserError(f"не вдалося підключитися до Chrome: {exc}") from exc
                self.log(
                    "! Chrome не може запустити пісочницю (SUID helper недоступний) — "
                    "запускаю з --no-sandbox"
                )
        return self

    def _launch(self, executable: str, profile: Path, extra: list[str]) -> None:
        from playwright.sync_api import sync_playwright

        self._port = _free_port()
        self._log_path = profile.parent / "chrome.log"
        self._log_file = open(self._log_path, "a", encoding="utf-8")
        self._log_offset = self._log_file.tell()
        args = [
            executable,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-search-engine-choice-screen",
            "--start-maximized",
            *extra,
            "about:blank",
        ]
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=self._log_file,
            start_new_session=True,
        )
        _wait_devtools(self._port, self._proc)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self._port}"
        )
        context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else self._browser.new_context()
        )
        self._page = context.pages[0] if context.pages else context.new_page()
        self._page.set_default_timeout(self.cfg.youtube.browser.timeout * 1000)

    def _sandbox_problem(self) -> bool:
        if self._log_path is None:
            return False
        try:
            with open(self._log_path, encoding="utf-8", errors="ignore") as handle:
                handle.seek(self._log_offset)
                tail = handle.read()[-8000:].lower()
        except OSError:
            return False
        return "sandbox" in tail and (
            "suid" in tail or "setuid" in tail or "configured correctly" in tail
        )

    def launch_plain(self, url: str, timeout: float | None = None) -> None:
        """Відкриває Chrome без CDP — щоб людина увійшла без слідів автоматизації.

        Чекає, доки вікно закриють; після цього cookies уже в профілі. Якщо Chrome
        не може запустити пісочницю (SUID helper), повторює з `--no-sandbox`.
        """
        executable = find_chrome(self.cfg)
        if not executable:
            raise BrowserError("не знайдено Google Chrome")
        profile = self.profile_path()
        timeout = timeout or float(self.cfg.youtube.browser.login_timeout)
        self.log(
            "Увійдіть у Google у вікні Chrome. Після успішного входу ЗАКРИЙТЕ вікно — "
            "програма перевірить сесію й продовжить."
        )
        for extra in ([], ["--no-sandbox"]):
            args = [
                executable,
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-search-engine-choice-screen",
                "--start-maximized",
                *extra,
                url,
            ]
            with open(profile.parent / "chrome.log", "a", encoding="utf-8") as log:
                proc = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    start_new_session=True,
                )
            time.sleep(2)
            if proc.poll() not in (None, 0) and not extra:
                continue  # найімовірніше, знову пісочниця
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                raise BrowserAuthError(
                    f"вікно Chrome не закрили за {timeout:.0f} с. "
                    "Увійдіть і закрийте вікно, потім повторіть: audiotube browser-auth"
                )
            except KeyboardInterrupt:
                proc.terminate()
                raise
            if proc.returncode == 0:
                return
            if extra:
                raise BrowserError(
                    f"Chrome завершився з кодом {proc.returncode}. Див. data/browser/chrome.log"
                )
        raise BrowserError("Chrome не запустився (див. data/browser/chrome.log)")

    @property
    def page(self):
        if self._page is None:
            raise BrowserError("браузер не відкрито")
        return self._page

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None
        self._page = None
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self._proc.kill()
        self._proc = None
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def detach(self) -> None:
        """Лишити вікно Chrome відкритим (--keep-open), відпустивши Playwright."""
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        self._playwright = None
        self._browser = None
        self._page = None
        self._proc = None
        self.log("браузер лишено відкритим — закрийте вікно вручну")

    def screenshot(self, label: str) -> Path | None:
        if self._page is None or self._page.is_closed():
            return None
        try:
            path = self.screenshots_dir() / f"{datetime.now():%Y%m%d-%H%M%S}-{label}.png"
            self._page.screenshot(
                path=str(path), full_page=False, timeout=10_000, animations="disabled"
            )
            return path
        except Exception:  # noqa: BLE001 — скриншот не має ховати основну помилку
            return None

    # --- вхід і канал ------------------------------------------------------
    def goto_studio(self) -> None:
        self.page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=90_000)
        self.dismiss_consent()

    def dismiss_consent(self) -> None:
        selectors = (
            'button:has-text("Прийняти все")',
            'button:has-text("Accept all")',
            'button:has-text("Погоджуюся")',
            'button:has-text("I agree")',
            'tp-yt-paper-button:has-text("Accept all")',
            'tp-yt-paper-button:has-text("Прийняти все")',
            "#introAgreeButton",
        )
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                if locator.count() and locator.first.is_visible():
                    locator.first.click(timeout=2_000)
                    return
            except Exception:  # noqa: BLE001
                continue

    def is_logged_in(self) -> bool:
        page = self._page
        if page is None or page.is_closed():
            return False
        if "accounts.google.com" in page.url:
            return False
        if not page.url.startswith("https://studio.youtube.com"):
            return False
        try:
            page.wait_for_selector(
                "#avatar-btn, ytcp-navigation, ytcp-entity-icon, ytcp-uploads-dialog",
                timeout=12_000,
            )
            return True
        except Exception:  # noqa: BLE001 — елемент не з'явився
            return False

    def wait_for_login(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._page is None or self._page.is_closed():
                raise BrowserError("вікно браузера закрито до завершення входу")
            if self.is_logged_in():
                return
            time.sleep(2)
        raise BrowserAuthError(
            f"не дочекалися входу в Google за {timeout:.0f} с. "
            "Спробуйте ще раз: audiotube browser-auth"
        )

    def channel_name(self) -> str:
        page = self.page
        for selector in ("#channel-title", "ytcp-channel-title", "#entity-name"):
            try:
                locator = page.locator(selector)
                if locator.count() and locator.first.is_visible():
                    text = locator.first.inner_text().strip()
                    if text:
                        return text
            except Exception:  # noqa: BLE001
                continue
        title = (page.title() or "").strip()
        for suffix in (" - YouTube Studio", " – YouTube Studio", " - YouTube"):
            title = title.replace(suffix, "")
        return title or "YouTube"

    def channel_id(self) -> str | None:
        page = self._page
        if page is None:
            return None
        match = re.search(r"/channel/(UC[\w-]{22})", page.url)
        if match:
            return match.group(1)
        try:
            match = re.search(r'"channelId"\s*:\s*"(UC[\w-]{22})"', page.content())
        except Exception:  # noqa: BLE001
            return None
        return match.group(1) if match else None


# --- драйвер YouTube Studio ---------------------------------------------------


class PlaywrightStudio:
    """Реальні кроки в UI Studio. Усе, що може змінитися, — тут і тільки тут."""

    def __init__(self, session, cfg: Config, log=print):
        self.session = session
        self.cfg = cfg
        self.log = log
        self._channel: str | None = None
        self._channel_id: str | None = None

    # --- службове ---------------------------------------------------------
    @property
    def page(self):
        return self.session.page

    def _click_text(
        self,
        labels: tuple[str, ...],
        *,
        scope=None,
        timeout: float = 8.0,
    ) -> bool:
        """Клік по кнопці/посиланню з одним із підписів (uk/en)."""
        root = scope or self.page
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for label in labels:
                for kind in ("button", "link"):
                    try:
                        locator = root.get_by_role(kind, name=label, exact=False)
                        count = locator.count()
                        for index in range(min(count, 4)):
                            item = locator.nth(index)
                            if item.is_visible():
                                item.click(timeout=3_000)
                                return True
                    except Exception:  # noqa: BLE001
                        continue
                try:
                    locator = root.get_by_text(label, exact=False)
                    count = locator.count()
                    for index in range(min(count, 4)):
                        item = locator.nth(index)
                        if item.is_visible():
                            item.click(timeout=3_000)
                            return True
                except Exception:  # noqa: BLE001
                    continue
            time.sleep(0.4)
        return False

    def _wait_visible(self, selectors: str, timeout: float = 60.0):
        locator = self.page.locator(selectors).first
        locator.wait_for(state="visible", timeout=timeout * 1000)
        return locator

    def _fill_editable(self, locator, text: str) -> None:
        locator.click()
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        try:
            locator.fill(text)
        except Exception:  # noqa: BLE001 — деякі версії Studio не дають fill
            lines = text.split("\n")
            for index, line in enumerate(lines):
                if index:
                    self.page.keyboard.press("Enter")
                if line:
                    self.page.keyboard.insert_text(line)

    def _dialog(self):
        """Видима частина діалогу: хост `ytcp-uploads-dialog` має height:0, тому не годиться."""
        return self.page.locator(
            "ytcp-uploads-dialog tp-yt-paper-dialog, "
            "ytcp-upload-dialog tp-yt-paper-dialog"
        ).first

    def _screenshot(self, label: str) -> str:
        path = self.session.screenshot(label)
        return f" Скриншот: {path}" if path else ""

    def _ensure_open(self, where: str = "роботи") -> None:
        is_closed = getattr(self.page, "is_closed", None)
        if callable(is_closed) and is_closed():
            raise BrowserError(f"вікно браузера закрито під час {where}")

    # --- вхід -------------------------------------------------------------
    def sign_in(self, *, interactive: bool = True) -> str:
        if self._channel:
            return self._channel
        self.session.goto_studio()
        try:
            self.page.bring_to_front()
        except Exception:  # noqa: BLE001 — фокус вікна не критичний
            pass
        if self.session.is_logged_in():
            self._remember_channel()
            return self._channel
        profile = self.cfg.path(self.cfg.youtube.browser.profile_dir)
        if not interactive:
            raise BrowserAuthError(
                f"немає входу в YouTube (профіль {profile}). " + AUTH_HINT
            )
        # Вхід — у звичайному вікні Chrome без CDP: Google часто блокує логін
        # у браузері, до якого підключена автоматизація.
        self.log("немає входу — відкриваю Chrome без автоматизації")
        self.session.close()
        self.session.launch_plain(STUDIO_URL, self.cfg.youtube.browser.login_timeout)
        self.session.open()
        self.session.goto_studio()
        if not self.session.is_logged_in():
            raise BrowserAuthError(
                "вхід не завершено: не бачу акаунта в Studio. "
                "Спробуйте ще раз: audiotube browser-auth. " + AUTH_HINT
            )
        self._remember_channel()
        self.log(f"вхід виконано. Канал: {self._channel}")
        return self._channel

    def _remember_channel(self) -> None:
        self._channel = self.session.channel_name()
        self.log(f"канал: {self._channel}")
        try:
            self._channel_id = self.session.channel_id()
        except Exception:  # noqa: BLE001 — id потрібен лише для фолбеку
            self._channel_id = None

    # --- публікація -------------------------------------------------------
    def publish(
        self,
        video: Path,
        meta: VideoMeta,
        *,
        thumbnail: Path | None,
        captions: Path | None,
        review: bool,
        dry_run: bool,
    ) -> PublishOutcome:
        plan = build_plan(meta, self.cfg, thumbnail=thumbnail, captions=captions)
        for warning in plan.warnings:
            self.log(f"! {warning}")
        try:
            self._open_upload_dialog()
            if dry_run:
                # Не вибираємо файл: інакше Studio починає 100+ МБ завантаження й показує
                # тимчасове посилання, яке легко сплутати з уже опублікованим відео.
                shot = self.session.screenshot("dry-run")
                suffix = f" Скриншот: {shot}" if shot else ""
                self.log(
                    "dry-run: доступ до Studio є; файл не вибрано, щоб не створювати чернетку"
                    + suffix
                )
                return PublishOutcome(published=False, warnings=list(plan.warnings))

            self._select_file(video)
            self._wait_details()
            thumbnail_done = bool(self._fill_details(plan))
            self._advance_to_visibility(plan.privacy)
            self._select_visibility(plan)

            # Браузерне завантаження йде, лише поки відкрите вікно: дотискати «Опублікувати»
            # раніше 100% означає обірвати файл на півдорозі (див. spec 016, R2).
            if not self._wait_upload_complete():
                self.log("! не дочекалися 100% завантаження — публікую те, що встигло")

            if review:
                shot = self.session.screenshot("review")
                suffix = f" Скриншот: {shot}" if shot else ""
                self.log(
                    "перевірте відео, натисніть «Опублікувати» (або «Зберегти») і закрийте "
                    f"діалог; чекаю до {self.cfg.youtube.browser.publish_timeout} с" + suffix
                )
                timeout = float(self.cfg.youtube.browser.publish_timeout)
            else:
                self._click_done()
                timeout = 900.0

            video_id = self._wait_video_id(plan, timeout=timeout, require_close=review)
            if not video_id:
                raise VideoIdUnknown(
                    "відео, ймовірно, опубліковано, але id не прочитано. "
                    "Перевірте Studio; повторний запуск не має дублювати відео."
                    + self._screenshot("video-id-unknown")
                )
            self.log(f"відео опубліковано: https://youtu.be/{video_id}")
            out = PublishOutcome(
                video_id=video_id,
                published=True,
                thumbnail=thumbnail_done,
                warnings=list(plan.warnings),
            )
            self._finish_assets(video_id, plan, out)
            return out
        except VideoIdUnknown:
            raise
        except Exception as exc:
            shot = "" if "Скриншот:" in str(exc) else self._screenshot("publish-error")
            raise BrowserError(f"{exc}{shot}") from exc

    def finish(
        self,
        video_id: str,
        meta: VideoMeta,
        *,
        thumbnail: Path | None,
        captions: Path | None,
    ) -> PublishOutcome:
        plan = build_plan(meta, self.cfg, thumbnail=thumbnail, captions=captions)
        out = PublishOutcome(
            video_id=video_id, published=True, warnings=list(plan.warnings)
        )
        self._finish_assets(video_id, plan, out)
        return out

    def _finish_assets(self, video_id: str, plan: PublishPlan, out: PublishOutcome) -> None:
        if plan.thumbnail and not out.thumbnail:
            out.thumbnail = self._set_thumbnail(video_id, plan.thumbnail, out.warnings)
        if plan.captions:
            out.captions = self._set_captions(video_id, plan.captions, out.warnings)
        for warning in out.warnings:
            self.log(f"! {warning}")

    # --- кроки майстра ----------------------------------------------------
    def _open_upload_dialog(self) -> None:
        self.page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=90_000)
        self.session.dismiss_consent()
        try:
            self._wait_visible(
                "ytcp-uploads-dialog tp-yt-paper-dialog, "
                "ytcp-upload-dialog tp-yt-paper-dialog",
                timeout=120,
            )
        except Exception as exc:
            raise BrowserError(
                "не відкрився діалог завантаження YouTube Studio. "
                "Перевірте, чи вибрано канал і чи є доступ до Studio." + self._screenshot("no-dialog")
            ) from exc

    def _select_file(self, video: Path) -> None:
        # Playwright відмовляється передавати файли >50 МБ у браузер, підключений
        # через CDP («not co-located»). Chrome локальний, тож ставимо шлях через
        # DOM.setFileInputFiles — без копіювання байтів через протокол.
        try:
            self._set_file_via_cdp(video)
        except Exception as exc:  # noqa: BLE001 — фолбек для малих файлів
            self.log(f"! CDP-вибір файлу не спрацював ({exc}); пробую Playwright")
            dialog = self._dialog()
            inputs = dialog.locator('input[type="file"]')
            if not inputs.count():
                inputs = self.page.locator('input[type="file"]')
            inputs.first.set_input_files(str(video))
        self.log("файл передано в Studio, чекаю форму деталей…")

    def _set_file_via_cdp(self, video: Path) -> None:
        cdp = self.page.context.new_cdp_session(self.page)
        try:
            root = cdp.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
            node_id = 0
            for selector in (
                "ytcp-uploads-file-picker input[type=file]",
                "ytcp-uploads-dialog input[type=file]",
                "input[type=file]",
            ):
                result = cdp.send(
                    "DOM.querySelector", {"nodeId": root, "selector": selector}
                )
                node_id = int(result.get("nodeId") or 0)
                if node_id:
                    break
            if not node_id:
                raise BrowserError("не знайдено поле вибору відеофайлу")
            cdp.send(
                "DOM.setFileInputFiles",
                {"files": [str(video.resolve())], "nodeId": node_id},
            )
        finally:
            cdp.detach()

    def _wait_details(self) -> None:
        try:
            self._wait_visible("#title-textarea #textbox", timeout=120)
        except Exception:
            self._wait_visible("ytcp-video-metadata-editor-basics", timeout=60)
        time.sleep(0.5)

    def _fill_details(self, plan: PublishPlan) -> bool:
        page = self.page
        title = page.locator("#title-textarea #textbox").first
        title.wait_for(state="visible", timeout=30_000)
        self._fill_editable(title, plan.title)

        description = page.locator("#description-textarea #textbox").first
        if description.count():
            self._fill_editable(description, plan.description)

        thumbnail_ok = False
        if plan.thumbnail:
            try:
                thumbnail_ok = self._upload_thumbnail_in_scope(
                    plan.thumbnail, scope=self._dialog()
                )
            except Exception as exc:  # noqa: BLE001 — спробуємо на /edit після публікації
                self.log(
                    f"! не вдалося додати обкладинку у діалозі ({exc}) — спробую після публікації"
                )
            else:
                if thumbnail_ok:
                    self.log("обкладинку додано у форму створення")
                else:
                    self.log(
                        "! не вдалося додати обкладинку у діалозі — спробую після публікації"
                    )

        self._set_made_for_kids(plan.made_for_kids)
        self._show_more()
        self._fill_tags(plan.tags)
        if plan.synthetic:
            self._set_synthetic_disclosure()
        return thumbnail_ok

    def _set_made_for_kids(self, made_for_kids: bool) -> None:
        name = "VIDEO_MADE_FOR_KIDS_MFK" if made_for_kids else "VIDEO_MADE_FOR_KIDS_NOT_MFK"
        try:
            radio = self.page.locator(f'tp-yt-paper-radio-button[name="{name}"]').first
            radio.wait_for(state="visible", timeout=15_000)
            radio.click()
        except Exception:  # noqa: BLE001 — у старій версії UI інші назви
            labels = ("Так, це для дітей", "Yes, it's made for kids") if made_for_kids else (
                "Ні, це не для дітей", "No, it's not made for kids",
            )
            if self._click_text(labels, scope=self._dialog(), timeout=5) is False:
                self.log("! не знайдено перемикач «для дітей» — перевірте вручну")

    def _show_more(self) -> None:
        try:
            button = self._dialog().locator("#toggle-button").first
            if button.count() and button.is_visible():
                text = button.inner_text()
                if "більше" in text.lower() or "more" in text.lower():
                    button.click()
                    time.sleep(0.4)
        except Exception:  # noqa: BLE001 — секція вже розгорнута
            pass

    def _fill_tags(self, tags: list[str]) -> None:
        if not tags:
            return
        try:
            field = self.page.locator("#tags-container #text-input, #tags-container input").first
            if not field.count():
                field = self.page.locator('ytcp-form-input-container#tags-container input').first
            field.wait_for(state="visible", timeout=10_000)
            for tag in tags:
                field.fill(tag)
                field.press("Enter")
        except Exception:  # noqa: BLE001 — теги не критичні
            self.log("! теги не додано через UI — додайте вручну")

    def _set_synthetic_disclosure(self) -> None:
        """Позначка «змінений/синтетичний контент» (BROW-R2). UI може не мати секції."""
        try:
            yes = self.page.locator(
                'tp-yt-paper-radio-button[name="VIDEO_HAS_ALTERED_CONTENT_YES"]'
            ).first
            if yes.count():
                yes.click()
            else:
                clicked = self._click_text(
                    ("Так, відео містить змінений", "Yes, it includes altered",
                     "Altered or synthetic content", "Змінений або синтетичний"),
                    scope=self._dialog(),
                    timeout=4,
                )
                if not clicked:
                    self.log(
                        "! не знайдено перемикач AI-контенту в UI Studio — "
                        "увімкніть позначку вручну"
                    )
                    return
            time.sleep(0.6)
            # діалог уточнення: вибираємо синтетичний голос/звук, якщо є
            for label in (
                "Синтетичний голос", "Synthetic voice", "Голос або звук",
                "Voice or sound", "Змінений голос", "Altered voice",
            ):
                try:
                    locator = self.page.get_by_text(label, exact=False)
                    if locator.count() and locator.first.is_visible():
                        locator.first.click()
                        break
                except Exception:  # noqa: BLE001
                    continue
            self._click_text(
                ("ПРОДОВЖИТИ", "CONTINUE", "Продовжити", "Continue"), timeout=3
            )
        except Exception as exc:  # noqa: BLE001 — не валимо публікацію
            self.log(f"! позначку AI-контенту не встановлено: {exc}")

    def _next(self) -> None:
        self._ensure_open("переходу до наступного кроку")
        button = self._dialog().locator("#next-button").first
        button.wait_for(state="visible", timeout=30_000)
        deadline = time.monotonic() + 180
        while button.is_disabled() and time.monotonic() < deadline:
            self._ensure_open("очікування кнопки «Далі»")
            time.sleep(0.5)
        button.click()
        time.sleep(0.8)

    def _advance_to_visibility(self, privacy: str) -> None:
        """Кроків у майстрі може бути 3 або 4 — ідемо, доки не з'явиться вибір приватності."""
        name = PRIVACY_RADIO.get(privacy, "PRIVATE")
        radio = self._dialog().locator(f'tp-yt-paper-radio-button[name="{name}"]')
        for _ in range(6):
            self._ensure_open("переходу до кроку «Приватність»")
            if radio.count() and radio.first.is_visible():
                return
            self._next()
        raise BrowserError("не вдалося дійти до кроку «Приватність» у майстрі Studio")

    def _wait_upload_complete(self, timeout: float | None = None) -> bool:
        """Чекає, доки Studio не завершить завантаження файлу (R2)."""
        self._ensure_open("очікування завершення завантаження")
        timeout = timeout or float(self.cfg.youtube.browser.publish_timeout)
        deadline = time.monotonic() + timeout
        no_progress_until = time.monotonic() + 60
        last = ""
        seen = False
        while time.monotonic() < deadline:
            self._ensure_open("очікування завершення завантаження")
            text = ""
            for selector in (
                ".progress-label",
                "span.progress-label",
                "ytcp-video-upload-progress",
                "ytcp-uploads-dialog .progress-label",
            ):
                locator = self.page.locator(selector).first
                try:
                    if locator.count():
                        text = (locator.inner_text() or "").strip()
                        if text:
                            break
                except Exception:  # noqa: BLE001
                    continue
            if text:
                seen = True
                lowered = text.lower()
                if any(
                    marker in lowered
                    for marker in (
                        "processing", "checks", "checks complete", "complete",
                        "finished", "uploaded", "no issues found",
                        "оброб", "перевір", "заверш", "готово",
                        "порушень не виявлено", "上传完成", "处理",
                    )
                ):
                    self.log(f"завантаження завершено: {text[:60]}")
                    return True
                if text != last:
                    self.log(f"завантаження: {text[:60]}")
                    last = text
            elif seen:
                return True
            elif time.monotonic() > no_progress_until:
                # індикатора немає зовсім — найімовірніше, файл уже оброблено
                return True
            time.sleep(5)
        return False

    def _select_visibility(self, plan: PublishPlan) -> None:
        name = PRIVACY_RADIO.get(plan.privacy, "PRIVATE")
        try:
            radio = self._dialog().locator(f'tp-yt-paper-radio-button[name="{name}"]').first
            radio.wait_for(state="visible", timeout=30_000)
            radio.click()
        except Exception:
            labels = PRIVACY_TEXT.get(plan.privacy, PRIVACY_TEXT["private"])
            if not self._click_text(labels, scope=self._dialog(), timeout=6):
                raise BrowserError(f"не знайдено перемикач приватності «{plan.privacy}»")

    def _click_done(self) -> None:
        self._ensure_open("натискання кнопки публікації")
        button = self._dialog().locator("#done-button").first
        button.wait_for(state="visible", timeout=30_000)
        deadline = time.monotonic() + 60
        while button.is_disabled() and time.monotonic() < deadline:
            self._ensure_open("очікування кнопки публікації")
            time.sleep(0.5)
        button.click()
        self.log("натиснуто фінальну кнопку, чекаю збереження…")

    # --- результат --------------------------------------------------------
    def _dialog_visible(self) -> bool:
        locator = self.page.locator(
            "ytcp-uploads-dialog tp-yt-paper-dialog:visible, "
            "ytcp-upload-dialog tp-yt-paper-dialog:visible"
        )
        return locator.count() > 0

    def _dialog_success(self) -> bool:
        if not self._dialog_visible():
            return False
        try:
            text = self._dialog().inner_text().lower()
        except Exception:  # noqa: BLE001
            return False
        return any(
            marker in text
            for marker in (
                "відео опубліковано", "video published",
                "відео збережено", "video saved",
                "опубліковано як приватне", "published as private",
            )
        )

    def _wait_video_id(
        self, plan: PublishPlan, timeout: float, *, require_close: bool = False
    ) -> str | None:
        """Чекає підтвердження публікації й шукає id у списку відео каналу.

        Посилання в діалозі до натискання «Опублікувати» — це ще неопублікована
        чернетка, тому довіряємо лише рядку в «Вміст» Studio (spec 016, R11).
        """
        deadline = time.monotonic() + timeout
        wait_limit = deadline if require_close else min(deadline, time.monotonic() + 30.0)
        while time.monotonic() < wait_limit:
            self._ensure_open("очікування завершення публікації")
            if self._dialog_success() or not self._dialog_visible():
                break
            time.sleep(2 if require_close else 1)

        remaining = max(60.0, deadline - time.monotonic())
        return self._video_id_from_list(plan.title, timeout=min(remaining, 240.0))

    def _video_id_from_list(self, title: str, timeout: float = 120.0) -> str | None:
        """Шукає відео за назвою у списку «Вміст» Studio, доки воно не з'явиться."""
        url = (
            f"{STUDIO_URL}channel/{self._channel_id}/videos"
            if self._channel_id
            else STUDIO_URL
        )
        needle = " ".join(title.strip().lower().split())[:40]
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            self._ensure_open("пошуку відео у списку Studio")
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                self.page.wait_for_selector("ytcp-video-row", timeout=30_000)
                rows = self.page.locator("ytcp-video-row")
                for index in range(min(rows.count(), 20)):
                    row = rows.nth(index)
                    try:
                        text = " ".join(row.inner_text().lower().split())
                    except Exception:  # noqa: BLE001
                        continue
                    if needle and needle not in text:
                        continue
                    link = row.locator('a[href*="/video/"]').first
                    href = link.get_attribute("href") if link.count() else None
                    found = video_id_from(href or "")
                    if found:
                        self.log(f"знайдено відео у «Вміст»: {found}")
                        return found
            except BrowserError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            time.sleep(5)
        if last_error:
            self.log(f"! не вдалося знайти відео у списку Studio: {last_error}")
        return None

    # --- обкладинка -------------------------------------------------------
    def _upload_thumbnail_in_scope(self, path: Path, *, scope) -> bool:
        """Завантажує обкладинку в межах діалогу створення чи сторінки редагування (BROW-AC15).

        `ytcp-thumbnail-uploader input#file-loader` має `hidden`/`display: none` (0x0),
        тому чекаємо `visible` на контейнері та `attached` на самому `input[type=file]`.
        """
        self._ensure_open("додавання обкладинки")
        box = scope.locator("ytcp-thumbnail-uploader").first
        box.wait_for(state="visible", timeout=20_000)
        field = scope.locator(
            'ytcp-thumbnail-uploader input[type="file"], '
            'input[type="file"][accept*="image"]'
        ).first
        field.wait_for(state="attached", timeout=10_000)
        field.set_input_files(str(path))

        preview = scope.locator(
            "ytcp-thumbnail-uploader #options-button, "
            "ytcp-thumbnail-uploader #preview-button, "
            'ytcp-thumbnail-uploader img#img-with-fallback[src^="data:"], '
            'ytcp-thumbnail-uploader img#img-with-fallback[src^="blob:"]'
        ).first
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            self._ensure_open("перевірки завантаженої обкладинки")
            try:
                if preview.count() and preview.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                pass
            if self._confirm_thumbnail_crop(timeout=0.4):
                return True
            time.sleep(0.3)
        return True

    def _set_thumbnail(self, video_id: str, path: Path, warnings: list[str]) -> bool:
        """Ставить обкладинку на сторінці редагування й зберігає сторінку (BROW-R2)."""
        try:
            self._ensure_open("переходу на сторінку обкладинки")
            self.page.goto(
                f"{STUDIO_URL}video/{video_id}/edit",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            if not self._upload_thumbnail_in_scope(path, scope=self.page):
                raise BrowserError("не вдалося завантажити обкладинку")
            if not self._save_edit_page(allow_disabled=True):
                warnings.append(
                    "обкладинку завантажено, але кнопка «Зберегти» не підтвердила зміну"
                )
                return False
            self.log("обкладинку встановлено")
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort
            warnings.append(f"обкладинку не встановлено: {exc}")
            return False

    def _confirm_thumbnail_crop(self, timeout: float = 3.0) -> bool:
        """Підтверджує кадрування обкладинки, якщо Studio його показала."""
        deadline = time.monotonic() + timeout
        selectors = (
            "ytcp-thumbnail-crop-dialog",
            "ytcp-image-cropper",
            "ytcp-thumbnail-editor",
        )
        while True:
            self._ensure_open("кадрування обкладинки")
            for scope_selector in selectors:
                scope = self.page.locator(scope_selector).last
                try:
                    if not scope.count() or not scope.is_visible():
                        continue
                except Exception:  # noqa: BLE001
                    continue
                for label in ("Готово", "Done", "Застосувати", "Apply"):
                    try:
                        button = scope.get_by_role("button", name=label, exact=False)
                        if button.count() and button.first.is_visible():
                            button.first.click()
                            time.sleep(0.8)
                            return True
                    except Exception:  # noqa: BLE001
                        continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.3)

    def _save_edit_page(self, *, allow_disabled: bool = False) -> bool:
        """Тисне «Зберегти» на сторінці редагування, якщо кнопка стала активною."""
        deadline = time.monotonic() + (6.0 if allow_disabled else 30.0)
        save = self.page.locator("ytcp-button#save, #save").first
        while time.monotonic() < deadline:
            self._ensure_open("збереження обкладинки")
            try:
                if save.count() and save.is_visible() and not save.is_disabled():
                    save.click()
                    # після збереження кнопка знову неактивна
                    for _ in range(30):
                        self._ensure_open("підтвердження збереження")
                        time.sleep(0.5)
                        try:
                            if save.is_disabled():
                                return True
                        except Exception:  # noqa: BLE001
                            return True
                    return False
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        if allow_disabled:
            try:
                if save.count() and save.is_disabled():
                    return True  # Studio вже зберегла зміну сама
            except Exception:  # noqa: BLE001
                pass
        return False

    # --- субтитри ---------------------------------------------------------
    def _set_captions(self, video_id: str, srt: Path, warnings: list[str]) -> bool:
        """Best-effort завантаження .srt через сторінку субтитрів Studio (BROW-R10)."""
        try:
            self.page.goto(
                f"{STUDIO_URL}video/{video_id}/edit",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            if not self._open_subtitles_tab():
                raise BrowserError("не знайдено вкладку «Субтитри»")
            self._choose_caption_language()
            self._click_text(("ДОДАТИ", "ADD", "Додати"), timeout=8)
            if not self._click_text(
                ("Завантажити файл", "Upload file", "Завантажити"), timeout=8
            ):
                raise BrowserError("не знайдено пункт «Завантажити файл»")
            self._click_text(("З таймінгом", "With timing"), timeout=6)
            self._click_text(
                ("ПРОДОВЖИТИ", "CONTINUE", "Продовжити", "Continue"), timeout=6
            )
            time.sleep(0.5)
            inputs = self.page.locator('input[type="file"]')
            if not inputs.count():
                raise BrowserError("немає поля вибору файлу субтитрів")
            inputs.last.set_input_files(str(srt))
            if not self._click_text(
                ("ЗАВАНТАЖИТИ", "UPLOAD", "Завантажити", "Upload"), timeout=10
            ):
                raise BrowserError("не знайдено кнопку «Завантажити»")
            self._click_text(("ЗБЕРЕГТИ", "SAVE", "Зберегти", "Save"), timeout=6)
            time.sleep(1)
            self.log("субтитри додано")
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort
            warnings.append(f"субтитри не додано: {exc}")
            return False

    def _open_subtitles_tab(self) -> bool:
        for selector in (
            'a[href*="/translations"]',
            '#subtitles-item',
            'ytcp-ve a[href*="subtitles"]',
        ):
            try:
                link = self.page.locator(selector).first
                if link.count() and link.is_visible():
                    link.click()
                    time.sleep(1.5)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return self._click_text(("Субтитри", "Subtitles"), timeout=6)

    def _choose_caption_language(self) -> None:
        language = self.cfg.youtube.captions_name or self.cfg.youtube.captions_language
        if not self._click_text(
            ("ДОДАТИ МОВУ", "ADD LANGUAGE", "Додати мову"), timeout=3
        ):
            return
        try:
            field = self.page.locator('input[type="text"]:visible').last
            field.fill(language)
            time.sleep(1)
            item = self.page.get_by_text(language, exact=False)
            if item.count():
                item.first.click()
            self._click_text(("ДОДАТИ", "ADD", "Підтвердити", "CONFIRM"), timeout=4)
        except Exception:  # noqa: BLE001 — мова вже додана
            pass

    # --- закриття ---------------------------------------------------------
    def close(self) -> None:
        self.session.close()

    def detach(self) -> None:
        self.session.detach()


# --- оркестратор --------------------------------------------------------------


class BrowserPublisher:
    """Публікує задачу через браузер, зберігаючи стан у SQLite (spec 016)."""

    def __init__(self, pipe: "Pipeline", log=None, driver: StudioDriver | None = None):
        self.pipe = pipe
        self.cfg: Config = pipe.cfg
        self.log = log or pipe.log
        self._driver = driver
        self._session: ChromeSession | None = None
        self._dry_run = False

    def _get_driver(self) -> StudioDriver:
        if self._driver is None:
            self._session = ChromeSession(self.cfg, self.log)
            self._session.open()
            self._driver = PlaywrightStudio(self._session, self.cfg, self.log)
        return self._driver

    def sign_in(self, *, interactive: bool = True) -> str:
        return self._get_driver().sign_in(interactive=interactive)

    def publish(self, job: "Job", *, review: bool = False, dry_run: bool = False) -> "Job":
        from audiotube.metadata import load_meta
        from audiotube.pipeline import STATUS_ORDER

        if job.status == "done":
            self.log(f"[{job.id}] уже опубліковано ({job.url}); пропускаю")
            return job
        if STATUS_ORDER.get(job.status, 0) < STATUS_ORDER["rendered"]:
            self.log(f"[{job.id}] спершу відрендеріть відео")
            return job
        self._dry_run = dry_run
        if dry_run and job.video_id:
            self.log(f"[{job.id}] dry-run: відео вже на YouTube ({job.url}); нічого не змінюю")
            job.error = None
            return job
        if job.status == "uploaded" and not job.video_id:
            return self._fail(
                job,
                "відео, ймовірно, вже опубліковано, але video_id не збережено. "
                "Перевірте YouTube Studio; щоб повторити публікацію, скиньте задачу: "
                f"audiotube reset {job.id} --to rendered --forget-youtube",
            )

        try:
            meta = load_meta(job.audio)
        except Exception as exc:  # noqa: BLE001
            return self._fail(job, f"метадані: {exc}")

        # dry-run не має права навіть створювати .srt з транскрипту
        subtitles = None
        if not dry_run:
            subtitles = self.pipe._subtitles_path(job, self.cfg.workspace / job.slug)
            if subtitles and (not subtitles.exists() or subtitles.suffix != ".srt"):
                subtitles = None
        thumbnail = job.thumbnail if job.thumbnail and job.thumbnail.exists() else None
        if thumbnail is None:
            candidate = self.cfg.output / f"{job.slug}.jpg"
            if candidate.exists():
                thumbnail = candidate

        if not job.video_id:
            if not job.video or not job.video.exists():
                return self._fail(job, f"файл відео не знайдено: {job.video}")
            if dry_run:
                privacy = meta.privacy or self.cfg.youtube.default_privacy
                self.log(
                    f"[{job.id}] dry-run: перевірю доступ до Studio; "
                    f"«{meta.youtube_title()}», приватність {privacy}, "
                    f"теги: {len(meta.youtube_tags(self.cfg.description.default_tags))}, "
                    f"обкладинка: {'є' if thumbnail else 'немає'}, "
                    f"субтитри: {'є' if job.subtitles and job.subtitles.exists() else 'немає'}"
                )

        try:
            driver = self._get_driver()
            channel = driver.sign_in(interactive=True)
            self.log(f"[{job.id}] канал: {channel}")
            if job.video_id:
                self.log(f"[{job.id}] відео вже на YouTube ({job.url}); догружаю обкладинку/субтитри")
                out = driver.finish(
                    job.video_id, meta, thumbnail=thumbnail, captions=subtitles
                )
                self._log_warnings(job, out)
                return self.pipe.db.update(job.id, status="done", error=None)

            if not job.video:
                return self._fail(job, "немає шляху до відеофайлу")
            self.log(f"[{job.id}] завантажую через браузер: «{meta.title}»")
            out = driver.publish(
                job.video,
                meta,
                thumbnail=thumbnail,
                captions=subtitles,
                review=review,
                dry_run=dry_run,
            )
            self._log_warnings(job, out)
            if dry_run:
                self.log(f"[{job.id}] dry-run завершено, БД не змінено")
                job.error = None
                return job
            if not out.video_id:
                return self.pipe.db.update(
                    job.id,
                    status="uploaded",
                    error="не вдалося визначити video_id; перевірте Studio",
                )
            job = self.pipe.db.update(
                job.id, video_id=out.video_id, status="uploaded", error=None
            )
            self.pipe.db.record_upload(job.id, out.video_id)
            self.log(f"[{job.id}] завантажено: {job.url}")
            return self.pipe.db.update(job.id, status="done", error=None)
        except VideoIdUnknown as exc:
            return self.pipe.db.update(
                job.id, status="uploaded", error=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 — одна задача не має валити решту
            return self._fail(job, f"браузер: {exc}")

    def _fail(self, job: "Job", message: str) -> "Job":
        if self._dry_run:
            self.log(f"[{job.id}] ПОМИЛКА {message} (dry-run: БД не змінено)")
            job.error = message
            return job
        return self.pipe._fail(job, message)

    def _log_warnings(self, job: "Job", out: PublishOutcome) -> None:
        for warning in out.warnings:
            self.log(f"[{job.id}] ! {warning}")

    def close(self, keep_open: bool = False) -> None:
        if self._driver is None:
            return
        try:
            if keep_open and hasattr(self._driver, "detach"):
                self._driver.detach()
            else:
                self._driver.close()
        finally:
            self._driver = None
            self._session = None
