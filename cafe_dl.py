#!/usr/bin/env python3
"""다음 카페 embed 영상 다운로더 — CLI

카페 게시판의 카카오TV embed 영상을 다운로드합니다.
핵심 로직은 core.py에서 가져옵니다.

사용법:
  python cafe_dl.py --grpid zz4c --cookies cookies.txt
  python cafe_dl.py --grpid zz4c --cookies cookies.txt --limit 5
  python cafe_dl.py --grpid zz4c --cookies cookies.txt --list-only
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from tqdm import tqdm

from core import (
    ROOT, STATE_DIR, DATA_DIR,
    load_cookies, sleep_polite,
    cafe_list_articles, cafe_download_article,
    _new_session,
)

# ── 로깅 ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kakao-tv-dl")

# ═══════════════════════════════════════════════════════════════════════════

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
    if not cookies:
        log.error(f"쿠키 파일 로드 실패: {args.cookies}")
        sys.exit(1)

    session = _new_session()

    # 1) 게시글 목록 수집
    log.info("=" * 60)
    log.info("카페 동영상 게시글 수집")
    log.info("=" * 60)
    articles = cafe_list_articles(
        session, cookies, args.grpid,
        page_start=args.page_start, page_end=args.page_end,
    )
    if not articles:
        log.error("게시글 없음. 쿠키가 만료되었을 수 있습니다.")
        return

    if args.list_only:
        list_file = STATE_DIR / f"cafe_{args.grpid}_articles.json"
        with open(list_file, "w") as f:
            json.dump(articles, f, ensure_ascii=False, indent=2)
        log.info(f"목록 저장: {list_file}")
        return

    # 2) 다운로드 (중복 체크는 cafe_download_article 내부에서 DB로 처리)
    log.info("=" * 60)
    log.info("다운로드 시작")
    log.info("=" * 60)

    success = fail = 0
    for i, article in enumerate(tqdm(articles, desc="카페 다운로드")):
        if args.limit and success >= args.limit:
            log.info(f"제한 도달 ({args.limit}건)")
            break

        fldid = article["fldid"]
        dataid = article["dataid"]
        log.info(f"[{i + 1}/{len(articles)}] fldid={fldid}, dataid={dataid}")

        try:
            ok = cafe_download_article(session, cookies, args.grpid, fldid, dataid)
            if ok:
                success += 1
            else:
                fail += 1
        except Exception as e:
            log.error(f"  에러: {e}")
            fail += 1

        sleep_polite()

    log.info("=" * 60)
    log.info(f"완료: 성공 {success}, 실패 {fail}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
