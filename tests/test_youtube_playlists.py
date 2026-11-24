"""Плейлисти каналу через Publisher — без мережі й без Google (spec 003)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from audiotube.config import load_config
from audiotube.db import Database, QuotaTracker
from audiotube.youtube import Publisher


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakePlaylists:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return FakeRequest(self.pages[len(self.calls) - 1])

    def insert(self, **kwargs):
        self.calls.append(("insert", kwargs))
        return FakeRequest({"id": "new-pl"})


class FakeService:
    def __init__(self, pages):
        self.pl = FakePlaylists(pages)

    def playlists(self):
        return self.pl


class ListPlaylistsTests(unittest.TestCase):
    def make(self, pages):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "config.yaml").write_text("youtube:\n  daily_quota: 10000\n", encoding="utf-8")
        cfg = load_config(root=tmp)
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        self.addCleanup(db.close)
        service = FakeService(pages)
        quota = QuotaTracker(db, cfg.youtube.daily_quota)
        return Publisher(service, cfg, quota, log=lambda _m: None), service, quota

    def test_collects_all_pages(self):
        """YT-AC5: `audiotube playlists` гортає всі сторінки playlists.list."""
        pub, service, quota = self.make([
            {"items": [{"id": "pl1"}], "nextPageToken": "t2"},
            {"items": [{"id": "pl2"}]},
        ])
        self.assertEqual([p["id"] for p in pub.list_playlists()], ["pl1", "pl2"])
        self.assertEqual([c[0] for c in service.pl.calls], ["list", "list"])
        self.assertEqual(service.pl.calls[0][1]["mine"], True)
        self.assertEqual(quota.used(), 2)

    def test_ensure_playlist_reuses_existing(self):
        """YT-AC6: наявний плейлист переюзується, новий не створюється."""
        pub, service, quota = self.make([
            {"items": [{"id": "pl1", "snippet": {"title": "Docker"}}]},
        ])
        self.assertEqual(pub.ensure_playlist("Docker"), "pl1")
        self.assertEqual([c[0] for c in service.pl.calls], ["list"])
        self.assertEqual(quota.used(), 1)


if __name__ == "__main__":
    unittest.main()
