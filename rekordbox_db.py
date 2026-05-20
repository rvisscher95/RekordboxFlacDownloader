"""
Rekordbox database interface.

Supports both:
  - Rekordbox 6/7 encrypted SQLite databases (via pyrekordbox + sqlcipher3).
  - Rekordbox 5 / unencrypted databases (plain sqlite3).

All public helpers that modify the database call ``commit()`` at the end so
callers don't have to remember to do so manually.
"""

import logging
import os
import sqlite3 as _sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class Playlist:
    id: str
    name: str
    parent_id: Optional[str]
    is_folder: bool = False

    def __repr__(self) -> str:
        return f"<Playlist id={self.id!r} name={self.name!r}>"


@dataclass
class ContentTrack:
    id: str
    title: str
    artist: str
    album: str
    folder_path: str      # Absolute path to the audio file
    file_type: int        # 1=MP3, 5=FLAC, …
    sample_rate: int      # e.g. 44100
    bit_depth: int        # e.g. 16
    bpm: float

    def __repr__(self) -> str:
        return f"<ContentTrack id={self.id!r} title={self.title!r}>"


# --------------------------------------------------------------------------- #
# Helper: unified database wrapper
# --------------------------------------------------------------------------- #

class RekordboxDatabase:
    """
    Thin wrapper around both pyrekordbox's ``Rekordbox6Database`` and a plain
    sqlite3 connection so that higher-level code stays database-agnostic.
    """

    def __init__(self, db_path: str):
        self._path = str(db_path)
        self._pyrekordbox_db = None  # Rekordbox6Database instance (if available)
        self._sqlite_conn = None     # plain sqlite3 connection (fallback)
        self._open()

    # ------------------------------------------------------------------ open

    def _open(self) -> None:
        """Try pyrekordbox first (handles encryption); fall back to sqlite3."""
        try:
            from pyrekordbox import Rekordbox6Database
            logger.debug("Opening %s via pyrekordbox (with decryption) …", self._path)
            self._pyrekordbox_db = Rekordbox6Database(path=self._path)
            logger.info("Opened Rekordbox database via pyrekordbox: %s", self._path)
            return
        except ImportError:
            logger.info("pyrekordbox not installed – trying plain sqlite3.")
        except Exception as exc:
            logger.warning("pyrekordbox failed (%s) – trying plain sqlite3.", exc)

        # Fallback: plain (unencrypted) sqlite3
        self._sqlite_conn = _sqlite3.connect(self._path)
        self._sqlite_conn.row_factory = _sqlite3.Row
        logger.info("Opened Rekordbox database via sqlite3: %s", self._path)

        # Verify the database is readable by checking for the expected table
        try:
            self._sqlite_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='djmdPlaylist'"
            ).fetchone()
        except _sqlite3.DatabaseError as exc:
            self._sqlite_conn.close()
            self._sqlite_conn = None
            raise RuntimeError(
                f"Cannot read database (it is likely encrypted): {exc}\n\n"
                "Rekordbox 6/7 databases are encrypted. To decrypt automatically:\n"
                "  pip install pyrekordbox sqlcipher3-wheels\n\n"
                "The tool will then decrypt the database using the built-in key."
            ) from exc

    # --------------------------------------------------------------- helpers

    @property
    def _uses_pyrekordbox(self) -> bool:
        return self._pyrekordbox_db is not None

    def _sql_query(self, sql: str, params: tuple = ()) -> List[_sqlite3.Row]:
        if self._sqlite_conn is None:
            raise RuntimeError("No sqlite3 connection available.")
        cur = self._sqlite_conn.execute(sql, params)
        return cur.fetchall()

    # --------------------------------------------------------------- playlists

    def get_playlists(self) -> List[Playlist]:
        """Return all user playlists (excluding hidden system folders)."""
        if self._uses_pyrekordbox:
            return self._get_playlists_pyrekordbox()
        return self._get_playlists_sqlite()

    def _get_playlists_pyrekordbox(self) -> List[Playlist]:
        db = self._pyrekordbox_db
        results: List[Playlist] = []
        for pl in db.get_playlist():
            # Skip special rekordbox system entries
            pl_id = str(pl.ID)
            if pl_id in ("root", "0", "100000", "200000"):
                continue
            parent_id = str(pl.ParentID) if pl.ParentID else None
            # Treat "root" or "0" parent as top-level
            if parent_id in ("root", "0"):
                parent_id = None
            results.append(Playlist(
                id=pl_id,
                name=pl.Name or "",
                parent_id=parent_id,
                is_folder=(getattr(pl, "Attribute", 0) == 1),
            ))
        return results

    def _get_playlists_sqlite(self) -> List[Playlist]:
        rows = self._sql_query(
            "SELECT ID, Name, ParentID, Attribute FROM djmdPlaylist ORDER BY Seq"
        )
        results = []
        for r in rows:
            pl_id = str(r["ID"])
            if pl_id in ("root", "0", "100000", "200000"):
                continue
            parent_id = str(r["ParentID"]) if r["ParentID"] else None
            # Treat "root" or "0" parent as top-level
            if parent_id in ("root", "0"):
                parent_id = None
            results.append(Playlist(
                id=pl_id,
                name=r["Name"] or "",
                parent_id=parent_id,
                is_folder=(r["Attribute"] == 1),
            ))
        return results

    # --------------------------------------------------------------- tracks

    def get_tracks_in_playlist(self, playlist_id: str) -> List[ContentTrack]:
        """Return tracks in the given playlist, in playlist order."""
        if self._uses_pyrekordbox:
            return self._get_tracks_pyrekordbox(playlist_id)
        return self._get_tracks_sqlite(playlist_id)

    def _get_tracks_pyrekordbox(self, playlist_id: str) -> List[ContentTrack]:
        db = self._pyrekordbox_db
        playlist = db.get_playlist(ID=playlist_id)
        if playlist is None:
            return []
        tracks = []
        for song in sorted(playlist.Songs, key=lambda s: s.TrackNo or 0):
            c = song.Content
            if c is None:
                continue
            tracks.append(_content_to_track(c))
        return tracks

    def _get_tracks_sqlite(self, playlist_id: str) -> List[ContentTrack]:
        rows = self._sql_query(
            """
            SELECT c.ID, c.Title, c.ArtistName, c.AlbumName,
                   c.FolderPath, c.FileType, c.SampleRate, c.BitRate, c.BPM
            FROM djmdSongPlaylist sp
            JOIN djmdContent c ON sp.ContentID = c.ID
            WHERE sp.PlaylistID = ?
            ORDER BY sp.TrackNo
            """,
            (playlist_id,),
        )
        return [_row_to_track(r) for r in rows]

    def get_all_tracks(self) -> List[ContentTrack]:
        """Return every track in the collection."""
        if self._uses_pyrekordbox:
            db = self._pyrekordbox_db
            return [_content_to_track(c) for c in db.get_content()]
        rows = self._sql_query(
            "SELECT ID, Title, ArtistName, AlbumName, FolderPath, "
            "FileType, SampleRate, BitRate, BPM FROM djmdContent"
        )
        return [_row_to_track(r) for r in rows]

    # --------------------------------------------------------------- update

    def update_track_path(self, track_id: str, new_path: str) -> None:
        """Change the audio file path for an existing track."""
        if self._uses_pyrekordbox:
            db = self._pyrekordbox_db
            content = db.get_content(ID=track_id)
            if content is None:
                raise ValueError(f"Track {track_id!r} not found in database.")
            content.FolderPath = new_path
            content.FileType = 5       # FLAC
            content.SampleRate = 44100
            db.commit()
        else:
            self._sqlite_conn.execute(
                "UPDATE djmdContent SET FolderPath=?, FileType=5, SampleRate=44100 WHERE ID=?",
                (new_path, track_id),
            )
            self._sqlite_conn.commit()

    def add_track(
        self,
        file_path: str,
        title: str,
        artist: str,
        album: str,
        genre: str = "",
    ) -> Optional[str]:
        """
        Add a new track to the collection.

        Returns the new track's ID, or ``None`` on failure.
        """
        if self._uses_pyrekordbox:
            return self._add_track_pyrekordbox(file_path, title, artist, album, genre)
        return self._add_track_sqlite(file_path, title, artist, album)

    def _add_track_pyrekordbox(
        self, file_path: str, title: str, artist: str, album: str, genre: str
    ) -> Optional[str]:
        db = self._pyrekordbox_db
        try:
            # Build kwargs for add_content with direct field names
            kwargs = {"Title": title}
            if artist:
                djm_artist = _get_or_create_artist(db, artist)
                kwargs["ArtistID"] = djm_artist.ID
                kwargs["ArtistName"] = artist
            if album:
                artist_id = kwargs.get("ArtistID")
                djm_album = _get_or_create_album(db, album, artist_id)
                kwargs["AlbumID"] = djm_album.ID
                kwargs["AlbumName"] = album
            if genre:
                djm_genre = _get_or_create_genre(db, genre)
                kwargs["GenreID"] = djm_genre.ID
                kwargs["GenreName"] = genre

            content = db.add_content(path=file_path, **kwargs)
            db.commit()
            return str(content.ID)
        except Exception as exc:
            logger.error("add_track_pyrekordbox failed: %s", exc)
            return None

    def _add_track_sqlite(
        self, file_path: str, title: str, artist: str, album: str
    ) -> Optional[str]:
        import time as _time
        new_id = str(int(_time.time() * 1000))[-10:]
        try:
            self._sqlite_conn.execute(
                """
                INSERT INTO djmdContent
                    (ID, Title, ArtistName, AlbumName, FolderPath, FileType,
                     SampleRate, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 5, 44100, datetime('now'), datetime('now'))
                """,
                (new_id, title, artist, album, file_path),
            )
            self._sqlite_conn.commit()
            return new_id
        except Exception as exc:
            logger.error("_add_track_sqlite failed: %s", exc)
            return None

    def add_track_to_playlist(self, track_id: str, playlist_id: str) -> None:
        """Append a track to the end of a playlist."""
        if self._uses_pyrekordbox:
            db = self._pyrekordbox_db
            playlist = db.get_playlist(ID=playlist_id)
            content = db.get_content(ID=track_id)
            if playlist is None or content is None:
                raise ValueError("Playlist or track not found.")
            db.add_to_playlist(playlist, content)
            db.commit()
        else:
            cur = self._sqlite_conn.execute(
                "SELECT MAX(TrackNo) FROM djmdSongPlaylist WHERE PlaylistID=?",
                (playlist_id,),
            )
            row = cur.fetchone()
            next_pos = (row[0] or 0) + 1
            self._sqlite_conn.execute(
                """
                INSERT INTO djmdSongPlaylist (PlaylistID, ContentID, TrackNo,
                    created_at, updated_at)
                VALUES (?, ?, ?, datetime('now'), datetime('now'))
                """,
                (playlist_id, track_id, next_pos),
            )
            self._sqlite_conn.commit()

    def create_playlist(self, name: str, parent_id: Optional[str] = None) -> Optional[str]:
        """Create a new playlist.  Returns its ID."""
        if self._uses_pyrekordbox:
            db = self._pyrekordbox_db
            pl = db.create_playlist(name)
            db.commit()
            return str(pl.ID)
        # Sqlite fallback
        import time as _time
        new_id = str(int(_time.time() * 1000))[-10:]
        try:
            self._sqlite_conn.execute(
                """
                INSERT INTO djmdPlaylist (ID, Name, ParentID, Attribute, Seq,
                    created_at, updated_at)
                VALUES (?, ?, ?, 0, 0, datetime('now'), datetime('now'))
                """,
                (new_id, name, parent_id),
            )
            self._sqlite_conn.commit()
            return new_id
        except Exception as exc:
            logger.error("create_playlist (sqlite) failed: %s", exc)
            return None

    # --------------------------------------------------------------- close

    def close(self) -> None:
        if self._pyrekordbox_db:
            try:
                self._pyrekordbox_db.close()
            except Exception:
                pass
        if self._sqlite_conn:
            self._sqlite_conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #

