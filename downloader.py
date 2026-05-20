"""
Qobuz FLAC downloader via multiple provider APIs.

Quality codes
-------------
5  – Hi-Res FLAC 24-bit / 192 kHz
6  – Hi-Res FLAC 24-bit / 96 kHz
7  – FLAC 16-bit / 44.1 kHz  (CD quality – used as default)
27 – MP3 320 kbps
"""

import hashlib
import os
import re
import time
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

QUALITY_FLAC_16 = 7   # 16-bit 44.1 kHz – the format requested by the user
QUALITY_HIRES_96 = 6
QUALITY_HIRES_192 = 5

# --- Search mirrors (qobuz.squid.wtf still works for search) ---
_SEARCH_SERVERS = [
    "https://eu.qobuz.squid.wtf",
    "https://us.qobuz.squid.wtf",
    "https://qobuz.squid.wtf",
]

# --- WJHE provider (primary download provider) ---
_WJHE_STREAM_URL = "https://music.wjhe.top/api/music/qobuz/url"
_WJHE_SEARCH_URL = "https://music.wjhe.top/api/music/qobuz/search"

# --- GDStudio provider (fallback download provider) ---
_GDSTUDIO_API_URLS = [
    "https://music.gdstudio.xyz/api.php",
    "https://music.gdstudio.org/api.php",
]
_GDSTUDIO_VERSION = "2026.5.10"

# --- Squid (legacy, may have expired tokens) ---
_SQUID_DOWNLOAD_SERVERS = [
    "https://eu.qobuz.squid.wtf",
    "https://us.qobuz.squid.wtf",
    "https://qobuz.squid.wtf",
]

_REQUEST_TIMEOUT = 20   # seconds for API calls
_DOWNLOAD_TIMEOUT = 120  # seconds for actual file stream
_CHUNK_SIZE = 65_536    # 64 KiB chunks

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _default_headers() -> dict:
    """Return standard browser-like headers for API requests."""
    return {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _get(endpoint: str, params: dict, retries: int = 3) -> Optional[dict]:
    """Try each search mirror in sequence; return first successful JSON body."""
    last_exc: Optional[Exception] = None
    for base in _SEARCH_SERVERS:
        url = f"{base}{endpoint}"
        for attempt in range(retries):
            try:
                resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT,
                                    headers=_default_headers())
                if resp.status_code == 200:
                    return resp.json()
                logger.debug("Server %s returned %s", base, resp.status_code)
            except requests.RequestException as exc:
                last_exc = exc
                logger.debug("Request error (%s, attempt %d): %s", base, attempt + 1, exc)
                if attempt < retries - 1:
                    time.sleep(1)
    if last_exc:
        logger.warning("All search servers failed: %s", last_exc)
    return None


# --------------------------------------------------------------------------- #
# Download providers
# --------------------------------------------------------------------------- #

