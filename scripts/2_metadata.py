#!/usr/bin/env python3
"""Stage 2 — Metadata: urls.jsonl → state/catalog.jsonl

For each video in urls.jsonl, fetch metadata via yt-dlp (no download)
and write normalized records to catalog.jsonl.

Idempotent: skips IDs already in catalog.jsonl.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import random
import logging
import csv
from pathlib import Path

import yaml
from tqdm import tqdm

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
URLS_FILE = STATE_DIR / "urls.jsonl"
CATALOG_FILE = STATE_DIR / "catalog.jsonl"
FAILED_FILE = STATE_DIR / "failed.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("metadata")

def load_config():
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    return {}

CFG = load_config()
THROTTLE = CFG.get("throttle", {})
SLEEP_MIN = THROTTLE.get("sleep_min", 2)
SLEEP_MAX = THROTTLE.get("sleep_max", 6)

def sleep_polite():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

def load_done_ids() -> set:
    ids = set()
    if CATALOG_FILE.exists():
        with open(CATALOG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ids.add(str(obj.get("id", "")))
                except json.JSONDecodeError:
                    pass
    return ids

def load_failed_ids() -> set:
    ids = set()
    if FAILED_FILE.exists():
        with open(FAILED_FILE) as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 2 and row[1] == "metadata":
                    ids.add(row[0])
    return ids

def record_failure(url: str, error: str):
    write_header = not FAILED_FILE.exists()
    with open(FAILED_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["url", "stage", "error"])
        writer.writerow([url, "metadata", error[:500]])

def normalize_meta(info: dict) -> dict:
    """Normalize yt-dlp info JSON to our catalog schema."""
    formats = []
    for fmt in info.get("formats", []):
        f = {}
        if fmt.get("format_id"):
            f["format_id"] = fmt["format_id"]
        if fmt.get("height"):
            f["height"] = fmt["height"]
        if fmt.get("ext"):
            f["ext"] = fmt["ext"]
        if fmt.get("vcodec"):
            f["vcodec"] = fmt["vcodec"]
        if f:
            formats.append(f)

    return {
        "id": str(info.get("id", "")),
        "title": info.get("title"),
        "channel": info.get("uploader") or info.get("channel"),
        "channel_id": info.get("uploader_id") or info.get("channel_id") or "",
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "thumbnail": info.get("thumbnail"),
        "description": info.get("description"),
        "webpage_url": info.get("webpage_url"),
        "formats_available": formats if formats else None,
        "comments": "N/A: 카카오TV 댓글 서비스 2024-07 종료",
    }

def fetch_metadata(url: str) -> dict | None:
    """Fetch metadata for a single video via yt-dlp."""
    try:
        proc = subprocess.run(
            ["yt-dlp", "--skip-download", "-J", url],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip()[:500]
            log.warning(f"  yt-dlp failed: {err}")
            record_failure(url, err)
            return None
        info = json.loads(proc.stdout)
        return normalize_meta(info)
    except subprocess.TimeoutExpired:
        log.warning(f"  yt-dlp timed out for {url}")
        record_failure(url, "timeout")
        return None
    except json.JSONDecodeError as e:
        log.warning(f"  JSON decode error: {e}")
        record_failure(url, f"json_decode: {e}")
        return None
    except Exception as e:
        log.warning(f"  unexpected error: {e}")
        record_failure(url, str(e))
        return None

def main():
    parser = argparse.ArgumentParser(description="Stage 2: Fetch metadata for enumerated videos")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Also retry previously failed items")
    args = parser.parse_args()

    if not URLS_FILE.exists():
        log.error(f"urls.jsonl not found. Run Stage 1 first.")
        sys.exit(1)

    # Load all URLs
    entries = []
    with open(URLS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    done_ids = load_done_ids()
    failed_ids = load_failed_ids() if not args.retry_failed else set()
    skip_ids = done_ids | failed_ids

    todo = [e for e in entries if str(e.get("id", "")) not in skip_ids]
    log.info(f"Total URLs: {len(entries)}, already done: {len(done_ids)}, "
             f"failed (skip): {len(failed_ids)}, to process: {len(todo)}")

    if not todo:
        log.info("Nothing to do.")
        return

    success = 0
    fail = 0
    for entry in tqdm(todo, desc="Fetching metadata"):
        url = entry.get("url", "")
        vid = str(entry.get("id", ""))
        if not url:
            continue

        meta = fetch_metadata(url)
        if meta:
            # Ensure ID is set even if yt-dlp didn't return it
            if not meta["id"]:
                meta["id"] = vid
            with open(CATALOG_FILE, "a") as f:
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            success += 1
        else:
            fail += 1

        sleep_polite()

    log.info(f"=== Done: {success} success, {fail} failed, "
             f"{len(done_ids) + success} total in catalog.jsonl ===")

if __name__ == "__main__":
    main()
