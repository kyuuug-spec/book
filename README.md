# 아울북 도서 모니터링 대시보드

주신 15권의 ISBN을 기준으로 교보문고·YES24·알라딘 공개 웹 데이터에서 지표를 수집하고, GitHub Pages에서 확인하는 정적 대시보드입니다.

## 등록 도서

- 초등 첫 문해력 신문 1~3
- 초등 첫 AI 문해력 신문
- 장동선의 미래 과학 프로젝트 1~3
- 법의학자 유성호의 인체 탐구 프로젝트 1~2
- 빨간내복야코 어린이 상식 1~6

총 15권입니다.

## 파일 구조

```text
.github/
  workflows/
    collect.yml
data/
  latest.json
  history.jsonl
.nojekyll
index.html
collect.py
books.json
00_처음부터_따라하기.txt
README.md
```

**`.github/workflows/collect.yml`이 반드시 있어야 Actions에 `collect-book-data`가 표시됩니다.**

## 처음 설치

자세한 클릭 순서는 `00_처음부터_따라하기.txt`를 보세요.

간단히 요약하면:

1. GitHub에 Public 저장소 생성
2. 이 폴더 안의 모든 파일/폴더 업로드
3. Settings → Pages → `Deploy from a branch` → `main` → `/(root)` → Save
4. Actions → `collect-book-data` → Run workflow
5. `data/latest.json`에 값이 들어왔는지 확인
6. Pages 사이트 새로고침

## 수집 항목

- 교보문고: 베스트 순위, 온라인/매장 재고, 리뷰/평점, 가격 등
- YES24: 판매지수, 분야 순위, 리뷰/평점
- 알라딘: Sales Point, 분야 순위, 리뷰/100자평

판매지수와 Sales Point는 실제 판매 부수와 동일하지 않습니다.

## 자동 실행

`.github/workflows/collect.yml`이 매일 오전 7시(KST)에 `collect.py`를 실행하고, 바뀐 데이터를 저장소에 자동 커밋합니다.

## 참고

서점 페이지나 공개 엔드포인트 구조가 바뀌거나 자동 요청을 제한하면 일부 지표가 수집되지 않을 수 있습니다. 그 경우 Actions 실행 로그에 원인이 남도록 구성했습니다.
