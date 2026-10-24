"""YouTube Data API v3: авторизація, завантаження, обкладинки, плейлисти.

Бібліотеки Google імпортуються ліниво, щоб рендер працював і без них.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

from audiotube.config import Config
from audiotube.db import QuotaTracker
from audiotube.metadata import VideoMeta

# youtube.force-ssl покриває плейлисти, обкладинки і (у наступних фазах) субтитри
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

COST_WRITE = 50
COST_READ = 1
COST_CAPTION = 400
RETRIABLE_STATUS = {500, 502, 503, 504}


class YouTubeError(RuntimeError):
    pass


class QuotaExceeded(YouTubeError):
    pass


def get_credentials(cfg: Config, interactive: bool = True):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = cfg.path(cfg.youtube.token)
    secret_path = cfg.path(cfg.youtube.client_secret)
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:  # noqa: BLE001 — токен відкликано, потрібна повторна авторизація
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise YouTubeError("немає дійсного токена YouTube. Виконайте: audiotube auth")
        if not secret_path.exists():
            raise YouTubeError(
                f"не знайдено {secret_path}. Створіть OAuth-клієнт типу «Desktop app» у Google Cloud Console "
                "і збережіть JSON за цим шляхом (див. docs/02_YOUTUBE_SETUP.md)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service(cfg: Config, interactive: bool = True):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=get_credentials(cfg, interactive), cache_discovery=False)


def video_body(meta: VideoMeta, cfg: Config) -> dict:
    yt = cfg.youtube
    status: dict = {
        "privacyStatus": meta.privacy or yt.default_privacy,
        "selfDeclaredMadeForKids": yt.made_for_kids,
        "containsSyntheticMedia": yt.contains_synthetic_media,
    }
    if meta.publish_at:
        dt = meta.publish_at
        if dt.tzinfo is None:
            raise YouTubeError("publish_at має містити часовий пояс, напр. 2026-10-25T18:00:00+03:00")
        status["privacyStatus"] = "private"  # вимога API для відкладеної публікації
        status["publishAt"] = dt.isoformat()
    return {
        "snippet": {
            "title": meta.youtube_title(),
            "description": meta.youtube_description(cfg.description.footer),
            "tags": meta.youtube_tags(cfg.description.default_tags),
            "categoryId": yt.category_id,
            "defaultLanguage": yt.language,
            "defaultAudioLanguage": yt.language,
        },
        "status": status,
    }


class Publisher:
    def __init__(self, service, cfg: Config, quota: QuotaTracker, log=print):
        self.yt = service
        self.cfg = cfg
        self.quota = quota
        self.log = log

    def _spend(self, units: int, what: str) -> None:
        if not self.quota.can_spend(units):
            raise QuotaExceeded(
                f"недостатньо квоти для «{what}»: потрібно {units}, лишилось {self.quota.remaining()} "
                "(скидається опівночі за тихоокеанським часом, ~10:00 за Києвом)"
            )
        self.quota.spend(units)

    # --- відео ---------------------------------------------------------------
    def upload(self, video: Path, meta: VideoMeta) -> str:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        self._spend(self.cfg.youtube.upload_cost, "завантаження відео")
        media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        request = self.yt.videos().insert(part="snippet,status", body=video_body(meta, self.cfg), media_body=media)

        response, attempt, last_pct = None, 0, -1
        while response is None:
            try:
                status, response = request.next_chunk()
                attempt = 0
                if status:
                    pct = int(status.progress() * 100)
                    if pct // 10 != last_pct // 10:
                        self.log(f"    завантаження: {pct}%")
                        last_pct = pct
            except HttpError as e:
                if e.resp.status not in RETRIABLE_STATUS:
                    raise YouTubeError(f"YouTube відхилив завантаження: {e}") from e
                attempt = self._backoff(attempt, e)
            except (ConnectionError, TimeoutError, OSError) as e:
                attempt = self._backoff(attempt, e)
        if "id" not in response:
            raise YouTubeError(f"неочікувана відповідь YouTube: {response}")
        return response["id"]

    def _backoff(self, attempt: int, err: Exception) -> int:
        attempt += 1
        if attempt > 8:
            raise YouTubeError(f"завантаження перервано після {attempt - 1} спроб: {err}") from err
        delay = min(2 ** attempt, 60) + random.random()
        self.log(f"    тимчасова помилка ({err}); повтор через {delay:.0f} с")
        time.sleep(delay)
        return attempt

    def set_thumbnail(self, video_id: str, thumb: Path) -> bool:
        """False, якщо канал ще не має права на власні обкладинки (потрібна верифікація телефоном)."""
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        self._spend(COST_WRITE, "обкладинка")
        try:
            self.yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg")).execute()
            return True
        except HttpError as e:
            if e.resp.status == 403:
                self.log("    ! обкладинку не встановлено: канал має бути верифікований (youtube.com/verify)")
                return False
            raise

    # --- субтитри --------------------------------------------------------------
    def set_captions(self, video_id: str, srt: Path) -> str | None:
        """Завантажує .srt як субтитри відео. None — якщо YouTube відмовив (не критично)."""
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        yt = self.cfg.youtube
        body = {
            "snippet": {
                "videoId": video_id,
                "language": yt.captions_language or yt.language,
                "name": yt.captions_name or yt.captions_language,
                "isDraft": False,
            }
        }
        self._spend(self.cfg.youtube.captions_cost, "субтитри")
        media = MediaFileUpload(str(srt), mimetype="application/octet-stream", resumable=False)
        try:
            resp = self.yt.captions().insert(part="snippet", body=body, media_body=media).execute()
            return resp.get("id")
        except HttpError as e:
            if e.resp.status in (400, 403, 409):
                self.log(f"    ! субтитри не додано (HTTP {e.resp.status}): {e}")
                return None
            raise

    # --- плейлисти -------------------------------------------------------------    def list_playlists(self) -> list[dict]:
        items, token = [], None
        while True:
            self._spend(COST_READ, "список плейлистів")
            resp = self.yt.playlists().list(part="snippet,status,contentDetails", mine=True,
                                            maxResults=50, pageToken=token).execute()
            items.extend(resp.get("items", []))
            token = resp.get("nextPageToken")
            if not token:
                return items

    def ensure_playlist(self, title: str, description: str = "") -> str:
        for p in self.list_playlists():
            if p["snippet"]["title"].strip().lower() == title.strip().lower():
                return p["id"]
        self._spend(COST_WRITE, "створення плейлиста")
        resp = self.yt.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {"title": title[:150], "description": description, "defaultLanguage": self.cfg.youtube.language},
                "status": {"privacyStatus": self.cfg.youtube.playlist_privacy},
            },
        ).execute()
        self.log(f"    створено плейлист «{title}»")
        return resp["id"]

    def add_to_playlist(self, playlist_id: str, video_id: str) -> None:
        self._spend(COST_READ, "перевірка плейлиста")
        existing = self.yt.playlistItems().list(part="id", playlistId=playlist_id, videoId=video_id).execute()
        if existing.get("items"):
            return
        self._spend(COST_WRITE, "додавання в плейлист")
        self.yt.playlistItems().insert(
            part="snippet",
            body={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
        ).execute()
