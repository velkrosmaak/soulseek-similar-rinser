#!/usr/bin/env python3
"""
soulseek-similar-rinser/beatport-local.py
Fetch Beatport Top 100 for a genre and download missing tracks via local sockseek CLI.
"""

import argparse
import json
import os
import re
import sys
import tty
import termios
import select
import time
import requests
import threading
import sqlite3
import subprocess
import signal

from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    DownloadColumn,
    TransferSpeedColumn,
    SpinnerColumn,
    TaskID,
)
from rich.panel import Panel
from rich.table import Table
from rich import box

try:
    import pushover_config
except ImportError:
    pushover_config = None

from config import PLEX_TOKEN, PLEX_URL

console = Console()

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beatport_downloads.db")
QUEUED_TIMEOUT = 60  # Seconds to wait if remotely queued before giving up
STALL_TIMEOUT = 120  # Seconds of "dead air" (no output/progress) before assuming stuck

def init_db():
    """Initialize the SQLite database for tracking downloads."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT,
            title TEXT,
            remix TEXT,
            username TEXT,
            success BOOLEAN,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Migration: Add columns if they don't exist in an older DB
    cursor.execute("PRAGMA table_info(downloads)")
    cols = [c[1] for c in cursor.fetchall()]
    if 'username' not in cols:
        cursor.execute("ALTER TABLE downloads ADD COLUMN username TEXT")
    if 'success' not in cols:
        cursor.execute("ALTER TABLE downloads ADD COLUMN success BOOLEAN DEFAULT 1")
    conn.commit()
    conn.close()

def track_exists(artist: str, title: str, remix: str) -> bool:
    """Check if a track has already been successfully downloaded."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM downloads WHERE artist = ? AND title = ? AND remix = ? AND success = 1', (artist, title, remix))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def add_to_db(artist: str, title: str, remix: str, username: str = None, success: bool = True):
    """Log a download attempt (success or failure) to the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO downloads (artist, title, remix, username, success) VALUES (?, ?, ?, ?, ?)', 
                   (artist, title, remix, username, int(success)))
    conn.commit()
    conn.close()

def get_db_stats() -> str:
    """Get formatted statistics of logged downloads."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM downloads WHERE success = 1')
    s_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM downloads WHERE success = 0')
    f_count = cursor.fetchone()[0]
    conn.close()
    return f"[bold green]{s_count}[/] successful, [bold red]{f_count}[/] failed"

# Global list to track downloaded file sizes for stats
DOWNLOADED_SIZES = []
DOWNLOADED_ARTISTS = []

GENRE_MAP = {
    "dnb": ("drum-bass", 1),
    "electronica": ("electronica", 3),
    "house": ("house", 5),
    "techno": ("techno-peak-time-driving", 6),
    "trance": ("trance", 7),
    "hard-dance": ("hard-dance-hardcore-neo-rave", 8),
    "breaks": ("breaks-breakbeat-uk-bass", 9),
    "tech-house": ("tech-house", 11),
    "deep-house": ("deep-house", 12),
    "psy-trance": ("psy-trance", 13),
    "minimal": ("minimal-deep-tech", 14),
    "progressive": ("progressive-house", 15),
    "dubstep": ("dubstep", 18),
    "indie-dance": ("indie-dance", 37),
    "trap": ("trap-future-bass", 38),
    "dance-pop": ("dance-pop", 39),
    "nu-disco": ("nu-disco-disco", 50),
    "ukg": ("uk-garage-bassline", 86),
    "afro-house": ("afro-house", 89),
    "melodic": ("melodic-house-techno", 90),
    "bass-house": ("bass-house", 91),
    "techno-raw": ("techno-raw-deep-hypnotic", 92),
    "mainstage": ("mainstage", 96),
}

def get_beatport_top_100(genre_key: str) -> list[dict]:
    """Scrape Beatport Top 100 tracks for a genre."""
    genre_name, genre_id = GENRE_MAP.get(genre_key.lower(), (genre_key, None))
    if not genre_id:
        console.print(f"[bold red]❌ Unknown genre key.[/]")
        return []

    url = f"https://www.beatport.com/genre/{genre_name}/{genre_id}/top-100"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', response.text)
        if not match: return []

        data = json.loads(match.group(1))
        queries = data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
        
        tracks = []
        for q in queries:
            results = q.get("state", {}).get("data", {}).get("results", [])
            if results:
                for t in results:
                    artists = ", ".join([a["name"] for a in t.get("artists", [])])
                    tracks.append({"artist": artists, "title": t.get("name"), "remix": t.get("mix_name", "Original Mix")})
                break
        return tracks
    except Exception as e:
        console.print(f"[bold red]❌ Failed to scrape Beatport: {e}[/]")
        return []

