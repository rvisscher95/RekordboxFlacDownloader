"""
Unit tests for downloader.py and rekordbox_db.py.

Run with:
    python -m pytest tests.py -v
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# ── downloader tests ──────────────────────────────────────────────────────────
from downloader import (
    QUALITY_FLAC_16,
    Track,
    _sanitise_filename,
    get_download_url,
    search,
)
from rekordbox_db import (
    ContentTrack,
    Playlist,
    RekordboxDatabase,
    _row_to_track,
)


class TestSanitiseFilename(unittest.TestCase):
    def test_removes_illegal_chars(self):
        result = _sanitise_filename('Artist: "Name" / Title?')
        for ch in '<>:"/\\|?*':
            self.assertNotIn(ch, result)

    def test_plain_name_unchanged(self):
        self.assertEqual(_sanitise_filename("Hello World"), "Hello World")


class TestTrack(unittest.TestCase):
    def _make_raw(self, **overrides):
        base = {
            "id": "42",
            "title": "Test Track",
            "performer": {"name": "Test Artist"},
            "album": {"title": "Test Album", "image": {}},
            "isrc": "USRC17607839",
            "duration": 240,
        }
        base.update(overrides)
        return base

    def test_basic_fields(self):
        t = Track(self._make_raw())
        self.assertEqual(t.track_id, "42")
        self.assertEqual(t.title, "Test Track")
        self.assertEqual(t.artist, "Test Artist")
        self.assertEqual(t.album, "Test Album")
        self.assertEqual(t.isrc, "USRC17607839")
        self.assertEqual(t.duration, 240)

    def test_missing_performer_falls_back_to_artist_key(self):
        raw = self._make_raw()
        del raw["performer"]
        raw["artist"] = {"name": "Fallback Artist"}
        t = Track(raw)
        self.assertEqual(t.artist, "Fallback Artist")

    def test_display_name(self):
        t = Track(self._make_raw())
        self.assertIn("Test Artist", t.display_name)
        self.assertIn("Test Track", t.display_name)

    def test_cover_url_extracted(self):
        raw = self._make_raw(album={"title": "A", "image": {"large": "http://img/large.jpg"}})
        t = Track(raw)
        self.assertEqual(t.cover_url, "http://img/large.jpg")


class TestSearchFunction(unittest.TestCase):
    def _make_response(self, items):
        return {
            "success": True,
            "data": {
                "tracks": {"items": items},
            },
        }

    @patch("downloader._get")
    def test_returns_tracks(self, mock_get):
        mock_get.return_value = self._make_response([
            {
                "id": "1",
                "title": "Song",
                "performer": {"name": "Artist"},
                "album": {"title": "Album", "image": {}},
            }
        ])
        results = search("Artist Song")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Song")

    @patch("downloader._get")
    def test_returns_empty_on_failure(self, mock_get):
        mock_get.return_value = None
        self.assertEqual(search("test"), [])

    @patch("downloader._get")
    def test_returns_empty_when_not_successful(self, mock_get):
        mock_get.return_value = {"success": False, "data": {}}
        self.assertEqual(search("test"), [])

    @patch("downloader._get")
    def test_uses_most_popular_key(self, mock_get):
        mock_get.return_value = {
            "success": True,
            "data": {
                "most_popular": {
                    "items": [
                        {
                            "type": "tracks",
                            "content": {
                                "id": "99",
                                "title": "Popular Song",
                                "performer": {"name": "Pop Star"},
                                "album": {"title": "Top 40", "image": {}},
                            },
                        }
                    ]
                }
            },
        }
        results = search("Pop Star Popular Song")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Popular Song")


class TestGetDownloadUrl(unittest.TestCase):
    @patch("downloader._get")
    def test_returns_url_on_success(self, mock_get):
        mock_get.return_value = {
            "success": True,
            "data": {"url": "https://cdn.example.com/file.flac"},
        }
        url = get_download_url("12345", quality=QUALITY_FLAC_16)
        self.assertEqual(url, "https://cdn.example.com/file.flac")
        mock_get.assert_called_once_with(
            "/api/download-music",
            params={"track_id": "12345", "quality": QUALITY_FLAC_16},
        )

    @patch("downloader._get")
    def test_returns_none_on_failure(self, mock_get):
        mock_get.return_value = {"success": False, "message": "not found"}
        self.assertIsNone(get_download_url("99"))

    @patch("downloader._get")
    def test_returns_none_when_no_response(self, mock_get):
        mock_get.return_value = None
        self.assertIsNone(get_download_url("99"))


# ── rekordbox_db tests ────────────────────────────────────────────────────────

def _make_test_db() -> str:
    """Create a minimal unencrypted Rekordbox-like SQLite DB in a temp file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE djmdPlaylist (
            ID TEXT PRIMARY KEY,
            Name TEXT,
            ParentID TEXT,
            Attribute INTEGER DEFAULT 0,
            Seq INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE djmdContent (
            ID TEXT PRIMARY KEY,
            Title TEXT,
            ArtistName TEXT,
            AlbumName TEXT,
            FolderPath TEXT,
            FileType INTEGER DEFAULT 1,
            SampleRate INTEGER DEFAULT 44100,
            BitRate INTEGER DEFAULT 0,
            BPM REAL DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE djmdSongPlaylist (
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            PlaylistID TEXT,
            ContentID TEXT,
            TrackNo INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        );

        INSERT INTO djmdPlaylist VALUES
            ('1', 'My Playlist', NULL, 0, 1, datetime('now'), datetime('now')),
            ('2', 'A Folder',    NULL, 1, 2, datetime('now'), datetime('now'));

        INSERT INTO djmdContent VALUES
            ('10', 'Track One', 'Artist A', 'Album A',
             '/music/track1.mp3', 1, 44100, 320, 128.0,
             datetime('now'), datetime('now')),
            ('11', 'Track Two', 'Artist B', 'Album B',
             '/music/track2.flac', 5, 44100, 0, 90.0,
             datetime('now'), datetime('now'));

        INSERT INTO djmdSongPlaylist VALUES
            (NULL, '1', '10', 1, datetime('now'), datetime('now')),
            (NULL, '1', '11', 2, datetime('now'), datetime('now'));
        """
    )
    conn.commit()
    conn.close()
    return path


class TestRekordboxDatabaseSqlite(unittest.TestCase):
    def setUp(self):
        self._db_path = _make_test_db()
        # Force the sqlite fallback by patching the pyrekordbox import
        with patch.dict("sys.modules", {"pyrekordbox": None}):
            self.rdb = RekordboxDatabase(self._db_path)

    def tearDown(self):
        self.rdb.close()
        os.unlink(self._db_path)

    def test_get_playlists(self):
        playlists = self.rdb.get_playlists()
        names = [p.name for p in playlists]
        self.assertIn("My Playlist", names)
        self.assertIn("A Folder", names)

    def test_folder_detected(self):
        playlists = self.rdb.get_playlists()
        folder = next(p for p in playlists if p.name == "A Folder")
        self.assertTrue(folder.is_folder)

    def test_get_tracks_in_playlist(self):
        tracks = self.rdb.get_tracks_in_playlist("1")
        self.assertEqual(len(tracks), 2)
        titles = {t.title for t in tracks}
        self.assertIn("Track One", titles)
        self.assertIn("Track Two", titles)

    def test_track_format_detected(self):
        tracks = self.rdb.get_tracks_in_playlist("1")
        mp3 = next(t for t in tracks if t.title == "Track One")
        flac = next(t for t in tracks if t.title == "Track Two")
        self.assertEqual(mp3.file_type, 1)
        self.assertEqual(flac.file_type, 5)

    def test_update_track_path(self):
        self.rdb.update_track_path("10", "/music/new/track1.flac")
        tracks = self.rdb.get_tracks_in_playlist("1")
        updated = next(t for t in tracks if t.id == "10")
        self.assertEqual(updated.folder_path, "/music/new/track1.flac")
        self.assertEqual(updated.file_type, 5)

    def test_add_track_and_retrieve(self):
        track_id = self.rdb.add_track(
            "/music/new.flac", "New Song", "New Artist", "New Album"
        )
        self.assertIsNotNone(track_id)
        self.rdb.add_track_to_playlist(track_id, "1")
        tracks = self.rdb.get_tracks_in_playlist("1")
        titles = {t.title for t in tracks}
        self.assertIn("New Song", titles)

    def test_create_playlist(self):
        new_id = self.rdb.create_playlist("New Playlist")
        self.assertIsNotNone(new_id)
        playlists = self.rdb.get_playlists()
        names = [p.name for p in playlists]
        self.assertIn("New Playlist", names)

    def test_get_all_tracks(self):
        tracks = self.rdb.get_all_tracks()
        self.assertEqual(len(tracks), 2)


if __name__ == "__main__":
    unittest.main()
