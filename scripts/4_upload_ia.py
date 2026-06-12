#!/usr/bin/env python3
"""Stage 4 — Upload to Internet Archive

Uploads downloaded videos+metadata to archive.org.
Uses state/done_upload.txt for idempotency.
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
import random
from pathlib import Path

import yaml
from tqdm import tqdm

try:
    import internetarchive
except ImportError:
    print("Install internetarchive: pip install internetarchive")
    print("Then configure: ia configure")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"
CATALOG_FILE = STATE_DIR / "catalog.jsonl"
DONE_FILE = STATE_DIR / "done_upload.txt"
FAILED_FILE = STATE_DIR / "failed.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("upload_ia")

def load_config():
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    return {}

CFG = load_config()
IA_CFG = CFG.get("ia", {})
COLLECTION = IA_CFG.get("collection", "opensource_movies")
THROTTLE = CFG.get("throttle", {})
SLEEP_MIN = THROTTLE.get("sleep_min", 2)
SLEEP_MAX = THROTTLE.get("sleep_max", 6)

def sleep_polite():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

def load_done_ids() -> set:
    ids = set()
    if DONE_FILE.exists():
        with open(DONE_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    ids.add(line)
    return ids

def mark_done(identifier: str):
    with open(DONE_FILE, "a") as f:
        f.write(identifier + "\n")

def record_failure(url: str, error: str):
    write_header = not FAILED_FILE.exists()
    with open(FAILED_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["url", "stage", "error"])
        writer.writerow([url, "upload", error[:500]])

def sanitize_identifier(channel_id: str, video_id: str) -> str:
    """Create a valid IA identifier: lowercase, alphanumeric + hyphens."""
    raw = f"kakaotv-{channel_id}-{video_id}"
    ident = re.sub(r"[^a-z0-9-]", "-", raw.lower())
    ident = re.sub(r"-+", "-", ident).strip("-")
    return ident[:100]  # IA limit

def load_catalog() -> dict:
    """Load catalog.jsonl as a dict keyed by video id."""
    catalog = {}
    if CATALOG_FILE.exists():
        with open(CATALOG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    catalog[str(obj.get("id", ""))] = obj
                except json.JSONDecodeError:
                    pass
    return catalog

def find_files(channel_id: str, video_id: str) -> list[Path]:
    """Find all files for a video in data/."""
    cid = channel_id or "unknown_channel"
    video_dir = DATA_DIR / cid / video_id
    if not video_dir.exists():
        return []
    return [f for f in video_dir.iterdir() if f.is_file()]

def upload_item(video_id: str, channel_id: str, meta: dict) -> bool:
    identifier = sanitize_identifier(channel_id or "unknown", video_id)
    files = find_files(channel_id, video_id)

    if not files:
        log.warning(f"  no files found for {video_id}")
        return False

    title = meta.get("title", f"Kakao TV - {video_id}")
    upload_date = meta.get("upload_date", "")
    date_str = ""
    if upload_date and len(upload_date) == 8:
        date_str = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

    webpage_url = meta.get("webpage_url", f"https://tv.kakao.com/v/{video_id}")
    channel_name = meta.get("channel", "Unknown")
    description = meta.get("description", "")

    md = {
        "title": title,
        "collection": COLLECTION,
        "mediatype": "movies",
        "creator": channel_name,
        "subject": ["kakaotv", "archive", "korea", "카카오TV"],
        "originalurl": webpage_url,
        "description": (
            f"Backed up from Kakao TV before 2026-06-30 shutdown.\n"
            f"Original: {webpage_url}\n\n"
            f"{description or ''}"
        ).strip(),
    }
    if date_str:
        md["date"] = date_str

    file_dict = {f.name: str(f) for f in files}

    try:
        responses = internetarchive.upload(
            identifier,
            files=file_dict,
            metadata=md,
            retries=3,
            retries_sleep=10,
        )
        # Check responses
        all_ok = all(r.status_code == 200 for r in responses)
        if all_ok:
            mark_done(identifier)
            log.info(f"  uploaded: {identifier}")
            return True
        else:
            errs = [f"{r.status_code}" for r in responses if r.status_code != 200]
            err_msg = f"upload_partial_failure: {','.join(errs)}"
            log.warning(f"  {err_msg}")
            record_failure(webpage_url, err_msg)
            return False
    except Exception as e:
        log.warning(f"  upload error: {e}")
        record_failure(webpage_url, str(e))
        return False

def main():
    parser = argparse.ArgumentParser(description="Stage 4: Upload to Internet Archive")
    parser.add_argument("--limit", type=int, default=0, help="Limit uploads (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be uploaded")
    args = parser.parse_args()

    catalog = load_catalog()
    if not catalog:
        log.error("catalog.jsonl empty or not found. Run Stage 2 first.")
        sys.exit(1)

    done_ids = load_done_ids()

    # Find videos that have files downloaded but not yet uploaded
    todo = []
    for vid, meta in catalog.items():
        channel_id = meta.get("channel_id", "") or ""
        identifier = sanitize_identifier(channel_id or "unknown", vid)
        if identifier in done_ids:
            continue
        files = find_files(channel_id, vid)
        if files:
            todo.append((vid, channel_id, meta))

    if args.limit:
        todo = todo[:args.limit]

    log.info(f"Catalog: {len(catalog)}, already uploaded: {len(done_ids)}, to upload: {len(todo)}")

    if args.dry_run:
        for vid, cid, meta in todo:
            ident = sanitize_identifier(cid or "unknown", vid)
            log.info(f"  would upload: {ident} — {meta.get('title', '?')}")
        return

    if not todo:
        log.info("Nothing to upload.")
        return

    success = 0
    fail = 0
    for vid, channel_id, meta in tqdm(todo, desc="Uploading"):
        ok = upload_item(vid, channel_id, meta)
        if ok:
            success += 1
        else:
            fail += 1
        sleep_polite()

    log.info(f"=== Done: {success} uploaded, {fail} failed ===")

if __name__ == "__main__":
    main()
