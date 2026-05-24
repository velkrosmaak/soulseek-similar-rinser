#!/usr/bin/env python3
"""
soulseek-similar-rinser/retry_cron.py
Automated retry script for failed or stuck slskd downloads.
"""

import logging
import os
from collections import Counter
from config import create_slskd_client

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
        total_files = 0
        retry_candidates = 0
        state_counts = Counter()

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
            f"{retry_candidates} retry candidates. States: {top_states}{Color.END}"
        )

        if retried_count > 0:
            logger.info(f"{Color.GREEN}✅ Successfully triggered {retried_count} retries.{Color.END}")
        else:
            logger.info("Nothing to retry.")

    except Exception as e:
        logger.error(f"{Color.RED}🔥 Critical error during slskd maintenance: {e}{Color.END}")

if __name__ == "__main__":
    main()
