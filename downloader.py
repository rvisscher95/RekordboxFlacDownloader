"""
Qobuz FLAC downloader via the qobuz.squid.wtf proxy API.

Quality codes
-------------
5  – Hi-Res FLAC 24-bit / 192 kHz
6  – Hi-Res FLAC 24-bit / 96 kHz
7  – FLAC 16-bit / 44.1 kHz  (CD quality – used as default)
27 – MP3 320 kbps
"""

import os
import re
import time
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

QUALITY_FLAC_16 = 7   # 16-bit 44.1 kHz – the format requested by the user
QUALITY_HIRES_96 = 6
QUALITY_HIRES_192 = 5

# Ordered list of mirror servers – tried in sequence until one works.
_API_SERVERS = [
    "https://eu.qobuz.squid.wtf",
    "https://us.qobuz.squid.wtf",
    "https://qobuz.squid.wtf",
]

_REQUEST_TIMEOUT = 20   # seconds for API calls
_DOWNLOAD_TIMEOUT = 120  # seconds for actual file stream
_CHUNK_SIZE = 65_536    # 64 KiB chunks


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _get(endpoint: str, params: dict, retries: int = 3) -> Optional[dict]:
    """Try each mirror server in sequence; return first successful JSON body."""
    last_exc: Optional[Exception] = None
    for base in _API_SERVERS:
        url = f"{base}{endpoint}"
        for attempt in range(retries):
            try:
                resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    return resp.json()
                logger.debug("Server %s returned %s", base, resp.status_code)
            except requests.RequestException as exc:
                last_exc = exc
                logger.debug("Request error (%s, attempt %d): %s", base, attempt + 1, exc)
                if attempt < retries - 1:
                    time.sleep(1)
    if last_exc:
        logger.warning("All servers failed: %s", last_exc)
    return None


