#!/usr/bin/env python3
"""
soulseek-similar-rinser/beatport.py
Fetch Beatport Top 100 for a genre and download missing tracks via slskd.
"""

import argparse
import json
import os
import re
import select
import sys
import termios
import time
import tty
import requests
import sqlite3
from tqdm import tqdm

from config import PLEX_TOKEN, PLEX_URL, SLSKD_API_KEY, SLSKD_URL, create_slskd_client, _getenv

slskd_client = create_slskd_client()
SEARCH_TIMEOUT = 40

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

def check_skip(timeout: float = 0.0) -> bool:
    """Check if 's' was pressed. If timeout > 0, waits for input."""
    if not sys.stdin.isatty():
        if timeout > 0: time.sleep(timeout)
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

def get_beatport_top_100(genre_key: str) -> list[dict]:
    """Scrape Beatport Top 100 tracks for a genre."""
    genre_name, genre_id = GENRE_MAP.get(genre_key.lower(), (genre_key, None))
    if not genre_id:
        print(f"{Color.RED}❌ Unknown genre key. Known keys: {', '.join(GENRE_MAP.keys())}{Color.END}")
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
        
        # Beatport embeds data in a __NEXT_DATA__ script tag
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', response.text)
        if not match:
            print(f"{Color.RED}❌ Could not find track data in page.{Color.END}")
            return []

        data = json.loads(match.group(1))
        # Dig through Next.js props to find the tracks
        # Note: Path can change depending on Beatport's frontend updates
        results = data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
        
        tracks = []
        for query in results:
            state_data = query.get("state", {}).get("data", {})
            if isinstance(state_data, dict) and "results" in state_data:
                for t in state_data["results"]:
                    artists = ", ".join([a["name"] for a in t.get("artists", [])])
                    tracks.append({
                        "artist": artists,
                        "title": t.get("name"),
                        "remix": t.get("mix_name", "Original Mix")
                    })
                break
        
        return tracks
    except Exception as e:
        print(f"{Color.RED}❌ Failed to scrape Beatport: {e}{Color.END}")
        return []

def check_plex_for_track(artist: str, track: str) -> bool:
    """Check if the track exists on Plex (Type 10)."""
    url = f"{PLEX_URL.rstrip('/')}/search"
    params = {"type": 10, "query": track, "X-Plex-Token": PLEX_TOKEN}
    headers = {"Accept": "application/json"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=5)
        data = response.json()
        items = data.get("MediaContainer", {}).get("Metadata", [])

        def normalize(s: str) -> str:
            if not s: return ""
            s = s.lower().strip()
            s = re.sub(r'[\(\[].*?[\)\]]', '', s)
            return re.sub(r'[\W_]+', '', s)

        n_artist, n_track = normalize(artist), normalize(track)

        for item in items:
            # For tracks, grandparentTitle is usually the Artist
            p_artist = normalize(item.get("grandparentTitle", ""))
            p_track = normalize(item.get("title", ""))
            
            artist_match = (n_artist in p_artist or p_artist in n_artist or p_artist in ["various", "variousartists"])
            track_match = (n_track in p_track or p_track in n_track)

            if artist_match and track_match:
                return True
        return False
    except:
        return False

