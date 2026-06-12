#!/usr/bin/env python3
"""카카오TV 아카이브 — 핵심 로직 (core)

GUI/CLI 공용 모듈. 다운로드, 검색, 채널, 카페, HTML 생성 등
모든 비즈니스 로직을 여기에 모아둔다.
"""

import base64
import csv
import hashlib
import json
import logging
import mimetypes
import random
import re
import sqlite3
import sys
import time
import threading
import uuid
from pathlib import Path
from urllib.parse import quote

from curl_cffi import requests as cffi_requests
import yaml

# ═══════════════════════════════════════════════════════════════════════════
#  경로 / 설정
# ═══════════════════════════════════════════════════════════════════════════

def get_root_dir() -> Path:
    """PyInstaller 빌드 시 exe 위치, 아니면 이 파일 위치."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = get_root_dir()
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"
FAILED_FILE = STATE_DIR / "failed.csv"
CAFE_DONE_FILE = STATE_DIR / "cafe_done.txt"


def load_config() -> dict:
    p = ROOT / "config.yaml"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


CFG = load_config()
THROTTLE = CFG.get("throttle", {})
SLEEP_MIN = THROTTLE.get("sleep_min", 2)
SLEEP_MAX = THROTTLE.get("sleep_max", 6)
MAX_HEIGHT = CFG.get("quality", {}).get("max_height", 1080)
EXCLUDE_CHANNELS = set(CFG.get("exclude", {}).get("channels", []))
EXCLUDE_KEYWORDS = [kw.lower() for kw in CFG.get("exclude", {}).get("keywords", ["뉴스", "news"])]
SEARCH_CFG = CFG.get("search", {})

# ═══════════════════════════════════════════════════════════════════════════
#  DB — 다운로드 이력 (sqlite3)
# ═══════════════════════════════════════════════════════════════════════════

DB_PATH = STATE_DIR / "history.db"
_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    """DB 연결 반환. 테이블 없으면 생성."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            video_id   TEXT PRIMARY KEY,
            source     TEXT,
            title      TEXT,
            channel_id TEXT,
            channel    TEXT,
            status     TEXT DEFAULT 'done',
            path       TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status)
    """)
    conn.commit()
    return conn


def db_is_done(video_id: str) -> bool:
    """이미 다운로드 완료된 영상인지 확인."""
    _ensure_migrated()
    with _db_lock:
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE video_id=? AND status='done'",
                (str(video_id),),
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def db_mark_done(video_id: str, source: str = "kakaotv",
                 title: str = "", channel_id: str = "",
                 channel: str = "", path: str = ""):
    """다운로드 완료 기록."""
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO downloads
                    (video_id, source, title, channel_id, channel, status, path)
                VALUES (?, ?, ?, ?, ?, 'done', ?)
            """, (str(video_id), source, title, channel_id, channel, path))
            conn.commit()
        finally:
            conn.close()


