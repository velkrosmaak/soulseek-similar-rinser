#!/usr/bin/env python3
"""
soulseek-similar-rinser/spotify-liked-rinser.py
Fetch all Spotify Liked Tracks (Saved Tracks) via Spotify API, store in spotify_liked.db,
and download missing/failed tracks via local sockseek CLI.
"""

import argparse
import json
import logging
import os
import re
import sys
import select
import time
import queue
import threading
import requests
import sqlite3
import subprocess
import signal
from dataclasses import dataclass, field

try:
    import mutagen
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

try:
    from textual.app import App, ComposeResult
    from textual.widgets import Static, RichLog, ProgressBar, Footer
    from textual.containers import Vertical, Horizontal
    from textual.reactive import reactive
except ImportError:
    print("❌  Textual not installed. Run:  pip install textual")
    sys.exit(1)

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
    HAS_SPOTIPY = True
except ImportError:
    HAS_SPOTIPY = False

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich import box

try:
    import pushover_config
except ImportError:
    pushover_config = None

try:
    import config
    FLARESOLVERR_URL = getattr(config, "FLARESOLVERR_URL", "")
    SPOTIFY_CLIENT_ID = getattr(config, "SPOTIFY_CLIENT_ID", "")
    SPOTIFY_CLIENT_SECRET = getattr(config, "SPOTIFY_CLIENT_SECRET", "")
    SPOTIFY_REDIRECT_URI = getattr(config, "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:9090")
except ImportError:
    FLARESOLVERR_URL = ""
    SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
    SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    SPOTIFY_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:9090")

console = Console()

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
DB_PATH        = os.path.join(SCRIPT_DIR, "spotify_liked.db")
LOG_PATH       = os.path.join(SCRIPT_DIR, "spotify-liked.log")
CACHE_PATH     = os.path.join(SCRIPT_DIR, ".cache-spotify-liked")
DEFAULT_DEST   = "/media/quark/dj/spotify liked"
QUEUED_TIMEOUT = 60   # Seconds to wait if remotely queued before giving up
STALL_TIMEOUT  = 60   # Seconds of dead air before assuming stuck


# ─────────────────────────────────────────────
#  Real-time file logging
# ─────────────────────────────────────────────

def _setup_file_logger() -> logging.Logger:
    """Set up a logger that writes timestamped events to spotify-liked.log."""
    logger = logging.getLogger("spotify_liked")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [Liked Tracks] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)

    return logger


file_logger = _setup_file_logger()


def _plain(message: str) -> str:
    """Strip Rich markup tags down to plain text for writing into the log file."""
    try:
        return Text.from_markup(message).plain.strip()
    except Exception:
        return message.strip()


def log_event(message: str) -> None:
    """Write a plain-text, timestamped event straight to the log file."""
    plain = _plain(message)
    if plain:
        file_logger.info(plain)


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def format_size(bytes_qty: float) -> str:
    if bytes_qty <= 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while bytes_qty >= 1000.0 and i < len(units) - 1:
        bytes_qty /= 1000.0
        i += 1
    return f"{int(bytes_qty)} B" if i == 0 else f"{bytes_qty:.1f} {units[i]}"


