"""
RekordboxFlacDownloader – main application.

Launch with:
    python app.py

Requirements:
    pip install -r requirements.txt
"""

import logging
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

from downloader import QUALITY_FLAC_16, Track, download_track, search
from rekordbox_db import ContentTrack, Playlist, RekordboxDatabase

# --------------------------------------------------------------------------- #
# Logging setup
# --------------------------------------------------------------------------- #

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Search dialog
# --------------------------------------------------------------------------- #

class SearchDialog(tk.Toplevel):
    """Modal dialog for searching and adding a new track to a playlist."""

    def __init__(self, parent: "App", playlist_id: str, playlist_name: str):
        super().__init__(parent)
        self.title(f"Add track → {playlist_name}")
        self.resizable(True, True)
        self.grab_set()   # modal

        self._parent = parent
        self._playlist_id = playlist_id
        self._playlist_name = playlist_name
        self._results: List[Track] = []
        self._selected_track: Optional[Track] = None

        self._build_ui()
        self.geometry("700x460")
        self.minsize(500, 360)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # Search bar
        search_frame = ttk.Frame(self)
        search_frame.pack(fill="x", **pad)

        ttk.Label(search_frame, text="Search:").pack(side="left")
        self._search_var = tk.StringVar()
        entry = ttk.Entry(search_frame, textvariable=self._search_var, width=50)
        entry.pack(side="left", padx=4, fill="x", expand=True)
        entry.bind("<Return>", lambda _: self._do_search())

        ttk.Button(search_frame, text="Search", command=self._do_search).pack(side="left")

        # Results list
        cols = ("artist", "title", "album")
        self._tree = ttk.Treeview(self, columns=cols, show="headings", height=12)
        self._tree.heading("artist", text="Artist")
        self._tree.heading("title", text="Title")
        self._tree.heading("album", text="Album")
        self._tree.column("artist", width=180)
        self._tree.column("title", width=200)
        self._tree.column("album", width=180)
        self._tree.pack(fill="both", expand=True, **pad)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        # Status label
        self._status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._status_var, foreground="gray").pack(**pad)

        # Buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", **pad)
        self._add_btn = ttk.Button(btn_frame, text="Download & Add",
                                   command=self._do_add, state="disabled")
        self._add_btn.pack(side="right", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="right")

    # --------------------------------------------------------------- actions

    def _do_search(self) -> None:
        query = self._search_var.get().strip()
        if not query:
            return
        self._status_var.set("Searching …")
        self._tree.delete(*self._tree.get_children())
        self._add_btn.config(state="disabled")

        def worker():
            results = search(query, limit=15)
            self.after(0, self._populate_results, results)

        threading.Thread(target=worker, daemon=True).start()

    def _populate_results(self, results: List[Track]) -> None:
        self._results = results
        self._tree.delete(*self._tree.get_children())
        if not results:
            self._status_var.set("No results found.")
            return
        for t in results:
            self._tree.insert("", "end", iid=t.track_id,
                              values=(t.artist, t.title, t.album))
        self._status_var.set(f"{len(results)} result(s).")

    def _on_select(self, _event=None) -> None:
        sel = self._tree.selection()
        if sel:
            tid = sel[0]
            self._selected_track = next((t for t in self._results if t.track_id == tid), None)
            self._add_btn.config(state="normal" if self._selected_track else "disabled")

    def _do_add(self) -> None:
        if self._selected_track is None:
            return
        self._add_btn.config(state="disabled")
        self._status_var.set("Downloading …")
        track = self._selected_track

        def worker():
            result_path = download_track(
                track,
                output_dir=self._parent.download_dir,
                quality=QUALITY_FLAC_16,
            )
            self.after(0, self._finish_add, track, result_path)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_add(self, track: Track, result_path: Optional[str]) -> None:
        if result_path is None:
            self._status_var.set("Download failed.")
            messagebox.showerror("Download failed",
                                 f"Could not download:\n{track.display_name}",
                                 parent=self)
            self._add_btn.config(state="normal")
            return

        db = self._parent.db
        if db is None:
            self._status_var.set("No database open.")
            return

        try:
            track_id = db.add_track(
                file_path=result_path,
                title=track.title,
                artist=track.artist,
                album=track.album,
            )
            if track_id:
                db.add_track_to_playlist(track_id, self._playlist_id)
            self._parent.log(f"✓ Added: {track.display_name} → {self._playlist_name}")
            self._parent.refresh_track_list()
            self.destroy()
        except Exception as exc:
            logger.exception("Error adding track to database")
            self._status_var.set(f"Database error: {exc}")
            messagebox.showerror("Database error", str(exc), parent=self)
            self._add_btn.config(state="normal")


