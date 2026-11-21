"""Читання ноутбуків і аудіопереказів із NotebookLM.

Єдина точка дотику до неофіційного клієнта `notebooklm-py`: якщо Google змінить внутрішні
RPC, лагодити треба лише цей файл. Решта застосунку залежить від протоколу `Adapter`
(його ж реалізує `FakeAdapter` у тестах).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


class NotebookLMError(RuntimeError):
    """Помилка доступу до NotebookLM (мережа, авторизація, формат відповіді)."""


class NotebookLMUnavailable(NotebookLMError):
    """Не встановлено пакет notebooklm-py."""


@dataclass(frozen=True)
class RemoteNotebook:
    id: str
    title: str
    created_at: datetime | None = None
    shared: bool = False  # ноутбук не наш (viewer/editor)


@dataclass(frozen=True)
class RemoteArtifact:
    id: str
    title: str
    created_at: datetime | None = None
    duration: float | None = None
    ready: bool = True
    status: str = ""


class Adapter(Protocol):
    """Мінімум, який потрібен синхронізації."""

    async def __aenter__(self) -> "Adapter": ...

    async def __aexit__(self, exc_type, exc, tb) -> None: ...

    async def notebooks(self) -> list[RemoteNotebook]: ...

    async def audio(self, notebook_id: str) -> list[RemoteArtifact]: ...

    async def download(self, notebook_id: str, artifact_id: str, dest: Path) -> None: ...


class PyNotebookLMAdapter:
    """Реалізація поверх notebooklm-py (імпорт лінивий — без пакета решта працює)."""

    def __init__(self, profile: str = "default", home: Path | None = None):
        self.profile = profile
        self.home = home
        self._ctx = None
        self._client = None

    async def __aenter__(self) -> "PyNotebookLMAdapter":
        if self.home is not None:
            os.environ["NOTEBOOKLM_HOME"] = str(self.home)
        if self.profile:
            os.environ.setdefault("NOTEBOOKLM_PROFILE", self.profile)

        try:
            from notebooklm import NotebookLMClient
        except ImportError as e:  # pragma: no cover — залежить від середовища
            raise NotebookLMUnavailable(
                'не встановлено notebooklm-py. Спробуйте: uv tool install "notebooklm-py[browser]" '
                "або pip install notebooklm-py (у віртуальному середовищі)"
            ) from e

        try:
            ctx = NotebookLMClient.from_storage(profile=self.profile)
            if hasattr(ctx, "__aenter__"):
                self._ctx = ctx
                self._client = await ctx.__aenter__()
            else:  # старіші версії повертали готовий клієнт
                self._client = ctx
        except Exception as e:  # noqa: BLE001 — будь-яка причина = «немає доступу»
            detail = str(e).strip().rstrip(".")
            raise NotebookLMError(
                f"не вдалося підключитися до NotebookLM: {detail}. "
                "Перевірте `notebooklm login` і `notebooklm auth check --test`."
            ) from e
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._ctx is not None:
            await self._ctx.__aexit__(exc_type, exc, tb)
            self._ctx = None
        self._client = None

    def _need_client(self):
        if self._client is None:
            raise NotebookLMError("клієнт NotebookLM не відкритий (використовуйте `async with`)")
        return self._client

    async def notebooks(self) -> list[RemoteNotebook]:
        try:
            items = await self._need_client().notebooks.list()
        except NotebookLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise NotebookLMError(f"не вдалося отримати список ноутбуків: {e}") from e

        out = []
        for nb in items:
            # is_owner застаріле, але є в усіх версіях; role може бути None
            shared = not bool(getattr(nb, "is_owner", True))
            out.append(
                RemoteNotebook(
                    id=str(nb.id),
                    title=(nb.title or "").strip() or "Без назви",
                    created_at=getattr(nb, "created_at", None),
                    shared=shared,
                )
            )
        return out

    async def audio(self, notebook_id: str) -> list[RemoteArtifact]:
        try:
            items = await self._need_client().artifacts.list_audio(notebook_id)
        except NotebookLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise NotebookLMError(f"не вдалося отримати аудіо ноутбука {notebook_id}: {e}") from e

        out = []
        for art in items:
            out.append(
                RemoteArtifact(
                    id=str(art.id),
                    title=(art.title or "").strip(),
                    created_at=getattr(art, "created_at", None),
                    duration=getattr(art, "duration_seconds", None),
                    ready=bool(getattr(art, "is_completed", True)),
                    status=str(getattr(art, "status_str", "") or ""),
                )
            )
        return out

    async def download(self, notebook_id: str, artifact_id: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._need_client().artifacts.download_audio(
                notebook_id, str(dest), artifact_id=artifact_id
            )
        except Exception as e:  # noqa: BLE001
            raise NotebookLMError(f"не вдалося завантажити аудіо {artifact_id}: {e}") from e