def elapsed_str(start: float) -> str:
    if start <= 0:
        return "—"
    e = int(time.time() - start)
    m, s = divmod(e, 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


# ─────────────────────────────────────────────
#  Shared State (worker → TUI)
# ─────────────────────────────────────────────

@dataclass
class TrackState:
    """Thread-safe shared state between the download worker and the Textual UI."""
    genre: str = "Liked Tracks"
    total_tracks: int = 0
    dev_mode: bool = False

    track_num: int = 0
    artist: str = ""
    title: str = ""
    remix: str = ""

    # idle | searching | downloading | converting | done | failed | skipped | owned
    status: str = "idle"

    progress_bytes: int = 0
    total_bytes: int = 0
    current_file_size: int = 0
    remote_user: str = ""

    track_start_time: float = 0.0

    last_rx_time: float = 0.0
    last_tx_time: float = 0.0

    downloaded: int = 0
    failed: int = 0
    skipped: int = 0
    already_owned: int = 0

    skip_requested: bool = False
    quit_requested: bool = False
    done: bool = False

    _log_queue: queue.Queue = field(default_factory=queue.Queue)
    _lock: threading.Lock   = field(default_factory=threading.Lock)

    def log(self, message: str) -> None:
        self._log_queue.put(message)
        log_event(message)

    def update_fields(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)


# ─────────────────────────────────────────────
#  Textual Application
# ─────────────────────────────────────────────

APP_CSS = """
Screen {
    background: #07070f;
}

#header {
    dock: top;
    height: 3;
    background: #10102a;
    border-bottom: solid #1db954;
    padding: 0 2;
    content-align: left middle;
    color: #1db954;
    text-style: bold;
}

#track-panel {
    height: 9;
    margin: 1 1 0 1;
    padding: 1 2;
    border: round #1db954;
    background: #0c0c20;
}

#dl-bar {
    height: 1;
    margin: 0 3;
}

#overall-bar {
    height: 1;
    margin: 1 3 0 3;
}

#overall-label {
    height: 1;
    margin: 0 3;
    color: #6b7280;
    text-style: italic;
}

#history-header {
    height: 1;
    margin: 1 2 0 2;
    color: #1db954;
    text-style: bold;
}

#history {
    height: 1fr;
    margin: 0 1 1 1;
    border: round #1e1b4b;
    background: #050508;
    padding: 0 1;
}

Footer {
    background: #10102a;
    color: #1db954;
}

ProgressBar > .bar--bar {
    color: #1db954;
}
ProgressBar > .bar--complete {
    color: #10b981;
}
ProgressBar > .bar--indeterminate {
    color: #f59e0b;
}
"""


class SpotifyLikedApp(App):
    CSS = APP_CSS
    TITLE = "Spotify Liked Tracks Rinser"
    BINDINGS = [
        ("s", "skip_track", "Skip Track"),
        ("q", "quit_app",   "Quit"),
    ]

    def __init__(self, state: TrackState, **kwargs):
        super().__init__(**kwargs)
        self.state = state

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        yield Static("", id="track-panel")
        yield ProgressBar(id="dl-bar",      total=100, show_eta=False, show_percentage=False)
        yield Static("", id="overall-label")
        yield ProgressBar(id="overall-bar", total=100, show_eta=False, show_percentage=True)
        yield Static("📋  History", id="history-header")
        yield RichLog(id="history", highlight=False, markup=True, wrap=False, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.1, self._refresh_ui)

    def _refresh_ui(self) -> None:
        s = self.state

        # Drain log queue → RichLog
        history = self.query_one("#history", RichLog)
        for _ in range(30):
            try:
                history.write(s._log_queue.get_nowait())
            except queue.Empty:
                break

        self.query_one("#header",        Static).update(self._render_header())
        self.query_one("#track-panel",   Static).update(self._render_track_panel())

        dl_bar = self.query_one("#dl-bar", ProgressBar)
        if s.total_bytes > 0:
            dl_bar.update(total=s.total_bytes, progress=s.progress_bytes)
        else:
            dl_bar.update(total=100, progress=0)

        overall_bar = self.query_one("#overall-bar", ProgressBar)
        if s.total_tracks > 0:
            done = s.downloaded + s.failed + s.skipped + s.already_owned
            overall_bar.update(total=s.total_tracks, progress=done)
            self.query_one("#overall-label", Static).update(
                f"[dim]Overall: {done}/{s.total_tracks}"
                f"  ·  ✅ {s.downloaded}  ❌ {s.failed}  ⏩ {s.skipped}  💾 {s.already_owned}[/dim]"
            )

        if s.done and s._log_queue.empty():
            self.exit()

    def _render_header(self) -> str:
        s = self.state
        dev = "  [bold red]⚠ DEV MODE[/bold red]" if s.dev_mode else ""
        return (
            f"💚  [bold]Spotify Liked Tracks Rinser[/bold]"
            f"   ·   [dim]Track [bold white]{s.track_num}[/bold white]/{s.total_tracks}[/dim]"
            f"{dev}"
        )

    def _render_track_panel(self) -> str:
        s = self.state

        STATUS_ICONS = {
            "idle":        "⏳  Waiting",
            "searching":   "🔍  Searching...",
            "downloading": "🚀  Downloading",
            "converting":  "🔄  Converting to MP3",
            "done":        "✅  Complete",
            "failed":      "❌  Failed",
            "skipped":     "⏩  Skipped",
            "owned":       "💾  Already in Library",
        }
        status_text = STATUS_ICONS.get(s.status, s.status)

        artist_str = f"[bold white]{s.artist}[/bold white]" if s.artist else "[dim]—[/dim]"
        title_str  = f"[italic]{s.title}[/italic]"         if s.title  else "[dim]—[/dim]"
        if s.remix and "original" not in s.remix.lower():
            title_str += f" [dim]({s.remix})[/dim]"

        if s.total_bytes > 0:
            size_str = (
                f"[cyan]{format_size(s.progress_bytes)}[/cyan]"
                f" [dim]/[/dim] "
                f"[cyan]{format_size(s.total_bytes)}[/cyan]"
            )
        elif s.current_file_size > 0:
            size_str = f"[cyan]{format_size(s.current_file_size)}[/cyan] [dim](on disk)[/dim]"
        else:
            size_str = "[dim]—[/dim]"

        t  = time.time()
        rx = "🔵" if t - s.last_rx_time < 0.2 else "⚫"
        tx = "🟢" if t - s.last_tx_time < 0.5 else "⚫"
        if s.status == "searching":
            scan = "🟡" if int(t * 3) % 2 == 0 else "⚫"
            link = f"[Scan: {scan}]"
        else:
            link = f"[Link: {tx}{rx}]"

        user_str = f"[dim cyan]{s.remote_user}[/dim cyan]" if s.remote_user else "[dim]—[/dim]"
        el_str   = elapsed_str(s.track_start_time)

        lines = [
            f"  🎧  {artist_str}",
            f"  🎵  {title_str}",
            f"  📡  {status_text}   ·   {link}   ·   👤 {user_str}",
            f"  💾  {size_str}   ·   ⏱️  [bright_magenta]{el_str}[/bright_magenta]",
        ]

        if s.total_bytes > 0:
            pct    = min(s.progress_bytes / s.total_bytes, 1.0)
            bar_w  = 32
            filled = int(bar_w * pct)
            bar    = "█" * filled + "░" * (bar_w - filled)
            lines.append(
                f"  ⬇️   [bold green]{bar}[/bold green]"
                f" [bright_green]{pct * 100:.0f}%[/bright_green]"
            )

        return "\n".join(lines)

    def action_skip_track(self) -> None:
        self.state.skip_requested = True
        self.state.log("[bold yellow]⏩  Skip requested…[/bold yellow]")

    def action_quit_app(self) -> None:
        self.state.quit_requested = True
        self.exit()


# ─────────────────────────────────────────────
#  Database Management (spotify_liked.db)
# ─────────────────────────────────────────────

def init_liked_db():
    """Initialize SQLite database spotify_liked.db for tracking liked track downloads."""
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS liked_tracks (
            spotify_id          TEXT PRIMARY KEY,
            artist              TEXT NOT NULL,
            title               TEXT NOT NULL,
            remix               TEXT,
            album               TEXT,
            added_at            TEXT,
            download_status     TEXT DEFAULT 'pending',
            download_timestamp  DATETIME,
            username            TEXT,
            file_path           TEXT,
            error_message       TEXT
        )
    ''')
    conn.commit()
    conn.close()


def sync_liked_tracks_to_db(tracks: list[dict]) -> tuple[int, int]:
    """
    Insert new liked tracks into spotify_liked.db.
    Returns (total_tracks_count, new_tracks_added_count).
    """
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    new_added = 0
    for t in tracks:
        sid      = t["spotify_id"]
        artist   = t["artist"]
        title    = t["title"]
        remix    = t["remix"]
        album    = t.get("album", "")
        added_at = t.get("added_at", "")

        cursor.execute("SELECT download_status FROM liked_tracks WHERE spotify_id=?", (sid,))
        row = cursor.fetchone()

        if row is None:
            cursor.execute('''
                INSERT INTO liked_tracks (spotify_id, artist, title, remix, album, added_at, download_status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
            ''', (sid, artist, title, remix, album, added_at))
            new_added += 1
        else:
            # Update metadata in case title/artist changed, preserve download_status
            cursor.execute('''
                UPDATE liked_tracks
                SET artist=?, title=?, remix=?, album=?, added_at=?
                WHERE spotify_id=?
            ''', (artist, title, remix, album, added_at, sid))

    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM liked_tracks")
    total_count = cursor.fetchone()[0]

    conn.close()
    return total_count, new_added


def get_tracks_for_download(retry_failed: bool = False) -> list[dict]:
    """Retrieve tracks that have status 'pending' (or also 'failed' if retry_failed=True)."""
    conn   = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if retry_failed:
        cursor.execute("SELECT * FROM liked_tracks WHERE download_status IN ('pending', 'failed') ORDER BY added_at DESC")
    else:
        cursor.execute("SELECT * FROM liked_tracks WHERE download_status = 'pending' ORDER BY added_at DESC")

    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_track_status(spotify_id: str, status: str, username: str = None, file_path: str = None, error: str = None):
    """Update the download status and metadata for a specific track in spotify_liked.db."""
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now    = time.strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute('''
        UPDATE liked_tracks
        SET download_status=?, download_timestamp=?, username=?, file_path=?, error_message=?
        WHERE spotify_id=?
    ''', (status, now, username, file_path, error, spotify_id))

    conn.commit()
    conn.close()


def get_liked_db_stats() -> dict[str, int]:
    """Return dictionary with counts of tracks by download_status."""
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "pending": 0}
    cursor.execute("SELECT download_status, COUNT(*) FROM liked_tracks GROUP BY download_status")
    for status, count in cursor.fetchall():
        if status in stats:
            stats[status] = count

    cursor.execute("SELECT COUNT(*) FROM liked_tracks")
    stats["total"] = cursor.fetchone()[0]

    conn.close()
    return stats


# ─────────────────────────────────────────────
#  Spotify API Retrieval
# ─────────────────────────────────────────────

def parse_spotify_title(spotify_title: str) -> tuple[str, str]:
    """Extract clean title and remix info from a Spotify track title."""
    keywords = r"mix|remix|edit|rework|version|dub|mashup|remaster|cut|reconstruction"
    
    current_title = spotify_title.strip()
    remixes = []
    
    while True:
        # Check parentheses at the end
        parentheses_regex = r"\s*\(([^)]*(?:" + keywords + r")[^)]*)\)\s*$"
        m = re.search(parentheses_regex, current_title, re.IGNORECASE)
        if m:
            remixes.insert(0, f"({m.group(1).strip()})")
            current_title = re.sub(parentheses_regex, "", current_title, flags=re.IGNORECASE).strip()
            continue
            
        # Check brackets at the end
        brackets_regex = r"\s*\[([^\]]*(?:" + keywords + r")[^\]]*)\]\s*$"
        m = re.search(brackets_regex, current_title, re.IGNORECASE)
        if m:
            remixes.insert(0, f"[{m.group(1).strip()}]")
            current_title = re.sub(brackets_regex, "", current_title, flags=re.IGNORECASE).strip()
            continue

        # Check dash at the end
        dash_regex = r"\s+-\s+([^-(]*(?:" + keywords + r")[^-(]*)$"
        m = re.search(dash_regex, current_title, re.IGNORECASE)
        if m:
            remixes.insert(0, m.group(1).strip())
            current_title = re.sub(dash_regex, "", current_title, flags=re.IGNORECASE).strip()
            continue
            
        break

    if remixes:
        remix_str = ""
        for r in remixes:
            if remix_str:
                if r.startswith('(') or r.startswith('['):
                    remix_str += " " + r
                else:
                    remix_str += " - " + r
            else:
                remix_str = r
                
        if remix_str.startswith('(') and remix_str.endswith(')'):
            remix_str = remix_str[1:-1].strip()
        elif remix_str.startswith('[') and remix_str.endswith(']'):
            remix_str = remix_str[1:-1].strip()
            
        return current_title, remix_str
    else:
        return current_title, "Original Mix"


def fetch_spotify_liked_tracks(dev_mode: bool = False) -> list[dict]:
    """Fetch all saved (liked) tracks for the authenticated Spotify user using spotipy."""
    if not HAS_SPOTIPY:
        console.print("[bold red]❌  spotipy is not installed. Install with: pip install spotipy[/]")
        log_event("[bold red]❌  spotipy is not installed. Aborting run.[/bold red]")
        return []

    client_id     = SPOTIFY_CLIENT_ID
    client_secret = SPOTIFY_CLIENT_SECRET
    redirect_uri  = SPOTIFY_REDIRECT_URI

    if not client_id or not client_secret:
        console.print(
            "[bold red]❌ Spotify API credentials missing.[/]\n"
            "[yellow]Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in config.py or environment variables.[/]"
        )
        log_event("[bold red]❌  Spotify API credentials missing in config.py / environment.[/bold red]")
        return []

    try:
        auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope="user-library-read",
            cache_path=CACHE_PATH,
            open_browser=False,
        )
        sp = spotipy.Spotify(auth_manager=auth_manager)

        console.print("[bold magenta]🎵  Fetching Spotify Liked Tracks via API…[/]")
        log_event("[cyan]🎵  Fetching Spotify Liked Tracks via API…[/cyan]")

        tracks = []
        limit  = 50
        offset = 0

        while True:
            results = sp.current_user_saved_tracks(limit=limit, offset=offset)
            items   = results.get("items", [])

            if not items:
                break

            for item in items:
                track = item.get("track")
                if not track:
                    continue

                sid          = track.get("id") or track.get("uri", "").split(":")[-1]
                raw_artists  = track.get("artists", [])
                artist_names = ", ".join(a.get("name", "") for a in raw_artists if a.get("name"))
                spotify_name = track.get("name", "")
                album_name   = track.get("album", {}).get("name", "")
                added_at     = item.get("added_at", "")

                clean_title, remix = parse_spotify_title(spotify_name)

                tracks.append({
                    "spotify_id": sid,
                    "artist":     artist_names,
                    "title":      clean_title,
                    "remix":      remix,
                    "album":      album_name,
                    "added_at":   added_at,
                })

                if dev_mode and len(tracks) >= 5:
                    break

            if dev_mode and len(tracks) >= 5:
                break

            if not results.get("next"):
                break

            offset += limit

        console.print(f"[bold green]✅  Fetched {len(tracks)} liked tracks from Spotify API.[/]")
        log_event(f"[bold green]✅  Fetched {len(tracks)} liked tracks from Spotify API.[/bold green]")
        return tracks

    except Exception as e:
        console.print(f"[bold red]❌ Spotify API error: {e}[/]")
        log_event(f"[bold red]❌  Spotify API error: {e}[/bold red]")
        return []


# ─────────────────────────────────────────────
#  Audio Helpers
# ─────────────────────────────────────────────

def convert_to_mp3(file_path: str, state: TrackState = None) -> str:
    """Convert a file to 320 kbps MP3 using ffmpeg if it is not already an MP3."""
    if not file_path or not os.path.exists(file_path):
        return file_path

    base, ext = os.path.splitext(file_path)
    if ext.lower() == '.mp3':
        return file_path

    new_file = base + ".mp3"

    msg = f"[bold magenta]🔄  Converting {os.path.basename(file_path)} → MP3 320 kbps…[/bold magenta]"
    if state:
        state.log(msg)
    else:
        console.log(msg)

    try:
        cmd = ["ffmpeg", "-y", "-i", file_path, "-codec:a", "libmp3lame", "-b:a", "320k", new_file]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        os.remove(file_path)
        ok_msg = f"[bold green]✨  Conversion complete: {os.path.basename(new_file)}[/bold green]"
        if state:
            state.log(ok_msg)
        return new_file
    except Exception as e:
        err_msg = f"[bold red]❌  Conversion failed: {e}[/bold red]"
        if state:
            state.log(err_msg)
        else:
            console.log(err_msg)
        return file_path


def update_album_tag(file_path: str, album_name: str = "Spotify Liked Tracks", state: TrackState = None):
    """Update album, year, and artist tags for playlist compatibility."""
    if not HAS_MUTAGEN or not os.path.exists(file_path):
        return

    try:
        from mutagen import File as MutagenFile

        current_year = str(time.localtime().tm_year)

        if file_path.lower().endswith(".mp3"):
            from mutagen.easyid3 import EasyID3

            try:
                audio = EasyID3(file_path)
                audio["album"] = album_name
                audio["date"] = current_year

                artist = audio.get("artist", [])
                albumartist = audio.get("albumartist", [])

                if (not artist or not any(a.strip() for a in artist)) and albumartist:
                    audio["artist"] = albumartist

                audio["albumartist"] = ["Spotify Liked"]
                audio.save()

            except Exception:
                from mutagen.id3 import ID3, TALB, TDRC, TPE1, TPE2

                tags = ID3(file_path)
                tags.add(TALB(encoding=3, text=album_name))
                tags.add(TDRC(encoding=3, text=current_year))

                artist_frame = tags.get("TPE1")
                albumartist_frame = tags.get("TPE2")

                artist_text = artist_frame.text if artist_frame else []
                albumartist_text = albumartist_frame.text if albumartist_frame else []

                if (not artist_text or not any(t.strip() for t in artist_text)) and albumartist_text:
                    tags.setall("TPE1", [TPE1(encoding=3, text=albumartist_text)])

                tags.setall("TPE2", [TPE2(encoding=3, text="Spotify Liked")])
                tags.save()

        else:
            audio = MutagenFile(file_path)
            if audio is None:
                return

            audio["album"] = album_name
            audio["date"] = current_year

            artist = audio.get("artist", [])
            albumartist = audio.get("albumartist", [])

            if (not artist or not any(str(a).strip() for a in artist)) and albumartist:
                audio["artist"] = albumartist

            audio["albumartist"] = ["Spotify Liked"]
            audio.save()

        if state:
            state.log(
                f"[dim]  🏷️   Tagged: album='{album_name}'  year={current_year}  albumartist='Spotify Liked'[/dim]"
            )

    except Exception as e:
        if state:
            state.log(f"[bold red]⚠️   Tagging failed: {e}[/bold red]")


# ─────────────────────────────────────────────
#  Pushover
# ─────────────────────────────────────────────

def send_pushover_notification(title: str, message: str):
    """Send a notification via Pushover."""
    if (not pushover_config
            or not getattr(pushover_config, "PUSHOVER_API_TOKEN", None)
            or not getattr(pushover_config, "PUSHOVER_USER_KEY", None)):
        console.log("[bold yellow]⚠️ Pushover skipped: credentials missing.[/]")
        return
    try:
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":   pushover_config.PUSHOVER_API_TOKEN,
                "user":    pushover_config.PUSHOVER_USER_KEY,
                "title":   title,
                "message": message,
            },
            timeout=10,
        )
        if response.status_code == 200:
            console.log("[bold green]✅ Pushover notification sent.[/]")
        else:
            console.log(f"[bold red]❌ Pushover error ({response.status_code}): {response.text}[/]")
    except Exception as e:
        console.log(f"[bold red]❌ Pushover failed: {e}[/]")


# ─────────────────────────────────────────────
#  Download Engine
# ─────────────────────────────────────────────

def parse_size_to_bytes(value: str, unit: str) -> int:
    """Convert size strings like '10.5' + 'MB' to bytes."""
    units = {"kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "b": 1}
    return int(float(value) * units.get(unit.lower(), 1))


def get_active_download_file_info(dest_path: str, downloaded_file_path: str | None) -> dict[str, int]:
    """Find in-progress audio files in dest_path and return path→size."""
    AUDIO_EXTS     = {'.mp3', '.flac', '.m4a', '.mp4', '.ogg', '.opus', '.wav'}
    files_to_check = []

    if downloaded_file_path:
        files_to_check.append(downloaded_file_path)
        files_to_check.append(f"{downloaded_file_path}.incomplete")

    if os.path.isdir(dest_path):
        try:
            for root, _, files in os.walk(dest_path):
                for f in files:
                    if f.endswith('.incomplete') or os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                        files_to_check.append(os.path.join(root, f))
        except Exception:
            pass

    seen, unique_files = set(), []
    for f in files_to_check:
        abs_p = os.path.abspath(f)
        if abs_p not in seen:
            seen.add(abs_p)
            unique_files.append(abs_p)

    active = {}
    for f in unique_files:
        if os.path.exists(f):
            try:
                active[f] = os.path.getsize(f)
            except Exception:
                pass
    return active


def run_sockseek(
    artist: str,
    title: str,
    remix: str,
    dest_path: str,
    state: TrackState,
    track_start_time: float | None = None,
) -> tuple[bool, str | None, str | None]:
    """Run the local sockseek command and monitor progress, feeding updates into TrackState."""
    query = f"{artist} {title}"
    if remix and "original" not in remix.lower():
        query += f" {remix}"
    query = re.sub(r'[\W_]+', ' ', query).strip()

    os.makedirs(dest_path, exist_ok=True)
    cmd = [
        "./sockseek", query,
        "-p", dest_path,
        "--user", "velkrosmaak3",
        "--pass", "1Ndustry",
    ]

    state.update_fields(
        status="searching",
        progress_bytes=0,
        total_bytes=0,
        current_file_size=0,
        last_rx_time=0.0,
        last_tx_time=0.0,
        remote_user="",
        track_start_time=track_start_time if track_start_time is not None else time.time(),
    )

    job_succeeded        = False
    remote_user          = None
    downloaded_file_path = None

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            preexec_fn=os.setsid,
        )

        queued_start_time = None
        last_activity     = time.time()
        buffer            = ""

        AUDIO_EXTS      = {'.mp3', '.flac', '.m4a', '.mp4', '.ogg', '.opus', '.wav'}
        last_file_sizes = {}
        if os.path.isdir(dest_path):
            try:
                for root, _, files in os.walk(dest_path):
                    for f in files:
                        if f.endswith('.incomplete') or os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                            full_p = os.path.abspath(os.path.join(root, f))
                            last_file_sizes[full_p] = os.path.getsize(full_p)
            except Exception:
                pass
        initial_file_set = set(last_file_sizes.keys())
        last_disk_check  = time.time()

        while True:
            if state.skip_requested or state.quit_requested:
                state.log(f"[bold yellow]⏩  Killing: {artist} — {title}[/bold yellow]")
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except Exception:
                    pass
                return False, remote_user, None

            current_time = time.time()
            if current_time - last_disk_check >= 2.0:
                last_disk_check = current_time
                active_files    = get_active_download_file_info(dest_path, downloaded_file_path)

                size_increased = False
                for path, current_size in active_files.items():
                    prev_size = last_file_sizes.get(path)
                    if prev_size is None:
                        if current_size > 0:
                            size_increased = True
                    elif current_size > prev_size:
                        size_increased = True
                    last_file_sizes[path] = current_size

                new_files = {
                    p: sz for p, sz in active_files.items()
                    if p not in initial_file_set or p.endswith('.incomplete')
                }
                if new_files:
                    state.update_fields(current_file_size=max(new_files.values()))

                if size_increased:
                    last_activity = current_time
                    state.update_fields(last_tx_time=current_time)

            rlist, _, _ = select.select([process.stdout.fileno()], [], [], 0.05)

            if process.stdout.fileno() in rlist:
                char = process.stdout.read(1)
                if not char:
                    break

                last_activity = time.time()
                state.update_fields(last_rx_time=last_activity)

                if char in ['\n', '\r']:
                    clean_line = buffer.strip()
                    if clean_line:
                        state.log(f"[grey37]  ↳  {clean_line}[/grey37]")
                        lower_line = clean_line.lower()

                        size_match = re.search(
                            r"(\d+(?:\.\d+)?)\s*([KMG]?B)\s*/\s*(\d+(?:\.\d+)?)\s*([KMG]?B)",
                            clean_line, re.IGNORECASE,
                        )
                        if size_match:
                            cur_val, cur_unit, tot_val, tot_unit = size_match.groups()
                            state.update_fields(
                                status="downloading",
                                progress_bytes=parse_size_to_bytes(cur_val, cur_unit),
                                total_bytes=parse_size_to_bytes(tot_val, tot_unit),
                            )
                        else:
                            m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", clean_line)
                            if m_pct:
                                state.update_fields(status="downloading")

                        if "songjob: succeeded" in lower_line:
                            job_succeeded = True

                        if "songjob: download error:" in lower_line:
                            state.log(f"[bold red]❌  Sockseek failure: {clean_line}[/bold red]")
                            try:
                                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                            except Exception:
                                pass

                        possible_path = None
                        if "songjob:" in lower_line:
                            m = re.search(r"SongJob:.*?:.*?: (.*)", clean_line)
                            if m and m.group(1).strip():
                                potential = m.group(1).strip()
                                if "\\" in potential or "/" in potential:
                                    possible_path = potential
                        elif re.search(r"^[a-zA-Z0-9].*[\\\/].*\.[a-zA-Z0-9]+$", clean_line):
                            possible_path = clean_line

                        if possible_path:
                            rel_path             = possible_path.replace('\\', os.sep).replace('/', os.sep)
                            downloaded_file_path = os.path.normpath(os.path.join(dest_path, rel_path))
                            if not remote_user and os.sep in rel_path:
                                remote_user = rel_path.split(os.sep)[0]
                                state.update_fields(remote_user=remote_user)

                        if "queued" in lower_line:
                            if queued_start_time is None:
                                queued_start_time = time.time()
                            if time.time() - queued_start_time > QUEUED_TIMEOUT:
                                state.log(f"[bold red]⏱️  Queued {QUEUED_TIMEOUT}s — cancelling.[/bold red]")
                                try:
                                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                                except Exception:
                                    pass
                                return False, remote_user, None
                        elif "downloading" in lower_line:
                            queued_start_time = None

                    buffer = ""
                else:
                    buffer += char

            else:
                if time.time() - last_activity > STALL_TIMEOUT:
                    state.log(f"[bold red]❌  Stall: no output for {STALL_TIMEOUT}s. Killing.[/bold red]")
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except Exception:
                        pass
                    return False, remote_user, None

                if process.poll() is not None:
                    break

        return (job_succeeded or process.returncode == 0), remote_user, downloaded_file_path

    except Exception as e:
        state.log(f"[bold red]❌  sockseek error: {e}[/bold red]")
        return False, None, None

    finally:
        if not job_succeeded:
            time.sleep(0.5)
            incomplete_paths = []
            if downloaded_file_path:
                incomplete_paths.append(f"{downloaded_file_path}.incomplete")
            if 'last_file_sizes' in locals():
                for path in last_file_sizes.keys():
                    if path.endswith('.incomplete'):
                        incomplete_paths.append(path)
            if os.path.isdir(dest_path):
                try:
                    for root, _, files in os.walk(dest_path):
                        for f in files:
                            if f.endswith('.incomplete'):
                                incomplete_paths.append(os.path.join(root, f))
                except Exception:
                    pass
            for incomplete_path in set(os.path.abspath(p) for p in incomplete_paths):
                if os.path.exists(incomplete_path):
                    try:
                        os.remove(incomplete_path)
                        state.log(f"[bold yellow]🧹  Removed partial: {os.path.basename(incomplete_path)}[/bold yellow]")
                    except Exception as ce:
                        state.log(f"[bold red]⚠️  Cleanup failed: {ce}[/bold red]")


# ─────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download Spotify Liked Tracks via local sockseek.")
    parser.add_argument("--download",      action="store_true", help="Trigger downloads of pending/failed tracks")
    parser.add_argument("--dev",           action="store_true", help="Dev mode: fetch/process top 5 tracks only")
    parser.add_argument("--retry-failed",  action="store_true", help="Include previously failed tracks when downloading")
    parser.add_argument("--sync-only",     action="store_true", help="Only sync Spotify Liked tracks to DB without downloading")
    parser.add_argument("--dest-dir",      default=DEFAULT_DEST, help=f"Destination directory for downloads (default: {DEFAULT_DEST})")
    args = parser.parse_args()

    init_liked_db()
    log_event(f"[bold cyan]▶️  Run started for Spotify Liked Tracks (dev_mode={args.dev}, download={args.download})[/bold cyan]")

    # 1. Fetch liked tracks from Spotify API
    spotify_tracks = fetch_spotify_liked_tracks(dev_mode=args.dev)

    if spotify_tracks:
        total_in_db, new_added = sync_liked_tracks_to_db(spotify_tracks)
        console.print(f"[dim]DB sync complete: {total_in_db} total tracks in DB ({new_added} newly added).[/dim]")
        log_event(f"[cyan]DB sync: {total_in_db} total in DB, {new_added} new added.[/cyan]")

    db_stats = get_liked_db_stats()
    console.print(
        f"[dim]DB Statistics — Total: {db_stats['total']} | Success: {db_stats['success']} | "
        f"Failed: {db_stats['failed']} | Pending: {db_stats['pending']} | Skipped: {db_stats['skipped']}[/dim]"
    )

    if args.sync_only:
        console.print("[bold green]✅  Sync complete (--sync-only specified). Exiting.[/]")
        return

    # 2. Get tracks that require downloading
    pending_tracks = get_tracks_for_download(retry_failed=args.retry_failed)

    if args.dev and pending_tracks:
        pending_tracks = pending_tracks[:5]

    if not pending_tracks:
        console.print("[bold green]✨ All liked tracks are already downloaded! No pending tracks to process.[/]")
        log_event("[bold green]✨ All liked tracks already downloaded. No pending tracks.[/bold green]")
        return

    # 3. Setup Shared State
    state = TrackState(
        genre="Spotify Liked",
        total_tracks=len(pending_tracks),
        dev_mode=args.dev,
        already_owned=db_stats['success'],
    )

    downloaded_sizes         = []
    newly_downloaded_artists = []

    # 4. Worker thread for downloading
    def worker():
        for i, t in enumerate(pending_tracks, 1):
            if state.quit_requested:
                break

            sid    = t['spotify_id']
            artist = t['artist']
            title  = t['title']
            remix  = t['remix']
            tag    = f"[{i:03d}]"

            state.update_fields(
                track_num=i,
                artist=artist,
                title=title,
                remix=remix,
                status="idle",
                progress_bytes=0,
                total_bytes=0,
                current_file_size=0,
                remote_user="",
                track_start_time=0.0,
                skip_requested=False,
            )

            if args.download:
                track_start = time.time()
                state.update_fields(track_start_time=track_start)

                state.log(f"[cyan]🔍  {tag} Searching: {artist} — {title}[/cyan]")

                success, r_user, f_path = run_sockseek(
                    artist, title, remix, args.dest_dir, state, track_start,
                )

                was_skipped = state.skip_requested
                state.update_fields(skip_requested=False)
                el = elapsed_str(track_start)

                if success:
                    status_str = "success"
                    state.update_fields(downloaded=state.downloaded + 1)
                    newly_downloaded_artists.append(artist)
                    state.log(
                        f"[bold green]✅  {tag} {artist} — {title}"
                        f"  [dim]({r_user or '?'}) · {el}[/dim][/bold green]"
                    )

                    final_path = f_path
                    if final_path and not os.path.exists(final_path):
                        for _ in range(6):
                            time.sleep(0.5)
                            if os.path.exists(final_path):
                                break
                        if not os.path.exists(final_path):
                            filename  = os.path.basename(final_path)
                            for root, _, files in os.walk(args.dest_dir):
                                if filename in files:
                                    final_path = os.path.join(root, filename)
                                    break

                    if final_path and os.path.exists(final_path):
                        downloaded_sizes.append(os.path.getsize(final_path))
                        state.update_fields(status="converting")
                        final_mp3 = convert_to_mp3(final_path, state)
                        album_to_tag = t.get("album") or "Spotify Liked Tracks"
                        update_album_tag(final_mp3, album_to_tag, state)
                        state.update_fields(status="done")
                        update_track_status(sid, "success", username=r_user, file_path=final_mp3)
                    elif f_path:
                        state.log(f"[bold yellow]⚠️  File missing at: {f_path}[/bold yellow]")
                        update_track_status(sid, "failed", username=r_user, error="File missing after download")
                    else:
                        update_track_status(sid, "failed", username=r_user, error="Could not determine download file path")

                elif was_skipped:
                    state.update_fields(status="skipped", skipped=state.skipped + 1)
                    state.log(f"[yellow]⏩  {tag} {artist} — {title}  [dim]Skipped · {el}[/dim][/yellow]")
                    update_track_status(sid, "skipped", username=r_user, error="User skipped")
                else:
                    state.update_fields(status="failed", failed=state.failed + 1)
                    state.log(f"[bold red]❌  {tag} {artist} — {title}  [dim]Failed · {el}[/dim][/bold red]")
                    update_track_status(sid, "failed", username=r_user, error="Download failed or timed out")

            else:
                state.log(
                    f"[yellow]🔍  {tag} {artist} — {title}  [dim](pending, --download not set)[/dim][/yellow]"
                )
                state.update_fields(status="failed", failed=state.failed + 1)

            # Brief inter-track pause (2 s), interruptible
            for _ in range(20):
                if state.skip_requested or state.quit_requested:
                    break
                time.sleep(0.1)

        state.done = True

    # 5. Launch Textual UI
    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    SpotifyLikedApp(state).run()

    worker_thread.join(timeout=5.0)

    # 6. Post-run stats & Pushover notification
    if downloaded_sizes:
        total_b = sum(downloaded_sizes)
        table   = Table(
            title="[bold green]Download Statistics[/]",
            box=box.ROUNDED,
            header_style="bold green",
        )
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")

        def to_mb(b): return f"{b / 1024 / 1024:.2f} MB"

        table.add_row("Files Downloaded",  str(len(downloaded_sizes)))
        table.add_row("Total Data Volume", to_mb(total_b))
        table.add_row("Average File Size", to_mb(total_b / len(downloaded_sizes)))
        table.add_row("Smallest File",     to_mb(min(downloaded_sizes)))
        table.add_row("Largest File",      to_mb(max(downloaded_sizes)))
        console.print("\n", table)

    unique_artists = sorted(set(newly_downloaded_artists))
    msg = (
        f"Run complete for Spotify Liked Tracks.\n"
        f"• Total Pending: {state.total_tracks} | Downloaded: {state.downloaded}\n"
        f"• Failed: {state.failed} | Skipped: {state.skipped} | Already Owned: {state.already_owned}"
    )
    if unique_artists:
        suffix = "…" if len(unique_artists) > 12 else ""
        msg += f"\n\nNew Artists: {', '.join(unique_artists[:12])}{suffix}"

    send_pushover_notification("Soulseek Rinser: Spotify Liked Tracks", msg)
    console.print(f"\n[bold green]✅  All liked tracks processed.[/]")
    log_event(
        f"[bold green]✅  Run complete. "
        f"TotalPending={state.total_tracks} Downloaded={state.downloaded} "
        f"Failed={state.failed} Skipped={state.skipped} Owned={state.already_owned}[/bold green]"
    )


if __name__ == "__main__":
    main()
