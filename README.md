# kakao-tv-dl - 카카오TV 아카이브

카카오TV(`https://tv.kakao.com/`)가 **2026-06-30 서비스 종료** 예정입니다.
이 도구는 종료 전에 영상과 메타데이터를 백업합니다.

## 기능

- **카카오TV 영상/채널/검색어** 기반 대량 다운로드
- **다음 카페 embed 영상** 다운로드 (Selenium 불필요, 쿠키만 있으면 됨)
- 영상마다 **카카오TV 스타일 오프라인 HTML** 자동 생성
- archive.org 업로드 지원 (script only)
- GUI (tkinter) + CLI 모두 지원

## 빠른 시작

### GUI (권장)

[Releases](../../releases)에서 OS별 바이너리를 받거나, 직접 실행:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -U yt-dlp
python gui.py
```

GUI에서 URL/검색어를 입력하고 **시작** 버튼을 누르면 됩니다.

### CLI

```bash
# 올인원 파이프라인 (검색어 → 메타 → 다운로드 → HTML)
python main.py

# 카페 embed 영상 다운로드
python cafe_dl.py --grpid zz4c --cookies cookies.txt

# 개별 단계
python main.py --only enumerate
python main.py --only download --limit 10
```

## 지원 입력

| 유형 | 예시 | 설명 |
|------|------|------|
| 영상 URL | `https://tv.kakao.com/v/300621662` | 단일 영상 |
| 채널 URL | `https://tv.kakao.com/channel/2688359` | 채널 전체 |
| 검색어 | `더빙` | 카카오TV 검색 결과 전체 |
| 카페 URL | `https://cafe.daum.net/mycafe` | 카페 동영상 게시판 (쿠키 필요) |

## 카페 embed 영상

카페에 embed된 비공개 카카오TV 영상도 다운로드 가능합니다.

1. 브라우저에서 카페에 로그인
2. DevTools (F12) → Application → Cookies에서 `.daum.net` 쿠키 복사
3. `cookies.txt`에 JSON 형태로 저장하거나, GUI의 **붙여넣기** 버튼 사용

세션 쿠키(`HTS`, `LSID` 등)는 만료되면 갱신이 필요합니다.

## 출력 구조

```
data/
  <channel_id>/
    <video_id>/
      video.mp4          # 영상 파일
      video.html         # 카카오TV 스타일 오프라인 페이지
      video.info.json    # 메타데이터
      thumb.png          # 썸네일
      video.description  # 설명
```

`video.html`은 외부 의존성 없이 브라우저에서 바로 재생 가능합니다.

## 설정

`config.yaml`에서 요청 속도, 화질, 제외 규칙 등을 조정합니다.

```yaml
throttle: { sleep_min: 2, sleep_max: 6, concurrency: 2 }
quality:  { max_height: 1080 }
exclude:  { channels: [], keywords: ["뉴스", "news"] }
```

## 알려진 제한

- **댓글 백업 불가**: 카카오TV가 2024년 7월에 VOD 댓글 서비스를 종료
- **일부 영상 불가**: 유료/DRM/비공개/삭제/지역 제한. 실패는 `state/failed.csv`에 기록
- **전수 크롤링 불가**: 카카오TV에 전체 영상 인덱스가 없어 seeds + 검색어로 범위 지정 필요

## 면책

- 서비스 종료로 소실될 콘텐츠의 **개인적 보존/아카이빙** 목적
- 저작권·이용약관 준수 책임은 사용자에게 있음
- 권리 없는 콘텐츠의 재배포(특히 archive.org 공개 업로드)는 법적 위험
- 사이트 부하/차단 회피를 위해 보수적 요청 속도 유지