def check_plex_for_track(artist: str, track: str) -> bool:
    """Check if the track exists on Plex."""
    if not PLEX_TOKEN or not PLEX_URL: return False
    url = f"{PLEX_URL.rstrip('/')}/search"
    params = {"type": 10, "query": track, "X-Plex-Token": PLEX_TOKEN}
    try:
        response = requests.get(url, params=params, headers={"Accept": "application/json"}, timeout=5)
        items = response.json().get("MediaContainer", {}).get("Metadata", [])
        n_artist, n_track = artist.lower().replace(" ", ""), track.lower().replace(" ", "")
        for item in items:
            p_artist = item.get("grandparentTitle", "").lower().replace(" ", "")
            p_track = item.get("title", "").lower().replace(" ", "")
            if n_track in p_track and (n_artist in p_artist or "various" in p_artist):
                return True
        return False
    except: return False

def convert_to_mp3(file_path: str, progress: Progress = None):
    """Convert a file to 320kbps MP3 using ffmpeg if it's not already an MP3."""
    if not file_path or not os.path.exists(file_path):
        return
    
    base, ext = os.path.splitext(file_path)
    if ext.lower() == '.mp3':
        return

    new_file = base + ".mp3"
    
    task_id = None
    if progress:
        task_id = progress.add_task(f"[bold magenta]🔄 Converting {os.path.basename(file_path)}", total=None)
    else:
        console.log(f"[bold magenta]🔄 Converting {os.path.basename(file_path)} to 320kbps MP3...[/]")

    try:
        cmd = ["ffmpeg", "-y", "-i", file_path, "-codec:a", "libmp3lame", "-b:a", "320k", new_file]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        os.remove(file_path)
        console.log(f"[bold green]✨ Conversion complete: {os.path.basename(new_file)}[/]")
    except Exception as e:
        console.log(f"[bold red]❌ Conversion failed for {os.path.basename(file_path)}: {e}[/]")
    finally:
        if progress and task_id:
            progress.remove_task(task_id)

def check_skip(timeout: float = 0.0) -> bool:
    """Check if 's' was pressed. If timeout > 0, waits for input."""
    if not sys.stdin.isatty():
        return False
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        rlist, _, _ = select.select([sys.stdin], [], [], timeout)
        if rlist:
            char = sys.stdin.read(1)
            if char.lower() == 's':
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
                return True
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
    return False

def send_pushover_notification(title, message):
    """Send a notification via Pushover."""
    if not pushover_config or not pushover_config.PUSHOVER_API_TOKEN or not pushover_config.PUSHOVER_USER_KEY:
        console.log("[bold yellow]⚠️ Pushover notification skipped: Credentials not found in pushover_config.py[/]")
        return

    url = "https://api.pushover.net/1/messages.json"
    data = {
        "token": pushover_config.PUSHOVER_API_TOKEN,
        "user": pushover_config.PUSHOVER_USER_KEY,
        "title": title,
        "message": message
    }

    console.log(f"[bold cyan]📲 Attempting to send Pushover notification: {title}...[/]")
    try:
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            console.log("[bold green]✅ Pushover notification sent successfully![/]")
        else:
            console.log(f"[bold red]❌ Pushover API error ({response.status_code}): {response.text}[/]")
    except Exception as e:
        console.log(f"[bold red]❌ Failed to connect to Pushover: {e}[/]")