def _get_download_url_wjhe(track_id: str, quality: int) -> Optional[str]:
    """
    Get download URL from the WJHE provider.

    WJHE API: GET /api/music/qobuz/url?ID={track_id}&quality={bitrate}&format=flac
    Returns a redirect URL or stream URL.
    """
    # Map quality code to WJHE quality value
    if quality in (27, 7):
        wjhe_quality = 2000
        wjhe_format = "flac"
    elif quality == 6:
        wjhe_quality = 1000
        wjhe_format = "flac"
    else:
        wjhe_quality = 320
        wjhe_format = "mp3"

    params = {
        "ID": track_id,
        "quality": wjhe_quality,
        "format": wjhe_format,
    }

    try:
        # First try HEAD to get redirect Location
        resp = requests.head(
            _WJHE_STREAM_URL, params=params,
            timeout=_REQUEST_TIMEOUT, headers=_default_headers(),
            allow_redirects=False,
        )

        # Check for redirect URL
        location = resp.headers.get("Location", "").strip()
        if location and _url_looks_streamable(location):
            logger.debug("WJHE returned redirect: %s", location[:80])
            return location

        # If HEAD is not supported, try GET
        if resp.status_code in (405, 501, 404):
            resp = requests.get(
                _WJHE_STREAM_URL, params=params,
                timeout=_REQUEST_TIMEOUT, headers=_default_headers(),
                allow_redirects=False,
            )
            location = resp.headers.get("Location", "").strip()
            if location and _url_looks_streamable(location):
                return location

            # Try to extract URL from response body
            if resp.status_code == 200:
                stream_url = _extract_stream_url(resp.text)
                if stream_url:
                    return stream_url

        # Also check if the final URL after redirect is streamable
        if resp.status_code in (301, 302, 303, 307, 308):
            # Follow the redirect
            resp2 = requests.get(
                _WJHE_STREAM_URL, params=params,
                timeout=_REQUEST_TIMEOUT, headers=_default_headers(),
                allow_redirects=True,
            )
            if resp2.url and _url_looks_streamable(resp2.url):
                return resp2.url
            stream_url = _extract_stream_url(resp2.text)
            if stream_url:
                return stream_url

        # If status is 200 and no redirect, try parsing body
        if resp.status_code == 200:
            resp = requests.get(
                _WJHE_STREAM_URL, params=params,
                timeout=_REQUEST_TIMEOUT, headers=_default_headers(),
                allow_redirects=True,
            )
            if resp.url and resp.url != _WJHE_STREAM_URL and _url_looks_streamable(resp.url):
                return resp.url
            stream_url = _extract_stream_url(resp.text)
            if stream_url:
                return stream_url

        logger.debug("WJHE: no stream URL found (status=%d)", resp.status_code)
    except requests.RequestException as exc:
        logger.debug("WJHE request failed: %s", exc)

    return None


def _get_download_url_gdstudio(track_id: str, quality: int) -> Optional[str]:
    """
    Get download URL from GDStudio provider.

    POST to API with form data including a signature.
    """
    # Map quality to bitrate
    if quality in (27, 7):
        bitrate = "999"
    elif quality == 6:
        bitrate = "740"
    else:
        bitrate = "320"

    for api_url in _GDSTUDIO_API_URLS:
        try:
            ts9 = _gdstudio_get_ts9(api_url)
            signature = _gdstudio_build_signature(api_url, track_id, ts9)

            data = {
                "types": "url",
                "id": track_id,
                "source": "qobuz",
                "br": bitrate,
                "s": signature,
            }

            host = _gdstudio_get_host(api_url)
            headers = _default_headers()
            headers.update({
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": f"https://{host}",
                "Referer": f"https://{host}/",
            })

            resp = requests.post(
                api_url, data=data,
                timeout=_REQUEST_TIMEOUT, headers=headers,
            )

            if resp.status_code != 200:
                logger.debug("GDStudio %s returned status %d", api_url, resp.status_code)
                continue

            stream_url = _extract_stream_url(resp.text)
            if stream_url:
                return stream_url

            logger.debug("GDStudio %s: no stream URL in response", api_url)
        except requests.RequestException as exc:
            logger.debug("GDStudio %s failed: %s", api_url, exc)

    return None