def db_mark_failed(video_id: str, source: str = "kakaotv",
                   title: str = "", channel_id: str = "",
                   channel: str = ""):
    """다운로드 실패 기록 (재시도 시 다시 시도됨)."""
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO downloads
                    (video_id, source, title, channel_id, channel, status, path)
                VALUES (?, ?, ?, ?, ?, 'failed', '')
            """, (str(video_id), source, title, channel_id, channel))
            conn.commit()
        finally:
            conn.close()


def db_migrate_old_files():
    """기존 done_download.txt / cafe_done.txt → DB 마이그레이션. 1회만."""
    archive = STATE_DIR / "done_download.txt"
    if archive.exists():
        with _db_lock:
            conn = _get_db()
            try:
                with open(archive) as f:
                    for line in f:
                        parts = line.strip().split()
                        vid = parts[1] if len(parts) >= 2 else parts[0] if parts else ""
                        if vid:
                            conn.execute("""
                                INSERT OR IGNORE INTO downloads (video_id, source, status)
                                VALUES (?, 'kakaotv', 'done')
                            """, (vid,))
                conn.commit()
            finally:
                conn.close()
        archive.rename(archive.with_suffix(".txt.migrated"))
        log.info(f"done_download.txt → DB 마이그레이션 완료")

    cafe_done = CAFE_DONE_FILE
    if cafe_done.exists():
        with _db_lock:
            conn = _get_db()
            try:
                with open(cafe_done) as f:
                    for line in f:
                        vid = line.strip()
                        if vid:
                            conn.execute("""
                                INSERT OR IGNORE INTO downloads (video_id, source, status)
                                VALUES (?, 'cafe', 'done')
                            """, (vid,))
                conn.commit()
            finally:
                conn.close()
        cafe_done.rename(cafe_done.with_suffix(".txt.migrated"))
        log.info(f"cafe_done.txt → DB 마이그레이션 완료")


_migrated = False

def _ensure_migrated():
    global _migrated
    if not _migrated:
        _migrated = True
        db_migrate_old_files()

# ═══════════════════════════════════════════════════════════════════════════
#  상수
# ═══════════════════════════════════════════════════════════════════════════

SEARCH_URL = "https://tv.kakao.com/api/v1/ft/search/cliplinks"
LIST_KEYS = ["clipList", "clips", "list", "items", "documents"]
_PLAYMETA_URL = "http://tv.kakao.com/api/v1/ft/playmeta/cliplink/%s/"
_CDN_URL = "https://tv.kakao.com/katz/v1/ft/cliplink/%s/readyNplay"
_PROFILE_PREF = ["HIGH4", "HIGH", "MAIN", "BASE", "LOW"]

LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 40" width="140" height="28">'
    '<text x="0" y="30" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif" '
    'font-size="28" font-weight="800" fill="#1e1e1e" letter-spacing="-1">kakao</text>'
    '<rect x="120" y="6" width="50" height="28" rx="14" fill="#fae100"/>'
    '<text x="130" y="27" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif" '
    'font-size="17" font-weight="800" fill="#1e1e1e">tv</text></svg>'
)

# ═══════════════════════════════════════════════════════════════════════════
#  유틸
# ═══════════════════════════════════════════════════════════════════════════

log = logging.getLogger("kakao-tv-dl")


def sleep_polite():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


def html_esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def record_failure(url: str, stage: str, error: str):
    write_header = not FAILED_FILE.exists()
    FAILED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FAILED_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["url", "stage", "error"])
        w.writerow([url, stage, error[:500]])


def should_exclude(title: str, channel: str = "") -> bool:
    if channel and channel in EXCLUDE_CHANNELS:
        return True
    title_lower = (title or "").lower()
    return any(kw in title_lower for kw in EXCLUDE_KEYWORDS)


def make_video_url(clip_id, channel_id=None) -> str:
    if channel_id:
        return f"https://tv.kakao.com/channel/{channel_id}/cliplink/{clip_id}"
    return f"https://tv.kakao.com/v/{clip_id}"


def extract_video_id(url: str) -> str | None:
    m = re.search(r"(?:/v/|/cliplink/)(\d+)", url)
    return m.group(1) if m else None


def _safe_name(s: str, max_len: int = 80) -> str:
    """파일시스템에 안전한 이름으로 변환."""
    if not s:
        return ""
    # Windows/Mac/Linux 공통 금지 문자 제거
    s = re.sub(r'[\\/:*?"<>|]', '_', s)
    # 제어 문자 제거
    s = re.sub(r'[\x00-\x1f]', '', s)
    # 앞뒤 공백/마침표 제거 (Windows 문제)
    s = s.strip().strip('.')
    # 길이 제한
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def make_channel_dir(channel_id: str, channel_name: str = "") -> str:
    """채널 폴더명: [<channel_id>]_<safe_channel_name>"""
    cid = channel_id or "unknown"
    name = _safe_name(channel_name)
    if name:
        return f"[{cid}]_{name}"
    return f"[{cid}]"


def make_video_dir(video_id: str, video_title: str = "") -> str:
    """영상 폴더명: [<video_id>]_<safe_video_name>"""
    vid = video_id or "unknown"
    name = _safe_name(video_title)
    if name:
        return f"[{vid}]_{name}"
    return f"[{vid}]"


def get_video_path(channel_id: str, channel_name: str,
                   video_id: str, video_title: str) -> Path:
    """data/ 하위 영상 디렉토리 경로 반환. 기존(ID만) 폴더가 있으면 그쪽 사용."""
    # 새 경로
    new_path = DATA_DIR / make_channel_dir(channel_id, channel_name) / make_video_dir(video_id, video_title)
    # 기존(ID만) 경로 — 호환성
    old_path = DATA_DIR / (channel_id or "unknown") / video_id
    if old_path.exists() and not new_path.exists():
        return old_path
    return new_path


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _new_session() -> cffi_requests.Session:
    return cffi_requests.Session(impersonate="chrome")


def classify_input(line: str) -> dict | None:
    """입력 줄을 분류: video / channel / search / cafe."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "cafe.daum.net" in line:
        m = re.search(r'grpid=([a-zA-Z0-9]+)', line)
        if not m:
            m = re.search(r'cafe\.daum\.net/([a-zA-Z0-9_]+)', line)
        grpid = m.group(1) if m else ""
        return {"type": "cafe", "url": line, "grpid": grpid}
    if re.search(r'tv\.kakao\.com.*/(?:v|cliplink)/\d+', line):
        return {"type": "video", "url": line}
    if re.search(r'tv\.kakao\.com/channel/\d+', line):
        return {"type": "channel", "url": line}
    if line.startswith("http"):
        return {"type": "video", "url": line}
    return {"type": "search", "query": line}


