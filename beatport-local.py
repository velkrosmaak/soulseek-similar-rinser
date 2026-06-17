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

def parse_size_to_bytes(value: str, unit: str) -> int:
    """Convert size strings like '10.5' and 'MB' to bytes."""
    units = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "b": 1}
    return int(float(value) * units.get(unit.lower(), 1))

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

    # Start with total=None to show a pulsing "searching" bar until download starts
    task_id = progress.add_task(f"[bold cyan]🔍 Searching: {query}", total=None)
    
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
            stdin=subprocess.DEVNULL,
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
            # Use file descriptors for select to bypass TextIOWrapper buffering issues
            inputs = [process.stdout.fileno()]
            if sys.stdin.isatty():
                inputs.append(sys.stdin.fileno())
            
            rlist, _, _ = select.select(inputs, [], [], 0.05)

            # Check for skip press
            if sys.stdin.isatty() and sys.stdin.fileno() in rlist:
                char = sys.stdin.read(1)
                if char.lower() == 's':
                    progress.console.log(f"[bold yellow]⏩ Skip requested. Killing search/download for: {artist} - {title}[/]")
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    termios.tcflush(sys.stdin, termios.TCIFLUSH)
                    progress.remove_task(task_id)
                    return False, remote_user, None

            if process.stdout.fileno() in rlist:
                # Read character by character to catch \r progress updates
                char = process.stdout.read(1)
                if not char:
                    break

                last_activity = time.time()

                if char in ['\n', '\r']:
                    clean_line = buffer.strip()
                    if clean_line:
                        # RAW DEBUG: Print the line exactly as it comes from the process
                        progress.console.log(f"[grey37]RAW: {clean_line}[/]")
                        lower_line = clean_line.lower()
                        
                        # Try to parse byte sizes for meaningful progress (e.g., "5.1 MB / 10.2 MB")
                        # This makes DownloadColumn and TransferSpeedColumn work correctly
                        size_match = re.search(r"(\d+(?:\.\d+)?)\s*([KMG]?B)\s*/\s*(\d+(?:\.\d+)?)\s*([KMG]?B)", clean_line, re.IGNORECASE)
                        if size_match:
                            cur_val, cur_unit, tot_val, tot_unit = size_match.groups()
                            cur_bytes = parse_size_to_bytes(cur_val, cur_unit)
                            tot_bytes = parse_size_to_bytes(tot_val, tot_unit)
                            progress.update(task_id, completed=cur_bytes, total=tot_bytes, description=f"[bold cyan]🚀 Downloading: {query}")
                        else:
                            # Fallback: Parse percentage progress if present: (50.5%)
                            m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", clean_line)
                            if m_pct:
                                progress.update(task_id, completed=float(m_pct.group(1)), total=100, description=f"[bold cyan]🚀 Downloading: {query}")

                        if "songjob: succeeded" in lower_line:
                            job_succeeded = True

                        if "songjob: download error:" in lower_line:
                            progress.console.log(f"[bold red]❌ Sockseek reported failure: {clean_line}[/]")
                            # Kill the process group to abort immediately; this ensures returncode != 0
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)

                        # Track username and file path from SongJob output
                        # sockseek often prints the path on the line following 'succeeded'
                        possible_path = None
                        if "songjob:" in lower_line:
                            m = re.search(r"SongJob:.*?:.*?: (.*?)(?:\s+\(|$)", clean_line)
                            if m and m.group(1).strip():
                                potential = m.group(1).strip()
                                if "\\" in potential or "/" in potential:
                                    possible_path = potential
                        elif re.search(r"^[a-zA-Z0-9].*[\\/].*\.[a-zA-Z0-9]+$", clean_line):
                            # Standalone line looking like a relative path: User\Path\File.ext
                            possible_path = clean_line

                        if possible_path:
                            rel_path = possible_path.replace('\\', os.sep).replace('/', os.sep)
                            downloaded_file_path = os.path.normpath(os.path.join(dest_path, rel_path))
                            if not remote_user and os.sep in rel_path:
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

    processed_stats = {"total": len(tracks), "missing": 0, "downloaded": 0, "already_owned": 0}
    newly_downloaded_artists = []

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
                processed_stats["already_owned"] += 1
                progress.advance(overall_task)
                continue

            # 2. Download
            if args.download:
                success, r_user, f_path = run_sockseek(artist, title, remix, genre_display, progress)
                add_to_db(artist, title, remix, r_user, success)
                if success:
                    progress.console.log(f"[bold magenta]{track_tag} 📦 Finished (User: {r_user or 'Unknown'})[/]")
                    processed_stats["downloaded"] += 1

                    # Filesystem settle/location check
                    final_path = f_path
                    if final_path and not os.path.exists(final_path):
                        # 1. Wait briefly for move/sync
                        for _ in range(6):
                            time.sleep(0.5)
                            if os.path.exists(final_path): break
                        
                        # 2. If still missing, check if it's nested under a query folder or similar
                        if not os.path.exists(final_path):
                            filename = os.path.basename(final_path)
                            genre_dir = f"/media/quark/dj/beatport top 100/{genre_display}"
                            for root, dirs, files in os.walk(genre_dir):
                                if filename in files:
                                    final_path = os.path.join(root, filename)
                                    break

                    if final_path and os.path.exists(final_path):
                        DOWNLOADED_SIZES.append(os.path.getsize(final_path))
                        DOWNLOADED_ARTISTS.append(artist)
                        newly_downloaded_artists.append(artist)
                        # Run conversion synchronously to ensure it completes before the script exits
                        convert_to_mp3(final_path, progress)
                    elif f_path:
                        progress.console.log(f"[bold yellow]⚠️ Downloaded file missing at: {f_path}[/]")
                    else:
                        progress.console.log(f"[bold red]⚠️ Could not determine file path for conversion.[/]")
                else:
                    progress.console.log(f"[bold red]{track_tag} ❌ Failed or Skipped (User: {r_user or 'Unknown'})[/]")
                    processed_stats["missing"] += 1
            else:
                progress.console.log(f"[bold yellow]{track_tag} ❌ {artist} - {title} (Missing)[/]")
                processed_stats["missing"] += 1

            progress.advance(overall_task)
            if check_skip(2.0): # Port release wait + skip check
                progress.console.log(f"[bold yellow]⏩ Skipping to next track...[/]")

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

    # Send Pushover Notification (Always triggered at the end of the run)
    unique_new_artists = sorted(list(set(newly_downloaded_artists)))
    msg = (
        f"Run complete for {genre_display}.\n"
        f"• Total: {processed_stats['total']} | Downloaded: {processed_stats['downloaded']}\n"
        f"• Missing: {processed_stats['missing']} | Owned: {processed_stats['already_owned']}"
    )
    if unique_new_artists:
        msg += f"\n\nNew Artists: {', '.join(unique_new_artists[:12])}{'...' if len(unique_new_artists) > 12 else ''}"
    
    send_pushover_notification(f"Soulseek Rinser: {genre_display}", msg)
    
    console.print(f"\n[bold green]✅ All tracks processed for {genre_display}.[/]")

if __name__ == "__main__":
    main()