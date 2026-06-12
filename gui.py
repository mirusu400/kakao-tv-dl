#!/usr/bin/env python3
"""카카오TV 아카이브 — GUI

지원 입력:
  - 카카오TV 영상 URL (tv.kakao.com/v/... , tv.kakao.com/channel/.../cliplink/...)
  - 카카오TV 채널 URL (tv.kakao.com/channel/...)
  - 검색어 (일반 텍스트)
  - 다음 카페 URL (cafe.daum.net/...) — 쿠키 필요
"""

import base64
import hashlib
import json
import logging
import os
import queue
import random
import re
import sys
import threading
import time
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox

from curl_cffi import requests as cffi_requests
import yaml
import yt_dlp

# ── 경로 ──────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"

# ── config ────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    p = ROOT / "config.yaml"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}

CFG = _load_config()
THROTTLE = CFG.get("throttle", {})
SLEEP_MIN = THROTTLE.get("sleep_min", 2)
SLEEP_MAX = THROTTLE.get("sleep_max", 6)
MAX_HEIGHT = CFG.get("quality", {}).get("max_height", 1080)
EXCLUDE_KEYWORDS = [kw.lower() for kw in CFG.get("exclude", {}).get("keywords", ["뉴스", "news"])]
LIST_KEYS = ["clipList", "clips", "list", "items", "documents"]
SEARCH_URL = "https://tv.kakao.com/api/v1/ft/search/cliplinks"

# ── 로깅 → GUI 연동 ─────────────────────────────────────────────────────

log_queue: queue.Queue = queue.Queue()

class QueueHandler(logging.Handler):
    def emit(self, record):
        log_queue.put(self.format(record))

logger = logging.getLogger("kakao-gui")
logger.setLevel(logging.INFO)
_qh = QueueHandler()
_qh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_qh)

# ═══════════════════════════════════════════════════════════════════════════
#  공용 함수
# ═══════════════════════════════════════════════════════════════════════════

def _sleep():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

def _esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 40" width="140" height="28"><text x="0" y="30" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif" font-size="28" font-weight="800" fill="#1e1e1e" letter-spacing="-1">kakao</text><rect x="120" y="6" width="50" height="28" rx="14" fill="#fae100"/><text x="130" y="27" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif" font-size="17" font-weight="800" fill="#1e1e1e">tv</text></svg>'

def _build_html(video_dir: Path, meta: dict):
    """video.html 생성."""
    import mimetypes
    thumb_b64 = thumb_mime = ""
    for ext in (".png",".jpg",".jpeg",".webp"):
        tf = video_dir / f"thumb{ext}"
        if not tf.exists():
            for f in video_dir.iterdir():
                if f.suffix == ext and "thumb" in f.name.lower():
                    tf = f; break
                if f.suffix in (".png",".jpg",".jpeg",".webp") and not f.name.startswith("video"):
                    tf = f; break
        if tf.exists():
            thumb_mime = mimetypes.guess_type(str(tf))[0] or "image/png"
            thumb_b64 = base64.b64encode(tf.read_bytes()).decode("ascii")
            break
    has_video = any((video_dir / f"video.{e}").exists() for e in ("mp4","mkv","webm"))
    dur = int(meta.get("duration") or 0)
    ud = meta.get("upload_date","")
    html = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{_esc(meta.get('title',''))} - kakaoTV</title>
