#!/usr/bin/env python3
"""Stage 5 — Build offline static site from catalog + downloaded data.

Generates a self-contained HTML site in site/ that works offline.
- site/index.html: channel grid with search/filter/sort
- site/<channel_id>/<video_id>.html: detail page with local video player
"""

import json
import logging
import shutil
import sys
from pathlib import Path
from collections import defaultdict

try:
    from jinja2 import Environment, BaseLoader
except ImportError:
    print("Install jinja2: pip install jinja2")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
CATALOG_FILE = STATE_DIR / "catalog.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("build_site")

# ── Templates ───────────────────────────────────────────────────────────────

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>카카오TV 아카이브</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #1a1a1a; color: #e0e0e0; }
.header { background: #fae100; color: #000; padding: 20px; text-align: center; }
.header h1 { font-size: 24px; }
.header p { font-size: 14px; margin-top: 4px; color: #333; }
.controls { padding: 16px 20px; background: #222; display: flex; gap: 12px; flex-wrap: wrap; }
.controls input, .controls select {
    padding: 8px 12px; border: 1px solid #444; border-radius: 4px;
    background: #333; color: #e0e0e0; font-size: 14px;
}
.controls input { flex: 1; min-width: 200px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: 16px; padding: 20px; }
.card { background: #2a2a2a; border-radius: 8px; overflow: hidden;
        transition: transform 0.2s; cursor: pointer; }
.card:hover { transform: translateY(-2px); }
.card img { width: 100%; aspect-ratio: 16/9; object-fit: cover; background: #333; }
.card .info { padding: 12px; }
.card .title { font-size: 14px; font-weight: 600; margin-bottom: 4px;
               display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
               overflow: hidden; }
.card .meta { font-size: 12px; color: #888; }
.card a { color: inherit; text-decoration: none; }
.stats { padding: 8px 20px; background: #222; font-size: 13px; color: #888; }
.no-results { text-align: center; padding: 60px 20px; color: #666; }
</style>
</head>
<body>
<div class="header">
  <h1>카카오TV 아카이브</h1>
  <p>서비스 종료(2026-06-30) 전 백업 — 총 {{ videos|length }}개 영상</p>
</div>
<div class="controls">
  <input type="text" id="search" placeholder="검색 (제목, 채널)..." oninput="filterCards()">
  <select id="sort" onchange="filterCards()">
    <option value="date_desc">최신순</option>
    <option value="date_asc">오래된순</option>
    <option value="views">조회수순</option>
    <option value="title">제목순</option>
  </select>
  <select id="channel-filter" onchange="filterCards()">
    <option value="">전체 채널</option>
    {% for ch in channels %}
    <option value="{{ ch }}">{{ ch }}</option>
    {% endfor %}
  </select>
</div>
<div class="stats" id="stats"></div>
<div class="grid" id="grid">
{% for v in videos %}
  <div class="card" data-title="{{ v.title|lower }}" data-channel="{{ v.channel or '' }}"
       data-date="{{ v.upload_date or '00000000' }}" data-views="{{ v.view_count or 0 }}">
    <a href="{{ v.channel_id or 'unknown' }}/{{ v.id }}.html">
      <img src="{{ v.thumb_path }}" alt="{{ v.title }}" loading="lazy"
           onerror="this.style.display='none'">
      <div class="info">
        <div class="title">{{ v.title }}</div>
        <div class="meta">
          {{ v.channel or '알 수 없음' }}
          {% if v.upload_date %} · {{ v.upload_date[:4] }}.{{ v.upload_date[4:6] }}.{{ v.upload_date[6:8] }}{% endif %}
          {% if v.view_count %} · 조회수 {{ "{:,}".format(v.view_count) }}{% endif %}
          {% if v.duration %} · {{ (v.duration // 60) }}:{{ "%02d"|format(v.duration % 60) }}{% endif %}
        </div>
      </div>
    </a>
  </div>
{% endfor %}
</div>
<div class="no-results" id="no-results" style="display:none">검색 결과가 없습니다</div>
<script>
function filterCards() {
  const q = document.getElementById('search').value.toLowerCase();
  const sort = document.getElementById('sort').value;
  const chFilter = document.getElementById('channel-filter').value;
  const grid = document.getElementById('grid');
  const cards = Array.from(grid.children);
  let visible = 0;
  cards.forEach(c => {
    const title = c.dataset.title;
    const channel = c.dataset.channel;
    const match = (!q || title.includes(q) || channel.toLowerCase().includes(q))
                && (!chFilter || channel === chFilter);
    c.style.display = match ? '' : 'none';
    if (match) visible++;
  });
  // Sort
  const sorted = cards.sort((a, b) => {
    if (sort === 'date_desc') return b.dataset.date.localeCompare(a.dataset.date);
    if (sort === 'date_asc') return a.dataset.date.localeCompare(b.dataset.date);
    if (sort === 'views') return (parseInt(b.dataset.views)||0) - (parseInt(a.dataset.views)||0);
    if (sort === 'title') return a.dataset.title.localeCompare(b.dataset.title);
    return 0;
  });
  sorted.forEach(c => grid.appendChild(c));
  document.getElementById('stats').textContent = visible + '개 영상 표시 중';
  document.getElementById('no-results').style.display = visible ? 'none' : '';
}
filterCards();
</script>
</body>
</html>"""

DETAIL_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ v.title }} — 카카오TV 아카이브</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #1a1a1a; color: #e0e0e0; max-width: 900px; margin: 0 auto; }
.nav { padding: 12px 16px; background: #222; }
.nav a { color: #fae100; text-decoration: none; font-size: 14px; }
.player { background: #000; }
.player video { width: 100%; max-height: 70vh; }
.info { padding: 20px; }
.info h1 { font-size: 20px; margin-bottom: 8px; }
.meta { color: #888; font-size: 14px; margin-bottom: 16px; }
.meta span { margin-right: 16px; }
.description { white-space: pre-wrap; font-size: 14px; line-height: 1.6;
               background: #2a2a2a; padding: 16px; border-radius: 8px; margin-top: 12px; }
.comments-notice { margin-top: 16px; padding: 12px; background: #2a2a2a;
                    border-radius: 8px; color: #666; font-size: 13px; }
.original-link { margin-top: 12px; font-size: 13px; }
.original-link a { color: #fae100; }
</style>
</head>
<body>
<div class="nav"><a href="../../index.html">← 목록으로</a></div>
<div class="player">
  {% if v.video_path %}
  <video controls preload="metadata" poster="{{ v.thumb_path }}">
    <source src="{{ v.video_path }}" type="video/mp4">
    브라우저가 동영상을 지원하지 않습니다.
  </video>
  {% else %}
  <div style="padding:60px;text-align:center;color:#666">영상 파일 없음</div>
  {% endif %}
</div>
<div class="info">
  <h1>{{ v.title }}</h1>
  <div class="meta">
    <span>{{ v.channel or '알 수 없음' }}</span>
    {% if v.upload_date %}<span>{{ v.upload_date[:4] }}.{{ v.upload_date[4:6] }}.{{ v.upload_date[6:8] }}</span>{% endif %}
    {% if v.view_count %}<span>조회수 {{ "{:,}".format(v.view_count) }}</span>{% endif %}
    {% if v.duration %}<span>{{ (v.duration // 60) }}:{{ "%02d"|format(v.duration % 60) }}</span>{% endif %}
  </div>
  {% if v.description %}
  <div class="description">{{ v.description }}</div>
  {% endif %}
  <div class="comments-notice">댓글: 카카오TV 댓글 서비스가 2024년 7월에 종료되어 제공되지 않습니다.</div>
  <div class="original-link">원본: <a href="{{ v.webpage_url }}" target="_blank">{{ v.webpage_url }}</a></div>
</div>
</body>
</html>"""

# ── Build ───────────────────────────────────────────────────────────────────

def find_video_file(channel_id: str, video_id: str) -> str | None:
    cid = channel_id or "unknown_channel"
    video_dir = DATA_DIR / cid / video_id
    if not video_dir.exists():
        return None
    for f in video_dir.iterdir():
        if f.name.startswith("video.") and f.suffix in (".mp4", ".mkv", ".webm"):
            return f.name
    return None

def find_thumb_file(channel_id: str, video_id: str) -> str | None:
    cid = channel_id or "unknown_channel"
    video_dir = DATA_DIR / cid / video_id
    if not video_dir.exists():
        return None
    for f in video_dir.iterdir():
        if "thumb" in f.name.lower() or f.suffix in (".jpg", ".jpeg", ".png", ".webp"):
            if not f.name.startswith("video."):
                return f.name
    return None

def build():
    if not CATALOG_FILE.exists():
        log.error("catalog.jsonl not found. Run Stage 2 first.")
        sys.exit(1)

    # Load catalog
    videos = []
    with open(CATALOG_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                videos.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    log.info(f"Loaded {len(videos)} videos from catalog")

    # Prepare site directory
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    env = Environment(loader=BaseLoader())

    # Enrich with file paths and build detail pages
    channels = set()
    for v in videos:
        vid = str(v.get("id", ""))
        cid = v.get("channel_id", "") or "unknown_channel"
        channel_name = v.get("channel", "")
        if channel_name:
            channels.add(channel_name)

        # Find local files
        video_file = find_video_file(cid, vid)
        thumb_file = find_thumb_file(cid, vid)

        # Relative paths for detail page (detail is at site/<cid>/<vid>.html)
        v["video_path"] = f"../../data/{cid}/{vid}/{video_file}" if video_file else None
        v["thumb_path"] = f"../../data/{cid}/{vid}/{thumb_file}" if thumb_file else ""

        # Ensure duration/view_count are ints
        if v.get("duration"):
            v["duration"] = int(v["duration"])
        if v.get("view_count"):
            v["view_count"] = int(v["view_count"])

        # Build detail page
        detail_dir = SITE_DIR / cid
        detail_dir.mkdir(parents=True, exist_ok=True)
        detail_tmpl = env.from_string(DETAIL_TEMPLATE)
        detail_html = detail_tmpl.render(v=v)
        (detail_dir / f"{vid}.html").write_text(detail_html, encoding="utf-8")

    # Adjust thumb paths for index (index is at site/index.html)
    for v in videos:
        vid = str(v.get("id", ""))
        cid = v.get("channel_id", "") or "unknown_channel"
        thumb_file = find_thumb_file(cid, vid)
        v["thumb_path"] = f"../data/{cid}/{vid}/{thumb_file}" if thumb_file else ""

    # Sort by date desc
    videos.sort(key=lambda v: v.get("upload_date") or "00000000", reverse=True)

    # Build index
    index_tmpl = env.from_string(INDEX_TEMPLATE)
    index_html = index_tmpl.render(videos=videos, channels=sorted(channels))
    (SITE_DIR / "index.html").write_text(index_html, encoding="utf-8")

    log.info(f"=== Site built: {len(videos)} videos, {len(channels)} channels ===")
    log.info(f"Open: {SITE_DIR / 'index.html'}")

if __name__ == "__main__":
    build()