def _content_to_track(c) -> ContentTrack:
    """Convert a pyrekordbox DjmdContent object to a ContentTrack."""
    return ContentTrack(
        id=str(c.ID),
        title=c.Title or "",
        artist=c.ArtistName or "",
        album=c.AlbumName or "",
        folder_path=c.FolderPath or "",
        file_type=int(c.FileType or 0),
        sample_rate=int(c.SampleRate or 0),
        bit_depth=int(getattr(c, "BitDepth", 0) or 0),
        bpm=float(c.BPM or 0),
    )


def _row_to_track(r) -> ContentTrack:
    """Convert a sqlite3 Row to a ContentTrack."""
    return ContentTrack(
        id=str(r["ID"]),
        title=r["Title"] or "",
        artist=r["ArtistName"] or "",
        album=r["AlbumName"] or "",
        folder_path=r["FolderPath"] or "",
        file_type=int(r["FileType"] or 0),
        sample_rate=int(r["SampleRate"] or 0),
        bit_depth=0,
        bpm=float(r["BPM"] or 0),
    )


def _get_or_create_artist(db, name: str):
    artist = db.get_artist(Name=name)
    if hasattr(artist, "first"):
        artist = artist.first()
    if artist is not None:
        return artist
    return db.add_artist(name)


def _get_or_create_album(db, name: str, artist_id):
    album = db.get_album(Name=name)
    if hasattr(album, "first"):
        album = album.first()
    if album is not None:
        return album
    return db.add_album(name, artist=artist_id)


def _get_or_create_genre(db, name: str):
    genre = db.get_genre(Name=name)
    if hasattr(genre, "first"):
        genre = genre.first()
    if genre is not None:
        return genre
    return db.add_genre(name)
