#!/usr/bin/env python3
"""
soulseek-similar-rinser/retry_cron.py
Automated retry script for failed or stuck slskd downloads.
"""

import logging
import os
import re
import time
from collections import Counter
from config import create_slskd_client

# search for alternatives for stuck or just queued downloads

# Setup logging
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "retry_cron.log")

class Color:
    CYAN = '\033[96m'
    DARKCYAN = '\033[36m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    END = '\033[0m'

# Configure logging to both console and file
logger = logging.getLogger("RetryCron")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(formatter)
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setFormatter(formatter)
logger.addHandler(ch)

GENERIC_PATH_PARTS = {
    "complete",
    "downloads",
    "incomplete",
    "music",
    "nicotine+",
    "shared",
    "various",
    "various artists",
}


def normalize_path(path):
    return (path or "").replace("\\", "/")


def infer_album_context(file_info):
    """Infer album/artist/search text from a transfer file path."""
    full_path = normalize_path(file_info.get("filename"))
    parts = [part.strip() for part in full_path.split("/") if part.strip()]
    track_name = parts[-1] if parts else "Unknown"
    album = parts[-2] if len(parts) >= 2 else "Unknown"
    parent = parts[-3] if len(parts) >= 3 else ""

    stem = os.path.splitext(track_name)[0]
    stem = re.sub(r"^\d+\s*", "", stem).strip()
    track_artist = stem.split(" - ", 1)[0].strip() if " - " in stem else ""

    parent_lc = parent.lower().strip()
    if parent and parent_lc not in GENERIC_PATH_PARTS:
        artist = parent
    elif track_artist:
        artist = track_artist
    else:
        artist = album

    search_text = album
    if artist and artist.lower() not in album.lower():
        search_text = f"{artist} {album}"

    album_key = normalize_path("/".join(parts[:-1])) or full_path or track_name
    return {
        "album": album,
        "artist": artist,
        "search_text": search_text,
        "album_key": album_key,
        "track_name": track_name,
    }


def infer_expected_album_tracks(transfer, file_info):
    """Infer expected track count from the current transfer directory when possible."""
    target_path = normalize_path(file_info.get("filename"))
    target_dir = normalize_path(os.path.dirname(target_path))

    for directory in transfer.get("directories", []) or []:
        remote_dir = normalize_path(directory.get("directory"))
        files = directory.get("files", []) or []
        if remote_dir == target_dir and files:
            return max(1, len(files))

    return 3


def choose_best_album_match(client, search_text, min_tracks=3):
    """Search slskd for a replacement album, strongly preferring no queue."""
    busy_users = set()
    try:
        busy_users = {
            t.get("username")
            for t in client.transfers.get_all_downloads()
            if t.get("username")
        }
    except Exception:
        pass

    search = client.searches.search_text(searchText=search_text)
    search_id = search.get("id")

    try:
        best_response = None
        for attempt in range(6):
            if attempt:
                time.sleep(5)

            responses = client.searches.search_responses(id=search_id)
            if not responses:
                continue

            candidates = []
            for resp in responses:
                files = resp.get("files", [])
                audio_files = [
                    f for f in files
                    if (f.get("filename") or "").lower().endswith((".mp3", ".flac", ".m4a", ".wav"))
                ]
                if len(audio_files) < min_tracks:
                    continue

                bitrate = 0
                for audio_file in audio_files:
                    try:
                        bitrate = max(
                            bitrate,
                            int(audio_file.get("bitRate") or audio_file.get("bitrate") or 0),
                        )
                    except (TypeError, ValueError):
                        continue

                is_lossless = any(
                    (audio_file.get("filename") or "").lower().endswith((".flac", ".wav"))
                    for audio_file in audio_files
                )
                has_free_slot = bool(
                    resp.get("hasFreeUploadSlot", resp.get("hasFreeSlots", False))
                )
                queue_length = int(resp.get("queueLength") or 0)
                username = resp.get("username")
                is_high_quality = is_lossless or bitrate >= 320

                candidates.append({
                    "username": username,
                    "files": files,
                    "track_count": len(audio_files),
                    "queue_length": queue_length,
                    "has_free_slot": has_free_slot,
                    "bitrate": bitrate,
                    "is_lossless": is_lossless,
                    "score": (
                        int(has_free_slot),
                        -queue_length,
                        int(is_high_quality),
                        int(username not in busy_users),
                        bitrate,
                        int(is_lossless),
                        len(audio_files),
                    ),
                })

            if candidates:
                candidates.sort(key=lambda c: c["score"], reverse=True)
                best_response = candidates[0]
                if best_response["has_free_slot"] and best_response["queue_length"] == 0:
                    break

        return best_response
    finally:
        if search_id:
            client.searches.delete(id=search_id)


def enqueue_album_match(client, match):
    formatted_files = [
        {"filename": f.get("filename"), "size": f.get("size")}
        for f in match["files"]
    ]
    return client.transfers.enqueue(username=match["username"], files=formatted_files)

def retry_download(client, username, file_info):
    """Retry a download using the best available API for this slskd client."""
    file_id = file_info.get("id")
    filename = file_info.get("filename")
    size = file_info.get("size")

    if hasattr(client.transfers, "retry"):
        client.transfers.retry(username=username, id=file_id)
        return "direct"

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

    return "cancel+enqueue"

def main():
    logger.info(f"{Color.BOLD}{Color.CYAN}Starting slskd maintenance check...{Color.END}")

    try:
        client = create_slskd_client()
        all_transfers = client.transfers.get_all_downloads()

        if isinstance(all_transfers, dict):
            transfers_list = list(all_transfers.values())
        else:
            transfers_list = all_transfers

        logger.info(f"Processing {len(transfers_list)} users with active transfers...")

        successful_states = {
            "Completed, Succeeded",
            "Queued, Remotely",
        }

        def should_retry(state_str):
            return str(state_str).strip() not in successful_states

        retried_count = 0
        fallback_search_count = 0
        fallback_enqueued_count = 0
        total_files = 0
        retry_candidates = 0
        state_counts = Counter()
        searched_album_keys = set()

        for transfer in transfers_list:
            username = transfer.get("username")
            # Robustly collect files from both the root and nested directories
            files = transfer.get("files") or []
            if not files and "directories" in transfer:
                for d in transfer.get("directories", []):
                    files.extend(d.get("files", []))

            for f in files:
                raw_state = f.get("state", "Unknown")
                total_files += 1
                state_counts[str(raw_state).strip() or "Unknown"] += 1
                full_path = f.get("filename") or "Unknown"
                filename = full_path.replace('\\', '/').split('/')[-1]

                if should_retry(raw_state):
                    retry_candidates += 1
                    logger.info(f"{Color.YELLOW}🔄 Retrying: {filename} (User: {username}, State: {raw_state}){Color.END}")
                    try:
                        retry_method = retry_download(client, username, f)
                        retried_count += 1
                    except Exception as retry_err:
                        logger.error(
                            f"{Color.RED}❌ Failed to retry file {f.get('id', 'unknown')}: "
                            f"{retry_err}{Color.END}"
                        )
                        context = infer_album_context(f)
                        if context["album_key"] in searched_album_keys:
                            logger.info(
                                f"{Color.DARKCYAN}Debug: fallback search already attempted "
                                f"for {context['album']}{Color.END}"
                            )
                            continue

                        searched_album_keys.add(context["album_key"])
                        fallback_search_count += 1
                        logger.info(
                            f"{Color.YELLOW}🔎 Fallback search: artist={context['artist']} "
                            f"album={context['album']} query={context['search_text']}{Color.END}"
                        )
                        try:
                            min_tracks = infer_expected_album_tracks(transfer, f)
                            match = choose_best_album_match(
                                client,
                                context["search_text"],
                                min_tracks=min_tracks,
                            )
                            if not match:
                                logger.warning(
                                    f"{Color.YELLOW}⚠️ No suitable replacement found for "
                                    f"{context['artist']} - {context['album']}{Color.END}"
                                )
                                continue

                            enqueued = enqueue_album_match(client, match)
                            if not enqueued:
                                raise RuntimeError("enqueue returned false")

                            fallback_enqueued_count += 1
                            quality = "Lossless" if match["is_lossless"] else f"{match['bitrate']}kbps"
                            logger.info(
                                f"{Color.GREEN}📦 Fallback enqueued: {context['artist']} - "
                                f"{context['album']} from {match['username']} "
                                f"(queue={match['queue_length']}, quality={quality}, "
                                f"tracks={match['track_count']}, min={min_tracks}){Color.END}"
                            )
                        except Exception as search_err:
                            logger.error(
                                f"{Color.RED}❌ Fallback search failed for "
                                f"{context['artist']} - {context['album']}: "
                                f"{search_err}{Color.END}"
                            )
                    else:
                        logger.info(
                            f"{Color.DARKCYAN}Debug: retry method={retry_method} "
                            f"file={filename}{Color.END}"
                        )

        top_states = ", ".join(
            f"{state}={count}" for state, count in state_counts.most_common(6)
        )
        logger.info(
            f"{Color.DARKCYAN}Debug: scanned {total_files} files, "
            f"{retry_candidates} retry candidates, "
            f"{fallback_search_count} fallback searches, "
            f"{fallback_enqueued_count} fallback enqueues. States: {top_states}{Color.END}"
        )

        if retried_count > 0:
            logger.info(f"{Color.GREEN}✅ Successfully triggered {retried_count} retries.{Color.END}")
        else:
            logger.info("Nothing to retry.")

    except Exception as e:
        logger.error(f"{Color.RED}🔥 Critical error during slskd maintenance: {e}{Color.END}")

if __name__ == "__main__":
    main()
