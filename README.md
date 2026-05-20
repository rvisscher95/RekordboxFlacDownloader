# RekordboxFlacDownloader

A Python desktop tool that automatically replaces MP3 (and other) tracks in your
**Rekordbox** library with high-quality **FLAC** files (16-bit / 44.1 kHz) sourced
from the [qobuz.squid.wtf](https://qobuz.squid.wtf) proxy API.

---

## Features

| Feature | Details |
|---------|---------|
| 📂 Open Rekordbox DB | Browse to any `master.db` file (Rekordbox 5/6/7) |
| 🎵 Playlist browser | Visual tree of all playlists and folders |
| ⬇ Batch download | Replace every non-FLAC track in a playlist with a CD-quality FLAC |
| 🔍 Search & add | Find a new track by name, download it, and add it straight to a playlist |
| ✏ DB patching | File paths and format metadata are updated so the library works directly in Rekordbox |
| 📋 Activity log | Every action is logged with a success/failure indicator |
| 🎚 FLAC quality | 16-bit / 44.1 kHz (Qobuz quality code 7) with automatic fallback |

---

## Installation

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Python 3.9+** | <https://www.python.org> |
| **Rekordbox 6 / 7** *(optional)* | Only needed if you want pyrekordbox to decrypt the database automatically. Rekordbox 5 databases are plain SQLite – no extra setup required. |
| **SQLCipher** *(Rekordbox 6/7 only)* | Installed automatically via `sqlcipher3-wheels` on most platforms. See [pyrekordbox installation guide](https://pyrekordbox.readthedocs.io/en/latest/installation.html) if it fails. |

> ⚠️ **Always back up your `master.db` before using this tool.**
> In Rekordbox: *File → Library → Backup Library*

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/rvisscher95/RekordboxFlacDownloader.git
cd RekordboxFlacDownloader

# 2. (Recommended) Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Launch the tool
python app.py
```

---

## Usage

1. **Open your Rekordbox database**
   Click *Browse…* next to "Rekordbox Database" and select your `master.db`.

   Default locations:
   - **Windows**: `%APPDATA%\Pioneer\rekordbox\master.db`
   - **macOS**: `~/Library/Application Support/Pioneer/rekordbox/master.db`

2. **Select a playlist** from the left panel.

3. **Replace all tracks with FLAC**
   Click *⬇ Download All (FLAC)*. The tool will:
   - Search qobuz.squid.wtf for each non-FLAC track.
   - Download the CD-quality FLAC to the configured download directory.
   - Update the file path in your Rekordbox database.
   - Show a summary in the log panel.

4. **Add a new track**
   Click *＋ Add Track…*, search by artist/title, select a result,
   and click *Download & Add*. The track is downloaded and added to the
   currently selected playlist.

5. **Change the download directory**
   Click *Browse…* next to "Download dir". Default:
   `~/Music/RekordboxFlac/`

---

## File layout

```
RekordboxFlacDownloader/
├── app.py            – UI application (tkinter)
├── downloader.py     – Qobuz squid.wtf API wrapper + download logic
├── rekordbox_db.py   – Rekordbox database interface (pyrekordbox / sqlite3)
├── tests.py          – Unit tests
└── requirements.txt
```

---

## Running tests

```bash
pip install pytest
python -m pytest tests.py -v
```

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `pyrekordbox` | Read/write Rekordbox 6/7 databases (handles SQLCipher encryption) |
| `requests` | HTTP calls to the Qobuz proxy API |
| `mutagen` | Embed metadata (title, artist, cover art) into downloaded FLAC files |

---

## Disclaimer

This tool downloads music via the community-maintained [qobuz.squid.wtf](https://qobuz.squid.wtf)
proxy. Use it **only for music you own or have the rights to**. The authors are not
affiliated with Qobuz or Pioneer/AlphaTheta and take no responsibility for misuse.