<style>*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,"Malgun Gothic","맑은 고딕","Segoe UI",sans-serif;background:#f5f5f5;color:#1e1e1e;line-height:1.5}}a{{color:inherit;text-decoration:none}}.gnb{{background:#fff;border-bottom:1px solid #e5e5e5;height:56px;display:flex;align-items:center;padding:0 24px;position:sticky;top:0;z-index:100}}.gnb-logo{{display:flex;align-items:center}}.channel-bar{{background:#fff;border-bottom:1px solid #e5e5e5;padding:0 24px;display:flex;align-items:center;height:52px;max-width:1200px;margin:0 auto}}.channel-name{{font-size:17px;font-weight:700;padding-bottom:2px;border-bottom:3px solid #fae100}}.container{{max-width:860px;margin:0 auto;background:#fff}}.player-wrap{{background:#000}}.player-wrap video{{width:100%;display:block;max-height:480px}}.no-video{{padding:120px 20px;text-align:center;color:#888;font-size:15px;background:#000}}.clip-title{{font-size:17px;padding:20px 24px 8px}}.clip-meta{{padding:0 24px 16px;font-size:13px;color:#999}}.divider{{height:1px;background:#e5e5e5;margin:0 24px}}.clip-desc{{padding:16px 24px;font-size:14px;color:#555;white-space:pre-wrap;line-height:1.7}}.comments-notice{{margin:0 24px 16px;padding:14px 16px;background:#f9f9f9;border:1px solid #eee;border-radius:6px;font-size:13px;color:#999}}.original-link{{padding:0 24px 24px;font-size:13px;color:#999}}.original-link a{{color:#3c78c8}}.archive-banner{{background:#fff8d6;border-top:1px solid #f0e6a0;padding:12px 24px;font-size:12px;color:#8a7a2a;text-align:center}}.footer{{background:#fff;border-top:1px solid #e5e5e5;padding:24px;text-align:center;font-size:12px;color:#999;max-width:860px;margin:0 auto}}</style></head><body>
<div class="gnb"><div class="gnb-logo">{LOGO_SVG}</div></div>
<div class="channel-bar"><span class="channel-name">{_esc(meta.get('channel',''))}</span></div>
<div class="container"><div class="player-wrap">{'<video controls preload="metadata"' + (f' poster="data:{thumb_mime};base64,{thumb_b64}"' if thumb_b64 else '') + '><source src="video.mp4" type="video/mp4"></video>' if has_video else '<div class="no-video">영상 파일 없음</div>'}</div>
<div class="clip-title">{_esc(meta.get('title',''))}</div>
<div class="clip-meta">{(ud[:4]+'.'+ud[4:6]+'.'+ud[6:8]) if len(ud)>=8 else _esc(meta.get('create_time',''))}{f' &middot; {dur//3600:02d}:{(dur%3600)//60:02d}:{dur%60:02d}' if dur else ''}</div>
<div class="divider"></div>
{f'<div class="clip-desc">{_esc(meta.get("description",""))}</div><div class="divider"></div>' if meta.get('description') else ''}
<div class="comments-notice">댓글 서비스가 2024년 7월에 종료되어 더 이상 제공되지 않습니다.</div>
<div class="original-link">원본: <a href="{_esc(meta.get('webpage_url',''))}" target="_blank">{_esc(meta.get('webpage_url',''))}</a></div>
</div><div class="archive-banner">이 페이지는 카카오TV 서비스 종료(2026-06-30) 전 아카이빙 목적으로 생성되었습니다.</div>
<div class="footer">Archived from kakaoTV &middot; Original &copy; Kakao Corp.</div></body></html>"""
    (video_dir / "video.html").write_text(html, encoding="utf-8")

# ═══════════════════════════════════════════════════════════════════════════
#  URL 분류
# ═══════════════════════════════════════════════════════════════════════════

def classify_input(line: str) -> dict:
    """입력 줄을 분류해서 dict 반환."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # 카페 URL
    if "cafe.daum.net" in line:
        m = re.search(r'grpid=([a-zA-Z0-9]+)', line)
        if not m:
            m = re.search(r'cafe\.daum\.net/([a-zA-Z0-9_]+)', line)
        grpid = m.group(1) if m else ""
        return {"type": "cafe", "url": line, "grpid": grpid}
    # 카카오TV 영상 URL
    if re.search(r'tv\.kakao\.com.*/(?:v|cliplink)/\d+', line):
        return {"type": "video", "url": line}
    # 카카오TV 채널 URL
    if re.search(r'tv\.kakao\.com/channel/\d+', line):
        return {"type": "channel", "url": line}
    # 그 외 URL
    if line.startswith("http"):
        return {"type": "video", "url": line}
    # 일반 텍스트 → 검색어
    return {"type": "search", "query": line}

# ═══════════════════════════════════════════════════════════════════════════
#  Worker — 카카오TV 영상 (yt-dlp)
# ═══════════════════════════════════════════════════════════════════════════

