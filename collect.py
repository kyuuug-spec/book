#!/usr/bin/env python3
"""국내 서점 모니터링 수집기 v5.

수정 핵심
1) 교보·YES24·알라딘 공개 리뷰 본문 수집 보강
2) 베스트 순위는 '종합 주간 베스트' / '어린이(아동) 주간 베스트'를
   실제 베스트셀러 목록에서 ISBN/상품ID로 찾아 저장. 추정값/0위 사용 안 함.
3) YES24 리뷰 수는 '종이책 리뷰'만 사용. 회원리뷰 합계값은 사용하지 않음.
4) 값이 없거나 순위권 밖이면 None -> 화면에서 '–' 표시.

외부 라이브러리 없이 Python 표준 라이브러리만 사용합니다.
"""
from __future__ import annotations

import gzip
import html as htmllib
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BOOKS_PATH = ROOT / "books.json"
KST = timezone(timedelta(hours=9))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36"
)

def fetch(url: str, *, referer: str | None = None,
          accept: str = "text/html,*/*", timeout: int = 30) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read()
        enc = (res.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")

def fetch_json(url: str, *, referer: str | None = None):
    return json.loads(fetch(url, referer=referer, accept="application/json,text/plain,*/*"))

def strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(?:p|div|li|tr|td|h\d)>", "\n", s, flags=re.I)
    s = htmllib.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[\t\r ]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()

def compact_text(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip()

def to_int(v):
    if v is None:
        return None
    try:
        n = int(str(v).replace(",", "").strip())
        return n
    except Exception:
        return None

def valid_rank(v):
    n = to_int(v)
    return n if n is not None and n > 0 else None

def ordered_unique(values):
    out, seen = [], set()
    for x in values:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

# ---------- 상품 ID 자동 탐색 ----------
def discover_yes24(isbn: str) -> str | None:
    q = urllib.parse.quote(isbn)
    html = fetch(f"https://www.yes24.com/Product/Search?domain=ALL&query={q}")
    ids = re.findall(r'/Product/Goods/(\d+)', html, re.I)
    return ids[0] if ids else None

def discover_kyobo(isbn: str) -> str | None:
    q = urllib.parse.quote(isbn)
    for url in [
        f"https://search.kyobobook.co.kr/search?keyword={q}",
        f"https://search.kyobobook.co.kr/search?gbCode=TOT&target=total&keyword={q}",
    ]:
        try:
            html = fetch(url)
        except Exception:
            continue
        ids = re.findall(r'product\.kyobobook\.co\.kr/detail/(S\d+)', html, re.I)
        if ids:
            return ids[0]
    return None

# ---------- 실제 베스트셀러 순위 ----------
def crawl_rank_pages(url_builder, id_pattern: str, target_ids: set[str],
                     max_pages: int, delay: float = 0.12) -> dict[str, int]:
    """베스트 목록의 실제 노출 순서로 상품ID->순위 맵 생성."""
    found: dict[str, int] = {}
    seen_all: set[str] = set()
    running_rank = 0

    for page in range(1, max_pages + 1):
        try:
            html = fetch(url_builder(page))
        except Exception as e:
            print(f"  rank page {page} 실패: {e}", file=sys.stderr)
            continue

        ids = ordered_unique(re.findall(id_pattern, html, re.I))
        new_ids = [x for x in ids if x not in seen_all]
        if not new_ids:
            if page >= 2:
                break
            continue

        for pid in new_ids:
            seen_all.add(pid)
            running_rank += 1
            if pid in target_ids and pid not in found:
                found[pid] = running_rank

        if target_ids and target_ids.issubset(found.keys()):
            break
        time.sleep(delay)
    return found

def build_kyobo_rank_maps(books: list[dict]) -> tuple[dict, dict]:
    targets = {str(b["kyobo"]) for b in books if b.get("kyobo")}
    if not targets:
        return {}, {}

    # 교보 '종합 주간 베스트' 전체 / 아동
    overall = crawl_rank_pages(
        lambda p: f"https://store.kyobobook.co.kr/bestseller/total/weekly?page={p}&per=50&pcMode=on",
        r'product\.kyobobook\.co\.kr/detail/(S\d+)',
        targets, 20
    )
    children = crawl_rank_pages(
        lambda p: f"https://store.kyobobook.co.kr/bestseller/total/weekly/children?page={p}&per=50&pcMode=on",
        r'product\.kyobobook\.co\.kr/detail/(S\d+)',
        targets, 20
    )
    return overall, children

def build_yes24_rank_maps(books: list[dict]) -> tuple[dict, dict]:
    targets = {str(b["yes24"]) for b in books if b.get("yes24")}
    if not targets:
        return {}, {}

    # YES24 국내도서 종합 베스트 / 어린이 종합 베스트
    overall = crawl_rank_pages(
        lambda p: f"https://www.yes24.com/Product/Category/BestSeller?categoryNumber=001&pageNumber={p}&pageSize=120",
        r'/Product/Goods/(\d+)',
        targets, 12
    )
    children = crawl_rank_pages(
        lambda p: f"https://www.yes24.com/Product/Category/BestSeller?categoryNumber=001001016&pageNumber={p}&pageSize=120",
        r'/Product/Goods/(\d+)',
        targets, 12
    )
    return overall, children

def build_aladin_rank_maps(books: list[dict]) -> tuple[dict, dict]:
    # itemId가 아직 없으므로 ISBN으로 목록 내 상품 블록을 찾는 대신
    # 먼저 상품 페이지에서 itemId를 확보한 뒤 호출하는 구조.
    targets = {str(b["aladinItemId"]) for b in books if b.get("aladinItemId")}
    if not targets:
        return {}, {}

    overall = crawl_rank_pages(
        lambda p: f"https://www.aladin.co.kr/shop/common/wbest.aspx?BestType=Bestseller&BranchType=1&CID=0&page={p}&cnt=1000&SortOrder=1",
        r'wproduct\.aspx\?ItemId=(\d+)',
        targets, 20
    )
    # 알라딘 국내도서 어린이 CID=1108
    children = crawl_rank_pages(
        lambda p: f"https://www.aladin.co.kr/shop/common/wbest.aspx?BestType=Bestseller&BranchType=1&CID=1108&page={p}&cnt=1000&SortOrder=1",
        r'wproduct\.aspx\?ItemId=(\d+)',
        targets, 20
    )
    return overall, children

# ---------- 리뷰 공통 ----------
def clean_review_text(s: str) -> str:
    s = compact_text(s)
    for token in [
        "신고", "차단", "도움이 됐어요", "이 리뷰가 도움이 되었나요?",
        "댓글", "좋아요", "공감", "더보기", "접어보기", "원문주소"
    ]:
        p = s.find(token)
        if p > 20:
            s = s[:p]
    return s.strip(" |·-")

def parse_dated_reviews(text: str, *, date_pattern: str, limit: int = 5,
                        default_author: str = "회원") -> list[dict]:
    """날짜를 경계로 공개 리뷰 본문을 보수적으로 추출."""
    matches = list(re.finditer(date_pattern, text))
    out = []
    for i, m in enumerate(matches):
        if len(out) >= limit:
            break
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(text), m.end() + 3500)
        before = text[max(0, m.start() - 180):m.start()]
        after = text[m.end():end]

        content = clean_review_text(after)
        # 메뉴/안내문처럼 너무 짧거나 너무 뻔한 문자열 제외
        if len(content) < 12:
            continue
        bad_starts = ("구매한 리뷰만", "최신 순", "리뷰 작성", "상품정보", "장바구니")
        if any(content.startswith(x) for x in bad_starts):
            continue

        ids = re.findall(r'([A-Za-z0-9가-힣][A-Za-z0-9가-힣*._-]{1,32})', before)
        author = ids[-1] if ids else default_author
        date = m.group(1).replace(".", "-").replace("/", "-")
        out.append({"author": author, "date": date, "content": content[:1500]})
    return out

# ---------- 교보문고 ----------
def collect_kyobo(pid: str, overall_rank=None, children_rank=None) -> dict:
    product_url = f"https://product.kyobobook.co.kr/detail/{pid}"
    out: dict = {"id": pid, "url": product_url}
    base_v2 = f"https://product.kyobobook.co.kr/api/gw/pdt/v2/product/{pid}"

    top = fetch_json(f"https://product.kyobobook.co.kr/api/gw/pdt/v2/product/component/{pid}/top").get("data", {}).get("top", {})
    info = top.get("info") or {}
    order = top.get("order") or {}

    out["overallRank"] = valid_rank(overall_rank)
    out["childrenRank"] = valid_rank(children_rank)
    out["rankBasis"] = "교보문고 종합 주간 베스트 / 아동 주간 베스트"

    review = info.get("review") or {}
    out["reviewCount"] = to_int(review.get("count"))
    try:
        out["reviewScore"] = float(review.get("score")) if review.get("score") is not None else None
    except Exception:
        out["reviewScore"] = None

    out["price"] = to_int((order.get("price") or {}).get("discountPrice"))
    out["status"] = (order.get("status") or {}).get("value")

    pdata = fetch_json(base_v2).get("data") or {}
    pinfo = pdata.get("info") or {}
    policy = pdata.get("policy") or {}
    out["onlineStock"] = to_int(pinfo.get("realInvnQntt"))
    out["likeCount"] = to_int(policy.get("likeCount"))

    try:
        inv = fetch_json(f"{base_v2}/location-inventory").get("data")
        stores = []
        for group in (inv or []):
            for s in group.get("list") or []:
                stores.append({
                    "name": s.get("strName"),
                    "stock": to_int(s.get("realInvnQntt")) or 0,
                    "address": (s.get("strAdrs") or "").strip(),
                })
        out["storeStockTotal"] = sum(x["stock"] for x in stores)
        out["stores"] = stores
    except Exception as e:
        out["inventoryError"] = f"{type(e).__name__}: {e}"

    # 교보 상품 페이지에는 공개 Klover 리뷰 본문이 서버 HTML에 노출됨.
    try:
        page_html = fetch(product_url)
        text = strip_html(page_html)
        pos = text.find("Klover 리뷰")
        section = text[pos:pos + 50000] if pos >= 0 else text
        reviews = parse_dated_reviews(
            section,
            date_pattern=r"(20\d{2}\.\d{2}\.\d{2})",
            limit=5,
            default_author="교보 회원",
        )
        out["reviews"] = reviews
        out["reviewSourceUrl"] = product_url + "#kloverReview"
    except Exception as e:
        out["reviews"] = []
        out["reviewContentError"] = f"{type(e).__name__}: {e}"
    return out

# ---------- YES24 ----------
def parse_yes24_reviews(module_html: str, limit: int = 5) -> list[dict]:
    text = strip_html(module_html)
    dates = list(re.finditer(r"(20\d{2}-\d{2}-\d{2})", text))
    out = []
    for i, m in enumerate(dates):
        if len(out) >= limit:
            break
        end = dates[i+1].start() if i+1 < len(dates) else min(len(text), m.end()+3500)
        seg = text[m.end():end]
        content = clean_review_text(seg)
        if len(content) < 12:
            continue

        before = text[max(0, m.start()-220):m.start()]
        score_m = re.search(r"평점\s*([0-9.]+)", before)
        authors = re.findall(r'([A-Za-z0-9가-힣][A-Za-z0-9가-힣*._-]{2,40})', before)
        row = {
            "author": authors[-1] if authors else "YES24 회원",
            "date": m.group(1),
            "content": content[:1500],
        }
        if score_m:
            try:
                row["score"] = float(score_m.group(1))
            except Exception:
                pass
        out.append(row)
    return out

def extract_yes24_sort_no(html: str) -> str:
    for pat in [
        r'goodsSortNo["\']?\s*[:=]\s*["\']([0-9]+)',
        r'name=["\']goodsSortNo["\'][^>]*value=["\']([0-9]+)',
        r'goodsSortNo=([0-9]+)',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return "001001016"

def collect_yes24(gid: str, overall_rank=None, children_rank=None) -> dict:
    url = f"https://www.yes24.com/product/goods/{gid}"
    html = fetch(url)
    text = compact_text(strip_html(html))
    out = {
        "id": gid,
        "url": url,
        "cover": f"https://image.yes24.com/goods/{gid}/XL",
        "overallRank": valid_rank(overall_rank),
        "childrenRank": valid_rank(children_rank),
        "rankBasis": "YES24 국내도서 종합 베스트 / 어린이 종합 베스트",
    }

    m = re.search(r"판매지수\s*([\d,]+)", text)
    out["salesIndex"] = to_int(m.group(1)) if m else None

    # 중요: 회원리뷰(N건)은 리뷰+한줄평 합산처럼 보일 수 있으므로 절대 쓰지 않음.
    # '종이책 리뷰' 또는 '리뷰/한줄평 (리뷰수/한줄평수)'만 사용.
    review_count = None
    for pat in [
        r"종이책\s*리뷰\s*\(\s*([\d,]+)\s*건\s*\)",
        r"리뷰/한줄평\s*\(\s*([\d,]+)\s*/\s*[\d,]+\s*\)",
    ]:
        m = re.search(pat, text)
        if m:
            review_count = to_int(m.group(1))
            break
    out["reviewCount"] = review_count

    m = re.search(r"리뷰\s*총점\s*([\d.]+)", text)
    try:
        out["reviewScore"] = float(m.group(1)) if m else None
    except Exception:
        out["reviewScore"] = None

    try:
        goods_sort_no = extract_yes24_sort_no(html)
        params = urllib.parse.urlencode({
            "goodsSortNo": goods_sort_no,
            "resourceKeyGb": "01",
            "goodsStateGb": "02",
            "goodsSetYn": "N",
            "Sort": "1",
            "PageNumber": "1",
        })
        review_url = f"https://www.yes24.com/Product/CommunityModules/GoodsReviewList/{gid}?{params}"
        review_html = fetch(review_url, referer=url)
        out["reviews"] = parse_yes24_reviews(review_html, 5)
        out["reviewSourceUrl"] = review_url
    except Exception as e:
        out["reviews"] = []
        out["reviewContentError"] = f"{type(e).__name__}: {e}"

    return out

# ---------- 알라딘 ----------
def discover_aladin_item_id(isbn: str) -> str | None:
    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={urllib.parse.quote(isbn)}"
    html = fetch(url)
    m = re.search(r"ItemId=(\d+)", html, re.I)
    return m.group(1) if m else None

def parse_aladin_review_page(page_html: str, limit: int = 5) -> list[dict]:
    text = strip_html(page_html)
    # 알라딘 리뷰 페이지는 YYYY-MM-DD 또는 YYYY.MM.DD 형태를 모두 볼 수 있어 둘 다 처리.
    return parse_dated_reviews(
        text,
        date_pattern=r"(20\d{2}[-./]\d{2}[-./]\d{2})",
        limit=limit,
        default_author="알라딘 회원",
    )

def collect_aladin(isbn: str, item_id: str | None,
                   overall_rank=None, children_rank=None) -> dict:
    url = (f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}"
           if item_id else
           f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={urllib.parse.quote(isbn)}")
    html = fetch(url)
    text = compact_text(strip_html(html))
    out: dict = {
        "url": url,
        "itemId": item_id,
        "overallRank": valid_rank(overall_rank),
        "childrenRank": valid_rank(children_rank),
        "rankBasis": "알라딘 주간 베스트 종합 / 어린이",
    }

    m = re.search(r"세일즈포인트\s*:?\s*([\d,]+)", text, re.I)
    if not m:
        m = re.search(r"Sales\s*Point\s*:?\s*([\d,]+)", text, re.I)
    out["salesPoint"] = to_int(m.group(1)) if m else None

    # 상품 페이지의 평점 (리뷰 수와 혼동하지 않도록 별도)
    score = None
    m = re.search(r"([0-9.]+)\s*\(\s*[\d,]+\s*\)\s*\|\s*세일즈포인트", text)
    if m:
        try:
            score = float(m.group(1))
        except Exception:
            pass
    out["reviewScore"] = score

    # 100자평 / 마이리뷰 개수
    out["commentCount"] = None
    out["reviewCount"] = None
    patterns = [
        r"100자평\s*\(\s*([\d,]+)\s*\).*?리뷰\s*\(\s*([\d,]+)\s*\)",
        r"100자평\s*([\d,]+).*?마이리뷰\s*([\d,]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.S)
        if m:
            out["commentCount"] = to_int(m.group(1))
            out["reviewCount"] = to_int(m.group(2))
            break

    # 전용 마이리뷰 페이지에서 실제 본문 수집
    review_url = (
        "https://www.aladin.co.kr/shop/common/wbook_talktalk.aspx?"
        + urllib.parse.urlencode({
            "ISBN": isbn,
            "BranchType": "1",
            "CommunityType": "MyReview",
        })
    )
    try:
        review_html = fetch(review_url, referer=url)
        out["reviews"] = parse_aladin_review_page(review_html, 5)
        out["reviewSourceUrl"] = review_url
    except Exception as e:
        out["reviews"] = []
        out["reviewContentError"] = f"{type(e).__name__}: {e}"

    return out

def main() -> None:
    DATA.mkdir(exist_ok=True)
    books = json.loads(BOOKS_PATH.read_text(encoding="utf-8"))
    latest_path = DATA / "latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.exists() else {}
    now = datetime.now(KST).replace(microsecond=0)
    books_changed = False

    # 1) 필요한 상품 ID 먼저 확정
    for idx, book in enumerate(books, 1):
        isbn = book["isbn"].strip()
        print(f"[ID {idx}/{len(books)}] {book['title']}")
        if not book.get("yes24"):
            try:
                gid = discover_yes24(isbn)
                if gid:
                    book["yes24"] = gid
                    books_changed = True
            except Exception as e:
                print(f"  YES24 ID 탐색 실패: {e}", file=sys.stderr)

        if not book.get("kyobo"):
            try:
                pid = discover_kyobo(isbn)
                if pid:
                    book["kyobo"] = pid
                    books_changed = True
            except Exception as e:
                print(f"  교보 ID 탐색 실패: {e}", file=sys.stderr)

        if not book.get("aladinItemId"):
            try:
                aid = discover_aladin_item_id(isbn)
                if aid:
                    book["aladinItemId"] = aid
                    books_changed = True
            except Exception as e:
                print(f"  알라딘 ID 탐색 실패: {e}", file=sys.stderr)

        time.sleep(0.1)

    if books_changed:
        BOOKS_PATH.write_text(json.dumps(books, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 2) 베스트셀러 목록을 실제로 훑어서 정확한 순위 맵 생성
    print("[순위] 교보문고")
    k_overall, k_children = build_kyobo_rank_maps(books)
    print("[순위] YES24")
    y_overall, y_children = build_yes24_rank_maps(books)
    print("[순위] 알라딘")
    a_overall, a_children = build_aladin_rank_maps(books)

    # 3) 상품별 상세/리뷰 수집
    history_lines = []
    for idx, book in enumerate(books, start=1):
        isbn = book["isbn"].strip()
        print(f"[수집 {idx}/{len(books)}] {book['title']} ({isbn})")
        snap: dict = {
            "t": now.isoformat(),
            "book": book["id"],
            "title": book["title"],
            "isbn": isbn,
        }

        if book.get("kyobo"):
            pid = str(book["kyobo"])
            try:
                snap["kyobo"] = collect_kyobo(
                    pid, k_overall.get(pid), k_children.get(pid)
                )
            except Exception as e:
                snap["kyoboError"] = f"{type(e).__name__}: {e}"

        if book.get("yes24"):
            gid = str(book["yes24"])
            try:
                snap["yes24"] = collect_yes24(
                    gid, y_overall.get(gid), y_children.get(gid)
                )
            except Exception as e:
                snap["yes24Error"] = f"{type(e).__name__}: {e}"

        aid = str(book.get("aladinItemId") or "")
        try:
            snap["aladin"] = collect_aladin(
                isbn,
                aid or None,
                a_overall.get(aid) if aid else None,
                a_children.get(aid) if aid else None,
            )
        except Exception as e:
            snap["aladinError"] = f"{type(e).__name__}: {e}"

        latest[book["id"]] = snap
        history_lines.append(json.dumps(snap, ensure_ascii=False, separators=(",", ":")))
        time.sleep(0.25)

    latest_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (DATA / "history.jsonl").open("a", encoding="utf-8") as f:
        for line in history_lines:
            f.write(line + "\n")

    print(f"완료: {now.isoformat()} / {len(books)}권")

if __name__ == "__main__":
    main()
