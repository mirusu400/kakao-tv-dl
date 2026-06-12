#!/usr/bin/env python3
"""Stage 3 — Download: videos + thumbnails + info.json

Downloads video files for each entry in urls.jsonl using yt-dlp.
Uses --download-archive for idempotency.
Verifies downloaded file duration against metadata.
"""

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import time
import random
from pathlib import Path

import yaml
from tqdm import tqdm

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"
URLS_FILE = STATE_DIR / "urls.jsonl"
CATALOG_FILE = STATE_DIR / "catalog.jsonl"
ARCHIVE_FILE = STATE_DIR / "done_download.txt"
FAILED_FILE = STATE_DIR / "failed.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("download")

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
MAX_HEIGHT = CFG.get("quality", {}).get("max_height", 1080)

def sleep_polite():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

def record_failure(url: str, error: str):
    write_header = not FAILED_FILE.exists()
    with open(FAILED_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["url", "stage", "error"])
        writer.writerow([url, "download", error[:500]])

def load_catalog_durations() -> dict:
    """Load expected durations from catalog.jsonl for verification."""
    durations = {}
    if CATALOG_FILE.exists():
        with open(CATALOG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    vid = str(obj.get("id", ""))
                    dur = obj.get("duration")
                    if vid and dur:
                        durations[vid] = dur
                except json.JSONDecodeError:
                    pass
    return durations

def load_archive_ids() -> set:
    """Load IDs from yt-dlp download archive file."""
    ids = set()
    if ARCHIVE_FILE.exists():
        with open(ARCHIVE_FILE) as f:
            for line in f:
                # yt-dlp format: "extractor id"
                parts = line.strip().split()
                if len(parts) >= 2:
                    ids.add(parts[1])
    return ids

def get_file_duration(filepath: str) -> float | None:
    """Get duration of a media file using ffprobe."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", filepath],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            dur = data.get("format", {}).get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return None

def download_video(url: str, vid: str, channel_id: str) -> bool:
    """Download a single video with yt-dlp."""
    cid = channel_id or "unknown_channel"
    output_template = str(DATA_DIR / f"{cid}/{vid}/video.%(ext)s")

    format_sel = f"bestvideo[height<={MAX_HEIGHT}]+bestaudio/best[height<={MAX_HEIGHT}]/best"

    cmd = [
        "yt-dlp",
        "-f", format_sel,
        "--write-info-json",
        "--write-thumbnail",
        "--write-description",
        "--merge-output-format", "mp4",
        "--no-overwrites",
        "--continue",
        "--retries", "5",
        "--fragment-retries", "10",
        "--sleep-interval", str(SLEEP_MIN),
        "--max-sleep-interval", str(SLEEP_MAX),
        "--concurrent-fragments", "4",
        "-o", output_template,
        "--download-archive", str(ARCHIVE_FILE),
        url,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            err = proc.stderr.strip()[-500:]
            # Check if it was just "already in archive"
            if "has already been recorded" in (proc.stdout + proc.stderr):
                return True
            log.warning(f"  yt-dlp error: {err}")
            record_failure(url, err)
            return False
        return True
    except subprocess.TimeoutExpired:
        log.warning(f"  download timed out (10min): {url}")
        record_failure(url, "download_timeout_600s")
        return False
    except Exception as e:
        log.warning(f"  download error: {e}")
        record_failure(url, str(e))
        return False

def verify_duration(vid: str, channel_id: str, expected_duration: float | None) -> bool:
    """Check if downloaded file duration roughly matches expected."""
    if expected_duration is None:
        return True  # Can't verify without expected duration

    cid = channel_id or "unknown_channel"
    video_dir = DATA_DIR / cid / vid
    if not video_dir.exists():
        return False

    for f in video_dir.iterdir():
        if f.name.startswith("video.") and f.suffix in (".mp4", ".mkv", ".webm"):
            actual = get_file_duration(str(f))
            if actual is None:
                return True  # Can't verify, assume ok
            # Allow 10% tolerance
            if abs(actual - expected_duration) > max(expected_duration * 0.1, 5):
                log.warning(f"  duration mismatch for {vid}: "
                            f"expected {expected_duration}s, got {actual}s "
                            f"(possible ad download?)")
                return False
            return True
    return True

def main():
    parser = argparse.ArgumentParser(description="Stage 3: Download videos")
    parser.add_argument("--verify", action="store_true", default=True,
                        help="Verify file duration against metadata (default: on)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip duration verification")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limit number of downloads (0=unlimited)")
    args = parser.parse_args()

    if not URLS_FILE.exists():
        log.error("urls.jsonl not found. Run Stage 1 first.")
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Load entries
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

    archive_ids = load_archive_ids()
    durations = load_catalog_durations()

    todo = [e for e in entries if str(e.get("id", "")) not in archive_ids]
    if args.limit:
        todo = todo[:args.limit]

    log.info(f"Total: {len(entries)}, already done: {len(archive_ids)}, to download: {len(todo)}")

    if not todo:
        log.info("Nothing to download.")
        return

    success = 0
    fail = 0
    for entry in tqdm(todo, desc="Downloading"):
        url = entry.get("url", "")
        vid = str(entry.get("id", ""))
        channel_id = entry.get("channel_id", "") or ""

        if not url:
            continue

        ok = download_video(url, vid, channel_id)
        if ok:
            # Duration verification
            if not args.no_verify:
                expected_dur = durations.get(vid)
                if not verify_duration(vid, channel_id, expected_dur):
                    log.warning(f"  {vid}: duration mismatch — flagged in failed.csv")
                    record_failure(url, "duration_mismatch_possible_ad")
                    fail += 1
                    continue
            success += 1
        else:
            fail += 1

    log.info(f"=== Done: {success} success, {fail} failed, "
             f"{len(archive_ids) + success} total downloaded ===")

if __name__ == "__main__":
    main()
