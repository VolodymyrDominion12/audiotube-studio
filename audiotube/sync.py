"""Синхронізація аудіопереказів із NotebookLM у папки-ноутбуки.

Етап «Бібліотекар»: ноутбук -> `inbox/<slug>/`, файл + sidecar-YAML + рядок у SQLite.
Ключ ідемпотентності — id артефакту в NotebookLM, а не ім'я файлу.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from audiotube import AUDIO_EXTENSIONS
from audiotube.config import Config
from audiotube.db import Database
from audiotube.media import duration as probe_duration
from audiotube.metadata import slugify
from audiotube.notebooklm import (
    Adapter,
    NotebookLMError,
    PyNotebookLMAdapter,
    RemoteArtifact,
    RemoteNotebook,
)
from audiotube.pipeline import Pipeline

AUDIO_SUFFIX = ".m4a"
NOTEBOOK_URL = "https://notebooklm.google.com/notebook/{id}"

# NotebookLM інколи називає всі аудіо однаково — такі назви замінюємо на осмислені
_GENERIC_TITLES = {
    "audio overview",
    "audio overviews",
    "аудіопереказ",
    "аудіоогляд",
    "аудиообзор",
    "переказ",
}


@dataclass
class SyncItem:
    action: str  # download | repair | skip | plan | error | missing | restored | changed
    notebook: str
    title: str
    path: Path | None = None
    artifact_id: str = ""
    message: str = ""


@dataclass
class SyncReport:
    notebooks: int = 0
    downloaded: int = 0
    skipped: int = 0
    planned: int = 0
    missing: int = 0  # зникли з NotebookLM у цьому прогоні (файли лишаються)
    restored: int = 0  # були missing і знову з'явилися
    changed: int = 0  # --force: той самий id, інший вміст (версії ще не підтримуються)
    errors: list[str] = field(default_factory=list)
    items: list[SyncItem] = field(default_factory=list)

    @property
    def total_new(self) -> int:
        return self.downloaded + self.planned


def build_adapter(cfg: Config) -> Adapter:
    home = cfg.path(cfg.notebooklm.home) if cfg.notebooklm.home else None
    return PyNotebookLMAdapter(profile=cfg.notebooklm.profile, home=home)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _short_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", value)[:6].lower()
    return cleaned or "x"


def _created_key(art: RemoteArtifact) -> float:
    return art.created_at.timestamp() if art.created_at else 0.0


def clean_title(art: RemoteArtifact, notebook: RemoteNotebook) -> str:
    title = (art.title or "").strip()
    if title and title.lower() not in _GENERIC_TITLES:
        return title
    if art.created_at:
        return f"{notebook.title} — переказ {art.created_at.date().isoformat()}"
    return f"{notebook.title} — переказ"


class SyncService:
    def __init__(
        self,
        cfg: Config,
        db: Database | None = None,
        adapter: Adapter | None = None,
        log=print,
    ):
        self.cfg = cfg
        self._owns_db = db is None
        self.db = db or Database(cfg.db_path)
        self.adapter = adapter
        self.log = log

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    # --- публічний вхід -----------------------------------------------------
    def run(
        self,
        *,
        dry_run: bool = False,
        notebook: str | None = None,
        limit: int | None = None,
        force: bool = False,
    ) -> SyncReport:
        return asyncio.run(
            self._run(dry_run=dry_run, notebook=notebook, limit=limit, force=force)
        )

    async def _run(
        self,
        *,
        dry_run: bool,
        notebook: str | None,
        limit: int | None,
        force: bool,
    ) -> SyncReport:
        report = SyncReport()
        # dry-run показує весь план, а не обрізається запобіжником max_per_run
        cap = limit if limit is not None else (10**9 if dry_run else self.cfg.notebooklm.max_per_run)
        cap = max(1, cap)
        adapter = self.adapter or build_adapter(self.cfg)
        stop = False

        async with adapter as api:
            remote = await api.notebooks()
            # «Існує» рахуємо до фільтрів: відфільтрований ноутбук не є зниклим
            if not notebook:
                self._reconcile_notebooks({nb.id for nb in remote}, report, dry_run=dry_run)
            if not self.cfg.notebooklm.include_shared:
                remote = [nb for nb in remote if not nb.shared]
            if notebook:
                remote = [nb for nb in remote if self._matches(nb, notebook)]

            for nb in sorted(remote, key=lambda n: n.title.lower()):
                if stop:
                    break
                report.notebooks += 1
                slug = self._notebook_slug(nb)
                folder = self.cfg.inbox / slug

                if not dry_run:
                    folder.mkdir(parents=True, exist_ok=True)
                    self.db.save_notebook(
                        nb.id, nb.title, slug, NOTEBOOK_URL.format(id=nb.id), None
                    )
                    self._write_notebook_json(folder, nb)

                try:
                    artifacts = await api.audio(nb.id)
                except NotebookLMError as e:
                    # помилка API — не доказ, що артефакти зникли: стани не чіпаємо
                    report.errors.append(f"{nb.title}: {e}")
                    continue

                self._reconcile_artifacts(nb, {a.id for a in artifacts}, report, dry_run=dry_run)

                for art in sorted(artifacts, key=lambda a: (_created_key(a), a.title)):
                    if stop:
                        break
                    title = clean_title(art, nb)
                    await self._one(
                        api, nb, art, title, folder, report, dry_run=dry_run, force=force
                    )
                    if report.total_new >= cap:
                        stop = True

        if not dry_run:
            self._register_jobs(report)
        return report

    # --- один артефакт ------------------------------------------------------
    async def _one(
        self,
        api: Adapter,
        nb: RemoteNotebook,
        art: RemoteArtifact,
        title: str,
        folder: Path,
        report: SyncReport,
        *,
        dry_run: bool,
        force: bool,
    ) -> None:
        row = self.db.artifact(art.id)
        existing = Path(row["path"]) if row and row["path"] else None

        if not art.ready:
            report.skipped += 1
            report.items.append(
                SyncItem("skip", nb.title, title, existing, art.id, f"ще генерується ({art.status})")
            )
            return

        if existing and existing.exists():
            if force:
                await self._verify(api, nb, art, title, existing, row, report, dry_run=dry_run)
            else:
                report.skipped += 1
                report.items.append(SyncItem("skip", nb.title, title, existing, art.id, "уже є"))
            return

        action = "repair" if existing else "download"
        if existing and not existing.exists() and not self.cfg.notebooklm.repair_missing:
            report.skipped += 1
            report.items.append(
                SyncItem("skip", nb.title, title, existing, art.id, "файл зник, repair вимкнено")
            )
            return

        date = art.created_at.date().isoformat() if art.created_at else datetime.now().date().isoformat()
        stem = (
            f"{date}_{slugify(title, self.cfg.notebooklm.filename_max_len)}"
            f"-{_short_id(art.id)}"
        )
        dest = folder / f"{stem}{AUDIO_SUFFIX}"

        if dry_run:
            report.planned += 1
            report.items.append(
                SyncItem("plan", nb.title, title, dest, art.id, f"буде завантажено ({action})")
            )
            return

        part = dest.with_name(dest.name + ".part")
        try:
            await api.download(nb.id, art.id, part)
            if not part.exists() or part.stat().st_size == 0:
                raise NotebookLMError("отримано порожній файл")
            size = part.stat().st_size
            digest = _sha256(part)
            duration = self._probe(part)
            os.replace(part, dest)
        except Exception as e:  # noqa: BLE001 — один збій не валить усю синхронізацію
            part.unlink(missing_ok=True)
            report.errors.append(f"{nb.title} / {title}: {e}")
            report.items.append(SyncItem("error", nb.title, title, None, art.id, str(e)))
            return

        self.db.save_artifact(
            artifact_id=art.id,
            notebook_id=nb.id,
            title=title,
            created_at=art.created_at.isoformat() if art.created_at else None,
            duration=duration,
            path=dest,
            sha256=digest,
            size=size,
        )
        self._write_sidecar(dest, nb, art, title, self.cfg.youtube.default_privacy)

        report.downloaded += 1
        report.items.append(SyncItem(action, nb.title, title, dest, art.id, ""))
        self.log(f"[{nb.title}] {title} -> {dest.name}")

        delay = self.cfg.notebooklm.delay_seconds
        if delay > 0:
            await asyncio.sleep(delay)
        return

    async def _verify(
        self,
        api: Adapter,
        nb: RemoteNotebook,
        art: RemoteArtifact,
        title: str,
        existing: Path,
        row,
        report: SyncReport,
        *,
        dry_run: bool,
    ) -> None:
        """--force для наявного файлу: перекачати в .part і порівняти, але ніколи не перезаписувати."""
        if dry_run:
            report.skipped += 1
            report.items.append(
                SyncItem("skip", nb.title, title, existing, art.id, "буде перевірено (--force)")
            )
            return

        part = existing.with_name(existing.name + ".part")
        try:
            await api.download(nb.id, art.id, part)
            digest = _sha256(part)
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"{nb.title} / {title}: {e}")
            report.items.append(SyncItem("error", nb.title, title, None, art.id, str(e)))
            return
        finally:
            part.unlink(missing_ok=True)

        if digest == row["sha256"]:
            report.skipped += 1
            report.items.append(SyncItem("skip", nb.title, title, existing, art.id, "не змінився"))
        else:
            # Версії (LIFE-R5/R6) ще не реалізовані: локальний файл, можливо, вже на YouTube,
            # тож нову версію лише показуємо, а не підміняємо нею стару.
            report.changed += 1
            report.items.append(
                SyncItem(
                    "changed", nb.title, title, existing, art.id,
                    "вміст у NotebookLM змінився; локальний файл не чіпаємо",
                )
            )

        delay = self.cfg.notebooklm.delay_seconds
        if delay > 0:
            await asyncio.sleep(delay)

    # --- зниклі артефакти (спека 005) ----------------------------------------
    def _reconcile_notebooks(self, alive: set[str], report: SyncReport, *, dry_run: bool) -> None:
        """Ноутбук пропав із NotebookLM цілком -> усі його артефакти missing."""
        for row in self.db.notebooks():
            if row["id"] in alive:
                continue
            gone = self.db.artifacts(notebook_id=row["id"], state="downloaded")
            self._mark(gone, "missing", row["title"], report, dry_run=dry_run)

    def _reconcile_artifacts(
        self, nb: RemoteNotebook, alive: set[str], report: SyncReport, *, dry_run: bool
    ) -> None:
        rows = self.db.artifacts(notebook_id=nb.id)
        gone = [r for r in rows if r["state"] == "downloaded" and r["id"] not in alive]
        back = [r for r in rows if r["state"] == "missing" and r["id"] in alive]
        self._mark(gone, "missing", nb.title, report, dry_run=dry_run)
        self._mark(back, "downloaded", nb.title, report, dry_run=dry_run)

    def _mark(self, rows, state: str, notebook: str, report: SyncReport, *, dry_run: bool) -> None:
        if not rows:
            return
        action = "missing" if state == "missing" else "restored"
        for r in rows:
            path = Path(r["path"]) if r["path"] else None
            report.items.append(SyncItem(action, notebook, r["title"] or r["id"], path, r["id"], ""))
        if action == "missing":
            report.missing += len(rows)
        else:
            report.restored += len(rows)
        if not dry_run:
            self.db.set_artifact_state([r["id"] for r in rows], state)

    # --- допоміжне ----------------------------------------------------------
    @staticmethod
    def _matches(nb: RemoteNotebook, query: str) -> bool:
        q = query.lower()
        return q in nb.id.lower() or q in nb.title.lower()

    def _notebook_slug(self, nb: RemoteNotebook) -> str:
        row = self.db.notebook(nb.id)
        if row:
            return row["slug"]
        taken = {r["slug"] for r in self.db.notebooks()}
        base = slugify(nb.title, 50)
        slug = base
        if slug in taken:
            slug = f"{base}-{_short_id(nb.id)}"
        n = 2
        while slug in taken:
            slug = f"{base}-{_short_id(nb.id)}-{n}"
            n += 1
        return slug

    @staticmethod
    def _probe(path: Path) -> float | None:
        try:
            return probe_duration(path)
        except Exception:  # noqa: BLE001 — без ffprobe синк не має падати
            return None

    @staticmethod
    def _write_notebook_json(folder: Path, nb: RemoteNotebook) -> None:
        """Папка сама розповідає, з якого вона ноутбука (корисно без БД і в git)."""
        data = {
            "id": nb.id,
            "title": nb.title,
            "url": NOTEBOOK_URL.format(id=nb.id),
            "synced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        (folder / "notebook.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @staticmethod
    def _write_sidecar(
        dest: Path, nb: RemoteNotebook, art: RemoteArtifact, title: str, privacy: str
    ) -> None:
        """Не перезаписуємо існуючий YAML: там можуть бути ручні правки."""
        side = dest.with_suffix(".yaml")
        if side.exists():
            return
        data = {
            "title": title,
            "notebook": nb.title,
            "notebook_id": nb.id,
            "artifact_id": art.id,
            "source": "notebooklm",
            "privacy": privacy,
        }
        if art.created_at:
            data["generated_at"] = art.created_at.date().isoformat()
        side.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _register_jobs(self, report: SyncReport) -> None:
        """Реєструє нові файли тим самим шляхом, що й `audiotube scan`."""
        Pipeline(self.cfg, db=self.db, log=self.log).scan()
        for item in report.items:
            if item.path is None or not item.path.exists():
                continue
            job_id = self.db.job_for_audio(item.path)
            if job_id:
                self.db.link_artifact_job(item.artifact_id, job_id)


def import_audio(
    cfg: Config, sources: list[str], *, move: bool = False
) -> tuple[list[Path], list[Path]]:
    """Копіює вже завантажені вручну файли в inbox/ (аварійний шлях без NotebookLM API)."""
    copied: list[Path] = []
    skipped: list[Path] = []
    for raw in sources:
        src = Path(raw).expanduser()
        if not src.exists():
            raise SystemExit(f"немає шляху: {raw}")

        if src.is_file():
            pairs = [(src, cfg.inbox / "imported" / src.name)]
        else:
            root = cfg.inbox / slugify(src.name)
            pairs = [
                (f, root / f.relative_to(src))
                for f in sorted(src.rglob("*"))
                if f.is_file()
                and f.suffix.lower() in AUDIO_EXTENSIONS
                and not any(p.startswith(".") for p in f.relative_to(src).parts)
            ]

        for file, dest in pairs:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                skipped.append(dest)
                continue
            if move:
                shutil.move(str(file), str(dest))
            else:
                shutil.copy2(file, dest)
            copied.append(dest)
    return copied, skipped