def run_sockseek(artist: str, title: str, remix: str, genre_folder: str, progress: Progress) -> tuple[bool, str | None, str | None]:
    """Run the local sockseek command and monitor for remote queues."""
    query = f"{artist} {title}"
    if remix and "original" not in remix.lower():
        query += f" {remix}"
    query = re.sub(r'[\W_]+', ' ', query).strip()

    dest_path = f"/media/quark/dj/beatport top 100/{genre_folder}"
    
    cmd = [
        "./sockseek",
        query,
        "-p", dest_path,
        "--user", "velkrosmaak3",
        "--pass", "1Ndustry"
    ]

    task_id = progress.add_task(f"[bold cyan]🔍 Searching: {query}", total=100)
    
    # Set up TTY for skip detection if possible
    old_settings = None
    if sys.stdin.isatty():
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    try:
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True,
            preexec_fn=os.setsid
        )

        queued_start_time = None
        job_succeeded = False
        remote_user = None
        downloaded_file_path = None
        last_activity = time.time()
        buffer = ""

        while True:
            # Use select to check for data from stdout and stdin (for skip)
            inputs = [process.stdout]
            if sys.stdin.isatty():
                inputs.append(sys.stdin)
            
            rlist, _, _ = select.select(inputs, [], [], 1.0)

            # Check for skip press
            if sys.stdin.isatty() and sys.stdin in rlist:
                char = sys.stdin.read(1)
                if char.lower() == 's':
                    progress.console.log(f"[bold yellow]⏩ Skip requested. Killing search/download for: {artist} - {title}[/]")
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    termios.tcflush(sys.stdin, termios.TCIFLUSH)
                    progress.remove_task(task_id)
                    return False, remote_user, None

            if process.stdout in rlist:
                # Read character by character to catch \r progress updates
                char = process.stdout.read(1)
                if not char:
                    break

                last_activity = time.time()

                if char in ['\n', '\r']:
                    clean_line = buffer.strip()
                    if clean_line:
                        # Filter out common CLI noise (separators like ________ or ---------)
                        if re.match(r'^[_\-=\s*]+$', clean_line):
                            buffer = ""
                            continue
                            
                        # Log sockseek output to console above progress bars
                        progress.console.log(f"[dim]  ↳ {clean_line}[/]")

                        lower_line = clean_line.lower()
                        
                        # Parse percentage progress if present: (50.5%)
                        m_pct = re.search(r"\((\d+(?:\.\d+)?)%\)", clean_line)
                        if m_pct:
                            progress.update(task_id, completed=float(m_pct.group(1)), description=f"[bold cyan]🚀 Downloading: {query}")

                        if "songjob: succeeded" in lower_line:
                            job_succeeded = True

                        # Track username and file path from any SongJob output for cleanup/status tracking
                        if "songjob:" in lower_line:
                            # Format: SongJob: status/succeeded: Query: User\Path\to\file.ext (Progress%)
                            path_info_match = re.search(r"SongJob:.*?:.*?: (.*?)(?:\s+\(|$)", clean_line)
                            if path_info_match:
                                rel_path = path_info_match.group(1).strip().replace('\\', os.sep).replace('/', os.sep)
                                # Capture path if it contains at least one separator (indicating User\File or User\Dir\File)
                                if os.sep in rel_path:
                                    downloaded_file_path = os.path.join(dest_path, rel_path)
                                    if not remote_user:
                                        # Remote user is the first part of the relative path
                                        remote_user = rel_path.split(os.sep)[0]

                        # Monitor Queue logic
                        if "queued" in lower_line:
                            if queued_start_time is None:
                                queued_start_time = time.time()
                            if time.time() - queued_start_time > QUEUED_TIMEOUT:
                                progress.console.log(f"[bold red]⏱️ Queued for too long ({QUEUED_TIMEOUT}s). Canceling...[/]")
                                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                                progress.remove_task(task_id)
                                return False, remote_user, None
                        elif "downloading" in lower_line:
                            queued_start_time = None
                    buffer = ""
                else:
                    buffer += char
            else:
                # No data received in the last second
                if time.time() - last_activity > STALL_TIMEOUT:
                    progress.console.log(f"[bold red]❌ Stall detected: No output for {STALL_TIMEOUT}s. Killing...[/]")
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    progress.remove_task(task_id)
                    return False, remote_user, None
                
                if process.poll() is not None:
                    break

        progress.remove_task(task_id)
        return (job_succeeded or process.returncode == 0), remote_user, downloaded_file_path

    except Exception as e:
        progress.console.log(f"    [bold red]❌ sockseek error: {e}[/]")
        return False, None, None
    finally:
        if old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)

        # Cleanup partial files if the job didn't finish successfully
        if not job_succeeded and downloaded_file_path:
            # Brief sleep to ensure file handles are released after process termination
            time.sleep(0.5)
            incomplete_path = f"{downloaded_file_path}.incomplete"
            if os.path.exists(incomplete_path):
                try:
                    os.remove(incomplete_path)
                    progress.console.log(f"[bold yellow]🧹 Removed partial file: {os.path.basename(incomplete_path)}[/]")
                except Exception as cleanup_err:
                    progress.console.log(f"[bold red]⚠️ Cleanup failed for {os.path.basename(incomplete_path)}: {cleanup_err}[/]")

