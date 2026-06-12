#!/usr/bin/env python3
"""카카오TV 아카이브 — 올인원 파이프라인

한 번에 실행: 열거 → 메타데이터 → 다운로드 → 오프라인 사이트 빌드

사용법:
    python main.py                          # 전체 파이프라인
    python main.py --skip-download          # 열거+메타만 (목록 확보)
    python main.py --skip-site              # 사이트 빌드 생략
    python main.py --limit 10              # 다운로드 수 제한
    python main.py --only enumerate        # 특정 단계만
    python main.py --only metadata
    python main.py --only download
    python main.py --only site
"""

import argparse
import csv
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from curl_cffi import requests as cffi_requests
import yaml
from tqdm import tqdm

# ── 경로 ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
URLS_FILE = STATE_DIR / "urls.jsonl"
CATALOG_FILE = STATE_DIR / "catalog.jsonl"
ARCHIVE_FILE = STATE_DIR / "done_download.txt"
FAILED_FILE = STATE_DIR / "failed.csv"

# ── 로깅 ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kakao-tv-dl")

# ── config ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
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
MAX_HEIGHT = CFG.get("quality", {}).get("max_height", 1080)

# 검색 API에서 클립 배열을 찾을 키 후보
LIST_KEYS = ["clipList", "clips", "list", "items", "documents"]

# ── 공통 유틸 ───────────────────────────────────────────────────────────────

def sleep_polite():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

def should_exclude(title: str, channel: str) -> bool:
    if channel and channel in EXCLUDE_CHANNELS:
        return True
    title_lower = (title or "").lower()
    return any(kw in title_lower for kw in EXCLUDE_KEYWORDS)

def make_video_url(clip_id, channel_id=None):
    if channel_id:
        return f"https://tv.kakao.com/channel/{channel_id}/cliplink/{clip_id}"
    return f"https://tv.kakao.com/v/{clip_id}"

def record_failure(url: str, stage: str, error: str):
    write_header = not FAILED_FILE.exists()
    with open(FAILED_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["url", "stage", "error"])
        w.writerow([url, stage, error[:500]])

def load_jsonl(path: Path) -> list[dict]:
    items = []
    if path.exists():
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return items

def load_ids_from_jsonl(path: Path, key: str = "id") -> set:
    return {str(obj.get(key, "")) for obj in load_jsonl(path)}

def append_jsonl(path: Path, obj: dict):
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 1 — ENUMERATE
# ═══════════════════════════════════════════════════════════════════════════

def is_single_video(url: str) -> bool:
    return bool(re.search(r"/v/\d+|/cliplink/\d+", url))

def extract_id_from_url(url: str) -> str | None:
    m = re.search(r"(?:/v/|/cliplink/)(\d+)", url)
    return m.group(1) if m else None

