#!/usr/bin/env python3
"""
similar-artists/sa.py
Find top similar artists using the Last.fm API.
"""

import argparse
import requests
import sys
import time
import os
import re
import select
import tty
import termios
from tqdm import tqdm

from config import (
    LASTFM_API_KEY,
    LASTFM_BASE_URL,
    PLEX_TOKEN,
    PLEX_URL,
    SLSKD_API_KEY,
    SLSKD_URL,
    create_slskd_client,
)

slskd_client = create_slskd_client()

class Color:
    PURPLE = '\033[95m'
    CYAN = '\033[96m'
    DARKCYAN = '\033[36m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'

def retry_download(client, username, file_info):
    """Retry a download using the best available API for this slskd client."""
    file_id = file_info.get("id")
    filename = file_info.get("filename")
    size = file_info.get("size")

    if hasattr(client.transfers, "retry"):
        client.transfers.retry(username=username, id=file_id)
        return

    if not file_id or not filename:
        raise ValueError("missing file id or filename")

    cancelled = client.transfers.cancel_download(username=username, id=file_id, remove=False)
    if not cancelled:
        raise RuntimeError("cancel_download returned false")

    enqueue_payload = {"filename": filename}
    if size is not None:
        enqueue_payload["size"] = size

    enqueued = client.transfers.enqueue(username=username, files=[enqueue_payload])
    if not enqueued:
        raise RuntimeError("enqueue returned false")

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