def main():
    parser = argparse.ArgumentParser(description="Download Beatport Top 100 via local sockseek.")
    parser.add_argument("genre", help=f"Genre key ({', '.join(GENRE_MAP.keys())})")
    parser.add_argument("--download", action="store_true", help="Trigger downloads")
    parser.add_argument("--dev", action="store_true", help="Dev mode: only process top 5 tracks")
    args = parser.parse_args()

    init_db()
    
    genre_key = args.genre.lower()
    if genre_key not in GENRE_MAP:
        console.print(f"[bold red]❌ Unknown genre.[/] Choose from: [cyan]{', '.join(GENRE_MAP.keys())}[/]")
        sys.exit(1)

    # Prettify the genre name (e.g., tech-house -> Tech House)
    genre_display = GENRE_MAP[genre_key][0].replace('-', ' ').title()
    
    status_msg = f"Genre: [bold yellow]{genre_display}[/]\nStats: {get_db_stats()}"
    if args.dev:
        status_msg += "\nMode: [bold red]DEVELOPMENT (Top 5 Only)[/]"
    
    console.print(Panel(
        status_msg,
        title="[bold magenta]Soulseek Similar Rinser[/]",
        border_style="magenta",
        box=box.DOUBLE
    ))

    tracks = get_beatport_top_100(genre_key)
    if not tracks:
        console.print(f"[bold red]No tracks found.[/]")
        return

    if args.dev:
        tracks = tracks[:5]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        expand=True
    ) as progress:
        
        overall_task = progress.add_task(f"[bold yellow]Processing Top 100: {genre_display}", total=len(tracks))

        for i, t in enumerate(tracks, 1):
            artist, title, remix = t['artist'], t['title'], t['remix']
            track_tag = f"[{i:03d}]"
            
            progress.update(overall_task, description=f"[bold yellow]Processing: {artist} - {title}")

            # Allow skipping before processing starts
            if check_skip():
                progress.console.log(f"[bold yellow]⏩ Skipping track: {artist} - {title}[/]")
                progress.advance(overall_task)
                continue

            # 1. Check DB
            if track_exists(artist, title, remix):
                progress.console.log(f"[blue]{track_tag} 💾 {artist} - {title} (In Local DB)[/]")
                progress.advance(overall_task)
                continue

            # 2. Check Plex
            if check_plex_for_track(artist, title):
                progress.console.log(f"[green]{track_tag} ✅ {artist} - {title} (In Plex)[/]")
                progress.advance(overall_task)
                continue

            # 3. Download
            if args.download:
                success, r_user, f_path = run_sockseek(artist, title, remix, genre_display, progress)
                add_to_db(artist, title, remix, r_user, success)
                if success:
                    progress.console.log(f"[bold magenta]{track_tag} 📦 Finished (User: {r_user or 'Unknown'})[/]")
                    if f_path and os.path.exists(f_path):
                        DOWNLOADED_SIZES.append(os.path.getsize(f_path))
                        DOWNLOADED_ARTISTS.append(artist)
                        # Run conversion synchronously to ensure it completes before the script exits
                        convert_to_mp3(f_path, progress)
                    elif f_path:
                        progress.console.log(f"[bold yellow]⚠️ Downloaded file missing at: {f_path}[/]")
                    else:
                        progress.console.log(f"[bold red]⚠️ Could not determine file path for conversion.[/]")
                else:
                    progress.console.log(f"[bold red]{track_tag} ❌ Failed or Skipped (User: {r_user or 'Unknown'})[/]")
            else:
                progress.console.log(f"[bold yellow]{track_tag} ❌ {artist} - {title} (Missing)[/]")

            progress.advance(overall_task)
            check_skip(2.0) # Port release wait + skip check

    # Final Stats Summary
    if DOWNLOADED_SIZES:
        total_bytes = sum(DOWNLOADED_SIZES)
        avg_bytes = total_bytes / len(DOWNLOADED_SIZES)
        min_bytes = min(DOWNLOADED_SIZES)
        max_bytes = max(DOWNLOADED_SIZES)

        table = Table(title="[bold cyan]Download Statistics[/]", box=box.ROUNDED, header_style="bold blue")
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")
        
        def to_mb(b): return f"{b / 1024 / 1024:.2f} MB"

        table.add_row("Total Files Downloaded", str(len(DOWNLOADED_SIZES)))
        table.add_row("Total Data Volume", to_mb(total_bytes))
        table.add_row("Average File Size", to_mb(avg_bytes))
        table.add_row("Smallest File", to_mb(min_bytes))
        table.add_row("Largest File", to_mb(max_bytes))

        console.print("\n", table)

        # Send Pushover Notification
        unique_artists = sorted(list(set(DOWNLOADED_ARTISTS)))
        msg = (
            f"Tracks: {len(DOWNLOADED_SIZES)}\n"
            f"Total Size: {to_mb(total_bytes)}\n"
            f"Artists: {', '.join(unique_artists)}"
        )
        send_pushover_notification(args.genre, msg)
    
    console.print(f"\n[bold green]✅ All tracks processed.[/]")

if __name__ == "__main__":
    main()