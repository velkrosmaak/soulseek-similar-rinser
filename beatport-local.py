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
import select
import time
import requests
import threading
import sqlite3
import subprocess
import signal
from tqdm import tqdm

from config import PLEX_TOKEN, PLEX_URL

class Color:
    PURPLE = '\033[95m'
    CYAN = '\033[96m'
    DARKCYAN = '\033[36m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    END = '\033[0m'

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
    return f"{s_count} successful, {f_count} failed"

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
        print(f"{Color.RED}❌ Unknown genre key.{Color.END}")
        return []

    url = f"https://www.beatport.com/genre/{genre_name}/{genre_id}/top-100"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
    }

    print(f"{Color.DARKCYAN}[⚙️ DEBUG] Fetching Beatport Top 100 for {genre_name.title()}...{Color.END}")
    
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
        print(f"{Color.RED}❌ Failed to scrape Beatport: {e}{Color.END}")
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

def convert_to_mp3(file_path: str):
    """Convert a file to 320kbps MP3 using ffmpeg if it's not already an MP3."""
    if not file_path or not os.path.exists(file_path):
        return
    
    base, ext = os.path.splitext(file_path)
    if ext.lower() == '.mp3':
        return

    new_file = base + ".mp3"
    print(f"\n      {Color.PURPLE}🔄 Converting {ext[1:].upper()} to 320kbps MP3...{Color.END}")
    try:
        cmd = ["ffmpeg", "-y", "-i", file_path, "-codec:a", "libmp3lame", "-b:a", "320k", new_file]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        os.remove(file_path)
        print(f"      {Color.GREEN}✨ Conversion complete: {os.path.basename(new_file)}{Color.END}")
    except Exception as e:
        print(f"      {Color.RED}❌ Conversion failed: {e}{Color.END}")

def run_sockseek(artist: str, title: str, remix: str, genre_folder: str) -> tuple[bool, str | None, str | None]:
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

    print(f"    {Color.CYAN}🚀 Running sockseek: {query}{Color.END}")
    
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
            # Use select to check for data with a 1-second timeout
            rlist, _, _ = select.select([process.stdout], [], [], 1.0)

            if rlist:
                # Read character by character to catch \r progress updates
                char = process.stdout.read(1)
                if not char:
                    break

                last_activity = time.time()

                if char in ['\n', '\r']:
                    clean_line = buffer.strip()
                    if clean_line:
                        # Print the line, clearing trailing characters if it's a \r update
                        sys.stdout.write(f"\r      {Color.DARKCYAN}» {clean_line}{Color.END}          ")
                        if char == '\n':
                            sys.stdout.write('\n')
                        sys.stdout.flush()

                        lower_line = clean_line.lower()
                        if "songjob: succeeded" in lower_line:
                            job_succeeded = True
                            # Extract local path. Format: SongJob: succeeded: Query: User\Path\to\file.ext
                            path_match = re.search(r"SongJob: succeeded:.*?: (.*)", clean_line)
                            if path_match:
                                rel_path = path_match.group(1).replace('\\', os.sep).replace('/', os.sep)
                                downloaded_file_path = os.path.join(dest_path, rel_path)
                                
                                # Also update remote_user from this line if not already caught
                                if not remote_user:
                                    remote_user = rel_path.split(os.sep)[0]

                        # Try to extract username: [4] SongJob: status: Query: User\Path
                        if "songjob:" in lower_line:
                            # User is typically after the third colon and before the first backslash
                            job_match = re.search(r"SongJob:.*?:.*?: (.*?)[\\/]", clean_line)
                            if job_match:
                                remote_user = job_match.group(1).strip()

                        # Monitor Queue logic
                        if "queued" in lower_line:
                            if queued_start_time is None:
                                queued_start_time = time.time()
                            if time.time() - queued_start_time > QUEUED_TIMEOUT:
                                print(f"\n    {Color.RED}⏱️ Queued for too long ({QUEUED_TIMEOUT}s). Canceling...{Color.END}")
                                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                                return False, remote_user, None
                        elif "downloading" in lower_line:
                            queued_start_time = None
                    buffer = ""
                else:
                    buffer += char
            else:
                # No data received in the last second
                if time.time() - last_activity > STALL_TIMEOUT:
                    print(f"\n    {Color.RED}❌ Stall detected: No output for {STALL_TIMEOUT}s. Killing...{Color.END}")
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    return False, remote_user, None
                
                if process.poll() is not None:
                    break

        return (job_succeeded or process.returncode == 0), remote_user, downloaded_file_path

    except Exception as e:
        print(f"    {Color.RED}❌ sockseek error: {e}{Color.END}")
        return False, None, None

def main():
    parser = argparse.ArgumentParser(description="Download Beatport Top 100 via local sockseek.")
    parser.add_argument("genre", help=f"Genre key ({', '.join(GENRE_MAP.keys())})")
    parser.add_argument("--download", action="store_true", help="Trigger downloads")
    args = parser.parse_args()

    init_db()
    
    genre_key = args.genre.lower()
    if genre_key not in GENRE_MAP:
        print(f"{Color.RED}Unknown genre. Choose from: {', '.join(GENRE_MAP.keys())}{Color.END}")
        sys.exit(1)

    tracks = get_beatport_top_100(genre_key)
    if not tracks:
        print(f"{Color.RED}No tracks found.{Color.END}")
        return

    genre_display = GENRE_MAP[genre_key][0]
    print(f"\n{Color.BOLD}{Color.CYAN}🚀 Local Rinser: Top 100 {genre_display}...{Color.END}\n")
    print(f"    {Color.DARKCYAN}📊 Database stats: {get_db_stats()}{Color.END}\n")

    for i, t in enumerate(tracks, 1):
        artist, title, remix = t['artist'], t['title'], t['remix']
        track_tag = f"{Color.DARKCYAN}[{i:03d}]{Color.END}"
        
        # 1. Check DB
        if track_exists(artist, title, remix):
            print(f"  {track_tag} {Color.BLUE}💾 {artist} - {title} (In Local DB){Color.END}")
            continue

        # 2. Check Plex
        if check_plex_for_track(artist, title):
            print(f"  {track_tag} {Color.GREEN}✅ {artist} - {title} (In Plex){Color.END}")
            continue

        # 3. Download
        if args.download:
            print(f"  {track_tag} {Color.YELLOW}🔍 {artist} - {title}{Color.END}")
            success, r_user, f_path = run_sockseek(artist, title, remix, genre_display)
            add_to_db(artist, title, remix, r_user, success)
            if success:
                print(f"    {Color.PURPLE}📦 Finished (User: {r_user or 'Unknown'}).{Color.END}")
                if f_path:
                    # Start conversion in a separate thread so the next search can begin
                    threading.Thread(target=convert_to_mp3, args=(f_path,), daemon=True).start()
            else:
                print(f"    {Color.RED}❌ Failed or Skipped (User: {r_user or 'Unknown'}).{Color.END}")
        else:
            print(f"  {track_tag} {Color.YELLOW}❌ {artist} - {title} (Missing){Color.END}")

        # Brief sleep to let the OS release the listening port (49998)
        time.sleep(2)

    print(f"\n{Color.BOLD}{Color.GREEN}✅ Processing complete.{Color.END}")

if __name__ == "__main__":
    main()