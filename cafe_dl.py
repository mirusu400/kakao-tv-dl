#!/usr/bin/env python3
"""다음 카페 embed 영상 다운로더

카페 게시판의 카카오TV embed 영상을 Selenium 없이 다운로드합니다.

흐름:
  1) cafe.daum.net 게시글 목록 → dataid + fldid 추출
  2) 각 게시글 → ptoken + clip_id 추출
  3) kakaotv.daum.net/embed → axz_vod JWT 추출 (SSR)
  4) kamp.daum.net/vod/v1/src → signed mp4 URL
  5) mp4 다운로드

사용법:
  python cafe_dl.py --grpid zz4c --cookies cookies.txt
  python cafe_dl.py --grpid zz4c --cookies cookies.txt --limit 5
  python cafe_dl.py --grpid zz4c --cookies cookies.txt --page-start 1 --page-end 10
  python cafe_dl.py --grpid zz4c --cookies cookies.txt --list-only   # 목록만 저장
"""

import argparse
import base64
import csv
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path

from curl_cffi import requests as cffi_requests
import yaml
from tqdm import tqdm

# ── 경로 ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"
CAFE_STATE = STATE_DIR / "cafe_videos.jsonl"
CAFE_DONE = STATE_DIR / "cafe_done.txt"
FAILED_FILE = STATE_DIR / "failed.csv"

# ── 로깅 ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cafe-dl")

# ── config ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    return {}

CFG = load_config()
THROTTLE = CFG.get("throttle", {})
SLEEP_MIN = THROTTLE.get("sleep_min", 2)
SLEEP_MAX = THROTTLE.get("sleep_max", 6)

# ── 유틸 ────────────────────────────────────────────────────────────────────

def sleep_polite():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

def record_failure(url: str, stage: str, error: str):
    write_header = not FAILED_FILE.exists()
    with open(FAILED_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["url", "stage", "error"])
        w.writerow([url, stage, error[:500]])

def load_done_ids() -> set:
    ids = set()
    if CAFE_DONE.exists():
        with open(CAFE_DONE) as f:
            for line in f:
                line = line.strip()
                if line:
                    ids.add(line)
    return ids

def mark_done(clip_id: str):
    with open(CAFE_DONE, "a") as f:
        f.write(clip_id + "\n")

def append_jsonl(path: Path, obj: dict):
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def load_cookies(cookie_path: str) -> dict:
    """쿠키 파일 로드. key=value 형태 또는 JSON."""
    cookies = {}
    p = Path(cookie_path)
    if not p.exists():
        log.error(f"쿠키 파일 없음: {cookie_path}")
        sys.exit(1)
    text = p.read_text().strip()
    # JSON 형태
    if text.startswith("{"):
        return json.loads(text)
    # key=value 또는 key\tvalue 형태
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
#  STAGE 1 — 카페 게시글 목록 수집
# ═══════════════════════════════════════════════════════════════════════════

