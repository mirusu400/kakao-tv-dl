#!/usr/bin/env python3
"""카카오TV 아카이브 — CLI 파이프라인

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
import json
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from tqdm import tqdm

from core import (
    ROOT, STATE_DIR, DATA_DIR,
    CFG, SEARCH_CFG, SLEEP_MIN, SLEEP_MAX,
    load_config, sleep_polite, should_exclude, make_video_url,
    load_jsonl, load_ids_from_jsonl, append_jsonl,
    record_failure, classify_input,
    fetch_kakao_meta, find_stream_url, download_stream,
    download_single_video, download_file,
    search_and_download as core_search_and_download,
    save_meta_and_html, build_html,
    _find_clip_list, _new_session,
    SEARCH_URL, LIST_KEYS, MAX_HEIGHT,
)

# ── 경로 ────────────────────────────────────────────────────────────────

URLS_FILE = STATE_DIR / "urls.jsonl"
CATALOG_FILE = STATE_DIR / "catalog.jsonl"
ARCHIVE_FILE = STATE_DIR / "done_download.txt"

# ── 로깅 ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kakao-tv-dl")


# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 1 — ENUMERATE (seeds + searches → urls.jsonl)
# ═══════════════════════════════════════════════════════════════════════════

def _is_single_video(url: str) -> bool:
    return bool(re.search(r"/v/\d+|/cliplink/\d+", url))

def _extract_id(url: str) -> str | None:
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
        if _is_single_video(url):
            vid = _extract_id(url)
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
                    capture_output=True, text=True, timeout=120)
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

    from urllib.parse import quote
    session = _new_session()

    added = 0
    for query in queries:
        log.info(f"  검색: {query}")
        referer = f"https://tv.kakao.com/search/cliplinks?q={quote(query)}"
        page = 1
        empty_streak = 0
        while page <= max_pages:
            import time
            params = {"sort": "Score", "q": query, "fulllevels": "list",
                      "fields": "-user,-clipChapterThumbnailList,-tagList",
                      "size": size, "page": page, "_": int(time.time() * 1000)}
            try:
                resp = session.get(SEARCH_URL, params=params,
                                   headers={"x-requested-with": "XMLHttpRequest",
                                            "referer": referer},
                                   timeout=30)
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
                log.warning(f"  클립 배열 없음 (page {page})")
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
                clip_id = str(item.get("id") or item.get("clipLinkId") or item.get("cliplink_id") or "")
                if not clip_id:
                    continue
                if clip_id in existing_ids:
                    continue
                ch = item.get("channel") or {}
                channel_id = str(ch.get("id", "")) if isinstance(ch, dict) else ""
                channel_name = ch.get("name", "") if isinstance(ch, dict) else str(ch)
                title = item.get("title") or item.get("displayTitle") or ""
                if should_exclude(title, channel_name):
                    continue
                existing_ids.add(clip_id)
                norm = {"id": clip_id, "url": make_video_url(clip_id, channel_id),
                        "channel": channel_name, "channel_id": channel_id,
                        "title": title, "src": "search"}
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
#  STAGE 2 — METADATA (직접 API, yt-dlp 불필요)
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_meta(clip_link: dict, clip: dict, url: str) -> dict:
    """playmeta 응답을 catalog 스키마로 정규화."""
    create_time = clip_link.get("createTime", "")
    ud = re.sub(r"[^0-9]", "", create_time[:10]) if create_time else None
    formats = []
    for fmt in clip.get("videoOutputList", []):
        if fmt.get("profile") == "AUDIO":
            continue
        f = {}
        if fmt.get("profile"):
            f["format_id"] = fmt["profile"]
        if fmt.get("height"):
            f["height"] = fmt["height"]
        if f:
            formats.append(f)
    return {
        "id": str(clip_link.get("id") or ""),
        "title": clip.get("title") or clip_link.get("displayTitle"),
        "channel": (clip_link.get("channel") or {}).get("name"),
        "channel_id": str(clip_link.get("channelId") or ""),
        "upload_date": ud,
        "duration": clip.get("duration"),
        "view_count": clip.get("playCount"),
        "thumbnail": clip.get("thumbnailUrl"),
        "description": clip.get("description"),
        "webpage_url": url,
        "formats_available": formats or None,
        "comments": "N/A: 카카오TV 댓글 서비스 2024-07 종료",
    }


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

        from core import extract_video_id
        actual_vid = extract_video_id(url) or vid
        meta_raw = fetch_kakao_meta(actual_vid, url)
        if meta_raw:
            clip_link = meta_raw.get("clipLink", {})
            clip = clip_link.get("clip", {})
            meta = _normalize_meta(clip_link, clip, url)
            if not meta["id"]:
                meta["id"] = vid
            append_jsonl(CATALOG_FILE, meta)
            success += 1
        else:
            record_failure(url, "metadata", "playmeta_failed")
            fail += 1
        sleep_polite()

    total = len(done_ids) + success
    log.info(f"메타데이터 완료: 성공 {success}, 실패 {fail}, 전체 {total}")
    return total


# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 3 — DOWNLOAD (직접 API, yt-dlp 불필요)
# ═══════════════════════════════════════════════════════════════════════════

def _load_archive_ids() -> set:
    ids = set()
    if ARCHIVE_FILE.exists():
        with open(ARCHIVE_FILE) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2:
                    ids.add(parts[1])
                elif parts:
                    ids.add(parts[0])
    return ids

def _mark_archive(vid: str):
    ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ARCHIVE_FILE, "a") as f:
        f.write(f"kakao {vid}\n")


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
        if not url:
            continue

        ok = download_single_video(url, lambda s: log.info(f"  {s}"))
        if ok:
            _mark_archive(vid)
            success += 1
        else:
            fail += 1

    log.info(f"다운로드 완료: 성공 {success}, 실패 {fail}, "
             f"전체 {len(archive_ids) + success}")


# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 5 — BUILD HTML (data/ 내 video.html 일괄 생성)
# ═══════════════════════════════════════════════════════════════════════════

def stage_build_site(args):
    log.info("=" * 60)
    log.info("STAGE 5 — HTML 빌드")
    log.info("=" * 60)

    if not CATALOG_FILE.exists():
        log.error("catalog.jsonl 없음")
        return

    videos = load_jsonl(CATALOG_FILE)
    if not videos:
        log.error("카탈로그가 비어 있음")
        return

    log.info(f"카탈로그 {len(videos)}건 로드")
    built = 0
    for v in tqdm(videos, desc="HTML 빌드"):
        vid = str(v.get("id", ""))
        cid = v.get("channel_id", "") or "unknown_channel"
        video_dir = DATA_DIR / cid / vid
        video_dir.mkdir(parents=True, exist_ok=True)

        build_html(video_dir, {
            "title": v.get("title", ""),
            "channel": v.get("channel", ""),
            "duration": int(v.get("duration") or 0),
            "upload_date": v.get("upload_date", ""),
            "description": v.get("description", ""),
            "webpage_url": v.get("webpage_url", ""),
        })
        built += 1

    log.info(f"HTML 빌드 완료: {built}개")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="카카오TV 아카이브 — CLI 파이프라인",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="예시:\n"
               "  python main.py                    # 전체 파이프라인\n"
               "  python main.py --only enumerate   # 열거만\n"
               "  python main.py --skip-download     # 다운로드 생략\n"
               "  python main.py --limit 5          # 다운로드 5개만\n",
    )
    parser.add_argument("--seeds", default=str(ROOT / "seeds.txt"))
    parser.add_argument("--searches", default=str(ROOT / "searches.txt"))
    parser.add_argument("--size", type=int, default=SEARCH_CFG.get("size", 20))
    parser.add_argument("--max-pages", type=int, default=SEARCH_CFG.get("max_pages", 200))
    parser.add_argument("--cookie-header", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-site", action="store_true")
    parser.add_argument("--only", choices=["enumerate", "metadata", "download", "site"])
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if args.only:
        {"enumerate": stage_enumerate,
         "metadata": stage_metadata,
         "download": stage_download,
         "site": stage_build_site}[args.only](args)
        return

    # 전체 파이프라인
    total_urls = stage_enumerate(args)
    if total_urls == 0:
        log.warning("열거된 URL이 0건입니다.")
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
        log.info("사이트 빌드 생략 (--skip-site)")

    log.info("=" * 60)
    log.info("파이프라인 완료!")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