def get_similar_artists(artist_name: str, limit: int = 10) -> list[str]:
    """Fetch similar artists from Last.fm API."""
    params = {
        "method": "artist.getSimilar",
        "artist": artist_name,
        "api_key": LASTFM_API_KEY,
        "format": "json",
        "limit": limit
    }
    
    try:
        print(f"{Color.DARKCYAN}[⚙️ DEBUG] Requesting similar artists for '{artist_name}' from Last.fm...{Color.END}")
        response = requests.get(LASTFM_BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if "error" in data:
            print(f"{Color.RED}❌ Last.fm Error: {data.get('message')}{Color.END}", file=sys.stderr)
            return []
            
        artists = data.get("similarartists", {}).get("artist", [])
        # Last.fm API ensures a list of artist objects
        if not isinstance(artists, list):
            print(f"{Color.YELLOW}[⚠️ DEBUG] Unexpected format for similar artists: {type(artists)}{Color.END}")
            return []
            
        return [a["name"] for a in artists]
        
    except requests.exceptions.RequestException as e:
        print(f"{Color.RED}❌ [ERROR] Last.fm API request failed (getSimilar): {e}{Color.END}", file=sys.stderr)
        return []

def get_top_albums(artist_name: str, limit: int = 5) -> list[str]:
    """Fetch top albums for an artist from Last.fm API."""
    params = {
        "method": "artist.getTopAlbums",
        "artist": artist_name,
        "api_key": LASTFM_API_KEY,
        "format": "json",
        "limit": limit
    }
    
    try:
        print(f"  {Color.DARKCYAN}[⚙️ DEBUG] Requesting top albums for '{artist_name}'...{Color.END}")
        response = requests.get(LASTFM_BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        albums = data.get("topalbums", {}).get("album", [])
        if not isinstance(albums, list):
            print(f"  {Color.YELLOW}[⚠️ DEBUG] Unexpected format for top albums: {type(albums)}{Color.END}")
            return []
            
        return [a["name"] for a in albums]
    except Exception as e:
        print(f"  {Color.RED}❌ [ERROR] Last.fm API request failed (getTopAlbums) for {artist_name}: {e}{Color.END}", file=sys.stderr)
        return []

def check_plex_for_album(artist: str, album: str) -> bool:
    """Check if the album by the artist exists on the Plex server."""
    if not PLEX_TOKEN:
        print(f"    {Color.YELLOW}[⚠️ DEBUG] Plex check skipped: PLEX_TOKEN not set{Color.END}")
        return False

    url = f"{PLEX_URL.rstrip('/')}/search"
    params = {
        "type": 9,
        "query": album,  # Searching by album title is more reliable than Artist+Album
        "X-Plex-Token": PLEX_TOKEN
    }
    headers = {"Accept": "application/json"}

    try:
        print(f"    {Color.DARKCYAN}[🏠 DEBUG] Searching Plex for Album: '{album}' (Artist: {artist}){Color.END}")
        response = requests.get(url, params=params, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        items = data.get("MediaContainer", {}).get("Metadata", [])

        def normalize(s: str) -> str:
            if not s: return ""
            s = s.lower().strip()
            # Remove content in parentheses/brackets (e.g. [FLAC], (Deluxe))
            s = re.sub(r'[\(\[].*?[\)\]]', '', s)
            if s.startswith("the "): s = s[4:]
            return re.sub(r'[\W_]+', '', s)

        norm_search_artist = normalize(artist)
        norm_search_album = normalize(album)

        for item in items:
            # In Plex (type 9), parentTitle is the Album Artist
            p_artist = normalize(item.get("parentTitle", ""))
            p_album = normalize(item.get("title", ""))
            o_artist = normalize(item.get("originalTitle", ""))

            # Flexible matching to handle differing metadata (e.g. "Artist feat. X" or "Album (Year)")
            artist_match = (norm_search_artist in p_artist or p_artist in norm_search_artist or 
                          norm_search_artist in o_artist or o_artist in norm_search_artist or
                          p_artist in ["variousartists", "various"])
            
            album_match = (norm_search_album in p_album or p_album in norm_search_album)

            if artist_match and album_match:
                print(f"    {Color.GREEN}[✨ DEBUG] Match confirmed on Plex: '{item.get('parentTitle')}' - '{item.get('title')}'{Color.END}")
                return True
        return False
    except Exception as e:
        print(f"    {Color.RED}❌ [ERROR] Plex search failed for {artist} - {album}: {e}{Color.END}", file=sys.stderr)
        return False

def download_from_slskd(username: str, files: list):
    """Enqueue a download using the slskd-api library."""
    try:
        print(f"    {Color.DARKCYAN}[🚀 DEBUG] Enqueuing {len(files)} files from {username} via library...{Color.END}")
        # The library handles the payload wrapping and endpoint routing correctly
        # It expects a list of dictionaries for the files
        formatted_files = [{"filename": f.get("filename"), "size": f.get("size")} for f in files]
        slskd_client.transfers.enqueue(username=username, files=formatted_files)
        print(f"    {Color.PURPLE}{Color.BOLD}📦 [slskd] Download started: {username} ({len(files)} files){Color.END}")
    except Exception as e:
        print(f"    {Color.RED}❌ [ERROR] slskd library failed to enqueue: {e}{Color.END}", file=sys.stderr)

def retry_failed_downloads():
    """Find all failed downloads in slskd and set them to retry."""
    try:
        all_transfers = slskd_client.transfers.get_all_downloads()
        print(f"{Color.DARKCYAN}[📊 RAW DEBUG] Transfers: {all_transfers}{Color.END}")
        retried_count = 0
        
        for transfer in all_transfers:
            username = transfer.get("username")
            files = transfer.get("files", [])
            
            for f in files:
                raw_state = f.get("state", "Unknown")
                state = str(raw_state).lower()
                
                # Check for any failure keywords in the state string
                is_stuck = any(x in state for x in ["error", "cancel", "timeout", "abort"])

                if is_stuck:
                    filename = os.path.basename(f.get("filename", "Unknown"))
                    print(f"    {Color.YELLOW}🔄 [slskd] Retrying: {filename} (State: {raw_state}){Color.END}")
                    if username:
                        retry_download(slskd_client, username, f)
                        retried_count += 1
        
        if retried_count > 0:
            print(f"{Color.CYAN}🔄 [slskd] Found {retried_count} failed downloads. Setting them to retry...{Color.END}")
    except Exception as e:
        print(f"{Color.RED}❌ [slskd] Could not retry failed downloads: {e}{Color.END}", file=sys.stderr)

def get_expected_track_count(artist: str, album: str) -> int:
    """Fetch the expected track count for an album from MusicBrainz."""
    url = "https://musicbrainz.org/ws/2/release"
    query = f'release:"{album}"'
    if artist.lower() not in ["various artists", "various", "va"]:
        query = f'artist:"{artist}" AND {query}'

    params = {"query": query, "fmt": "json", "limit": 1}
    headers = {"User-Agent": "SimilarArtistRinser/1.1 ( https://github.com/velkrosmaak )"}
    
    try:
        print(f"    {Color.DARKCYAN}[⚙️ DEBUG] Verifying expected track count for '{album}' via MusicBrainz...{Color.END}")
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        releases = data.get("releases", [])
        if releases:
            count = releases[0].get("track-count", 0)
            if count > 0:
                print(f"    {Color.DARKCYAN}[⚙️ DEBUG] MusicBrainz expects {count} tracks.{Color.END}")
                return count
    except Exception as e:
        print(f"    {Color.YELLOW}[⚠️ DEBUG] MusicBrainz track lookup failed: {e}{Color.END}")
    return 0

def trigger_slskd_search(artist: str, album: str):
    """Initiate a search on the slskd server for the given artist and album."""
    if not SLSKD_API_KEY:
        print("    [slskd] Skipping: SLSKD_API_KEY not set", file=sys.stderr)
        return

    try:
        print(f"    {Color.DARKCYAN}[🔍 DEBUG] Creating search via slskd library: {artist} {album}{Color.END}")

        expected_tracks = get_expected_track_count(artist, album)
        min_tracks = expected_tracks if expected_tracks > 0 else 3

        # Identify users currently in your transfer list to avoid over-burdening them
        busy_users = set()
        try:
            busy_users = {t.get("username") for t in slskd_client.transfers.get_all_downloads() if t.get("username")}
        except Exception:
            pass

        # Use the confirmed method 'search_text' with the camelCase keyword 'searchText'
        search = slskd_client.searches.search_text(searchText=f"{artist} {album}")
        search_id = search.get("id")
        print(f"    {Color.CYAN}📡 [slskd] Search {search_id} initiated. Waiting for results...{Color.END}")

        try:
            best_response = None
            frames = ["( •_•)>⌐■-■", "(⌐■_■)     ", "( •_•)>⌐■-■", "( •_•)     "]
            
            # Poll for results (Wait up to 40 seconds)
            for attempt in range(8):
                # 1. Fetch results from slskd (only once every 5 seconds)
                responses = slskd_client.searches.search_responses(id=search_id)
                if responses:
                    candidates = []
                    for resp in responses:
                        files = resp.get("files", [])
                        audio_files = [f for f in files if (f.get("filename") or "").lower().endswith(('.mp3', '.flac', '.m4a', '.wav'))]
                        if not audio_files: continue
                        
                        bitrate = 0
                        for f in audio_files:
                            try:
                                fb = int(f.get("bitRate") or f.get("bitrate") or 0)
                                if fb > bitrate: bitrate = fb
                            except (ValueError, TypeError): continue

                        sample = audio_files[0]
                        is_lossless = (sample.get("filename") or "").lower().endswith((".flac", ".wav"))
                        is_high_quality = is_lossless or (bitrate >= 320)
                        is_busy = resp.get("username") in busy_users

                        candidates.append({
                            "username": resp.get("username"),
                            "files": resp.get("files"),
                            "track_count": len(audio_files),
                            "score": (resp.get("hasFreeSlots", False), is_high_quality, not is_busy, bitrate, is_lossless)
                        })

                    if candidates:
                        candidates.sort(key=lambda x: x["score"], reverse=True)
                        best_response = next((c for c in candidates if c["track_count"] >= min_tracks), None)
                        # If we have a high-quality result with free slots, stop early
                        if best_response and best_response["score"][0] and best_response["score"][1]:
                            break

                # 2. Wait 5 seconds with animation and Skip-check
                for i in range(10):
                    remaining = 40 - (attempt * 5) - (i * 0.5)
                    frame = frames[i % len(frames)]
                    sys.stdout.write(f"\r    {Color.CYAN}⏳ [slskd] Digging... {frame} [{int(remaining)}s] - Press [s] to skip{Color.END}")
                    sys.stdout.flush()
                    
                    if check_skip(0.5):
                        print(f"\n    {Color.YELLOW}⏩ Skipping search for {artist} - {album}...{Color.END}")
                        return

            print("\r" + " " * 80 + "\r", end="", flush=True)
            
            if best_response:
                score = best_response["score"]
                q_desc = "Lossless" if score[4] else f"{score[3]}kbps"
                user_status = f"{Color.YELLOW}(Busy User){Color.DARKCYAN}" if best_response["username"] in busy_users else "(New User)"
                print(f"    {Color.DARKCYAN}[🎯 DEBUG] Picking best: User={best_response['username']} {user_status}, Slots={score[0]}, Quality={q_desc}{Color.END}")
                download_from_slskd(best_response["username"], best_response["files"])
            else:
                print(f"    {Color.YELLOW}⚠️ [slskd] No suitable results found for {artist} - {album}.{Color.END}")

        finally:
            # Always remove the search to keep slskd clean
            print(f"    {Color.DARKCYAN}[🧹 DEBUG] Removing search {search_id} from slskd...{Color.END}")
            slskd_client.searches.delete(id=search_id)

    except Exception as e:
        print(f"    {Color.RED}❌ [slskd] Failed to trigger search: {e}{Color.END}", file=sys.stderr)

def main():
    parser = argparse.ArgumentParser(description="List similar artists using Last.fm")
    parser.add_argument("artist", help="Name of the artist")
    parser.add_argument("--limit", type=int, default=10, help="Number of results (default 10)")
    parser.add_argument("--download", action="store_true", help="Trigger searches for these albums on slskd")
    parser.add_argument("--only-self", action="store_true", help="Only search for the artist provided, skipping similar artists")
    parser.add_argument("--include-self", action="store_true", help="Include the source artist in the search and download list")
    args = parser.parse_args()

    if LASTFM_API_KEY == "YOUR_API_KEY_HERE":
        print("Error: Please set your LASTFM_API_KEY environment variable or update config.py with your API key.")
        sys.exit(1)

    print(f"{Color.DARKCYAN}[⚙️ DEBUG] Target limit: {args.limit}")
    print(f"[⚙️ DEBUG] Download flag: {args.download}")
    print(f"[⚙️ DEBUG] slskd host: {SLSKD_URL}")
    print(f"[⚙️ DEBUG] Plex host: {PLEX_URL}{Color.END}")

    # Initial cleanup: Retry any previously failed downloads
    retry_failed_downloads()

    if args.only_self:
        print(f"\n{Color.BOLD}{Color.CYAN}🔎 Processing artist: '{args.artist}'...{Color.END}\n")
        similar = [args.artist]
    else:
        print(f"\n{Color.BOLD}{Color.CYAN}🔎 Searching for artists similar to '{args.artist}'...{Color.END}\n")
        similar = get_similar_artists(args.artist, args.limit)
        if args.include_self:
            similar.insert(0, args.artist)

    print("=" * 60)
    
    if not similar:
        print("No similar artists found or an error occurred.")
    else:
        for i, artist in enumerate(tqdm(similar, desc="🚀 Overall Progress", unit="artist"), 1):
            print(f"\n{Color.BOLD}{Color.PURPLE}🎧 {i:02d}. {artist}{Color.END}")
            albums = get_top_albums(artist, 5)
            if albums:
                for album in albums:
                    exists = check_plex_for_album(artist, album)
                    if exists:
                        print(f"    {Color.GREEN}✅ [Plex] {album}{Color.END}")
                    else:
                        print(f"    {Color.YELLOW}💿 [Last.fm] {album}{Color.END}")
                    
                    if args.download:
                        if not exists:
                            trigger_slskd_search(artist, album)
            else:
                print("    (No albums found)")

if __name__ == "__main__":
    main()