def list_cafe_articles(session, cookies: dict, grpid: str,
                       page_start: int = 1, page_end: int = 999,
                       listnum: int = 32) -> list[dict]:
    """카페 동영상 게시판에서 (fldid, dataid) 쌍 추출."""
    articles = []
    page = page_start

    while page <= page_end:
        resp = session.get(
            "https://cafe.daum.net/_c21_/movie_bbs_list",
            params={"grpid": grpid, "page": str(page), "listnum": str(listnum)},
            cookies=cookies,
            headers={"referer": f"https://cafe.daum.net/_c21_/movie_bbs_list?grpid={grpid}"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.error(f"목록 page {page}: HTTP {resp.status_code}")
            break

        # fldid + dataid 쌍 추출
        pairs = re.findall(
            r"fldid:\s*'([^']+)'.*?dataid:\s*'(\d+)'",
            resp.text, re.DOTALL,
        )
        if not pairs:
            log.info(f"page {page}: 게시글 없음 — 종료")
            break

        for fldid, dataid in pairs:
            articles.append({"fldid": fldid, "dataid": dataid, "grpid": grpid})

        log.info(f"page {page}: {len(pairs)}건")

        # 다음 페이지 존재 확인
        if f"page={page + 1}" not in resp.text:
            log.info(f"page {page}: 마지막 페이지")
            break

        page += 1
        sleep_polite()

    # 중복 제거 (fldid+dataid 기준)
    seen = set()
    unique = []
    for a in articles:
        key = f"{a['fldid']}_{a['dataid']}"
        if key not in seen:
            seen.add(key)
            unique.append(a)

    log.info(f"총 {len(unique)}건 게시글 수집 (중복 제거 후)")
    return unique

# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 2 — 게시글 → clip_id + ptoken → axz_vod → mp4 URL → 다운로드
# ═══════════════════════════════════════════════════════════════════════════

def extract_from_article(session, cookies: dict, grpid: str, fldid: str, dataid: str) -> dict | None:
    """게시글에서 clip_id, ptoken, 제목 추출."""
    url = f"https://cafe.daum.net/_c21_/bbs_read?grpid={grpid}&fldid={fldid}&datanum={dataid}"
    resp = session.get(url, cookies=cookies, timeout=15)
    if resp.status_code != 200:
        return None

    ptokens = re.findall(r'ptoken=([a-zA-Z0-9._-]+)', resp.text)
    clips = re.findall(r'cliplink/([a-zA-Z0-9]+)', resp.text)
    if not ptokens or not clips:
        return None

    # 게시글 제목
    title_match = re.search(r'<h3[^>]*class="tit_subject"[^>]*>([^<]+)</h3>', resp.text)
    if not title_match:
        title_match = re.search(r'<title>([^<]+)</title>', resp.text)
    title = title_match.group(1).strip() if title_match else ""
    title = title.replace(" - Daum 카페", "").strip()

    return {
        "clip_id": clips[0],
        "ptoken": ptokens[0],
        "title": title,
        "article_url": url,
    }


def get_axz_token(session, clip_id: str, ptoken: str) -> str | None:
    """embed 페이지 SSR에서 axz_vod JWT 추출."""
    embed_url = (
        f"https://kakaotv.daum.net/embed/player/cliplink/{clip_id}"
        f"?service=daum_cafe&f=p&ptoken={ptoken}&autoplay=0"
    )
    resp = session.get(embed_url,
        headers={"referer": "https://cafe.daum.net/"},
        timeout=15)
    if resp.status_code != 200:
        return None

    for jwt_str in set(re.findall(r'eyJ[a-zA-Z0-9._-]{50,}', resp.text)):
        try:
            parts = jwt_str.split(".")
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
            if payload.get("app_id") == "axz_vod":
                return jwt_str
        except Exception:
            continue
    return None


def get_mp4_url(session, cookies: dict, clip_id: str, ptoken: str, axz_token: str) -> dict | None:
    """kamp API에서 mp4 signed URL + 메타데이터 획득."""
    tid = hashlib.md5(f"{time.time()}_{uuid.uuid4()}".encode()).hexdigest()
    resp = session.get(
        f"https://kamp.daum.net/vod/v1/src/{clip_id}",
        params={
            "service": "daum_cafe", "f": "p",
            "ptoken": ptoken, "autoplay": "0",
            "tid": tid, "auth_type": "query",
            "csvc": "daum_cafe", "tit": "daum_cafe",
        },
        headers={
            "referer": "https://kakaotv.daum.net/",
            "origin": "https://kakaotv.daum.net",
            "x-kamp-auth": f"Bearer {axz_token}",
            "x-kamp-player": "kamp-player-web",
            "x-kamp-version": "2.0.21",
        },
        cookies=cookies,
        timeout=15,
    )
    if resp.status_code != 200:
        return None

    data = resp.json()
    # 최고 화질 mp4 URL 찾기
    profiles = data.get("profiles", [])
    mp4_urls = re.findall(r'https?://[^"\'\\,\s]+\.mp4[^"\'\\,\s]*', resp.text)

    best_profile = profiles[0] if profiles else {}
    return {
        "mp4_url": mp4_urls[0] if mp4_urls else None,
        "vid": data.get("vid", clip_id),
        "thumbnail": data.get("thumbnail", ""),
        "duration": best_profile.get("duration"),
        "width": best_profile.get("width"),
        "height": best_profile.get("height"),
        "filesize": best_profile.get("filesize"),
        "is_drm": data.get("is_drm", False),
        "profiles": profiles,
    }


def get_clip_metadata(session, clip_id: str) -> dict:
    """카카오TV API에서 메타데이터 획득."""
    try:
        resp = session.get(
            f"https://tv.kakao.com/api/v1/ft/cliplinks/{clip_id}",
            timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            ch = data.get("channel", {}) or {}
            return {
                "real_id": data.get("id"),
                "title": data.get("displayTitle", ""),
                "channel": ch.get("name", ""),
                "channel_id": str(ch.get("id", "")),
                "create_time": data.get("createTime", ""),
            }
    except Exception:
        pass
    return {}


def download_mp4(session, mp4_url: str, save_path: Path, desc: str = "") -> bool:
    """mp4 파일 다운로드 (Range 요청으로 이어받기 지원)."""
    save_path.parent.mkdir(parents=True, exist_ok=True)

    # 이미 완료된 파일 확인
    existing_size = save_path.stat().st_size if save_path.exists() else 0

    headers = {}
    if existing_size > 0:
        headers["Range"] = f"bytes={existing_size}-"

    try:
        resp = session.get(mp4_url, headers=headers, stream=True, timeout=600)
        if resp.status_code == 416:
            # Range not satisfiable — 이미 완료
            log.info(f"  이미 완료: {save_path.name}")
            return True
        if resp.status_code not in (200, 206):
            log.warning(f"  다운로드 실패: HTTP {resp.status_code}")
            return False

        total = None
        cr = resp.headers.get("content-range", "")
        if "/" in cr:
            total_str = cr.split("/")[-1]
            if total_str.isdigit():
                total = int(total_str)
        elif resp.headers.get("content-length"):
            total = int(resp.headers["content-length"]) + existing_size

        mode = "ab" if existing_size > 0 and resp.status_code == 206 else "wb"
        with open(save_path, mode) as f:
            with tqdm(total=total, initial=existing_size, unit="B",
                      unit_scale=True, desc=desc or save_path.name,
                      leave=False) as pbar:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        pbar.update(len(chunk))
        return True
    except Exception as e:
        log.warning(f"  다운로드 에러: {e}")
        return False


def download_thumbnail(session, thumb_url: str, save_path: Path) -> bool:
    """썸네일 다운로드."""
    if not thumb_url:
        return False
    try:
        resp = session.get(thumb_url, timeout=15)
        if resp.status_code == 200:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(resp.content)
            return True
    except Exception:
        pass
    return False

# ═══════════════════════════════════════════════════════════════════════════
#  HTML 생성 (main.py와 같은 카카오TV 스타일)
# ═══════════════════════════════════════════════════════════════════════════

KAKAO_TV_LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 40" width="140" height="28"><text x="0" y="30" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif" font-size="28" font-weight="800" fill="#1e1e1e" letter-spacing="-1">kakao</text><rect x="120" y="6" width="50" height="28" rx="14" fill="#fae100"/><text x="130" y="27" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif" font-size="17" font-weight="800" fill="#1e1e1e">tv</text></svg>'

def build_video_html(video_dir: Path, meta: dict):
    """video.html 생성 (카카오TV 스타일, self-contained)."""
    from jinja2 import Environment, BaseLoader

    # 썸네일 base64
    thumb_b64 = ""
    thumb_mime = ""
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        tf = video_dir / f"thumb{ext}"
        if tf.exists():
            thumb_mime = mimetypes.guess_type(str(tf))[0] or "image/png"
            thumb_b64 = base64.b64encode(tf.read_bytes()).decode("ascii")
            break

    has_video = (video_dir / "video.mp4").exists()
    duration = meta.get("duration") or 0
    view_count = meta.get("view_count") or 0
    upload_date = meta.get("upload_date", "")

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(meta.get('title',''))} - kakaoTV</title>
<style>
*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Malgun Gothic","맑은 고딕","Segoe UI",sans-serif;background:#f5f5f5;color:#1e1e1e;line-height:1.5}}
a{{color:inherit;text-decoration:none}}
.gnb{{background:#fff;border-bottom:1px solid #e5e5e5;height:56px;display:flex;align-items:center;padding:0 24px;position:sticky;top:0;z-index:100}}
.gnb-logo{{display:flex;align-items:center}}
.gnb-search{{margin-left:auto;display:flex;align-items:center;background:#f5f5f5;border-radius:20px;padding:6px 16px;width:300px}}
.gnb-search svg{{width:18px;height:18px;fill:#999;flex-shrink:0}}
.gnb-search span{{margin-left:8px;color:#999;font-size:14px}}
.channel-bar{{background:#fff;border-bottom:1px solid #e5e5e5;padding:0 24px;display:flex;align-items:center;height:52px;max-width:1200px;margin:0 auto}}
.channel-name{{font-size:17px;font-weight:700;color:#1e1e1e;padding-bottom:2px;border-bottom:3px solid #fae100}}
.container{{max-width:860px;margin:0 auto;background:#fff}}
.player-wrap{{background:#000;width:100%}}
.player-wrap video{{width:100%;display:block;max-height:480px;background:#000}}
.player-wrap .no-video{{padding:120px 20px;text-align:center;color:#888;font-size:15px;background:#000}}
.clip-title{{font-size:17px;font-weight:400;color:#1e1e1e;padding:20px 24px 8px;line-height:1.4}}
.clip-meta{{padding:0 24px 16px;font-size:13px;color:#999;display:flex;align-items:center;gap:4px}}
.clip-meta .sep{{margin:0 2px}}
.divider{{height:1px;background:#e5e5e5;margin:0 24px}}
.clip-desc{{padding:16px 24px;font-size:14px;color:#555;white-space:pre-wrap;word-break:break-word;line-height:1.7}}
.comments-notice{{margin:0 24px 16px;padding:14px 16px;background:#f9f9f9;border:1px solid #eee;border-radius:6px;font-size:13px;color:#999}}
.original-link{{padding:0 24px 24px;font-size:13px;color:#999}}
.original-link a{{color:#3c78c8}}
.original-link a:hover{{text-decoration:underline}}
.archive-banner{{background:#fff8d6;border-top:1px solid #f0e6a0;padding:12px 24px;font-size:12px;color:#8a7a2a;text-align:center}}
.footer{{background:#fff;border-top:1px solid #e5e5e5;padding:24px;text-align:center;font-size:12px;color:#999;max-width:860px;margin:0 auto}}
</style>
</head>
<body>
<div class="gnb">
  <div class="gnb-logo">{KAKAO_TV_LOGO_SVG}</div>
  <div class="gnb-search">
    <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
    <span>검색</span>
  </div>
</div>
<div class="channel-bar">
  <span class="channel-name">{_esc(meta.get('channel',''))}</span>
</div>
<div class="container">
  <div class="player-wrap">
  {'<video controls preload="metadata"' + (f' poster="data:{thumb_mime};base64,{thumb_b64}"' if thumb_b64 else '') + '><source src="video.mp4" type="video/mp4"></video>' if has_video else '<div class="no-video">영상 파일 없음</div>'}
  </div>
  <div class="clip-title">{_esc(meta.get('title',''))}</div>
  <div class="clip-meta">
    {f'<span>재생수 {view_count:,}</span><span class="sep">&middot;</span>' if view_count else ''}
    {f'<span>{upload_date[:4]}.{upload_date[4:6]}.{upload_date[6:8]}</span>' if len(upload_date) >= 8 else (f'<span>{_esc(meta.get("create_time",""))}</span>' if meta.get("create_time") else '')}
    {f'<span class="sep">&middot;</span><span>{duration//3600:02d}:{(duration%3600)//60:02d}:{duration%60:02d}</span>' if duration else ''}
  </div>
  <div class="divider"></div>
  {f'<div class="clip-desc">{_esc(meta.get("description",""))}</div><div class="divider"></div>' if meta.get("description") else ''}
  <div class="comments-notice">댓글 서비스가 2024년 7월에 종료되어 더 이상 제공되지 않습니다.</div>
  <div class="original-link">
    원본: <a href="{_esc(meta.get('article_url',''))}" target="_blank">{_esc(meta.get('article_url',''))}</a>
  </div>
</div>
<div class="archive-banner">이 페이지는 카카오TV 서비스 종료(2026-06-30) 전 아카이빙 목적으로 생성되었습니다.</div>
<div class="footer">Archived from kakaoTV &middot; Original &copy; Kakao Corp.</div>
</body>
</html>"""

    (video_dir / "video.html").write_text(html, encoding="utf-8")

def _esc(s: str) -> str:
    """HTML escape."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def process_one(session, cookies: dict, grpid: str, fldid: str, dataid: str) -> bool:
    """게시글 하나 처리: 추출 → 다운로드 → HTML 생성."""

    # 1) 게시글에서 clip_id + ptoken
    article = extract_from_article(session, cookies, grpid, fldid, dataid)
    if not article:
        log.warning(f"  [{dataid}] embed 영상 없음 — 스킵")
        return False

    clip_id = article["clip_id"]
    ptoken = article["ptoken"]
    title = article["title"]
    log.info(f"  [{dataid}] clip={clip_id}, title={title}")

    # 2) 메타데이터 (카카오TV API)
    meta = get_clip_metadata(session, clip_id)
    channel_id = meta.get("channel_id", "cafe_embed")
    channel = meta.get("channel", "")
    real_title = meta.get("title") or title
    sleep_polite()

    # 3) embed → axz_vod 토큰
    axz_token = get_axz_token(session, clip_id, ptoken)
    if not axz_token:
        log.warning(f"  [{dataid}] axz_vod 토큰 추출 실패")
        record_failure(article["article_url"], "cafe_axz", "no axz_vod token in embed")
        return False

    # 4) kamp → mp4 URL
    kamp_data = get_mp4_url(session, cookies, clip_id, ptoken, axz_token)
    if not kamp_data or not kamp_data.get("mp4_url"):
        log.warning(f"  [{dataid}] mp4 URL 획득 실패")
        record_failure(article["article_url"], "cafe_kamp", "no mp4 url")
        return False

    if kamp_data.get("is_drm"):
        log.warning(f"  [{dataid}] DRM 영상 — 스킵")
        record_failure(article["article_url"], "cafe_drm", "DRM protected")
        return False

    mp4_url = kamp_data["mp4_url"]
    duration = kamp_data.get("duration")
    filesize = kamp_data.get("filesize")
    log.info(f"  [{dataid}] {kamp_data.get('width')}x{kamp_data.get('height')}, "
             f"{filesize and f'{filesize/1024/1024:.1f}MB' or '?'}, "
             f"{duration and f'{duration}s' or '?'}")

    # 5) 다운로드 경로
    cid = channel_id or "cafe_embed"
    # clip_id가 알파벳이면 real_id 사용, 아니면 그대로
    vid = str(meta.get("real_id") or clip_id)
    video_dir = DATA_DIR / cid / vid
    video_dir.mkdir(parents=True, exist_ok=True)

    # mp4 다운로드
    mp4_path = video_dir / "video.mp4"
    if mp4_path.exists() and mp4_path.stat().st_size > 0:
        if filesize and abs(mp4_path.stat().st_size - filesize) < 1024:
            log.info(f"  [{dataid}] 이미 다운로드됨 — 스킵")
        else:
            log.info(f"  [{dataid}] 이어받기...")
            if not download_mp4(session, mp4_url, mp4_path, desc=real_title[:40]):
                return False
    else:
        if not download_mp4(session, mp4_url, mp4_path, desc=real_title[:40]):
            record_failure(article["article_url"], "cafe_download", "download failed")
            return False

    # 썸네일 다운로드
    thumb_url = kamp_data.get("thumbnail", "")
    if thumb_url:
        ext = ".png" if ".png" in thumb_url else ".jpg"
        download_thumbnail(session, thumb_url, video_dir / f"thumb{ext}")

    # info.json 저장
    info = {
        "clip_id": clip_id,
        "real_id": meta.get("real_id"),
        "title": real_title,
        "channel": channel,
        "channel_id": channel_id,
        "create_time": meta.get("create_time", ""),
        "duration": duration,
        "width": kamp_data.get("width"),
        "height": kamp_data.get("height"),
        "filesize": filesize,
        "article_url": article["article_url"],
        "source": "daum_cafe",
        "grpid": grpid,
    }
    (video_dir / "video.info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    # video.html 생성
    upload_date = ""
    ct = meta.get("create_time", "")
    if ct and len(ct) >= 10:
        upload_date = ct[:10].replace("-", "").replace(".", "")

    build_video_html(video_dir, {
        "title": real_title,
        "channel": channel,
        "duration": duration or 0,
        "view_count": 0,
        "upload_date": upload_date,
        "create_time": ct,
        "description": "",
        "article_url": article["article_url"],
    })

    # catalog에도 기록
    append_jsonl(CAFE_STATE, {
        "id": vid, "clip_id": clip_id,
        "title": real_title, "channel": channel,
        "channel_id": channel_id,
        "duration": duration,
        "article_url": article["article_url"],
        "src": "daum_cafe",
    })

    mark_done(clip_id)
    log.info(f"  [{dataid}] 완료!")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="다음 카페 embed 영상 다운로더",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  python cafe_dl.py --grpid zz4c --cookies cookies.txt\n"
            "  python cafe_dl.py --grpid zz4c --cookies cookies.txt --limit 5\n"
            "  python cafe_dl.py --grpid zz4c --cookies cookies.txt --list-only\n"
        ),
    )
    parser.add_argument("--grpid", required=True, help="카페 grpid (URL에서 확인)")
    parser.add_argument("--cookies", required=True, help="쿠키 파일 (JSON 또는 key=value)")
    parser.add_argument("--page-start", type=int, default=1)
    parser.add_argument("--page-end", type=int, default=999)
    parser.add_argument("--limit", type=int, default=0, help="다운로드 수 제한 (0=무제한)")
    parser.add_argument("--list-only", action="store_true", help="게시글 목록만 수집")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    cookies = load_cookies(args.cookies)
    session = cffi_requests.Session(impersonate="chrome")

    # 1) 게시글 목록 수집
    log.info("=" * 60)
    log.info("카페 동영상 게시글 수집")
    log.info("=" * 60)
    articles = list_cafe_articles(session, cookies, args.grpid,
                                   page_start=args.page_start,
                                   page_end=args.page_end)

    if not articles:
        log.error("게시글 없음. 쿠키가 만료되었을 수 있습니다.")
        return

    if args.list_only:
        # 목록만 저장
        list_file = STATE_DIR / f"cafe_{args.grpid}_articles.json"
        with open(list_file, "w") as f:
            json.dump(articles, f, ensure_ascii=False, indent=2)
        log.info(f"목록 저장: {list_file}")
        return

    # 2) 이미 완료된 항목 제외
    done_ids = load_done_ids()
    log.info(f"이미 완료: {len(done_ids)}건")

    # 3) 다운로드
    log.info("=" * 60)
    log.info("다운로드 시작")
    log.info("=" * 60)

    success = fail = skip = 0
    for i, article in enumerate(articles):
        if args.limit and success >= args.limit:
            log.info(f"제한 도달 ({args.limit}건)")
            break

        fldid = article["fldid"]
        dataid = article["dataid"]
        log.info(f"[{i+1}/{len(articles)}] fldid={fldid}, dataid={dataid}")

        # 게시글 읽어서 clip_id 확인 후 done 체크
        article_data = extract_from_article(session, cookies, args.grpid, fldid, dataid)
        if article_data and article_data["clip_id"] in done_ids:
            log.info(f"  이미 완료 — 스킵")
            skip += 1
            sleep_polite()
            continue

        try:
            ok = process_one(session, cookies, args.grpid, fldid, dataid)
            if ok:
                success += 1
            else:
                fail += 1
        except Exception as e:
            log.error(f"  에러: {e}")
            fail += 1

        sleep_polite()

    log.info("=" * 60)
    log.info(f"완료: 성공 {success}, 실패 {fail}, 스킵 {skip}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