def _download_single_video(url: str, update_fn):
    """yt-dlp로 단일 영상 다운로드 + HTML 생성."""
    # 메타데이터
    update_fn("메타데이터 수집...")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        if info is None:
            logger.warning("메타 실패: extract_info returned None")
            update_fn("실패 (메타)")
            return False
    except Exception as e:
        logger.warning(f"메타 에러: {e}")
        update_fn("실패 (메타)")
        return False

    vid = str(info.get("id",""))
    cid = info.get("uploader_id") or info.get("channel_id") or "unknown"
    title = info.get("title","")
    update_fn(f"다운로드: {title[:30]}...")

    output = str(DATA_DIR / f"{cid}/{vid}/video.%(ext)s")
    fmt = f"bestvideo[height<={MAX_HEIGHT}]+bestaudio/best[height<={MAX_HEIGHT}]/best"
    ydl_opts = {
        "format": fmt,
        "writeinfojson": True,
        "writethumbnail": True,
        "writedescription": True,
        "merge_output_format": "mp4",
        "nooverwrites": True,
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 10,
        "outtmpl": output,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        logger.warning(f"다운로드 실패: {e}")
        update_fn("실패 (다운로드)")
        return False

    # HTML 생성
    video_dir = DATA_DIR / cid / vid
    if video_dir.exists():
        ud = info.get("upload_date","")
        _build_html(video_dir, {
            "title": title,
            "channel": info.get("uploader") or info.get("channel",""),
            "duration": info.get("duration",0),
            "upload_date": ud,
            "description": info.get("description",""),
            "webpage_url": info.get("webpage_url", url),
        })

    update_fn("완료")
    logger.info(f"완료: {title}")
    return True

# ═══════════════════════════════════════════════════════════════════════════
#  Worker — 카카오TV 검색
# ═══════════════════════════════════════════════════════════════════════════

def _search_and_download(query: str, update_fn, stop_event: threading.Event):
    from urllib.parse import quote
    session = cffi_requests.Session(impersonate="chrome")
    session.headers.update({"x-requested-with": "XMLHttpRequest"})
    session.headers["referer"] = f"https://tv.kakao.com/search/cliplinks?q={quote(query)}"

    update_fn(f"검색: {query}")
    page = 1
    found = 0
    empty = 0
    while page <= 200 and not stop_event.is_set():
        params = {"sort":"Score","q":query,"fulllevels":"list",
                  "fields":"-user,-clipChapterThumbnailList,-tagList",
                  "size":20,"page":page,"_":int(time.time()*1000)}
        try:
            resp = session.get(SEARCH_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"검색 API 에러: {e}")
            break

        clips = None
        for k in LIST_KEYS:
            if k in data and isinstance(data[k], list):
                clips = data[k]; break
        if clips is None:
            for v in data.values():
                if isinstance(v, dict):
                    for k in LIST_KEYS:
                        if k in v and isinstance(v[k], list):
                            clips = v[k]; break
                    if clips: break
        if not clips:
            empty += 1
            if empty >= 2: break
            page += 1; _sleep(); continue

        new = 0
        for item in clips:
            if stop_event.is_set(): break
            clip_id = str(item.get("id") or item.get("clipLinkId") or "")
            if not clip_id: continue
            ch = item.get("channel") or {}
            ch_id = str(ch.get("id","")) if isinstance(ch,dict) else ""
            title = item.get("title") or ""
            if any(kw in title.lower() for kw in EXCLUDE_KEYWORDS): continue
            video_url = f"https://tv.kakao.com/channel/{ch_id}/cliplink/{clip_id}" if ch_id else f"https://tv.kakao.com/v/{clip_id}"
            update_fn(f"[{found+1}] {title[:30]}...")
            _download_single_video(video_url, lambda s: logger.info(f"  {s}"))
            found += 1; new += 1
            _sleep()

        if new == 0:
            empty += 1
            if empty >= 3: break
        else:
            empty = 0
        logger.info(f"검색 '{query}' page {page}: {len(clips)}건, {new}건 다운로드")
        page += 1; _sleep()

    update_fn(f"완료 ({found}건)")
    return found

# ═══════════════════════════════════════════════════════════════════════════
#  Worker — 채널
# ═══════════════════════════════════════════════════════════════════════════

def _channel_download(url: str, update_fn, stop_event: threading.Event):
    update_fn("채널 펼침...")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as ydl:
            data = ydl.extract_info(url, download=False)
        if data is None:
            update_fn("실패 (채널 로드)")
            return 0
    except Exception as e:
        update_fn(f"실패: {e}")
        return 0

    entries = data.get("entries",[])
    logger.info(f"채널 {url}: {len(entries)}개 영상")
    done = 0
    for i, e in enumerate(entries):
        if stop_event.is_set(): break
        eid = str(e.get("id",""))
        vurl = e.get("url") or e.get("webpage_url") or f"https://tv.kakao.com/v/{eid}"
        title = e.get("title","")
        update_fn(f"[{i+1}/{len(entries)}] {title[:25]}...")
        _download_single_video(vurl, lambda s: logger.info(f"  {s}"))
        done += 1
        _sleep()
    update_fn(f"완료 ({done}/{len(entries)})")
    return done

# ═══════════════════════════════════════════════════════════════════════════
#  Worker — 카페
# ═══════════════════════════════════════════════════════════════════════════

def _cafe_download(grpid: str, cookies: dict, update_fn, stop_event: threading.Event):
    """cafe_dl.py 로직 인라인."""
    session = cffi_requests.Session(impersonate="chrome")

    # 목록 수집
    update_fn("카페 목록 수집...")
    articles = []
    page = 1
    while page <= 999 and not stop_event.is_set():
        resp = session.get("https://cafe.daum.net/_c21_/movie_bbs_list",
            params={"grpid":grpid,"page":str(page),"listnum":"32"},
            cookies=cookies, timeout=15)
        if resp.status_code != 200: break
        pairs = re.findall(r"fldid:\s*'([^']+)'.*?dataid:\s*'(\d+)'", resp.text, re.DOTALL)
        if not pairs: break
        for fldid, dataid in pairs:
            articles.append({"fldid":fldid,"dataid":dataid})
        logger.info(f"카페 page {page}: {len(pairs)}건")
        if f"page={page+1}" not in resp.text: break
        page += 1; _sleep()

    # 중복 제거
    seen = set()
    unique = []
    for a in articles:
        k = f"{a['fldid']}_{a['dataid']}"
        if k not in seen: seen.add(k); unique.append(a)
    articles = unique
    logger.info(f"카페 총 {len(articles)}건")

    done_file = STATE_DIR / "cafe_done.txt"
    done_ids = set()
    if done_file.exists():
        done_ids = set(done_file.read_text().splitlines())

    success = 0
    for i, art in enumerate(articles):
        if stop_event.is_set(): break
        fldid, dataid = art["fldid"], art["dataid"]
        update_fn(f"[{i+1}/{len(articles)}] 게시글 {dataid}...")

        # 게시글 → clip_id + ptoken
        resp = session.get(f"https://cafe.daum.net/_c21_/bbs_read?grpid={grpid}&fldid={fldid}&datanum={dataid}",
            cookies=cookies, timeout=15)
        if resp.status_code != 200: continue
        ptokens = re.findall(r'ptoken=([a-zA-Z0-9._-]+)', resp.text)
        clips = re.findall(r'cliplink/([a-zA-Z0-9]+)', resp.text)
        if not ptokens or not clips:
            logger.info(f"  [{dataid}] embed 없음"); continue

        clip_id, ptoken = clips[0], ptokens[0]
        if clip_id in done_ids:
            logger.info(f"  [{dataid}] 이미 완료"); continue

        # 메타데이터
        meta = {}
        try:
            mr = session.get(f"https://tv.kakao.com/api/v1/ft/cliplinks/{clip_id}", timeout=10)
            if mr.status_code == 200:
                md = mr.json()
                ch = md.get("channel",{}) or {}
                meta = {"real_id":md.get("id"), "title":md.get("displayTitle",""),
                        "channel":ch.get("name",""), "channel_id":str(ch.get("id","")),
                        "create_time":md.get("createTime","")}
        except: pass
        _sleep()

        title = meta.get("title","")
        update_fn(f"[{i+1}/{len(articles)}] {title[:25]}...")

        # embed → axz_vod
        embed_url = f"https://kakaotv.daum.net/embed/player/cliplink/{clip_id}?service=daum_cafe&f=p&ptoken={ptoken}&autoplay=0"
        er = session.get(embed_url, headers={"referer":"https://cafe.daum.net/"}, timeout=15)
        axz = None
        for j in set(re.findall(r'eyJ[a-zA-Z0-9._-]{50,}', er.text)):
            try:
                p = json.loads(base64.urlsafe_b64decode(j.split(".")[1]+"=="))
                if p.get("app_id") == "axz_vod": axz = j; break
            except: continue
        if not axz:
            logger.warning(f"  [{dataid}] axz 토큰 실패"); continue

        # kamp → mp4
        tid = hashlib.md5(f"{time.time()}_{uuid.uuid4()}".encode()).hexdigest()
        kr = session.get(f"https://kamp.daum.net/vod/v1/src/{clip_id}",
            params={"service":"daum_cafe","f":"p","ptoken":ptoken,"autoplay":"0",
                    "tid":tid,"auth_type":"query","csvc":"daum_cafe","tit":"daum_cafe"},
            headers={"referer":"https://kakaotv.daum.net/","origin":"https://kakaotv.daum.net",
                     "x-kamp-auth":f"Bearer {axz}","x-kamp-player":"kamp-player-web","x-kamp-version":"2.0.21"},
            cookies=cookies, timeout=15)
        if kr.status_code != 200:
            logger.warning(f"  [{dataid}] kamp 실패: {kr.status_code}"); continue

        kdata = kr.json()
        mp4s = re.findall(r'https?://[^"\'\\,\s]+\.mp4[^"\'\\,\s]*', kr.text)
        if not mp4s:
            logger.warning(f"  [{dataid}] mp4 URL 없음"); continue

        mp4_url = mp4s[0]
        profiles = kdata.get("profiles",[])
        best = profiles[0] if profiles else {}
        filesize = best.get("filesize",0)
        logger.info(f"  [{dataid}] {best.get('width','?')}x{best.get('height','?')}, "
                     f"{filesize/1024/1024:.1f}MB" if filesize else "?")

        # 다운로드
        cid = meta.get("channel_id","cafe")
        vid = str(meta.get("real_id") or clip_id)
        video_dir = DATA_DIR / cid / vid
        video_dir.mkdir(parents=True, exist_ok=True)
        mp4_path = video_dir / "video.mp4"

        if mp4_path.exists() and filesize and abs(mp4_path.stat().st_size - filesize) < 1024:
            logger.info(f"  [{dataid}] 이미 존재")
        else:
            try:
                dl = session.get(mp4_url, stream=True, timeout=600)
                if dl.status_code not in (200,206):
                    logger.warning(f"  [{dataid}] 다운로드 HTTP {dl.status_code}"); continue
                with open(mp4_path,"wb") as f:
                    for chunk in dl.iter_content(256*1024):
                        if stop_event.is_set(): break
                        if chunk: f.write(chunk)
            except Exception as e:
                logger.warning(f"  [{dataid}] 다운로드 에러: {e}"); continue

        # 썸네일
        thumb = kdata.get("thumbnail","")
        if thumb:
            try:
                ext = ".png" if ".png" in thumb else ".jpg"
                tr = session.get(thumb, timeout=15)
                if tr.status_code == 200:
                    (video_dir / f"thumb{ext}").write_bytes(tr.content)
            except: pass

        # info.json
        (video_dir / "video.info.json").write_text(json.dumps({
            "clip_id":clip_id,"real_id":meta.get("real_id"),"title":title,
            "channel":meta.get("channel",""),"channel_id":cid,
            "duration":best.get("duration"),"source":"daum_cafe","grpid":grpid,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        # HTML
        ct = meta.get("create_time","")
        ud = ct[:10].replace("-","").replace(".","") if ct and len(ct)>=10 else ""
        _build_html(video_dir, {"title":title,"channel":meta.get("channel",""),
            "duration":best.get("duration",0),"upload_date":ud,"create_time":ct,
            "webpage_url":f"https://cafe.daum.net/_c21_/bbs_read?grpid={grpid}&fldid={fldid}&datanum={dataid}"})

        with open(done_file,"a") as f: f.write(clip_id+"\n")
        done_ids.add(clip_id)
        success += 1
        logger.info(f"  [{dataid}] 완료: {title}")
        _sleep()

    update_fn(f"완료 ({success}/{len(articles)})")
    return success

# ═══════════════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("카카오TV 아카이브")
        self.geometry("820x700")
        self.configure(bg="#f5f5f5")
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.cookies = {}
        self._build_ui()
        self._poll_log()

    # ── UI 빌드 ──────────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Yellow.TButton", background="#fae100", foreground="#1e1e1e", font=("sans-serif",11,"bold"))
        style.configure("TLabel", background="#f5f5f5", font=("sans-serif",10))
        style.configure("Header.TLabel", font=("sans-serif",16,"bold"), background="#f5f5f5")

        # ── 헤더 ──
        hdr = ttk.Frame(self, padding=10)
        hdr.pack(fill="x")
        ttk.Label(hdr, text="kakao tv 아카이브", style="Header.TLabel").pack(side="left")

        # ── URL 입력 ──
        frm_input = ttk.LabelFrame(self, text="  URL / 검색어 입력  ", padding=8)
        frm_input.pack(fill="x", padx=10, pady=(0,5))

        self.txt_urls = scrolledtext.ScrolledText(frm_input, height=5, font=("Consolas",11),
            wrap="word", bg="#fff", fg="#1e1e1e", insertbackground="#1e1e1e",
            relief="solid", borderwidth=1)
        self.txt_urls.pack(fill="x")
        self.txt_urls.insert("1.0",
            "# 한 줄에 하나씩 입력 (영상/채널 URL, 검색어, 카페 URL)\n"
            "# 예: https://tv.kakao.com/channel/12345\n"
            "# 예: 더빙\n"
            "# 예: https://cafe.daum.net/mycafe (쿠키 필요)\n")

        # ── 쿠키 ──
        frm_cookie = ttk.Frame(self, padding=(10,0))
        frm_cookie.pack(fill="x")
        ttk.Label(frm_cookie, text="쿠키 (카페용):").pack(side="left")
        self.var_cookie = tk.StringVar(value=str(ROOT/"cookies.txt") if (ROOT/"cookies.txt").exists() else "")
        ent = ttk.Entry(frm_cookie, textvariable=self.var_cookie, width=50)
        ent.pack(side="left", padx=5)
        ttk.Button(frm_cookie, text="찾아보기", command=self._browse_cookie).pack(side="left")
        ttk.Button(frm_cookie, text="붙여넣기", command=self._paste_cookie).pack(side="left", padx=5)

        # ── 버튼 ──
        frm_btn = ttk.Frame(self, padding=10)
        frm_btn.pack(fill="x")
        self.btn_start = ttk.Button(frm_btn, text=" 시작 ", style="Yellow.TButton", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(frm_btn, text=" 중지 ", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=5)
        self.lbl_status = ttk.Label(frm_btn, text="대기 중")
        self.lbl_status.pack(side="left", padx=10)

        # ── 작업 큐 ──
        frm_queue = ttk.LabelFrame(self, text="  작업 큐  ", padding=5)
        frm_queue.pack(fill="both", expand=True, padx=10, pady=5)

        cols = ("type","input","status")
        self.tree = ttk.Treeview(frm_queue, columns=cols, show="headings", height=6)
        self.tree.heading("type", text="유형")
        self.tree.heading("input", text="입력")
        self.tree.heading("status", text="상태")
        self.tree.column("type", width=70, stretch=False)
        self.tree.column("input", width=450)
        self.tree.column("status", width=200)
        sb = ttk.Scrollbar(frm_queue, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ── 로그 ──
        frm_log = ttk.LabelFrame(self, text="  로그  ", padding=5)
        frm_log.pack(fill="both", expand=True, padx=10, pady=(0,10))
        self.txt_log = scrolledtext.ScrolledText(frm_log, height=10, font=("Consolas",10),
            bg="#1e1e1e", fg="#e0e0e0", insertbackground="#e0e0e0",
            wrap="word", state="disabled", relief="flat")
        self.txt_log.pack(fill="both", expand=True)

    # ── 이벤트 핸들러 ────────────────────────────────────────────────────

    def _browse_cookie(self):
        path = filedialog.askopenfilename(title="쿠키 파일 선택",
            filetypes=[("Text/JSON","*.txt *.json"),("All","*.*")])
        if path:
            self.var_cookie.set(path)

    def _paste_cookie(self):
        """클립보드 내용을 cookies.txt로 저장."""
        try:
            text = self.clipboard_get()
        except:
            messagebox.showwarning("붙여넣기", "클립보드가 비어있습니다")
            return
        if not text.strip():
            return
        # key value 쌍 파싱 시도
        cookies = {}
        for line in text.strip().splitlines():
            line = line.strip()
            if not line: continue
            # "key    value    .domain ..." 형태 (DevTools 복사)
            parts = re.split(r'\s{2,}', line)
            if len(parts) >= 2 and not parts[0].startswith("."):
                cookies[parts[0]] = parts[1]
            elif "=" in line:
                k, v = line.split("=", 1)
                cookies[k.strip()] = v.strip()
        if cookies:
            path = ROOT / "cookies.txt"
            path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            self.var_cookie.set(str(path))
            logger.info(f"쿠키 저장: {len(cookies)}개 → {path}")
        else:
            messagebox.showwarning("붙여넣기", "쿠키를 파싱할 수 없습니다")

    def _start(self):
        text = self.txt_urls.get("1.0","end").strip()
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
        if not lines:
            messagebox.showinfo("알림", "URL 또는 검색어를 입력하세요")
            return

        # 쿠키 로드
        cookie_path = self.var_cookie.get().strip()
        if cookie_path and Path(cookie_path).exists():
            try:
                ct = Path(cookie_path).read_text().strip()
                self.cookies = json.loads(ct) if ct.startswith("{") else {}
            except:
                self.cookies = {}

        # 작업 큐 구성
        self.tree.delete(*self.tree.get_children())
        tasks = []
        for line in lines:
            c = classify_input(line)
            if not c: continue
            label_map = {"video":"영상","channel":"채널","search":"검색","cafe":"카페"}
            display = c.get("url") or c.get("query","")
            iid = self.tree.insert("","end", values=(label_map.get(c["type"],"?"), display[:60], "대기"))
            tasks.append((c, iid))

        if not tasks:
            return

        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_status.configure(text="작업 중...")

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.worker_thread = threading.Thread(target=self._worker, args=(tasks,), daemon=True)
        self.worker_thread.start()

    def _stop(self):
        self.stop_event.set()
        self.lbl_status.configure(text="중지 요청...")
        logger.info("중지 요청됨")

    def _worker(self, tasks):
        total = len(tasks)
        for idx, (task, iid) in enumerate(tasks):
            if self.stop_event.is_set():
                self._update_tree(iid, "중지됨")
                continue

            self.lbl_status.configure(text=f"작업 중... ({idx+1}/{total})")

            def update(s, _iid=iid):
                self._update_tree(_iid, s)

            try:
                if task["type"] == "video":
                    update("다운로드 중...")
                    _download_single_video(task["url"], update)
                elif task["type"] == "channel":
                    _channel_download(task["url"], update, self.stop_event)
                elif task["type"] == "search":
                    _search_and_download(task["query"], update, self.stop_event)
                elif task["type"] == "cafe":
                    if not self.cookies:
                        update("실패: 쿠키 없음")
                        logger.error("카페 다운로드에 쿠키가 필요합니다")
                    else:
                        grpid = task.get("grpid","")
                        if not grpid:
                            update("실패: grpid 없음")
                        else:
                            _cafe_download(grpid, self.cookies, update, self.stop_event)
            except Exception as e:
                logger.error(f"에러: {e}")
                update(f"에러: {str(e)[:50]}")

        self.after(0, self._work_done)

    def _work_done(self):
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        status = "완료" if not self.stop_event.is_set() else "중지됨"
        self.lbl_status.configure(text=status)
        logger.info(f"=== 전체 작업 {status} ===")

    def _update_tree(self, iid, status):
        try:
            self.tree.set(iid, "status", status)
        except:
            pass

    # ── 로그 폴링 ────────────────────────────────────────────────────────

    def _poll_log(self):
        while not log_queue.empty():
            try:
                msg = log_queue.get_nowait()
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", msg + "\n")
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
            except queue.Empty:
                break
        self.after(100, self._poll_log)

# ═══════════════════════════════════════════════════════════════════════════

def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
