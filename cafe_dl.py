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
    load_cookies, sleep_polite, record_failure,
    cafe_list_articles, cafe_download_article,
    _new_session, CAFE_DONE_FILE,
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

    # 2) 이미 완료된 항목 제외
    done_ids = set()
    if CAFE_DONE_FILE.exists():
        done_ids = set(CAFE_DONE_FILE.read_text().splitlines())
    log.info(f"이미 완료: {len(done_ids)}건")

    # 3) 다운로드
    log.info("=" * 60)
    log.info("다운로드 시작")
    log.info("=" * 60)

    import re
    success = fail = skip = 0
    for i, article in enumerate(tqdm(articles, desc="카페 다운로드")):
        if args.limit and success >= args.limit:
            log.info(f"제한 도달 ({args.limit}건)")
            break

        fldid = article["fldid"]
        dataid = article["dataid"]
        log.info(f"[{i + 1}/{len(articles)}] fldid={fldid}, dataid={dataid}")

        # done 체크
        resp = session.get(
            f"https://cafe.daum.net/_c21_/bbs_read?grpid={args.grpid}&fldid={fldid}&datanum={dataid}",
            cookies=cookies, timeout=15,
        )
        if resp.status_code == 200:
            clips_found = re.findall(r'cliplink/([a-zA-Z0-9]+)', resp.text)
            if clips_found and clips_found[0] in done_ids:
                log.info(f"  이미 완료 — 스킵")
                skip += 1
                sleep_polite()
                continue

        try:
            ok = cafe_download_article(session, cookies, args.grpid, fldid, dataid)
            if ok:
                # clip_id 기록
                if resp.status_code == 200 and clips_found:
                    CAFE_DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
                    with open(CAFE_DONE_FILE, "a") as f:
                        f.write(clips_found[0] + "\n")
                    done_ids.add(clips_found[0])
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
