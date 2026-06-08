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

GENRE_MAP = {
    "house": ("house", 5),
    "techno": ("techno-peak-time-driving", 6),
    "tech-house": ("tech-house", 11),
    "deep-house": ("deep-house", 12),
    "minimal": ("minimal-deep-tech", 75),
    "dnb": ("drum-bass", 1),
    "trance": ("trance", 7),
    "progressive": ("progressive-house", 15),
    "melodic": ("melodic-house-techno", 90),
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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}

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

def trigger_slskd_track_search(idx: int, artist: str, track: str, remix: str, used_users: set, destination: str):
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

            if best_response:
                user = best_response["username"]
                f = best_response["file"]
                
                enqueue_url = f"{SLSKD_URL.rstrip('/')}/api/v0/transfers/downloads/{user}"
                headers = {"X-API-Key": SLSKD_API_KEY, "Content-Type": "application/json"}
                
                # Build file object: size must be an integer
                file_obj = {
                    "filename": f.get("filename"),
                    "size": int(f.get("size") or 0)
                }

                payload = [file_obj] # API expects a list/array
                params = {"destination": destination}

                try:
                    # Post to API with destination as query parameter
                    response = requests.post(enqueue_url, json=payload, params=params, headers=headers, timeout=10)
                    if response.status_code >= 400:
                        raise RuntimeError(f"API returned {response.status_code}")
                    print(f"    {track_tag} {Color.PURPLE}📦 Enqueued from {user} into '{destination}' ({f.get('bitRate')}kbps){Color.END}")
                except Exception as e:
                    print(f"    {track_tag} {Color.YELLOW}⚠️ Request error: {e}. Falling back to default dir...{Color.END}")
                    slskd_client.transfers.enqueue(username=user, files=[file_obj])
                used_users.add(user)
            else:
                print(f"    {track_tag} {Color.YELLOW}⚠️ No suitable results found.{Color.END}")
        finally:
            slskd_client.searches.delete(id=search_id)
    except Exception as e:
        print(f"    {track_tag} {Color.RED}❌ Search failed: {e}{Color.END}")

def main():
    parser = argparse.ArgumentParser(description="Download Beatport Top 100 tracks.")
    parser.add_argument("genre", help=f"Genre key ({', '.join(GENRE_MAP.keys())})")
    parser.add_argument("--download", action="store_true", help="Trigger downloads")
    args = parser.parse_args()
    used_users = set()

    tracks = get_beatport_top_100(args.genre)
    if not tracks:
        return

    print(f"\n{Color.BOLD}{Color.CYAN}🚀 Processing Top 100 for {args.genre}...{Color.END}\n")
    destination = f"Beatport Top 100 {args.genre}"
    print(f"    {Color.DARKCYAN}📂 Subdirectory: {destination}{Color.END}\n")
    
    for i, t in enumerate(tracks, 1):
        artist, title, remix = t['artist'], t['title'], t['remix']
        track_tag = f"{Color.DARKCYAN}[{i:03d}]{Color.END}"
        
        exists = check_plex_for_track(artist, title)
        if exists:
            print(f"  {track_tag} {Color.GREEN}✅ {artist} - {title}{Color.END}")
        else:
            if args.download:
                trigger_slskd_track_search(i, artist, title, remix, used_users, destination)
            else:
                print(f"  {track_tag} {Color.YELLOW}❌ {artist} - {title} (Missing){Color.END}")

    print(f"\n{Color.BOLD}{Color.GREEN}✅ All tracks processed.{Color.END}")

if __name__ == "__main__":
    main()