# --------------------------------------------------------------------------- #
# Main application window
# --------------------------------------------------------------------------- #

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RekordboxFlacDownloader")
        self.geometry("1050x680")
        self.minsize(750, 500)

        # State
        self.db: Optional[RekordboxDatabase] = None
        self._playlists: List[Playlist] = []
        self._tracks: List[ContentTrack] = []
        self._playlist_map: Dict[str, Playlist] = {}  # id → Playlist
        self._active_playlist_id: Optional[str] = None

        # Download directory – default to ~/Music/RekordboxFlac
        self.download_dir = str(Path.home() / "Music" / "RekordboxFlac")

        # Log queue for thread-safe logging
        self._log_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._poll_log_queue()

    # ------------------------------------------------------------------ UI build

    def _build_ui(self) -> None:
        # Top bar
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")

        ttk.Label(top, text="Rekordbox Database:").pack(side="left")
        self._db_var = tk.StringVar(value="— no database selected —")
        ttk.Entry(top, textvariable=self._db_var, width=52, state="readonly").pack(
            side="left", padx=4)
        ttk.Button(top, text="Browse…", command=self._browse_db).pack(side="left")

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Label(top, text="Download dir:").pack(side="left")
        self._dldir_var = tk.StringVar(value=self.download_dir)
        ttk.Entry(top, textvariable=self._dldir_var, width=30, state="readonly").pack(
            side="left", padx=4)
        ttk.Button(top, text="Browse…", command=self._browse_dldir).pack(side="left")

        # Main content: paned window
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=6, pady=4)

        # Left: playlist tree
        left = ttk.Frame(paned, padding=2)
        paned.add(left, weight=1)

        ttk.Label(left, text="Playlists", font=("", 10, "bold")).pack(anchor="w")
        self._pl_tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self._pl_tree.pack(fill="both", expand=True)
        self._pl_tree.bind("<<TreeviewSelect>>", self._on_playlist_select)

        pl_scroll = ttk.Scrollbar(left, orient="vertical", command=self._pl_tree.yview)
        self._pl_tree.configure(yscrollcommand=pl_scroll.set)
        pl_scroll.pack(side="right", fill="y")

        # Right: track list + action bar
        right = ttk.Frame(paned, padding=2)
        paned.add(right, weight=3)

        # Action bar
        action_bar = ttk.Frame(right)
        action_bar.pack(fill="x", pady=(0, 4))

        self._dl_all_btn = ttk.Button(action_bar, text="⬇ Download All (FLAC)",
                                      command=self._download_all, state="disabled")
        self._dl_all_btn.pack(side="left", padx=2)

        self._add_track_btn = ttk.Button(action_bar, text="＋ Add Track…",
                                         command=self._open_search, state="disabled")
        self._add_track_btn.pack(side="left", padx=2)

        self._progress_var = tk.StringVar(value="")
        ttk.Label(action_bar, textvariable=self._progress_var, foreground="#555").pack(
            side="right", padx=6)

        # Track list
        cols = ("status", "title", "artist", "album", "format", "path")
        self._track_tree = ttk.Treeview(right, columns=cols, show="headings", height=16)
        self._track_tree.heading("status", text="")
        self._track_tree.heading("title", text="Title")
        self._track_tree.heading("artist", text="Artist")
        self._track_tree.heading("album", text="Album")
        self._track_tree.heading("format", text="Format")
        self._track_tree.heading("path", text="File path")
        self._track_tree.column("status", width=28, anchor="center", stretch=False)
        self._track_tree.column("title", width=200)
        self._track_tree.column("artist", width=160)
        self._track_tree.column("album", width=140)
        self._track_tree.column("format", width=60, anchor="center")
        self._track_tree.column("path", width=300)
        self._track_tree.pack(fill="both", expand=True)

        tr_scroll = ttk.Scrollbar(right, orient="vertical",
                                  command=self._track_tree.yview)
        self._track_tree.configure(yscrollcommand=tr_scroll.set)
        tr_scroll.pack(side="right", fill="y")

        # Bottom: log panel
        log_frame = ttk.LabelFrame(self, text="Log", padding=4)
        log_frame.pack(fill="x", padx=6, pady=(0, 6))

        self._log_text = tk.Text(log_frame, height=7, state="disabled",
                                 wrap="word", font=("Courier", 9))
        self._log_text.pack(fill="both", expand=True, side="left")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical",
                                   command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")

        # Colour tags for the log
        self._log_text.tag_configure("ok", foreground="#007700")
        self._log_text.tag_configure("err", foreground="#cc0000")
        self._log_text.tag_configure("info", foreground="#333333")

    # ------------------------------------------------------------------ helpers

    def log(self, message: str, tag: str = "info") -> None:
        """Thread-safe log helper."""
        self._log_queue.put((message, tag))

    def _poll_log_queue(self) -> None:
        while True:
            try:
                msg, tag = self._log_queue.get_nowait()
                self._append_log(msg, tag)
            except queue.Empty:
                break
        self.after(100, self._poll_log_queue)

    def _append_log(self, message: str, tag: str = "info") -> None:
        self._log_text.config(state="normal")
        self._log_text.insert("end", message + "\n", tag)
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _fmt_label(self, track: ContentTrack) -> str:
        """Return a short format string like 'FLAC' or 'MP3'."""
        ft = track.file_type
        return {1: "MP3", 2: "M4A", 3: "WAV", 5: "FLAC", 6: "AIFF"}.get(ft, str(ft))

    def _is_flac(self, track: ContentTrack) -> bool:
        return track.file_type == 5 or track.folder_path.lower().endswith(".flac")

    # ------------------------------------------------------------------ open db

    def _browse_db(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Rekordbox database",
            filetypes=[("SQLite database", "*.db"), ("All files", "*.*")],
        )
        if not path:
            return
        self._open_db(path)

    def _open_db(self, path: str) -> None:
        if self.db:
            self.db.close()
            self.db = None

        self._db_var.set(path)
        self.log(f"Opening database: {path}")
        try:
            self.db = RekordboxDatabase(path)
        except Exception as exc:
            logger.exception("Could not open database")
            messagebox.showerror("Database error",
                                 f"Could not open database:\n{exc}\n\n"
                                 "Rekordbox 6/7 databases are encrypted.\n"
                                 "Install the required packages to decrypt automatically:\n\n"
                                 "  pip install pyrekordbox sqlcipher3-wheels")
            self._db_var.set("— error —")
            return

        self.log(f"Database loaded: {path}", "ok")
        self._load_playlists()

    # ------------------------------------------------------------------ playlist tree

    def _load_playlists(self) -> None:
        self._pl_tree.delete(*self._pl_tree.get_children())
        self._playlist_map.clear()
        if self.db is None:
            return

        try:
            playlists = self.db.get_playlists()
        except Exception as exc:
            logger.exception("Error loading playlists")
            self.log(f"Error loading playlists: {exc}", "err")
            self.log(
                "The database may be encrypted. Install pyrekordbox with "
                "SQLCipher support for Rekordbox 6/7 databases.",
                "err",
            )
            messagebox.showerror(
                "Database error",
                f"Could not read playlists:\n{exc}\n\n"
                "If this is a Rekordbox 6/7 database, it is encrypted.\n"
                "Install pyrekordbox and sqlcipher3 to open it.",
            )
            return

        self._playlists = playlists
        for pl in playlists:
            self._playlist_map[pl.id] = pl

        if not playlists:
            self.log("No playlists found in the database.", "err")
            messagebox.showinfo(
                "No playlists",
                "No playlists were found in this database.\n\n"
                "Make sure the file is a valid Rekordbox master.db.",
            )
            return

        # Build tree hierarchy — insert parents before children.
        # Playlists whose parent_id is not in the map go at the root level.
        inserted = set()
        to_insert = list(playlists)
        while to_insert:
            prev_count = len(inserted)
            remaining = []
            for pl in to_insert:
                parent_iid = ""
                if pl.parent_id and pl.parent_id in self._playlist_map:
                    if pl.parent_id in inserted:
                        parent_iid = pl.parent_id
                    else:
                        # Parent not yet inserted — defer
                        remaining.append(pl)
                        continue
                icon = "📁 " if pl.is_folder else "🎵 "
                try:
                    self._pl_tree.insert(
                        parent_iid,
                        "end",
                        iid=pl.id,
                        text=f"{icon}{pl.name}",
                        open=True,
                    )
                    inserted.add(pl.id)
                except tk.TclError as exc:
                    logger.debug("Could not insert playlist %s: %s", pl.id, exc)
            to_insert = remaining
            # If no progress was made this pass, force remaining at root level
            if len(inserted) == prev_count and to_insert:
                for pl in to_insert:
                    icon = "📁 " if pl.is_folder else "🎵 "
                    try:
                        self._pl_tree.insert(
                            "", "end", iid=pl.id,
                            text=f"{icon}{pl.name}", open=True,
                        )
                        inserted.add(pl.id)
                    except tk.TclError:
                        pass
                break

        self.log(f"Loaded {len(inserted)} playlist(s).")

    def _on_playlist_select(self, _event=None) -> None:
        sel = self._pl_tree.selection()
        if not sel:
            return
        self._active_playlist_id = sel[0]
        pl = self._playlist_map.get(self._active_playlist_id)
        if pl and pl.is_folder:
            self._progress_var.set(f"📁 {pl.name} (folder – select a playlist inside)")
            return  # don't load track list for folders
        self._load_track_list()
        self._dl_all_btn.config(state="normal")
        self._add_track_btn.config(state="normal")

    # ------------------------------------------------------------------ track list

    def _load_track_list(self) -> None:
        self._track_tree.delete(*self._track_tree.get_children())
        if self.db is None or self._active_playlist_id is None:
            return

        try:
            tracks = self.db.get_tracks_in_playlist(self._active_playlist_id)
        except Exception as exc:
            logger.exception("Error loading tracks")
            self.log(f"Error loading tracks: {exc}", "err")
            return

        self._tracks = tracks
        for t in tracks:
            status = "✓" if self._is_flac(t) else ""
            self._track_tree.insert("", "end", iid=t.id,
                                    values=(status, t.title, t.artist,
                                            t.album, self._fmt_label(t),
                                            t.folder_path))
        self._progress_var.set(f"{len(tracks)} track(s) in playlist")

    def refresh_track_list(self) -> None:
        self._load_track_list()

    # ------------------------------------------------------------------ download

    def _browse_dldir(self) -> None:
        path = filedialog.askdirectory(title="Select download directory")
        if path:
            self.download_dir = path
            self._dldir_var.set(path)

    def _download_all(self) -> None:
        if self.db is None or self._active_playlist_id is None:
            messagebox.showinfo("No playlist selected",
                                "Please select a playlist first.")
            return
        if not self._tracks:
            messagebox.showinfo("No tracks", "No tracks in the selected playlist.")
            return

        # Filter to non-FLAC tracks only
        to_replace = [t for t in self._tracks if not self._is_flac(t)]
        if not to_replace:
            messagebox.showinfo("All done",
                                "All tracks in this playlist are already FLAC!")
            return

        # Confirm before batch download
        if not messagebox.askyesno(
            "Confirm download",
            f"Replace {len(to_replace)} non-FLAC track(s) with FLAC versions?\n\n"
            f"Download directory: {self.download_dir}",
        ):
            return

        self._dl_all_btn.config(state="disabled")
        self._add_track_btn.config(state="disabled")
        self.log(f"Starting batch download for {len(to_replace)} track(s) …")
        self._progress_var.set(f"Preparing to download {len(to_replace)} track(s)…")
        threading.Thread(
            target=self._batch_download_worker, args=(to_replace,), daemon=True
        ).start()

    def _batch_download_worker(self, tracks: List[ContentTrack]) -> None:
        ok_count = 0
        fail_count = 0
        total = len(tracks)

        for idx, track in enumerate(tracks, start=1):
            self.after(0, self._progress_var.set,
                       f"Downloading {idx}/{total}: {track.title} …")
            query = f"{track.artist} {track.title}"
            self.log(f"[{idx}/{total}] Searching: {query}")

            results = search(query, limit=5)
            if not results:
                self.log(f"  ✗ Not found: {track.artist} – {track.title}", "err")
                fail_count += 1
                continue

            # Pick the best match (first result, or artist match if possible).
            best = self._pick_best_match(results, track)
            if best is None:
                self.log(f"  ✗ No suitable match: {track.artist} – {track.title}", "err")
                fail_count += 1
                continue

            def _progress(done: int, total_bytes: int, t=best, i=idx, n=total):
                if total_bytes:
                    pct = done * 100 // total_bytes
                    self.after(0, self._progress_var.set,
                               f"[{i}/{n}] {t.title}: {pct}%")

            result_path = download_track(
                best,
                output_dir=self.download_dir,
                quality=QUALITY_FLAC_16,
                progress_callback=_progress,
            )

            if result_path is None:
                self.log(f"  ✗ Download failed: {track.artist} – {track.title}", "err")
                fail_count += 1
                continue

            # Update the database
            try:
                self.db.update_track_path(track.id, result_path)
                self.log(
                    f"  ✓ Replaced: {track.artist} – {track.title} → FLAC", "ok"
                )
                ok_count += 1
            except Exception as exc:
                self.log(f"  ✗ DB update failed ({track.id}): {exc}", "err")
                fail_count += 1

        summary = (
            f"Batch complete: {ok_count} replaced, {fail_count} failed "
            f"(out of {total} tracks)."
        )
        self.log(summary, "ok" if fail_count == 0 else "info")
        self.after(0, self._progress_var.set, summary)
        self.after(0, self.refresh_track_list)
        self.after(0, self._dl_all_btn.config, {"state": "normal"})
        self.after(0, self._add_track_btn.config, {"state": "normal"})

    @staticmethod
    def _pick_best_match(results: List[Track], target: ContentTrack) -> Optional[Track]:
        """Return the result whose artist/title best matches *target*."""
        target_artist = target.artist.lower().strip()
        target_title = target.title.lower().strip()

        # Exact artist match first
        for r in results:
            if r.artist.lower().strip() == target_artist:
                return r

        # Partial title match
        for r in results:
            if target_title in r.title.lower() or r.title.lower() in target_title:
                return r

        # Fall back to first result
        return results[0] if results else None

    # ------------------------------------------------------------------ add track

    def _open_search(self) -> None:
        if self.db is None or self._active_playlist_id is None:
            messagebox.showinfo("No playlist selected",
                                "Please select a playlist first.")
            return
        pl = self._playlist_map.get(self._active_playlist_id)
        if pl is None:
            return
        SearchDialog(self, self._active_playlist_id, pl.name)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