def _process_seeds(seeds_path: str, existing_ids: set) -> int:
    if not os.path.exists(seeds_path):
        log.warning(f"seeds 파일 없음: {seeds_path}")
        return 0

    urls = []
    with open(seeds_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    if not urls:
        return 0

    added = 0
    for url in urls:
        if is_single_video(url):
            vid = extract_id_from_url(url)
            if vid and vid in existing_ids:
                continue
            entry = {"id": vid, "url": url, "channel": None,
                     "channel_id": None, "title": None, "src": "seed"}
            existing_ids.add(vid)
            append_jsonl(URLS_FILE, entry)
            added += 1
            log.info(f"  + seed 영상: {vid}")
        else:
            log.info(f"  채널/플레이리스트 펼침: {url}")
            try:
                proc = subprocess.run(
                    ["yt-dlp", "--flat-playlist", "-J", url],
                    capture_output=True, text=True, timeout=120,
                )
                if proc.returncode != 0:
                    log.error(f"  yt-dlp 에러: {proc.stderr[:300]}")
                    continue
                data = json.loads(proc.stdout)
                entries = data.get("entries", [])
                log.info(f"  {len(entries)}개 영상 발견")
                for e in entries:
                    eid = str(e.get("id", ""))
                    if not eid or eid in existing_ids:
                        continue
                    title = e.get("title", "")
                    channel = e.get("uploader", e.get("channel", ""))
                    channel_id = e.get("channel_id", "")
                    if should_exclude(title, channel):
                        continue
                    video_url = (e.get("url") or e.get("webpage_url")
                                 or make_video_url(eid, channel_id))
                    entry = {"id": eid, "url": video_url, "channel": channel,
                             "channel_id": channel_id, "title": title, "src": "seed"}
                    existing_ids.add(eid)
                    append_jsonl(URLS_FILE, entry)
                    added += 1
                sleep_polite()
            except subprocess.TimeoutExpired:
                log.error(f"  타임아웃: {url}")
            except Exception as exc:
                log.error(f"  에러: {exc}")
    return added

SEARCH_URL = "https://tv.kakao.com/api/v1/ft/search/cliplinks"

def _normalize_search_item(item: dict) -> dict | None:
    clip_id = item.get("id") or item.get("clipLinkId") or item.get("cliplink_id")
    if not clip_id:
        return None
    clip_id = str(clip_id)
    ch = item.get("channel") or {}
    channel_id = str(ch.get("id", "")) if isinstance(ch, dict) else ""
    channel_name = ch.get("name", "") if isinstance(ch, dict) else str(ch)
    title = item.get("title") or item.get("displayTitle") or ""
    return {"id": clip_id, "url": make_video_url(clip_id, channel_id),
            "channel": channel_name, "channel_id": channel_id,
            "title": title, "src": "search"}

def _find_clip_list(data: dict) -> list | None:
    for key in LIST_KEYS:
        if key in data and isinstance(data[key], list):
            return data[key]
    for val in data.values():
        if isinstance(val, dict):
            for key in LIST_KEYS:
                if key in val and isinstance(val[key], list):
                    return val[key]
    return None

def _process_searches(searches_path: str, existing_ids: set,
                      size: int, max_pages: int, cookie_header: str | None) -> int:
    if not os.path.exists(searches_path):
        log.warning(f"searches 파일 없음: {searches_path}")
        return 0

    queries = []
    with open(searches_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                queries.append(line)
    if not queries:
        return 0

    session = cffi_requests.Session(impersonate="chrome")
    session.headers.update({
        "x-requested-with": "XMLHttpRequest",
    })
    if cookie_header:
        session.headers["Cookie"] = cookie_header

    added = 0
    for query in queries:
        log.info(f"  검색: {query}")
        from urllib.parse import quote
        session.headers["referer"] = f"https://tv.kakao.com/search/cliplinks?q={quote(query)}"
        page = 1
        empty_streak = 0
        while page <= max_pages:
            params = {"sort": "Score", "q": query, "fulllevels": "list",
                      "fields": "-user,-clipChapterThumbnailList,-tagList",
                      "size": size, "page": page, "_": int(time.time() * 1000)}
            try:
                resp = session.get(SEARCH_URL, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.error(f"  검색 API 에러 (page {page}): {exc}")
                break

            if page == 1:
                dp = STATE_DIR / f"_debug_search_{query.replace(' ', '_')}.json"
                with open(dp, "w") as df:
                    json.dump(data, df, ensure_ascii=False, indent=2)

            clips = _find_clip_list(data)
            if clips is None:
                log.warning(f"  클립 배열을 찾을 수 없음 (page {page}). "
                            f"_debug_search_*.json 확인 후 LIST_KEYS 조정 필요")
                break
            if not clips:
                empty_streak += 1
                if empty_streak >= 2:
                    log.info(f"  '{query}' 결과 끝 (page {page})")
                    break
                page += 1
                sleep_polite()
                continue

            page_added = 0
            for item in clips:
                norm = _normalize_search_item(item)
                if not norm or not norm["id"] or norm["id"] in existing_ids:
                    continue
                if should_exclude(norm["title"], norm["channel"]):
                    continue
                existing_ids.add(norm["id"])
                append_jsonl(URLS_FILE, norm)
                added += 1
                page_added += 1
            log.info(f"  page {page}: {len(clips)}건, {page_added}건 신규")
            if page_added == 0:
                empty_streak += 1
                if empty_streak >= 3:
                    log.info(f"  신규 0건 3회 연속 — '{query}' 종료")
                    break
            else:
                empty_streak = 0
            page += 1
            sleep_polite()
    return added

def stage_enumerate(args):
    log.info("=" * 60)
    log.info("STAGE 1 — 열거 (enumerate)")
    log.info("=" * 60)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    existing_ids = load_ids_from_jsonl(URLS_FILE)
    log.info(f"기존 URL: {len(existing_ids)}개")

    seeds_added = _process_seeds(args.seeds, existing_ids)
    log.info(f"Seeds → {seeds_added}건 추가")

    search_added = _process_searches(
        args.searches, existing_ids,
        size=args.size, max_pages=args.max_pages,
        cookie_header=args.cookie_header)
    log.info(f"검색 → {search_added}건 추가")

    total = len(load_ids_from_jsonl(URLS_FILE))
    log.info(f"열거 완료: 신규 {seeds_added + search_added}, 전체 {total}건")
    return total

# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 2 — METADATA
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_meta(info: dict) -> dict:
    formats = []
    for fmt in info.get("formats", []):
        f = {}
        if fmt.get("format_id"): f["format_id"] = fmt["format_id"]
        if fmt.get("height"): f["height"] = fmt["height"]
        if fmt.get("ext"): f["ext"] = fmt["ext"]
        if fmt.get("vcodec"): f["vcodec"] = fmt["vcodec"]
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
        "formats_available": formats or None,
        "comments": "N/A: 카카오TV 댓글 서비스 2024-07 종료",
    }

def _fetch_metadata(url: str, max_retries: int = 5) -> dict | None:
    for attempt in range(1, max_retries + 1):
        try:
            proc = subprocess.run(
                ["yt-dlp", "--skip-download", "-J", url],
                capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                err = proc.stderr.strip()[:500]
                is_server_error = any(code in err for code in ["502", "503", "429"])
                if is_server_error and attempt < max_retries:
                    wait = 2 ** attempt + random.uniform(0, 2)
                    log.warning(f"  메타 재시도 {attempt}/{max_retries} ({wait:.0f}s 대기): {err[:80]}")
                    time.sleep(wait)
                    continue
                log.warning(f"  메타 실패: {err[:120]}")
                record_failure(url, "metadata", err)
                return None
            return _normalize_meta(json.loads(proc.stdout))
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                log.warning(f"  메타 타임아웃 재시도 {attempt}/{max_retries}")
                time.sleep(2 ** attempt)
                continue
            record_failure(url, "metadata", "timeout")
            return None
        except Exception as e:
            record_failure(url, "metadata", str(e))
            return None
    return None

def stage_metadata(args):
    log.info("=" * 60)
    log.info("STAGE 2 — 메타데이터 수집")
    log.info("=" * 60)

    if not URLS_FILE.exists():
        log.error("urls.jsonl 없음 — Stage 1을 먼저 실행하세요")
        return 0

    entries = load_jsonl(URLS_FILE)
    done_ids = load_ids_from_jsonl(CATALOG_FILE)
    todo = [e for e in entries if str(e.get("id", "")) not in done_ids]
    if args.limit:
        todo = todo[:args.limit]
    log.info(f"전체 {len(entries)}, 완료 {len(done_ids)}, 대상 {len(todo)}")

    if not todo:
        log.info("메타데이터 수집할 항목 없음")
        return len(done_ids)

    success = fail = 0
    for entry in tqdm(todo, desc="메타데이터"):
        url = entry.get("url", "")
        vid = str(entry.get("id", ""))
        if not url:
            continue
        meta = _fetch_metadata(url)
        if meta:
            if not meta["id"]:
                meta["id"] = vid
            append_jsonl(CATALOG_FILE, meta)
            success += 1
        else:
            fail += 1
        sleep_polite()

    total = len(done_ids) + success
    log.info(f"메타데이터 완료: 성공 {success}, 실패 {fail}, 전체 {total}")
    return total

# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 3 — DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════

def _load_archive_ids() -> set:
    ids = set()
    if ARCHIVE_FILE.exists():
        with open(ARCHIVE_FILE) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2:
                    ids.add(parts[1])
    return ids

def _get_file_duration(filepath: str) -> float | None:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", filepath],
            capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            dur = json.loads(proc.stdout).get("format", {}).get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return None

def _download_one(url: str, vid: str, channel_id: str) -> bool:
    cid = channel_id or "unknown_channel"
    output_template = str(DATA_DIR / f"{cid}/{vid}/video.%(ext)s")
    fmt = f"bestvideo[height<={MAX_HEIGHT}]+bestaudio/best[height<={MAX_HEIGHT}]/best"
    cmd = [
        "yt-dlp", "-f", fmt,
        "--write-info-json", "--write-thumbnail", "--write-description",
        "--merge-output-format", "mp4",
        "--no-overwrites", "--continue",
        "--retries", "5", "--fragment-retries", "10",
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
            combined = proc.stdout + proc.stderr
            if "has already been recorded" in combined:
                return True
            record_failure(url, "download", proc.stderr.strip()[-500:])
            return False
        return True
    except subprocess.TimeoutExpired:
        record_failure(url, "download", "timeout_600s")
        return False
    except Exception as e:
        record_failure(url, "download", str(e))
        return False

def _verify_duration(vid: str, channel_id: str, expected: float | None) -> bool:
    if expected is None:
        return True
    cid = channel_id or "unknown_channel"
    vdir = DATA_DIR / cid / vid
    if not vdir.exists():
        return False
    for f in vdir.iterdir():
        if f.name.startswith("video.") and f.suffix in (".mp4", ".mkv", ".webm"):
            actual = _get_file_duration(str(f))
            if actual is None:
                return True
            if abs(actual - expected) > max(expected * 0.1, 5):
                log.warning(f"  길이 불일치 {vid}: 예상 {expected}s, 실제 {actual}s")
                return False
            return True
    return True

def stage_download(args):
    log.info("=" * 60)
    log.info("STAGE 3 — 다운로드")
    log.info("=" * 60)

    if not URLS_FILE.exists():
        log.error("urls.jsonl 없음")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entries = load_jsonl(URLS_FILE)
    archive_ids = _load_archive_ids()

    # catalog에서 기대 길이 로드
    durations = {}
    for obj in load_jsonl(CATALOG_FILE):
        vid = str(obj.get("id", ""))
        dur = obj.get("duration")
        if vid and dur:
            durations[vid] = dur

    todo = [e for e in entries if str(e.get("id", "")) not in archive_ids]
    if args.limit:
        todo = todo[:args.limit]

    log.info(f"전체 {len(entries)}, 완료 {len(archive_ids)}, 대상 {len(todo)}")
    if not todo:
        log.info("다운로드할 항목 없음")
        return

    success = fail = 0
    for entry in tqdm(todo, desc="다운로드"):
        url = entry.get("url", "")
        vid = str(entry.get("id", ""))
        cid = entry.get("channel_id", "") or ""
        if not url:
            continue
        ok = _download_one(url, vid, cid)
        if ok:
            expected_dur = durations.get(vid)
            if not _verify_duration(vid, cid, expected_dur):
                record_failure(url, "download", "duration_mismatch_possible_ad")
                fail += 1
                continue
            success += 1
        else:
            fail += 1

    log.info(f"다운로드 완료: 성공 {success}, 실패 {fail}, "
             f"전체 {len(archive_ids) + success}")

# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 5 — BUILD HTML (data/<cid>/<vid>/video.html, 카카오TV 스타일)
# ═══════════════════════════════════════════════════════════════════════════

import base64
import mimetypes

# 카카오TV 로고 SVG (인라인)
KAKAO_TV_LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 40" width="140" height="28"><text x="0" y="30" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif" font-size="28" font-weight="800" fill="#1e1e1e" letter-spacing="-1">kakao</text><rect x="120" y="6" width="50" height="28" rx="14" fill="#fae100"/><text x="130" y="27" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif" font-size="17" font-weight="800" fill="#1e1e1e">tv</text></svg>'

DETAIL_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ v.title|e }} - kakaoTV</title>
<style>
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Malgun Gothic", "맑은 고딕", "Segoe UI", sans-serif;
  background: #f5f5f5; color: #1e1e1e; line-height: 1.5;
}
a { color: inherit; text-decoration: none; }

/* ── Header (카카오TV 상단 바) ── */
.gnb {
  background: #fff; border-bottom: 1px solid #e5e5e5;
  height: 56px; display: flex; align-items: center;
  padding: 0 24px; position: sticky; top: 0; z-index: 100;
}
.gnb-logo { display: flex; align-items: center; }
.gnb-search {
  margin-left: auto; display: flex; align-items: center;
  background: #f5f5f5; border-radius: 20px; padding: 6px 16px; width: 300px;
}
.gnb-search svg { width: 18px; height: 18px; fill: #999; flex-shrink: 0; }
.gnb-search span { margin-left: 8px; color: #999; font-size: 14px; }

/* ── 채널 바 ── */
.channel-bar {
  background: #fff; border-bottom: 1px solid #e5e5e5;
  padding: 0 24px; display: flex; align-items: center; height: 52px;
  max-width: 1200px; margin: 0 auto;
}
.channel-name {
  font-size: 17px; font-weight: 700; color: #1e1e1e;
  padding-bottom: 2px; border-bottom: 3px solid #fae100;
}

/* ── 메인 컨테이너 ── */
.container { max-width: 860px; margin: 0 auto; padding: 0; background: #fff; }

/* ── 비디오 플레이어 ── */
.player-wrap { background: #000; width: 100%; position: relative; }
.player-wrap video {
  width: 100%; display: block; max-height: 480px; background: #000;
}
.player-wrap .no-video {
  padding: 120px 20px; text-align: center; color: #888;
  font-size: 15px; background: #000;
}
.player-wrap img.poster {
  width: 100%; display: block; max-height: 480px; object-fit: contain; background: #000;
}

/* ── 영상 정보 ── */
.clip-title {
  font-size: 17px; font-weight: 400; color: #1e1e1e;
  padding: 20px 24px 8px; line-height: 1.4;
}
.clip-meta {
  padding: 0 24px 16px; font-size: 13px; color: #999;
  display: flex; align-items: center; gap: 4px;
}
.clip-meta .sep { margin: 0 2px; }

.divider { height: 1px; background: #e5e5e5; margin: 0 24px; }

/* ── 설명 ── */
.clip-desc {
  padding: 16px 24px; font-size: 14px; color: #555;
  white-space: pre-wrap; word-break: break-word; line-height: 1.7;
}

/* ── 댓글 공지 ── */
.comments-notice {
  margin: 0 24px 16px; padding: 14px 16px;
  background: #f9f9f9; border: 1px solid #eee; border-radius: 6px;
  font-size: 13px; color: #999;
}

/* ── 원본 링크 ── */
.original-link {
  padding: 0 24px 24px; font-size: 13px; color: #999;
}
.original-link a { color: #3c78c8; }
.original-link a:hover { text-decoration: underline; }

/* ── 아카이브 배너 ── */
.archive-banner {
  background: #fff8d6; border-top: 1px solid #f0e6a0;
  padding: 12px 24px; font-size: 12px; color: #8a7a2a; text-align: center;
}

/* ── 푸터 ── */
.footer {
  background: #fff; border-top: 1px solid #e5e5e5;
  padding: 24px; text-align: center; font-size: 12px; color: #999;
  max-width: 860px; margin: 0 auto;
}
</style>
</head>
<body>

<!-- 상단 GNB -->
<div class="gnb">
  <div class="gnb-logo">{{ logo_svg }}</div>
  <div class="gnb-search">
    <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
    <span>검색</span>
  </div>
</div>

<!-- 채널 바 -->
<div class="channel-bar">
  <span class="channel-name">{{ v.channel|e or '알 수 없음' }}</span>
</div>

<!-- 메인 -->
<div class="container">
  <!-- 플레이어 -->
  <div class="player-wrap">
  {% if v.has_video %}
    <video controls preload="metadata"{% if v.thumb_b64 %} poster="data:{{ v.thumb_mime }};base64,{{ v.thumb_b64 }}"{% endif %}>
      <source src="video.mp4" type="video/mp4">
    </video>
  {% elif v.thumb_b64 %}
    <img class="poster" src="data:{{ v.thumb_mime }};base64,{{ v.thumb_b64 }}" alt="">
  {% else %}
    <div class="no-video">영상 파일 없음</div>
  {% endif %}
  </div>

  <!-- 제목 -->
  <div class="clip-title">{{ v.title|e }}</div>

  <!-- 메타 정보 -->
  <div class="clip-meta">
    {% if v.view_count %}<span>재생수 {{ "{:,}".format(v.view_count) }}</span><span class="sep">&middot;</span>{% endif %}
    {% if v.upload_date %}<span>{{ v.upload_date[:4] }}.{{ v.upload_date[4:6] }}.{{ v.upload_date[6:8] }}</span>{% endif %}
    {% if v.duration %}<span class="sep">&middot;</span><span>{{ "%02d"|format(v.duration // 3600) }}:{{ "%02d"|format((v.duration % 3600) // 60) }}:{{ "%02d"|format(v.duration % 60) }}</span>{% endif %}
  </div>

  <div class="divider"></div>

  {% if v.description %}
  <div class="clip-desc">{{ v.description|e }}</div>
  <div class="divider"></div>
  {% endif %}

  <!-- 댓글 -->
  <div class="comments-notice">
    댓글 서비스가 2024년 7월에 종료되어 더 이상 제공되지 않습니다.
  </div>

  <!-- 원본 링크 -->
  <div class="original-link">
    원본 URL: <a href="{{ v.webpage_url }}" target="_blank" rel="noopener">{{ v.webpage_url }}</a>
  </div>
</div>

<!-- 아카이브 배너 -->
<div class="archive-banner">
  이 페이지는 카카오TV 서비스 종료(2026-06-30) 전 아카이빙 목적으로 생성되었습니다.
</div>

<!-- 푸터 -->
<div class="footer">
  Archived from kakaoTV &middot; Original &copy; Kakao Corp.
</div>

</body>
</html>"""

def _find_video_file(cid: str, vid: str) -> str | None:
    vdir = DATA_DIR / (cid or "unknown_channel") / vid
    if not vdir.exists():
        return None
    for f in vdir.iterdir():
        if f.name.startswith("video.") and f.suffix in (".mp4", ".mkv", ".webm"):
            return f.name
    return None

def _find_thumb_file(cid: str, vid: str) -> str | None:
    vdir = DATA_DIR / (cid or "unknown_channel") / vid
    if not vdir.exists():
        return None
    for f in vdir.iterdir():
        if not f.name.startswith("video.") and f.suffix in (".jpg", ".jpeg", ".png", ".webp"):
            return f.name
        if "thumb" in f.name.lower() and not f.name.startswith("video."):
            return f.name
    return None

def _encode_thumb_b64(cid: str, vid: str) -> tuple[str, str]:
    """Read thumbnail and return (base64_string, mime_type). Empty if not found."""
    tf = _find_thumb_file(cid, vid)
    if not tf:
        return "", ""
    thumb_path = DATA_DIR / (cid or "unknown_channel") / vid / tf
    if not thumb_path.exists():
        return "", ""
    mime = mimetypes.guess_type(str(thumb_path))[0] or "image/png"
    try:
        data = thumb_path.read_bytes()
        return base64.b64encode(data).decode("ascii"), mime
    except Exception:
        return "", ""

def stage_build_site(args):
    log.info("=" * 60)
    log.info("STAGE 5 — HTML 빌드 (data/ 내 video.html)")
    log.info("=" * 60)

    from jinja2 import Environment, BaseLoader

    if not CATALOG_FILE.exists():
        log.error("catalog.jsonl 없음")
        return

    videos = load_jsonl(CATALOG_FILE)
    if not videos:
        log.error("카탈로그가 비어 있음")
        return

    log.info(f"카탈로그 {len(videos)}건 로드")
    env = Environment(loader=BaseLoader())
    tmpl = env.from_string(DETAIL_TEMPLATE)

    built = 0
    for v in tqdm(videos, desc="HTML 빌드"):
        vid = str(v.get("id", ""))
        cid = v.get("channel_id", "") or "unknown_channel"

        video_dir = DATA_DIR / cid / vid
        video_dir.mkdir(parents=True, exist_ok=True)

        # 파일 존재 확인
        vf = _find_video_file(cid, vid)
        v["has_video"] = vf is not None

        # 썸네일 base64 인코딩
        thumb_b64, thumb_mime = _encode_thumb_b64(cid, vid)
        v["thumb_b64"] = thumb_b64
        v["thumb_mime"] = thumb_mime

        if v.get("duration"):
            v["duration"] = int(v["duration"])
        if v.get("view_count"):
            v["view_count"] = int(v["view_count"])

        html = tmpl.render(v=v, logo_svg=KAKAO_TV_LOGO_SVG)
        (video_dir / "video.html").write_text(html, encoding="utf-8")
        built += 1

    log.info(f"HTML 빌드 완료: {built}개 video.html 생성")
    if built > 0:
        # 첫 번째 예시 경로 출력
        sample = videos[0]
        scid = sample.get("channel_id", "") or "unknown_channel"
        svid = str(sample.get("id", ""))
        log.info(f"예시: {DATA_DIR / scid / svid / 'video.html'}")

# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="카카오TV 아카이브 — 올인원 파이프라인",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="예시:\n"
               "  python main.py                    # 전체 파이프라인\n"
               "  python main.py --only enumerate   # 열거만\n"
               "  python main.py --skip-download     # 다운로드 생략\n"
               "  python main.py --limit 5          # 다운로드 5개만\n",
    )
    parser.add_argument("--seeds", default=str(ROOT / "seeds.txt"))
    parser.add_argument("--searches", default=str(ROOT / "searches.txt"))
    parser.add_argument("--size", type=int, default=SEARCH_CFG.get("size", 20),
                        help="검색 API 페이지 크기 (기본 20)")
    parser.add_argument("--max-pages", type=int, default=SEARCH_CFG.get("max_pages", 200),
                        help="검색어당 최대 페이지 (기본 200)")
    parser.add_argument("--cookie-header", default=None,
                        help="로그인 콘텐츠용 Cookie 헤더")
    parser.add_argument("--limit", type=int, default=0,
                        help="다운로드 수 제한 (0=무제한)")
    parser.add_argument("--skip-download", action="store_true",
                        help="다운로드 생략 (열거+메타만)")
    parser.add_argument("--skip-site", action="store_true",
                        help="사이트 빌드 생략")
    parser.add_argument("--only", choices=["enumerate", "metadata", "download", "site"],
                        help="특정 단계만 실행")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if args.only:
        if args.only == "enumerate":
            stage_enumerate(args)
        elif args.only == "metadata":
            stage_metadata(args)
        elif args.only == "download":
            stage_download(args)
        elif args.only == "site":
            stage_build_site(args)
        return

    # 전체 파이프라인
    total_urls = stage_enumerate(args)
    if total_urls == 0:
        log.warning("열거된 URL이 0건입니다. seeds.txt / searches.txt를 확인하세요.")
        return

    total_meta = stage_metadata(args)

    if not args.skip_download:
        stage_download(args)
    else:
        log.info("다운로드 생략 (--skip-download)")

    if not args.skip_site:
        if total_meta > 0:
            stage_build_site(args)
        else:
            log.warning("카탈로그가 비어 사이트 빌드를 생략합니다.")
    else:
        log.info("사이트 빌드 생략 (--skip-site)")

    log.info("=" * 60)
    log.info("파이프라인 완료!")
    log.info("=" * 60)

if __name__ == "__main__":
    main()
