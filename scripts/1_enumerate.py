#!/usr/bin/env python3
"""Stage 1 — Enumerate: seeds.txt + searches.txt → state/urls.jsonl

Collects video URLs from:
  - seeds.txt: channel/playlist/video URLs (expanded via yt-dlp --flat-playlist)
  - searches.txt: search queries via Kakao TV search API

Output: state/urls.jsonl  (one JSON object per line)
  {id, url, channel, channel_id, title, src}

Idempotent: skips IDs already in urls.jsonl on re-run.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import random
import logging
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
URLS_FILE = STATE_DIR / "urls.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("enumerate")

# ── config ──────────────────────────────────────────────────────────────────

def load_config():
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    return {}

CFG = load_config()
EXCLUDE_CHANNELS = set(CFG.get("exclude", {}).get("channels", []))
EXCLUDE_KEYWORDS = [kw.lower() for kw in CFG.get("exclude", {}).get("keywords", ["뉴스", "news"])]
SEARCH_CFG = CFG.get("search", {})
THROTTLE = CFG.get("throttle", {})
SLEEP_MIN = THROTTLE.get("sleep_min", 2)
SLEEP_MAX = THROTTLE.get("sleep_max", 6)

# Keys to look for video list in search API response (adjust after checking _debug dump)
LIST_KEYS = ["clipList", "clips", "list", "items", "documents"]

# ── helpers ─────────────────────────────────────────────────────────────────

def sleep_polite():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

def load_existing_ids() -> set:
    ids = set()
    if URLS_FILE.exists():
        with open(URLS_FILE) as f:
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

def append_entry(entry: dict):
    with open(URLS_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def should_exclude(title: str, channel: str) -> bool:
    if channel and channel in EXCLUDE_CHANNELS:
        return True
    title_lower = (title or "").lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw in title_lower:
            return True
    return False

def make_video_url(clip_id, channel_id=None):
    if channel_id:
        return f"https://tv.kakao.com/channel/{channel_id}/cliplink/{clip_id}"
    return f"https://tv.kakao.com/v/{clip_id}"

# ── seeds processing (yt-dlp) ──────────────────────────────────────────────

def is_single_video(url: str) -> bool:
    return bool(re.search(r"/v/\d+|/cliplink/\d+", url))

def extract_id_from_url(url: str) -> str | None:
    m = re.search(r"(?:/v/|/cliplink/)(\d+)", url)
    return m.group(1) if m else None

def process_seeds(seeds_path: str, existing_ids: set) -> list[dict]:
    if not os.path.exists(seeds_path):
        log.warning(f"seeds file not found: {seeds_path}")
        return []

    urls = []
    with open(seeds_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)

    if not urls:
        log.info("No seed URLs to process")
        return []

    results = []
    for url in urls:
        if is_single_video(url):
            vid = extract_id_from_url(url)
            if vid and vid in existing_ids:
                log.info(f"  skip (already have): {vid}")
                continue
            entry = {"id": vid, "url": url, "channel": None, "channel_id": None, "title": None, "src": "seed"}
            results.append(entry)
            existing_ids.add(vid)
            append_entry(entry)
            log.info(f"  added seed video: {vid}")
        else:
            # Channel or playlist — expand with yt-dlp
            log.info(f"Expanding seed: {url}")
            try:
                proc = subprocess.run(
                    ["yt-dlp", "--flat-playlist", "-J", url],
                    capture_output=True, text=True, timeout=120,
                )
                if proc.returncode != 0:
                    log.error(f"  yt-dlp error for {url}: {proc.stderr[:500]}")
                    continue
                data = json.loads(proc.stdout)
                entries = data.get("entries", [])
                log.info(f"  found {len(entries)} entries in {url}")
                for e in entries:
                    eid = str(e.get("id", ""))
                    if not eid or eid in existing_ids:
                        continue
                    title = e.get("title", "")
                    channel = e.get("uploader", e.get("channel", ""))
                    channel_id = e.get("channel_id", "")
                    if should_exclude(title, channel):
                        log.info(f"  excluded: {eid} ({title})")
                        continue
                    video_url = e.get("url") or e.get("webpage_url") or make_video_url(eid, channel_id)
                    entry = {
                        "id": eid,
                        "url": video_url,
                        "channel": channel,
                        "channel_id": channel_id,
                        "title": title,
                        "src": "seed",
                    }
                    results.append(entry)
                    existing_ids.add(eid)
                    append_entry(entry)
                sleep_polite()
            except subprocess.TimeoutExpired:
                log.error(f"  yt-dlp timed out for {url}")
            except Exception as exc:
                log.error(f"  error expanding {url}: {exc}")
    return results

# ── search API ──────────────────────────────────────────────────────────────

SEARCH_URL = "https://tv.kakao.com/api/v1/ft/search/cliplinks"

def normalize_item(item: dict) -> dict | None:
    """Extract id/channel/title from a search API clip item.
    Adjust field names here if the API schema differs from expectations."""
    clip_id = item.get("id") or item.get("clipLinkId") or item.get("cliplink_id")
    if not clip_id:
        return None
    clip_id = str(clip_id)

    channel_info = item.get("channel") or {}
    channel_id = str(channel_info.get("id", "")) if isinstance(channel_info, dict) else ""
    channel_name = channel_info.get("name", "") if isinstance(channel_info, dict) else str(channel_info)

    title = item.get("title") or item.get("displayTitle") or ""

    return {
        "id": clip_id,
        "url": make_video_url(clip_id, channel_id),
        "channel": channel_name,
        "channel_id": channel_id,
        "title": title,
        "src": "search",
    }

def find_clip_list(data: dict) -> list | None:
    """Try known keys to find the clip array in the search response."""
    for key in LIST_KEYS:
        if key in data and isinstance(data[key], list):
            return data[key]
    # Try nested: data might wrap in a container
    for val in data.values():
        if isinstance(val, dict):
            for key in LIST_KEYS:
                if key in val and isinstance(val[key], list):
                    return val[key]
    return None

def process_searches(searches_path: str, existing_ids: set,
                     size: int = 20, max_pages: int = 200,
                     cookie_header: str | None = None) -> list[dict]:
    if not os.path.exists(searches_path):
        log.warning(f"searches file not found: {searches_path}")
        return []

    queries = []
    with open(searches_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            queries.append(line)

    if not queries:
        log.info("No search queries to process")
        return []

    session = requests.Session()
    session.headers.update({
        "x-requested-with": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
    })
    if cookie_header:
        session.headers["Cookie"] = cookie_header

    results = []
    for query in queries:
        log.info(f"Searching: {query}")
        session.headers["referer"] = f"https://tv.kakao.com/search/cliplinks?q={query}"
        page = 1
        consecutive_empty = 0

        while page <= max_pages:
            params = {
                "sort": "Score",
                "q": query,
                "fulllevels": "list",
                "fields": "-user,-clipChapterThumbnailList,-tagList",
                "size": size,
                "page": page,
                "_": int(time.time() * 1000),
            }
            try:
                resp = session.get(SEARCH_URL, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.error(f"  search API error page {page}: {exc}")
                break

            # Debug dump: first page of each query
            if page == 1:
                debug_path = STATE_DIR / f"_debug_search_{query.replace(' ', '_')}.json"
                with open(debug_path, "w") as df:
                    json.dump(data, df, ensure_ascii=False, indent=2)
                log.info(f"  debug dump: {debug_path}")

            clips = find_clip_list(data)
            if clips is None:
                log.warning(f"  could not find clip list in response (page {page}). "
                            f"Check _debug_search_*.json and adjust LIST_KEYS.")
                break

            if not clips:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    log.info(f"  no more results for '{query}' at page {page}")
                    break
                page += 1
                sleep_polite()
                continue

            consecutive_empty = 0
            added_this_page = 0
            for item in clips:
                norm = normalize_item(item)
                if not norm or not norm["id"]:
                    continue
                if norm["id"] in existing_ids:
                    continue
                if should_exclude(norm["title"], norm["channel"]):
                    log.info(f"  excluded: {norm['id']} ({norm['title']})")
                    continue
                results.append(norm)
                existing_ids.add(norm["id"])
                append_entry(norm)
                added_this_page += 1

            log.info(f"  page {page}: {len(clips)} clips, {added_this_page} new")
            page += 1
            sleep_polite()

    return results

# ── main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stage 1: Enumerate Kakao TV videos")
    parser.add_argument("--seeds", default=str(ROOT / "seeds.txt"))
    parser.add_argument("--searches", default=str(ROOT / "searches.txt"))
    parser.add_argument("--size", type=int, default=SEARCH_CFG.get("size", 20),
                        help="Search API page size")
    parser.add_argument("--max-pages", type=int, default=SEARCH_CFG.get("max_pages", 200),
                        help="Max search pages per query")
    parser.add_argument("--cookie-header", default=None,
                        help="Cookie header for logged-in content")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    existing_ids = load_existing_ids()
    log.info(f"Existing IDs in urls.jsonl: {len(existing_ids)}")

    # Seeds
    log.info("=== Processing seeds ===")
    seed_results = process_seeds(args.seeds, existing_ids)
    log.info(f"Seeds: {len(seed_results)} new entries")

    # Searches
    log.info("=== Processing searches ===")
    search_results = process_searches(
        args.searches, existing_ids,
        size=args.size, max_pages=args.max_pages,
        cookie_header=args.cookie_header,
    )
    log.info(f"Searches: {len(search_results)} new entries")

    total = len(seed_results) + len(search_results)
    total_all = len(load_existing_ids())
    log.info(f"=== Done: {total} new, {total_all} total in urls.jsonl ===")

if __name__ == "__main__":
    main()