def _sanitise_filename(text: str) -> str:
    """Remove characters that are invalid in file names on Windows / macOS."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

class Track:
    """Lightweight container for a Qobuz track search result."""

    def __init__(self, raw: dict):
        self._raw = raw
        performer = raw.get("performer") or raw.get("artist") or {}
        self.artist: str = (
            performer.get("name", "Unknown Artist")
            if isinstance(performer, dict)
            else str(performer)
        )
        album = raw.get("album") or {}
        self.album: str = album.get("title", "") if isinstance(album, dict) else ""
        self.title: str = raw.get("title", "Unknown")
        self.track_id: str = str(raw.get("id", ""))
        self.isrc: str = raw.get("isrc", "")
        self.duration: int = raw.get("duration", 0)
        self.is_hires: bool = bool(raw.get("hires") or raw.get("hires_streamable"))
        # Cover art URL
        image = album.get("image", {}) if isinstance(album, dict) else {}
        self.cover_url: str = (
            image.get("large") or image.get("small") or image.get("thumbnail") or ""
            if isinstance(image, dict)
            else ""
        )

    @property
    def display_name(self) -> str:
        return f"{self.artist} – {self.title}"

    def __repr__(self) -> str:
        return f"<Track id={self.track_id!r} title={self.title!r} artist={self.artist!r}>"


def search(query: str, limit: int = 10) -> List[Track]:
    """
    Search for tracks on Qobuz via the squid.wtf proxy.

    Parameters
    ----------
    query : str
        Search string, e.g. ``"Artist Title"`` or an ISRC code.
    limit : int
        Maximum number of results to return.

    Returns
    -------
    list[Track]
        Matching tracks (may be empty if nothing was found).
    """
    data = _get("/api/get-music", params={"q": query, "offset": 0, "limit": limit})
    if data is None:
        logger.warning("Search returned no response for query: %s", query)
        return []

    if not data.get("success"):
        logger.warning("Search unsuccessful: %s", data.get("message", ""))
        return []

    payload = data.get("data") or {}
    # The API may return results under several keys; prefer 'most_popular'.
    items: List[dict] = []
    if "most_popular" in payload:
        for entry in payload["most_popular"].get("items", []):
            if entry.get("type") == "tracks":
                content = entry.get("content")
                if isinstance(content, dict):
                    items.append(content)
    if not items:
        tracks_obj = payload.get("tracks") or {}
        items = tracks_obj.get("items", []) if isinstance(tracks_obj, dict) else []

    return [Track(t) for t in items if isinstance(t, dict)]


def get_download_url(track_id: str, quality: int = QUALITY_FLAC_16) -> Optional[str]:
    """
    Retrieve the direct download URL for a track.

    Parameters
    ----------
    track_id : str
        The Qobuz numeric track identifier.
    quality : int
        Quality tier (default: 7 = FLAC 16-bit 44.1 kHz).

    Returns
    -------
    str or None
        The pre-signed download URL, or ``None`` if the request failed.
    """
    data = _get("/api/download-music", params={"track_id": track_id, "quality": quality})
    if data is None:
        return None
    if not data.get("success"):
        logger.warning("Download URL request unsuccessful for track %s: %s",
                       track_id, data.get("message", ""))
        return None
    return (data.get("data") or {}).get("url")


def download_track(
    track: Track,
    output_dir: str,
    quality: int = QUALITY_FLAC_16,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Optional[str]:
    """
    Download a track to *output_dir* as a FLAC file.

    Parameters
    ----------
    track : Track
        The track to download.
    output_dir : str
        Directory where the FLAC file will be saved.
    quality : int
        Quality code (default 7 = 16-bit / 44.1 kHz FLAC).
    progress_callback : callable, optional
        Called as ``progress_callback(bytes_downloaded, total_bytes)``
        during the download; ``total_bytes`` may be 0 if unknown.

    Returns
    -------
    str or None
        Absolute path of the saved FLAC file, or ``None`` on failure.
    """
    url = get_download_url(track.track_id, quality)
    if not url:
        # Try fallback qualities
        for fallback_quality in (QUALITY_HIRES_96, QUALITY_HIRES_192):
            url = get_download_url(track.track_id, fallback_quality)
            if url:
                logger.info("Fell back to quality %s for track %s", fallback_quality, track.track_id)
                break
    if not url:
        logger.error("Could not obtain download URL for track %s", track.track_id)
        return None

    try:
        resp = requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Download request failed for track %s: %s", track.track_id, exc)
        return None

    total = int(resp.headers.get("content-length", 0))

    safe_artist = _sanitise_filename(track.artist)
    safe_title = _sanitise_filename(track.title)
    filename = f"{safe_artist} - {safe_title}.flac"
    filepath = Path(output_dir) / filename

    # Ensure the output directory exists.
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    downloaded = 0
    try:
        with open(filepath, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if chunk:
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)
    except OSError as exc:
        logger.error("File write error for %s: %s", filepath, exc)
        return None

    # Sanity-check: FLAC files should be at least 1 MB.
    if filepath.stat().st_size < 1_000_000:
        logger.error(
            "Downloaded file is suspiciously small (%d bytes) – removing.",
            filepath.stat().st_size,
        )
        filepath.unlink(missing_ok=True)
        return None

    # Embed metadata.
    try:
        _write_metadata(filepath, track)
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not write metadata for %s: %s", filepath, exc)

    return str(filepath)


def _write_metadata(filepath: Path, track: Track) -> None:
    """Embed Vorbis comment tags and cover art into the downloaded FLAC file."""
    try:
        from mutagen.flac import FLAC, Picture
    except ImportError:
        logger.warning("mutagen not installed – skipping metadata embedding.")
        return

    audio = FLAC(filepath)
    audio["title"] = track.title
    audio["artist"] = track.artist
    if track.album:
        audio["album"] = track.album
    if track.isrc:
        audio["isrc"] = track.isrc

    if track.cover_url:
        try:
            img_resp = requests.get(track.cover_url, timeout=10)
            img_resp.raise_for_status()
            pic = Picture()
            pic.type = 3          # Front cover
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = img_resp.content
            audio.clear_pictures()
            audio.add_picture(pic)
        except Exception as exc:
            logger.debug("Could not embed cover art: %s", exc)

    audio.save()
