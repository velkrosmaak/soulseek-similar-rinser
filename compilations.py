#!/usr/bin/env python3
"""
similar-artists/compilation_rinser.py
Find all releases in a compilation series via MusicBrainz and download missing ones via slskd.
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

from config import PLEX_TOKEN, PLEX_URL, SLSKD_API_KEY, SLSKD_URL, create_slskd_client

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

def get_compilation_releases(series_name: str) -> list[dict]:
    """Fetch releases in a series from MusicBrainz."""
    print(f"{Color.DARKCYAN}[⚙️ DEBUG] Querying MusicBrainz for series: '{series_name}'...{Color.END}")
    
    url = "https://musicbrainz.org/ws/2/release"
    # We query the series field. Limit to 100 for a thorough grab.
    params = {
        "query": f'series:"{series_name}"',
        "fmt": "json",
        "limit": 100
    }
    # MusicBrainz requires a User-Agent
    headers = {"User-Agent": "CompilationRinser/1.0.0 ( https://github.com/velkrosmaak )"}
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        releases = data.get("releases", [])
        found = []
        seen = set()

        for r in releases:
            title = r.get("title")
            # Join artist credits (e.g., "Fabric 01: Craig Richards")
            artist = " ".join([a.get("name", "") for a in r.get("artist-credit", [])])
            
            # De-duplicate based on title to avoid grabbing every regional variation
            norm_title = title.lower().strip()
            if norm_title not in seen:
                found.append({"artist": artist, "title": title})
                seen.add(norm_title)
                
        return found
    except Exception as e:
        print(f"{Color.RED}❌ MusicBrainz API request failed: {e}{Color.END}")
        return []

def check_plex_for_album(artist: str, album: str) -> bool:
    """Check if the album exists on Plex (using normalization logic from sa.py)."""
    url = f"{PLEX_URL.rstrip('/')}/search"
    params = {"type": 9, "query": album, "X-Plex-Token": PLEX_TOKEN}
    headers = {"Accept": "application/json"}

    try:
        print(f"    {Color.DARKCYAN}[🏠 DEBUG] Searching Plex for Album: '{album}' (Artist: {artist} / Various Artists){Color.END}")
        response = requests.get(url, params=params, headers=headers, timeout=5)
        data = response.json()
        items = data.get("MediaContainer", {}).get("Metadata", [])

        def normalize(s: str) -> str:
            if not s: return ""
            s = s.lower().strip()
            # Remove content in parentheses/brackets (e.g. [FLAC], (Deluxe))
            s = re.sub(r'[\(\[].*?[\)\]]', '', s)
            if s.startswith("the "): s = s[4:]
            return re.sub(r'[\W_]+', '', s)

        n_artist, n_album, n_various = normalize(artist), normalize(album), normalize("Various Artists")

        for item in items:
            p_artist = normalize(item.get("parentTitle", ""))
            p_album = normalize(item.get("title", ""))
            o_artist = normalize(item.get("originalTitle", ""))

            # Match artist: allow matches for the specific artist OR "Various Artists"
            artist_match = (n_artist in p_artist or p_artist in n_artist or 
                          n_artist in o_artist or o_artist in n_artist or
                          n_various in p_artist or p_artist in n_various or
                          p_artist in ["various", "va"])
            
            # Match album: allow partials (e.g. "Album" matching "Album (Deluxe)")
            album_match = (n_album in p_album or p_album in n_album)

            if artist_match and album_match:
                print(f"    {Color.GREEN}[✨ DEBUG] Match confirmed on Plex: '{item.get('parentTitle')}' - '{item.get('title')}'{Color.END}")
                return True
        return False
    except:
        return False

def get_expected_track_count(artist: str, album: str) -> int:
    """Fetch the expected track count for an album from MusicBrainz."""
    url = "https://musicbrainz.org/ws/2/release"
    # Query for the specific artist and album to find the canonical track count
    query = f'release:"{album}"'
    if artist.lower() not in ["various artists", "various", "va"]:
        query = f'artist:"{artist}" AND {query}'

    params = {"query": query, "fmt": "json", "limit": 1}
    headers = {"User-Agent": "CompilationRinser/1.1 ( https://github.com/velkrosmaak )"}
    
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

def trigger_slskd_search(artist: str, album: str, used_users: set, destination: str = None):
    """Search and download the best quality album with free slots."""
    try:
        expected_tracks = get_expected_track_count(artist, album)
        # Use found count or fallback to 3 as a safety minimum
        min_tracks = expected_tracks if expected_tracks > 0 else 3

        # Identify busy users
        busy_users = set()
        try:
            busy_users = {t.get("username") for t in slskd_client.transfers.get_all_downloads() if t.get("username")}
        except: pass

        search = slskd_client.searches.search_text(searchText=f"{artist} {album}")
        search_id = search.get("id")

        try:
            best_response = None
            frames = ["( •_•)>⌐■-■", "(⌐■_■)     ", "( •_•)>⌐■-■", "( •_•)     "]

            for attempt in range(8):
                # Poll slskd every 5 seconds
                responses = slskd_client.searches.search_responses(id=search_id)
                if responses:
                    candidates = []
                    for resp in responses:
                        files = resp.get("files", [])
                        audio_files = [f for f in files if (f.get("filename") or "").lower().endswith(('.mp3', '.flac', '.m4a'))]
                        if len(audio_files) < min_tracks: continue

                        bitrate = 0
                        for f in audio_files:
                            try:
                                fb = int(f.get("bitRate") or f.get("bitrate") or 0)
                                if fb > bitrate: bitrate = fb
                            except: continue

                        is_lossless = any((f.get("filename") or "").lower().endswith(".flac") for f in audio_files)
                        is_high_quality = is_lossless or (bitrate >= 320)
                        candidates.append({
                            "username": resp.get("username"),
                            "files": files,
                            "track_count": len(audio_files),
                            "score": (resp.get("hasFreeSlots", False), is_high_quality, resp.get("username") not in busy_users, bitrate, is_lossless)
                        })

                    if candidates:
                        # Prefer users we haven't used yet in this run
                        fresh = [c for c in candidates if c["username"] not in used_users]
                        reused = [c for c in candidates if c["username"] in used_users]

                        ordered = fresh + reused
                        ordered.sort(key=lambda x: x["score"], reverse=True)

                        best_response = next((c for c in ordered if c["track_count"] >= min_tracks), None)
                        if best_response and best_response["score"][0] and best_response["score"][1]:
                            break

                # Animation sub-loop (checks skip every 0.5s)
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
                print(f"    {Color.DARKCYAN}[🎯 DEBUG] Picking: User={best_response['username']}, Slots={score[0]}, Quality={q_desc}{Color.END}")
                formatted_files = [{"filename": f.get("filename"), "size": f.get("size")} for f in best_response["files"]]

                if destination:
                    user = best_response["username"]
                    encoded_user = requests.utils.quote(user)
                    enqueue_url = f"{SLSKD_URL.rstrip('/')}/api/v0/transfers/downloads/{encoded_user}"
                    headers = {"X-API-Key": SLSKD_API_KEY, "Content-Type": "application/json"}
                    payload = {"files": formatted_files}
                    params = {"destination": destination}

                    try:
                        response = requests.post(enqueue_url, json=payload, params=params, headers=headers, timeout=10)
                        if response.status_code >= 400:
                            print(f"    {Color.YELLOW}⚠️ [slskd] Custom enqueue failed ({response.status_code}). Falling back...{Color.END}")
                            slskd_client.transfers.enqueue(username=user, files=formatted_files)
                        else:
                            print(f"    {Color.PURPLE}📦 [slskd] Download started into: {destination}{Color.END}")
                    except Exception as e:
                        print(f"    {Color.YELLOW}⚠️ [slskd] Custom request error: {e}. Falling back...{Color.END}")
                        slskd_client.transfers.enqueue(username=user, files=formatted_files)
                else:
                    slskd_client.transfers.enqueue(username=best_response["username"], files=formatted_files)
                    print(f"    {Color.PURPLE}📦 [slskd] Download started.{Color.END}")

                used_users.add(best_response["username"])
            else:
                print(f"    {Color.YELLOW}⚠️ No suitable results found.{Color.END}")
        finally:
            slskd_client.searches.delete(id=search_id)

    except Exception as e:
        print(f"    {Color.RED}❌ slskd search failed: {e}{Color.END}")

def retry_failed_downloads():
    """Cleanup failed downloads on start."""
    try:
        all_transfers = slskd_client.transfers.get_all_downloads()
        print(f"{Color.DARKCYAN}[📊 RAW DEBUG] Transfers: {all_transfers}{Color.END}")
        for transfer in all_transfers:
            username = transfer.get("username")
            for f in transfer.get("files", []):
                raw_state = f.get("state", "Unknown")
                state = str(raw_state).lower()
                
                if any(x in state for x in ["error", "cancel", "timeout", "abort"]):
                    print(f"    {Color.YELLOW}🔄 [slskd] Retrying failed file: {os.path.basename(f.get('filename', 'Unknown'))} ({raw_state}){Color.END}")
                    retry_download(slskd_client, username, f)
    except: pass

def main():
    parser = argparse.ArgumentParser(description="Download a whole compilation series.")
    parser.add_argument("compilation", help="Series name (e.g., 'Fabric', 'Back to Mine')")
    parser.add_argument("--download", action="store_true", help="Trigger downloads")
    args = parser.parse_args()
    used_users = set()

    retry_failed_downloads()

    releases = get_compilation_releases(args.compilation)
    # print(releases)
    # print("------------------------------------------------")
    if not releases:
        print(f"{Color.RED}No releases found for '{args.compilation}'.{Color.END}")
        return

    print(f"\n{Color.BOLD}{Color.CYAN}🚀 Processing {len(releases)} albums from the '{args.compilation}' series...{Color.END}\n")
    print("=" * 60)
    destination = f"Compilations/{args.compilation}"
    print(f"    {Color.DARKCYAN}📂 Subdirectory: {destination}{Color.END}\n")

    for i, rel in enumerate(releases, 1):
        artist = rel['artist']
        title = rel['title']
        
        print(f"\n{Color.BOLD}{Color.PURPLE}💿 {i:02d}. {artist} - {title}{Color.END}")
        
        exists = check_plex_for_album(artist, title)
        if exists:
            print(f"    {Color.GREEN}✅ [Plex] Already exists.{Color.END}")
        else:
            print(f"    {Color.YELLOW}❌ [Plex] Missing.{Color.END}")
            if args.download:
                trigger_slskd_search(artist, title, used_users, destination=destination)

if __name__ == "__main__":
    main()