def load_cookies(cookie_path: str) -> dict:
    """쿠키 파일 로드 (JSON 또는 key=value)."""
    cookies = {}
    p = Path(cookie_path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8").strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cookies[k.strip()] = v.strip()
        elif "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 2:
                cookies[parts[0].strip()] = parts[1].strip()
    return cookies


# ═══════════════════════════════════════════════════════════════════════════
#  카카오TV API — 메타데이터
# ═══════════════════════════════════════════════════════════════════════════

def fetch_kakao_meta(vid: str, url: str) -> dict | None:
    """playmeta API로 메타데이터 가져오기 (안정적, CDN 502 안 남)."""
    session = _new_session()
    params = {
        "player": "monet_html5",
        "referer": url,
        "uuid": "",
        "service": "kakao_tv",
        "section": "",
        "dteType": "PC",
        "fields": ",".join([
            "-*", "tid", "clipLink", "displayTitle", "clip", "title",
            "description", "channelId", "createTime", "duration", "playCount",
            "likeCount", "commentCount", "tagList", "channel", "name",
            "clipChapterThumbnailList", "thumbnailUrl", "timeInSec", "isDefault",
            "videoOutputList", "width", "height", "kbps", "profile", "label",
        ]),
    }
    for attempt in range(3):
        try:
            r = session.get(_PLAYMETA_URL % vid, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code >= 500 and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  카카오TV API — 스트림 URL
# ═══════════════════════════════════════════════════════════════════════════

def find_stream_url(vid: str, url: str, profiles: list[str]) -> tuple[str | None, str | None]:
    """CDN에서 스트림 URL 찾기. 모든 프로필 순회 + 재시도."""
    session = _new_session()
    params_base = {
        "player": "monet_html5",
        "referer": url,
        "uuid": "",
        "service": "kakao_tv",
        "section": "",
        "dteType": "PC",
        "fields": "-*,code,message,url",
    }
    ordered = sorted(profiles, key=lambda p: _PROFILE_PREF.index(p) if p in _PROFILE_PREF else 99)
    for profile in ordered:
        params = {**params_base, "profile": profile}
        for attempt in range(3):
            try:
                r = session.get(_CDN_URL % vid, params=params, timeout=15)
                if r.status_code == 200:
                    stream_url = r.json().get("videoLocation", {}).get("url")
                    if stream_url:
                        return stream_url, profile
                if r.status_code >= 500 and attempt < 2:
                    time.sleep(2 + random.uniform(0, 2))
                    continue
                break
            except Exception:
                if attempt < 2:
                    time.sleep(2)
                continue
    return None, None


# ═══════════════════════════════════════════════════════════════════════════
#  다운로드
# ═══════════════════════════════════════════════════════════════════════════

def download_stream(stream_url: str, dest: Path,
                    stop_event: threading.Event | None = None) -> bool:
    """스트림 URL에서 mp4 직접 다운로드."""
    session = _new_session()
    try:
        r = session.get(stream_url, stream=True, timeout=600)
        if r.status_code not in (200, 206):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                if stop_event and stop_event.is_set():
                    return False
                if chunk:
                    f.write(chunk)
        return dest.stat().st_size > 1024
    except Exception as e:
        log.warning(f"스트림 다운로드 에러: {e}")
        return False


def download_file(url: str, dest: Path, timeout: int = 15) -> bool:
    """일반 파일 (썸네일 등) 다운로드."""
    if not url:
        return False
    try:
        session = _new_session()
        r = session.get(url, timeout=timeout)
        if r.status_code == 200:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return True
    except Exception:
        pass
    return False


# ═══════════════════════════════════════════════════════════════════════════
#  단일 영상 다운로드 (통합)
# ═══════════════════════════════════════════════════════════════════════════

def download_single_video(
    url: str,
    progress_fn=None,
    stop_event: threading.Event | None = None,
) -> bool:
    """카카오TV 영상 1건 다운로드 + 메타/HTML 저장.

    progress_fn(status_str): 진행 상태 콜백 (GUI/CLI 공용).
    Returns True if successful.
    """
    def _update(s):
        if progress_fn:
            progress_fn(s)

    vid = extract_video_id(url)
    if not vid:
        log.warning(f"URL에서 ID 추출 실패: {url}")
        _update("실패 (URL)")
        return False

    # DB 중복 체크
    if db_is_done(vid):
        log.info(f"DB 스킵: {vid}")
        _update("완료 (DB 스킵)")
        return True

    # 1) 메타데이터
    _update("메타데이터 수집...")
    meta = fetch_kakao_meta(vid, url)
    if not meta:
        log.warning(f"메타 실패: {vid}")
        _update("실패 (메타)")
        return False

    clip_link = meta.get("clipLink", {})
    clip = clip_link.get("clip", {})
    title = clip.get("title") or clip_link.get("displayTitle") or ""
    cid = str(clip_link.get("channelId") or "unknown")
    channel_name = (clip_link.get("channel") or {}).get("name", "")

    _update(f"다운로드: {title[:30]}...")

    video_dir = get_video_path(cid, channel_name, vid, title)
    video_dir.mkdir(parents=True, exist_ok=True)
    mp4_path = video_dir / "video.mp4"

    # 이미 존재
    if mp4_path.exists() and mp4_path.stat().st_size > 1024:
        log.info(f"이미 존재: {vid}")
        _update("완료 (스킵)")
        save_meta_and_html(video_dir, vid, clip_link, clip, url)
        return True

    # 2) 스트림 URL
    available = [
        f.get("profile") for f in clip.get("videoOutputList", [])
        if f.get("profile") and f.get("profile") != "AUDIO"
    ]
    if not available:
        log.warning(f"프로필 없음: {vid}")
        _update("실패 (프로필 없음)")
        return False

    height_map = {f.get("profile"): f.get("height", 0) for f in clip.get("videoOutputList", []) if f.get("profile")}
    filtered = [p for p in available if height_map.get(p, 0) <= MAX_HEIGHT] or available

    stream_url, profile = find_stream_url(vid, url, filtered)
    if not stream_url:
        log.warning(f"CDN 502: 모든 프로필 실패 ({vid})")
        _update("실패 (CDN 502)")
        save_meta_and_html(video_dir, vid, clip_link, clip, url)
        return False

    height = height_map.get(profile, "?")
    log.info(f"스트림: {profile} ({height}p)")
    _update(f"다운로드 중: {title[:25]}... ({height}p)")

    # 3) 다운로드
    if not download_stream(stream_url, mp4_path, stop_event):
        log.warning(f"다운로드 실패: {vid}")
        _update("실패 (다운로드)")
        return False

    # 4) 메타 + 썸네일 + HTML
    save_meta_and_html(video_dir, vid, clip_link, clip, url)

    thumb_url = clip.get("thumbnailUrl")
    if thumb_url:
        ext = ".png" if ".png" in thumb_url else ".jpg"
        download_file(thumb_url, video_dir / f"thumb{ext}")

    # DB 기록
    db_mark_done(vid, source="kakaotv", title=title,
                 channel_id=cid, channel=channel_name,
                 path=str(video_dir.relative_to(DATA_DIR)))

    _update("완료")
    log.info(f"완료: {title}")
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  검색 → 다운로드
# ═══════════════════════════════════════════════════════════════════════════

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


def search_and_download(
    query: str,
    progress_fn=None,
    stop_event: threading.Event | None = None,
    max_pages: int = 200,
    page_size: int = 20,
) -> int:
    """검색어로 영상 찾아 순차 다운로드. 다운로드한 건수 반환."""
    def _update(s):
        if progress_fn:
            progress_fn(s)

    session = _new_session()
    referer = f"https://tv.kakao.com/search/cliplinks?q={quote(query)}"

    _update(f"검색: {query}")
    page = 1
    found = 0
    empty = 0
    while page <= max_pages and not (stop_event and stop_event.is_set()):
        params = {
            "sort": "Score", "q": query, "fulllevels": "list",
            "fields": "-user,-clipChapterThumbnailList,-tagList",
            "size": page_size, "page": page, "_": int(time.time() * 1000),
        }
        try:
            resp = session.get(
                SEARCH_URL, params=params,
                headers={"x-requested-with": "XMLHttpRequest", "referer": referer},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"검색 API 에러: {e}")
            break

        clips = _find_clip_list(data)
        if not clips:
            empty += 1
            if empty >= 2:
                break
            page += 1
            sleep_polite()
            continue

        new = 0
        for item in clips:
            if stop_event and stop_event.is_set():
                break
            clip_id = str(item.get("id") or item.get("clipLinkId") or "")
            if not clip_id:
                continue
            ch = item.get("channel") or {}
            ch_id = str(ch.get("id", "")) if isinstance(ch, dict) else ""
            title = item.get("title") or ""
            if should_exclude(title):
                continue
            video_url = make_video_url(clip_id, ch_id)
            _update(f"[{found + 1}] {title[:30]}...")
            download_single_video(video_url, lambda s: log.info(f"  {s}"), stop_event)
            found += 1
            new += 1
            sleep_polite()

        if new == 0:
            empty += 1
            if empty >= 3:
                break
        else:
            empty = 0
        log.info(f"검색 '{query}' page {page}: {len(clips)}건, {new}건 다운로드")
        page += 1
        sleep_polite()

    _update(f"완료 ({found}건)")
    return found


# ═══════════════════════════════════════════════════════════════════════════
#  채널 → 다운로드
# ═══════════════════════════════════════════════════════════════════════════

def channel_download(
    url: str,
    progress_fn=None,
    stop_event: threading.Event | None = None,
) -> int:
    """채널 URL의 모든 영상 다운로드. 건수 반환."""
    import yt_dlp

    def _update(s):
        if progress_fn:
            progress_fn(s)

    _update("채널 펼침...")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
            data = ydl.extract_info(url, download=False)
        if data is None:
            _update("실패 (채널 로드)")
            return 0
    except Exception as e:
        _update(f"실패: {e}")
        return 0

    entries = data.get("entries", [])
    log.info(f"채널 {url}: {len(entries)}개 영상")
    done = 0
    for i, e in enumerate(entries):
        if stop_event and stop_event.is_set():
            break
        eid = str(e.get("id", ""))
        vurl = e.get("url") or e.get("webpage_url") or f"https://tv.kakao.com/v/{eid}"
        title = e.get("title", "")
        _update(f"[{i + 1}/{len(entries)}] {title[:25]}...")
        download_single_video(vurl, lambda s: log.info(f"  {s}"), stop_event)
        done += 1
        sleep_polite()
    _update(f"완료 ({done}/{len(entries)})")
    return done


# ═══════════════════════════════════════════════════════════════════════════
#  카페 다운로드
# ═══════════════════════════════════════════════════════════════════════════

def cafe_list_articles(
    session: cffi_requests.Session,
    cookies: dict,
    grpid: str,
    page_start: int = 1,
    page_end: int = 999,
) -> list[dict]:
    """카페 동영상 게시판에서 (fldid, dataid) 쌍 추출."""
    articles = []
    page = page_start
    while page <= page_end:
        resp = session.get(
            "https://cafe.daum.net/_c21_/movie_bbs_list",
            params={"grpid": grpid, "page": str(page), "listnum": "32"},
            cookies=cookies, timeout=15,
        )
        if resp.status_code != 200:
            break
        pairs = re.findall(r"fldid:\s*'([^']+)'.*?dataid:\s*'(\d+)'", resp.text, re.DOTALL)
        if not pairs:
            break
        for fldid, dataid in pairs:
            articles.append({"fldid": fldid, "dataid": dataid})
        log.info(f"카페 page {page}: {len(pairs)}건")
        if f"page={page + 1}" not in resp.text:
            break
        page += 1
        sleep_polite()

    seen = set()
    unique = []
    for a in articles:
        k = f"{a['fldid']}_{a['dataid']}"
        if k not in seen:
            seen.add(k)
            unique.append(a)
    log.info(f"카페 총 {len(unique)}건 (중복 제거)")
    return unique


def cafe_download_article(
    session: cffi_requests.Session,
    cookies: dict,
    grpid: str,
    fldid: str,
    dataid: str,
) -> bool:
    """카페 게시글 1건 → clip_id → axz_vod → kamp → mp4 다운로드."""
    article_url = f"https://cafe.daum.net/_c21_/bbs_read?grpid={grpid}&fldid={fldid}&datanum={dataid}"

    # 게시글 → clip_id + ptoken
    resp = session.get(article_url, cookies=cookies, timeout=15)
    if resp.status_code != 200:
        return False
    ptokens = re.findall(r'ptoken=([a-zA-Z0-9._-]+)', resp.text)
    clips = re.findall(r'cliplink/([a-zA-Z0-9]+)', resp.text)
    if not ptokens or not clips:
        log.info(f"  [{dataid}] embed 없음")
        return False

    clip_id, ptoken = clips[0], ptokens[0]

    # DB 중복 체크
    if db_is_done(clip_id):
        log.info(f"  [{dataid}] DB 스킵: {clip_id}")
        return True

    # 메타데이터
    meta = {}
    try:
        mr = session.get(f"https://tv.kakao.com/api/v1/ft/cliplinks/{clip_id}", timeout=10)
        if mr.status_code == 200:
            md = mr.json()
            ch = md.get("channel", {}) or {}
            meta = {
                "real_id": md.get("id"),
                "title": md.get("displayTitle", ""),
                "channel": ch.get("name", ""),
                "channel_id": str(ch.get("id", "")),
                "create_time": md.get("createTime", ""),
            }
    except Exception:
        pass
    sleep_polite()

    title = meta.get("title", "")

    # embed → axz_vod JWT
    embed_url = (
        f"https://kakaotv.daum.net/embed/player/cliplink/{clip_id}"
        f"?service=daum_cafe&f=p&ptoken={ptoken}&autoplay=0"
    )
    er = session.get(embed_url, headers={"referer": "https://cafe.daum.net/"}, timeout=15)
    axz = None
    for jwt_str in set(re.findall(r'eyJ[a-zA-Z0-9._-]{50,}', er.text)):
        try:
            payload = json.loads(base64.urlsafe_b64decode(jwt_str.split(".")[1] + "=="))
            if payload.get("app_id") == "axz_vod":
                axz = jwt_str
                break
        except Exception:
            continue
    if not axz:
        log.warning(f"  [{dataid}] axz 토큰 실패")
        record_failure(article_url, "cafe_axz", "no axz_vod token")
        return False

    # kamp → mp4 URL
    tid = hashlib.md5(f"{time.time()}_{uuid.uuid4()}".encode()).hexdigest()
    kr = session.get(
        f"https://kamp.daum.net/vod/v1/src/{clip_id}",
        params={
            "service": "daum_cafe", "f": "p", "ptoken": ptoken, "autoplay": "0",
            "tid": tid, "auth_type": "query", "csvc": "daum_cafe", "tit": "daum_cafe",
        },
        headers={
            "referer": "https://kakaotv.daum.net/",
            "origin": "https://kakaotv.daum.net",
            "x-kamp-auth": f"Bearer {axz}",
            "x-kamp-player": "kamp-player-web",
            "x-kamp-version": "2.0.21",
        },
        cookies=cookies, timeout=15,
    )
    if kr.status_code != 200:
        log.warning(f"  [{dataid}] kamp 실패: {kr.status_code}")
        record_failure(article_url, "cafe_kamp", f"HTTP {kr.status_code}")
        return False

    kdata = kr.json()
    mp4s = re.findall(r'https?://[^"\'\\,\s]+\.mp4[^"\'\\,\s]*', kr.text)
    if not mp4s:
        log.warning(f"  [{dataid}] mp4 URL 없음")
        record_failure(article_url, "cafe_kamp", "no mp4 url")
        return False

    mp4_url = mp4s[0]
    profiles = kdata.get("profiles", [])
    best = profiles[0] if profiles else {}
    filesize = best.get("filesize", 0)
    if filesize:
        log.info(f"  [{dataid}] {best.get('width', '?')}x{best.get('height', '?')}, "
                 f"{filesize / 1024 / 1024:.1f}MB")

    # 다운로드
    cid = meta.get("channel_id", "cafe")
    vid = str(meta.get("real_id") or clip_id)
    channel_name = meta.get("channel", "")
    video_dir = get_video_path(cid, channel_name, vid, title)
    video_dir.mkdir(parents=True, exist_ok=True)
    mp4_path = video_dir / "video.mp4"

    if mp4_path.exists() and filesize and abs(mp4_path.stat().st_size - filesize) < 1024:
        log.info(f"  [{dataid}] 이미 존재")
    else:
        session_dl = _new_session()
        try:
            dl = session_dl.get(mp4_url, stream=True, timeout=600)
            if dl.status_code not in (200, 206):
                log.warning(f"  [{dataid}] 다운로드 HTTP {dl.status_code}")
                return False
            with open(mp4_path, "wb") as f:
                for chunk in dl.iter_content(256 * 1024):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            log.warning(f"  [{dataid}] 다운로드 에러: {e}")
            return False

    # 썸네일
    thumb = kdata.get("thumbnail", "")
    if thumb:
        ext = ".png" if ".png" in thumb else ".jpg"
        download_file(thumb, video_dir / f"thumb{ext}")

    # info.json
    (video_dir / "video.info.json").write_text(json.dumps({
        "clip_id": clip_id, "real_id": meta.get("real_id"), "title": title,
        "channel": meta.get("channel", ""), "channel_id": cid,
        "duration": best.get("duration"), "source": "daum_cafe", "grpid": grpid,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # HTML
    ct = meta.get("create_time", "")
    ud = ct[:10].replace("-", "").replace(".", "") if ct and len(ct) >= 10 else ""
    build_html(video_dir, {
        "title": title, "channel": meta.get("channel", ""),
        "duration": best.get("duration", 0), "upload_date": ud, "create_time": ct,
        "webpage_url": article_url,
    })

    # DB 기록
    db_mark_done(clip_id, source="cafe", title=title,
                 channel_id=cid, channel=channel_name,
                 path=str(video_dir.relative_to(DATA_DIR)))

    return True


def cafe_download_all(
    grpid: str,
    cookies: dict,
    progress_fn=None,
    stop_event: threading.Event | None = None,
) -> int:
    """카페 전체 다운로드 (GUI/CLI 공용)."""
    def _update(s):
        if progress_fn:
            progress_fn(s)

    session = _new_session()
    _update("카페 목록 수집...")
    articles = cafe_list_articles(session, cookies, grpid)
    if not articles:
        _update("실패: 게시글 없음")
        return 0

    success = 0
    for i, art in enumerate(articles):
        if stop_event and stop_event.is_set():
            break
        fldid, dataid = art["fldid"], art["dataid"]
        _update(f"[{i + 1}/{len(articles)}] 게시글 {dataid}...")

        # cafe_download_article 내부에서 DB 중복 체크 + 완료 기록 처리
        ok = cafe_download_article(session, cookies, grpid, fldid, dataid)
        if ok:
            success += 1
            log.info(f"  [{dataid}] 완료")
        sleep_polite()

    _update(f"완료 ({success}/{len(articles)})")
    return success


# ═══════════════════════════════════════════════════════════════════════════
#  HTML 생성
# ═══════════════════════════════════════════════════════════════════════════

def build_html(video_dir: Path, meta: dict):
    """video.html 생성 (카카오TV 스타일, self-contained)."""
    thumb_b64 = thumb_mime = ""
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        tf = video_dir / f"thumb{ext}"
        if not tf.exists():
            for f in video_dir.iterdir():
                if f.suffix == ext and "thumb" in f.name.lower():
                    tf = f
                    break
                if f.suffix in (".png", ".jpg", ".jpeg", ".webp") and not f.name.startswith("video"):
                    tf = f
                    break
        if tf.exists():
            thumb_mime = mimetypes.guess_type(str(tf))[0] or "image/png"
            thumb_b64 = base64.b64encode(tf.read_bytes()).decode("ascii")
            break

    has_video = any((video_dir / f"video.{e}").exists() for e in ("mp4", "mkv", "webm"))
    dur = int(meta.get("duration") or 0)
    ud = meta.get("upload_date", "")
    webpage_url = meta.get("webpage_url", "")

    player_html = (
        '<video controls preload="metadata"'
        + (f' poster="data:{thumb_mime};base64,{thumb_b64}"' if thumb_b64 else '')
        + '><source src="video.mp4" type="video/mp4"></video>'
    ) if has_video else '<div class="no-video">영상 파일 없음</div>'

    date_str = ""
    if len(ud) >= 8:
        date_str = f"{ud[:4]}.{ud[4:6]}.{ud[6:8]}"
    elif meta.get("create_time"):
        date_str = html_esc(meta["create_time"])

    dur_str = ""
    if dur:
        dur_str = f' &middot; {dur // 3600:02d}:{(dur % 3600) // 60:02d}:{dur % 60:02d}'

    desc_html = ""
    if meta.get("description"):
        desc_html = f'<div class="clip-desc">{html_esc(meta["description"])}</div><div class="divider"></div>'

    html = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{html_esc(meta.get('title', ''))} - kakaoTV</title>
<style>*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,"Malgun Gothic","맑은 고딕","Segoe UI",sans-serif;background:#f5f5f5;color:#1e1e1e;line-height:1.5}}a{{color:inherit;text-decoration:none}}.gnb{{background:#fff;border-bottom:1px solid #e5e5e5;height:56px;display:flex;align-items:center;padding:0 24px;position:sticky;top:0;z-index:100}}.gnb-logo{{display:flex;align-items:center}}.channel-bar{{background:#fff;border-bottom:1px solid #e5e5e5;padding:0 24px;display:flex;align-items:center;height:52px;max-width:1200px;margin:0 auto}}.channel-name{{font-size:17px;font-weight:700;padding-bottom:2px;border-bottom:3px solid #fae100}}.container{{max-width:860px;margin:0 auto;background:#fff}}.player-wrap{{background:#000}}.player-wrap video{{width:100%;display:block;max-height:480px}}.no-video{{padding:120px 20px;text-align:center;color:#888;font-size:15px;background:#000}}.clip-title{{font-size:17px;padding:20px 24px 8px}}.clip-meta{{padding:0 24px 16px;font-size:13px;color:#999}}.divider{{height:1px;background:#e5e5e5;margin:0 24px}}.clip-desc{{padding:16px 24px;font-size:14px;color:#555;white-space:pre-wrap;line-height:1.7}}.comments-notice{{margin:0 24px 16px;padding:14px 16px;background:#f9f9f9;border:1px solid #eee;border-radius:6px;font-size:13px;color:#999}}.original-link{{padding:0 24px 24px;font-size:13px;color:#999}}.original-link a{{color:#3c78c8}}.archive-banner{{background:#fff8d6;border-top:1px solid #f0e6a0;padding:12px 24px;font-size:12px;color:#8a7a2a;text-align:center}}.footer{{background:#fff;border-top:1px solid #e5e5e5;padding:24px;text-align:center;font-size:12px;color:#999;max-width:860px;margin:0 auto}}</style></head><body>
<div class="gnb"><div class="gnb-logo">{LOGO_SVG}</div></div>
<div class="channel-bar"><span class="channel-name">{html_esc(meta.get('channel', ''))}</span></div>
<div class="container"><div class="player-wrap">{player_html}</div>
<div class="clip-title">{html_esc(meta.get('title', ''))}</div>
<div class="clip-meta">{date_str}{dur_str}</div>
<div class="divider"></div>
{desc_html}
<div class="comments-notice">댓글 서비스가 2024년 7월에 종료되어 더 이상 제공되지 않습니다.</div>
<div class="original-link">원본: <a href="{html_esc(webpage_url)}" target="_blank">{html_esc(webpage_url)}</a></div>
</div><div class="archive-banner">이 페이지는 카카오TV 서비스 종료(2026-06-30) 전 아카이빙 목적으로 생성되었습니다.</div>
<div class="footer">Archived from kakaoTV &middot; Original &copy; Kakao Corp.</div></body></html>"""

    (video_dir / "video.html").write_text(html, encoding="utf-8")


def save_meta_and_html(video_dir: Path, vid: str, clip_link: dict, clip: dict, url: str):
    """info.json + video.html 저장 (카카오TV 직접 API 응답 기반)."""
    title = clip.get("title") or clip_link.get("displayTitle") or ""
    channel_name = (clip_link.get("channel") or {}).get("name", "")
    cid = str(clip_link.get("channelId") or "unknown")
    duration = clip.get("duration", 0)
    create_time = clip_link.get("createTime", "")
    ud = re.sub(r"[^0-9]", "", create_time[:10]) if create_time else ""

    info = {
        "id": vid,
        "title": title,
        "channel": channel_name,
        "channel_id": cid,
        "duration": duration,
        "upload_date": ud,
        "view_count": clip.get("playCount"),
        "like_count": clip.get("likeCount"),
        "description": clip.get("description", ""),
        "webpage_url": url,
        "thumbnail": clip.get("thumbnailUrl"),
        "formats": [
            {"profile": f.get("profile"), "width": f.get("width"), "height": f.get("height")}
            for f in clip.get("videoOutputList", []) if f.get("profile") != "AUDIO"
        ],
    }
    (video_dir / "video.info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    build_html(video_dir, {
        "title": title,
        "channel": channel_name,
        "duration": duration or 0,
        "upload_date": ud,
        "description": clip.get("description", ""),
        "webpage_url": url,
    })