def trigger_slskd_track_search(idx: int, artist: str, track: str, remix: str, used_users: set, destination: str, require_free_slots: bool = False) -> tuple[bool, str | None]:
    """Search and download the best quality track, spreading load across users."""
    track_tag = f"{Color.DARKCYAN}[{idx:03d}]{Color.END}"
    query = f"{artist} {track}"
    if remix and "original" not in remix.lower():
        query += f" {remix}"

    # Clean query: strip symbols and replace with spaces
    query = re.sub(r'[\W_]+', ' ', query).strip()
    print(f"  {track_tag} {Color.CYAN}🔍 Searching: {query}...{Color.END}")

    try:
        # Identify busy users
        busy_users = set()
        try:
            busy_users = {t.get("username") for t in slskd_client.transfers.get_all_downloads() if t.get("username")}
        except: pass

        search_id = None
        # Search creation with retry for 429s
        for attempt in range(3):
            try:
                search = slskd_client.searches.search_text(searchText=query)
                search_id = search.get("id")
                break
            except Exception as e:
                if "429" in str(e):
                    time.sleep(5 * (attempt + 1))
                    continue
                raise e

        if not search_id:
            raise RuntimeError("Failed to create search after retries (Rate Limited)")

        try:
            best_response = None
            frames = ["( •_•)>⌐■-■", "(⌐■_■)     ", "( •_•)>⌐■-■", "( •_•)     "]

            for attempt in range(SEARCH_TIMEOUT // 5):
                responses = slskd_client.searches.search_responses(id=search_id)
                if responses:
                    candidates = []
                    for resp in responses:
                        files = resp.get("files", [])
                        audio_files = [f for f in files if (f.get("filename") or "").lower().endswith(('.mp3', '.flac', '.m4a', '.wav'))]
                        if not audio_files: continue

                        for f in audio_files:
                            bitrate = int(f.get("bitRate") or f.get("bitrate") or 0)
                            is_lossless = (f.get("filename") or "").lower().endswith(('.flac', '.wav'))
                            is_hq = is_lossless or bitrate >= 320
                            
                            user_penalty = (resp.get("username") in used_users) or (resp.get("username") in busy_users)
                            # Check both possible keys for slots
                            has_free_slots = resp.get("hasFreeSlots", resp.get("hasFreeUploadSlot", False))
                            queue_length = int(resp.get("queueLength") or 0)

                            candidates.append({
                                "username": resp.get("username"),
                                "file": f,
                                # Score order: Quality > Free Slots > Shortest Queue > Fresh User
                                "score": (is_hq, has_free_slots, -queue_length, not user_penalty, bitrate, is_lossless)
                            })

                    if candidates:
                        candidates.sort(key=lambda x: x["score"], reverse=True)
                        best_response = candidates[0]
                        # Stop early if we find a "Good Enough" candidate (HQ + Free Slots)
                        if best_response["score"][0] and best_response["score"][1]:
                            break

                for i in range(10):
                    remaining = SEARCH_TIMEOUT - (attempt * 5) - (i * 0.5)
                    if check_skip(0.5):
                        print(f"\n    {Color.YELLOW}⏩ Skipping...{Color.END}")
                        return
                    sys.stdout.write(f"\r    {track_tag} {Color.CYAN}⏳ Digging... {frames[i % 4]} [{int(remaining)}s]{Color.END}")
                    sys.stdout.flush()

            print("\r" + " " * 80 + "\r", end="", flush=True)

            enqueued = False
            if best_response:
                if require_free_slots and not best_response["score"][1]:
                    # During optimization, we only care if we found someone with a free slot
                    return False

                user = best_response["username"]
                f = best_response["file"]

                # Use the user-specific endpoint for reliable subdirectory support
                enqueue_url = f"{SLSKD_URL.rstrip('/')}/api/v0/transfers/downloads/{user}"
                headers = {"X-API-Key": SLSKD_API_KEY, "Content-Type": "application/json"}
                
                payload = {
                    "files": [{"filename": f.get("filename"), "size": int(f.get("size") or 0)}],
                    "destination": destination
                }

                try:
                    # Enqueue respects the 'destination' field in the JSON body
                    response = requests.post(enqueue_url, json=payload, headers=headers, timeout=30)
                    if response.status_code >= 400:
                        raise RuntimeError(f"API returned {response.status_code}")
                    print(f"    {track_tag} {Color.PURPLE}📦 Enqueued from {user} into '{destination}' ({f.get('bitRate')}kbps){Color.END}")
                    enqueued = True
                except Exception as e:
                    print(f"    {track_tag} {Color.YELLOW}⚠️ Request error: {e}. Falling back to default dir...{Color.END}")
                    slskd_client.transfers.enqueue(username=user, files=[{"filename": f.get("filename"), "size": int(f.get("size") or 0)}])
                    enqueued = True
                used_users.add(user)
            else:
                print(f"    {track_tag} {Color.YELLOW}⚠️ No suitable results found.{Color.END}")
            return enqueued, target_user
        finally:
            slskd_client.searches.delete(id=search_id)
    except Exception as e:
        print(f"    {track_tag} {Color.RED}❌ Search failed: {e}{Color.END}")
        return False, None

def optimize_queued_downloads(destination: str, used_users: set, enqueued_metadata: list):
    """
    Look for downloads that are remotely queued and try to find a source with no queue.
    """
    print(f"\n{Color.BOLD}{Color.CYAN}🔍 Checking for remotely queued tracks to optimize...{Color.END}")
    
    try:
        all_transfers = slskd_client.transfers.get_all_downloads()
        if not all_transfers:
            return

        queued_items = []
        for transfer in all_transfers:
            username = transfer.get("username")
            if not username: continue

            for f in transfer.get("files", []):
                state = str(f.get("state", "")).strip()
                if "Queued, Remotely" in state:
                    # Try to match this file back to our metadata list by looking at the path
                    # Beatport downloads are typically 'Artist - Title (Remix).ext'
                    filename = (f.get("filename") or "").lower()
                    
                    match = None
                    for m in enqueued_metadata:
                        # Heuristic: both artist and title should be in the file path
                        if m['artist'].lower() in filename and m['title'].lower() in filename:
                            match = m
                            break
                    
                    if match:
                        queued_items.append({
                            "username": username,
                            "file_id": f.get("id"),
                            "metadata": match
                        })

        if not queued_items:
            print(f"    {Color.GREEN}✅ No remotely queued tracks found for optimization.{Color.END}")
            return

        print(f"    {Color.YELLOW}⏳ Found {len(queued_items)} queued tracks. Attempting to find immediate sources...{Color.END}")

        for item in queued_items:
            m = item["metadata"]
            old_user = item["username"]
            file_id = item["file_id"]
            
            track_tag = f"{Color.DARKCYAN}[{m['idx']:03d} OPT]{Color.END}"
            print(f"  {track_tag} {Color.CYAN}🔄 Re-searching for better source: {m['artist']} - {m['title']}{Color.END}")
            
            # Penalize the user who currently has the file queued
            temp_used = used_users.copy()
            temp_used.add(old_user)
            
            # require_free_slots=True ensures we only swap if we find someone with no queue
            if trigger_slskd_track_search(m['idx'], m['artist'], m['title'], m['remix'], temp_used, destination, require_free_slots=True):
                print(f"    {track_tag} {Color.GREEN}🚀 Found immediate source! Canceling queued download from {old_user}.{Color.END}")
                try:
                    slskd_client.transfers.cancel_download(username=old_user, id=file_id, remove=True)
                except: pass

    except Exception as e:
        print(f"    {Color.RED}❌ Optimization check failed: {e}{Color.END}")

def main():
    parser = argparse.ArgumentParser(description="Download Beatport Top 100 tracks.")
    parser.add_argument("genre", help=f"Genre key ({', '.join(GENRE_MAP.keys())})")
    parser.add_argument("--download", action="store_true", help="Trigger downloads")
    args = parser.parse_args()

    if not SLSKD_API_KEY or SLSKD_API_KEY == "your slskd api key":
        print(f"{Color.RED}Error: Please set your SLSKD_API_KEY environment variable or update config.py with your API key.{Color.END}")
        sys.exit(1)

    init_db()
    used_users = set()
    enqueued_metadata = []

    tracks = get_beatport_top_100(args.genre)
    if not tracks:
        return

    print(f"\n{Color.BOLD}{Color.CYAN}🚀 Processing Top 100 for {args.genre}...{Color.END}\n")
    destination = f"Beatport Top 100 {args.genre}"
    print(f"    {Color.DARKCYAN}📂 Subdirectory: {destination}{Color.END}\n")
    print(f"    {Color.DARKCYAN}📊 Database stats: {get_db_stats()} tracks previously downloaded.{Color.END}\n")
    
    for i, t in enumerate(tracks, 1):
        artist, title, remix = t['artist'], t['title'], t['remix']
        track_tag = f"{Color.DARKCYAN}[{i:03d}]{Color.END}"
        
        if track_exists(artist, title, remix):
            print(f"  {track_tag} {Color.BLUE}💾 {artist} - {title} (Already in DB){Color.END}")
            continue

        exists = check_plex_for_track(artist, title)
        if exists:
            print(f"  {track_tag} {Color.GREEN}✅ {artist} - {title}{Color.END}")
        else:
            if args.download:
                success, user = trigger_slskd_track_search(i, artist, title, remix, used_users, destination)
                add_to_db(artist, title, remix, username=user, success=success)
                if success:
                    enqueued_metadata.append({'idx': i, 'artist': artist, 'title': title, 'remix': remix})
            else:
                print(f"  {track_tag} {Color.YELLOW}❌ {artist} - {title} (Missing){Color.END}")

    # Post-process: try to find better sources for things that got queued
    if args.download and enqueued_metadata:
        # Brief sleep to allow slskd to update transfer states
        time.sleep(2)
        optimize_queued_downloads(destination, used_users, enqueued_metadata)

    print(f"\n{Color.BOLD}{Color.GREEN}✅ All tracks processed.{Color.END}")

if __name__ == "__main__":
    main()