def _get_download_url_squid(track_id: str, quality: int) -> Optional[str]:
    """
    Get download URL from qobuz.squid.wtf (legacy provider, may have expired tokens).
    """
    for base in _SQUID_DOWNLOAD_SERVERS:
        try:
            resp = requests.get(
                f"{base}/api/download-music",
                params={"track_id": track_id, "quality": quality},
                timeout=_REQUEST_TIMEOUT, headers=_default_headers(),
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    url = (data.get("data") or {}).get("url")
                    if url:
                        return url
                else:
                    logger.debug("Squid %s: %s", base, data.get("error", "unsuccessful"))
        except requests.RequestException as exc:
            logger.debug("Squid %s failed: %s", base, exc)

    return None


# --- GDStudio helpers ---

def _gdstudio_get_host(api_url: str) -> str:
    """Extract host from API URL."""
    from urllib.parse import urlparse
    parsed = urlparse(api_url)
    return parsed.netloc


def _gdstudio_get_ts9(api_url: str) -> str:
    """Get 9-digit timestamp from GDStudio's /time endpoint, or use local time."""
    fallback = str(int(time.time() * 1000))[:9]
    host = _gdstudio_get_host(api_url)
    if not host:
        return fallback
    try:
        resp = requests.get(
            f"https://{host}/time",
            timeout=5, headers=_default_headers(),
        )
        ts = resp.text.strip()
        if len(ts) >= 9:
            return ts[:9]
    except requests.RequestException:
        pass
    return fallback


def _gdstudio_padded_version() -> str:
    """Pad version parts to 2 digits: '2026.5.10' -> '20260510'."""
    parts = _GDSTUDIO_VERSION.split(".")
    padded = []
    for part in parts:
        part = part.strip()
        if len(part) == 1:
            part = "0" + part
        padded.append(part)
    return "".join(padded)


def _gdstudio_build_signature(api_url: str, track_id: str, ts9: str) -> str:
    """Build the GDStudio request signature."""
    host = _gdstudio_get_host(api_url)
    escaped_id = quote(track_id.strip(), safe="")
    signature_base = f"{host}|{_gdstudio_padded_version()}|{ts9}|{escaped_id}"
    digest = hashlib.md5(signature_base.encode()).hexdigest()
    return digest[-8:].upper()


# --- Stream URL extraction helpers ---

# Pattern to match Qobuz CDN streaming URLs
_STREAM_URL_PATTERN = re.compile(
    r'https?://[^\s"<>]+\.(?:qobuz|akamai|cloudfront|amazonaws)[^\s"<>]*',
    re.IGNORECASE,
)


def _url_looks_streamable(url: str) -> bool:
    """Check if a URL looks like a valid audio stream URL."""
    if not url or len(url) < 20:
        return False
    lower = url.lower()
    # Must be HTTPS and look like a CDN URL
    if not lower.startswith("http"):
        return False
    # Reject obviously non-audio URLs
    if any(x in lower for x in (".html", ".htm", ".js", ".css", ".png", ".jpg", ".gif")):
        return False
    return True


def _extract_stream_url(text: str) -> Optional[str]:
    """Try to extract a streaming URL from response text (JSON or plain)."""
    if not text:
        return None

    # Try JSON parsing first
    try:
        import json
        data = json.loads(text)
        # Look for common URL keys
        for key in ("url", "download_url", "stream_url", "file_url", "data"):
            if key in data:
                val = data[key]
                if isinstance(val, str) and _url_looks_streamable(val):
                    return val
                if isinstance(val, dict):
                    for subkey in ("url", "download_url", "stream_url"):
                        if subkey in val and isinstance(val[subkey], str):
                            if _url_looks_streamable(val[subkey]):
                                return val[subkey]
    except (ValueError, TypeError):
        pass

    # Try regex extraction
    match = _STREAM_URL_PATTERN.search(text)
    if match:
        url = match.group(0).rstrip('",}]')
        if _url_looks_streamable(url):
            return url

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
    Retrieve the direct download URL for a track using multiple providers.

    Tries providers in order: WJHE → GDStudio → Squid (legacy).

    Parameters
    ----------
    track_id : str
        The Qobuz numeric track identifier.
    quality : int
        Quality tier (default: 7 = FLAC 16-bit 44.1 kHz).

    Returns
    -------
    str or None
        The pre-signed download URL, or ``None`` if all providers failed.
    """
    providers = [
        ("WJHE", _get_download_url_wjhe),
        ("GDStudio", _get_download_url_gdstudio),
        ("Squid", _get_download_url_squid),
    ]

    for name, provider_fn in providers:
        try:
            url = provider_fn(track_id, quality)
            if url:
                logger.info("Download URL obtained via %s for track %s", name, track_id)
                return url
            logger.debug("Provider %s returned no URL for track %s", name, track_id)
        except Exception as exc:
            logger.debug("Provider %s error for track %s: %s", name, track_id, exc)

    logger.warning("All download providers failed for track %s (quality=%d)", track_id, quality)
    return None


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
        resp = requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT,
                            headers=_default_headers())
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
