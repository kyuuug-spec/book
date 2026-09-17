#!/usr/bin/env python3
"""국내 서점 모니터링 수집기 v4.

핵심 변경점
- YES24 리뷰 수를 '종이책 리뷰(...)' / '회원리뷰(...)' 기준으로 정확히 파싱
- YES24 최신 회원리뷰 내용(최대 5개) 수집
- 교보문고 Klover 리뷰 내용(최대 5개) 수집 시도
- 각 서점 순위를 '종합 베스트' / '어린이 베스트'로 분리 저장
- 기존 필드와 호환되도록 rank 원본 목록도 유지

주의: 서점 웹페이지/공개 엔드포인트 구조가 바뀌면 일부 파서 수정이 필요할 수 있습니다.
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


def fetch(url: str, *, referer: str | None = None, accept: str = "text/html,*/*", timeout: int = 30) -> str:
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
        raw = zlib.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def fetch_json(url: str, *, referer: str | None = None):
    return json.loads(fetch(url, referer=referer, accept="application/json,text/plain,*/*"))


def strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(?:p|div|li|tr|h\d)>", "\n", s, flags=re.I)
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
        return int(str(v).replace(",", "").strip())
    except Exception:
        return None


def min_rank(values):
    xs = [x for x in values if isinstance(x, int)]
    return min(xs) if xs else None


def classify_ranks(ranks: list[dict]) -> tuple[int | None, int | None]:
    """rank label 목록에서 종합/어린이 순위를 최대한 보수적으로 분리."""
    overall = []
    children = []
    for r in ranks or []:
        rank = to_int(r.get("rank"))
        if rank is None:
            continue
        label = compact_text(str(r.get("label") or ""))
        low = label.lower()
        if "어린이" in label or "아동" in label:
            children.append(rank)
        if "종합" in label or "국내도서" in label or low in {"book", "books"}:
            overall.append(rank)
    return min_rank(overall), min_rank(children)


# ---------- 상품 ID 자동 탐색 ----------
def discover_yes24(isbn: str) -> str | None:
    q = urllib.parse.quote(isbn)
    url = f"https://www.yes24.com/Product/Search?domain=ALL&query={q}"
    html = fetch(url)
    for pat in [
        r'href=["\']/Product/Goods/(\d+)',
        r'href=["\']/product/goods/(\d+)',
        r'/Goods/Detail/(\d+)',
        r'/goods/detail/(\d+)',
    ]:
        ids = re.findall(pat, html, re.I)
        if ids:
            return ids[0]
    return None


def discover_kyobo(isbn: str) -> str | None:
    # JSON 검색을 우선 사용. 실패 시 HTML 검색으로 폴백.
    q = urllib.parse.quote(isbn)
    json_urls = [
        f"https://search.kyobobook.co.kr/srp/api/v2/search/autocomplete/shop?keyword={q}",
        f"https://www.kyobobook.co.kr/api/gw/aco/search/commodity?keyword={q}&gbCode=TOT&page=1",
    ]
    for url in json_urls:
        try:
            obj = fetch_json(url)
            docs = (((obj or {}).get("data") or {}).get("resultDocuments") or [])
            for d in docs:
                pid = d.get("sale_CMDTID") or d.get("saleCmdtId") or d.get("saleCmdtid")
                if pid and re.fullmatch(r"S\d+", str(pid)):
                    return str(pid)
        except Exception:
            pass

    for url in [
        f"https://search.kyobobook.co.kr/search?keyword={q}",
        f"https://search.kyobobook.co.kr/search?gbCode=TOT&target=total&keyword={q}",
    ]:
        try:
            html = fetch(url)
        except Exception:
            continue
        for pat in [
            r'product\.kyobobook\.co\.kr/detail/(S\d+)',
            r'/detail/(S\d+)',
            r'data-pid=["\'](S\d+)',
            r'cmdtCode["\']?\s*[:=]\s*["\'](S\d+)',
        ]:
            ids = re.findall(pat, html, re.I)
            if ids:
                return ids[0]
    return None


# ---------- 리뷰 파서 ----------
def parse_yes24_reviews(module_html: str, limit: int = 5) -> list[dict]:
    text = strip_html(module_html)
    # 예: 평점10점 | YES마니아 : 로얄 j*******h | 2026-08-01 | 신고
    pat = re.compile(
        r"평점\s*([0-9.]+)\s*점?\s*\|\s*(.*?)\s*\|\s*(20\d{2}-\d{2}-\d{2})\s*\|\s*신고",
        re.S,
    )
    ms = list(pat.finditer(text))
    out = []
    for i, m in enumerate(ms[:limit]):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        seg = text[m.end():end]
        cut_points = []
        for marker in ["더보기", "원문주소", "이 리뷰가 도움이 되었나요?", "접어보기", "공감"]:
            p = seg.find(marker)
            if p >= 0:
                cut_points.append(p)
        if cut_points:
            seg = seg[:min(cut_points)]
        content = compact_text(seg)
        # 너무 짧거나 안내문인 경우 제외
        if len(content) < 4:
            continue
        out.append({
            "score": float(m.group(1)),
            "author": compact_text(m.group(2))[:80],
            "date": m.group(3),
            "content": content[:1200],
        })
    return out


def parse_kyobo_reviews(page_html: str, limit: int = 5) -> list[dict]:
    text = strip_html(page_html)
    pos = text.find("Klover 리뷰")
    if pos < 0:
        return []
    section = text[pos:]
    for stop in ["문장수집", "책 속으로", "교환/반품/품절"]:
        p = section.find(stop, 20)
        if p > 0:
            section = section[:p]
            break

    dates = list(re.finditer(r"(20\d{2}\.\d{2}\.\d{2})", section))
    out = []
    for i, m in enumerate(dates):
        if len(out) >= limit:
            break
        end = dates[i + 1].start() if i + 1 < len(dates) else len(section)
        after = section[m.end():end]
        after = re.sub(r"^\s*(?:신고|차단)\s*", "", after)
        cut = re.search(r"\b\d+\s*댓글\b|도움이 됐어요|좋아요", after)
        if cut:
            after = after[:cut.start()]
        content = compact_text(after)
        if len(content) < 4:
            continue
        before = section[max(0, m.start() - 120):m.start()]
        # 날짜 직전의 마스킹 ID를 최대한 추출. 못 찾으면 일반 표기.
        ids = re.findall(r"([A-Za-z0-9가-힣][A-Za-z0-9가-힣*._-]{1,35})", before)
        author = ids[-1] if ids else "교보 회원"
        out.append({
            "author": author,
            "date": m.group(1).replace(".", "-"),
            "content": content[:1200],
        })
    return out


def parse_aladin_recent_review(text: str) -> list[dict]:
    """상품 페이지에 최신 리뷰/100자평 본문이 노출된 경우에만 보수적으로 1건 수집."""
    # 알라딘은 상품 페이지에 항상 리뷰 본문을 노출하지 않으므로, 확실한 패턴이 있을 때만 저장.
    markers = ["마이리뷰", "100자평"]
    for marker in markers:
        pos = text.find(marker)
        if pos < 0:
            continue
        seg = text[pos:pos + 5000]
        # 날짜 + 평점 뒤의 문장을 시도
        m = re.search(r"(20\d{2}-\d{2}-\d{2}|20\d{2}\.\d{2}\.\d{2}).{0,160}?평점\s*[:：]?\s*([0-9.]+)?(.*)", seg, re.S)
        if m:
            content = compact_text(m.group(3))
            for stop in ["댓글", "좋아요", "공유하기", "장바구니"]:
                p = content.find(stop)
                if p > 0:
                    content = content[:p]
            if len(content) >= 10:
                return [{"date": m.group(1).replace(".", "-"), "score": to_int(m.group(2)), "author": "알라딘 회원", "content": content[:1200]}]
    return []


# ---------- 교보문고 ----------
def collect_kyobo(pid: str) -> dict:
    base_v2 = f"https://product.kyobobook.co.kr/api/gw/pdt/v2/product/{pid}"
    product_url = f"https://product.kyobobook.co.kr/detail/{pid}"
    out: dict = {"id": pid, "url": product_url}

    top_url = f"https://product.kyobobook.co.kr/api/gw/pdt/v2/product/component/{pid}/top"
    top = fetch_json(top_url).get("data", {}).get("top", {})
    info = top.get("info") or {}
    order = top.get("order") or {}
    ranks = [
        {"label": r.get("label"), "rank": to_int(r.get("rank"))}
        for r in (info.get("weeklyBest") or [])
        if to_int(r.get("rank")) is not None
    ]
    out["rank"] = ranks
    out["overallRank"], out["childrenRank"] = classify_ranks(ranks)

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
        inv_urls = [
            f"{base_v2}/location-inventory",
            f"https://product.kyobobook.co.kr/api/gw/pdt/product/{pid}/location-inventory",
        ]
        inv = None
        for u in inv_urls:
            try:
                inv = fetch_json(u).get("data")
                if inv is not None:
                    break
            except Exception:
                pass
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

    try:
        page_html = fetch(product_url)
        out["reviews"] = parse_kyobo_reviews(page_html)
    except Exception as e:
        out["reviewContentError"] = f"{type(e).__name__}: {e}"
        out["reviews"] = []
    return out


# ---------- YES24 ----------
def parse_rank_text(text: str) -> list[dict]:
    ranks = []
    seen = set()
    for label, num in re.findall(r"([가-힣A-Za-z0-9/()·&\- ]{1,55}?)\s*([\d,]+)위", text):
        label = re.sub(r"^베스트\s*", "", label.strip())
        if not label or len(label) > 55:
            continue
        rank = to_int(num)
        key = (label, rank)
        if rank is None or key in seen:
            continue
        seen.add(key)
        ranks.append({"label": label, "rank": rank})
        if len(ranks) >= 12:
            break
    return ranks


def yes24_rank_for_category(gid: str, category: str, label_hint: str, referer: str) -> tuple[int | None, list[dict]]:
    url = f"https://www.yes24.com/Product/addModules/BestSellerRank_Book/{gid}/?categoryNumber={category}&FreePrice=N"
    html = fetch(url, referer=referer)
    text = compact_text(strip_html(html))
    ranks = parse_rank_text(text)
    # 라벨 힌트가 있는 순위를 우선, 없으면 모듈에서 노출한 가장 작은 순위 사용
    matched = [r["rank"] for r in ranks if label_hint in str(r.get("label") or "")]
    if matched:
        return min_rank(matched), ranks
    all_ranks = [r["rank"] for r in ranks]
    return min_rank(all_ranks), ranks


def extract_yes24_sort_no(html: str) -> str:
    patterns = [
        r'goodsSortNo["\']?\s*[:=]\s*["\']([0-9]+)',
        r'name=["\']goodsSortNo["\'][^>]*value=["\']([0-9]+)',
        r'goodsSortNo=([0-9]+)',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return "001001016"


def collect_yes24(gid: str) -> dict:
    url = f"https://www.yes24.com/product/goods/{gid}"
    html = fetch(url)
    text = compact_text(strip_html(html))
    out: dict = {
        "id": gid,
        "url": url,
        "cover": f"https://image.yes24.com/goods/{gid}/XL",
    }

    m = re.search(r"판매지수\s*([\d,]+)", text)
    out["salesIndex"] = to_int(m.group(1)) if m else None

    # YES24에서 "회원리뷰"는 리뷰+한줄평 합계일 수 있으므로 사용하지 않음.
    # 실제 장문 리뷰 수인 "종이책 리뷰" 또는 "회원리뷰 (N건)" 섹션 제목만 사용.
    review_count = None
    review_patterns = [
        r"종이책\s*리뷰\s*\(\s*([\d,]+)\s*건\s*\)",
        r"리뷰/한줄평\s*\(\s*([\d,]+)\s*/\s*[\d,]+\s*\)",
        r"회원리뷰\s*\(\s*([\d,]+)\s*건\s*\)",
    ]
    for pat in review_patterns:
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

    # 최신 실제 회원리뷰 내용. 상품 페이지의 goodsSortNo를 읽어 리뷰 모듈에 전달.
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
        reviews = parse_yes24_reviews(review_html, limit=5)
        # 일부 상품은 모듈 형식이 달라 상품 본문에 리뷰가 포함될 수 있어 폴백 시도
        if not reviews:
            reviews = parse_yes24_reviews(html, limit=5)
        out["reviews"] = reviews
        out["reviewListUrl"] = review_url
    except Exception as e:
        out["reviews"] = parse_yes24_reviews(html, limit=5)
        out["reviewContentError"] = f"{type(e).__name__}: {e}"

    # 종합 / 어린이 순위는 각각 별도 모듈을 조회.
    overall_rank = children_rank = None
    rank_rows: list[dict] = []
    try:
        overall_rank, r = yes24_rank_for_category(gid, "001", "국내도서", url)
        rank_rows.extend(r)
    except Exception as e:
        out["overallRankError"] = f"{type(e).__name__}: {e}"
    try:
        children_rank, r = yes24_rank_for_category(gid, "001001016", "어린이", url)
        rank_rows.extend(r)
    except Exception as e:
        out["childrenRankError"] = f"{type(e).__name__}: {e}"

    if not rank_rows:
        rank_rows = parse_rank_text(text)
    fallback_overall, fallback_children = classify_ranks(rank_rows)
    out["overallRank"] = overall_rank if overall_rank is not None else fallback_overall
    out["childrenRank"] = children_rank if children_rank is not None else fallback_children
    # 중복 제거한 원본 순위
    uniq = []
    seen = set()
    for r in rank_rows:
        key = (r.get("label"), r.get("rank"))
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    out["rank"] = uniq[:12]
    return out


# ---------- 알라딘 ----------
def collect_aladin(isbn: str) -> dict:
    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={urllib.parse.quote(isbn)}"
    html = fetch(url)
    text = compact_text(strip_html(html))
    out: dict = {"url": url}

    m = re.search(r"ItemId=(\d+)", html, re.I)
    if m:
        out["itemId"] = m.group(1)
        out["url"] = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={m.group(1)}"

    m = re.search(r"Sales\s*Point\s*:?\s*([\d,]+)", text, re.I)
    out["salesPoint"] = to_int(m.group(1)) if m else None

    m = re.search(r"100자평\s*\(\s*([\d,]+)\s*\)\s*리뷰\s*\(\s*([\d,]+)\s*\)", text)
    if m:
        out["commentCount"] = to_int(m.group(1))
        out["reviewCount"] = to_int(m.group(2))
    else:
        m = re.search(r"리뷰\s*\(\s*([\d,]+)\s*\)", text)
        out["reviewCount"] = to_int(m.group(1)) if m else None
        m = re.search(r"100자평\s*\(\s*([\d,]+)\s*\)", text)
        out["commentCount"] = to_int(m.group(1)) if m else None

    m = re.search(r"별점\s*([\d.]+)", text)
    try:
        out["reviewScore"] = float(m.group(1)) if m else None
    except Exception:
        out["reviewScore"] = None

    ranks = []
    # 알라딘은 상품페이지 상단에 '종합 주간 9위', '어린이 주간 12위'처럼 노출될 때만 확정값으로 사용.
    for label, period, num in re.findall(r"([가-힣A-Za-z0-9/· ]{1,35}?)\s*(주간|월간|연간)\s*([\d,]+)위", text[:30000]):
        ranks.append({"label": f"{compact_text(label)} {period}", "rank": to_int(num)})
        if len(ranks) >= 12:
            break
    out["rank"] = [r for r in ranks if r["rank"] is not None]
    out["overallRank"], out["childrenRank"] = classify_ranks(out["rank"])

    out["reviews"] = parse_aladin_recent_review(text)
    return out


def main() -> None:
    DATA.mkdir(exist_ok=True)
    books = json.loads(BOOKS_PATH.read_text(encoding="utf-8"))
    latest_path = DATA / "latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.exists() else {}
    now = datetime.now(KST).replace(microsecond=0)
    history_lines = []
    books_changed = False

    for idx, book in enumerate(books, start=1):
        isbn = book["isbn"].strip()
        print(f"[{idx}/{len(books)}] {book['title']} ({isbn})")

        if not book.get("yes24"):
            try:
                gid = discover_yes24(isbn)
                if gid:
                    book["yes24"] = gid
                    books_changed = True
                    print(f"  YES24 ID 발견: {gid}")
            except Exception as e:
                print(f"  YES24 탐색 실패: {e}", file=sys.stderr)

        if not book.get("kyobo"):
            try:
                pid = discover_kyobo(isbn)
                if pid:
                    book["kyobo"] = pid
                    books_changed = True
                    print(f"  교보 ID 발견: {pid}")
            except Exception as e:
                print(f"  교보 탐색 실패: {e}", file=sys.stderr)

        snap: dict = {
            "t": now.isoformat(),
            "book": book["id"],
            "title": book["title"],
            "isbn": isbn,
        }

        if book.get("kyobo"):
            try:
                snap["kyobo"] = collect_kyobo(book["kyobo"])
            except Exception as e:
                snap["kyoboError"] = f"{type(e).__name__}: {e}"
                print(f"  교보 수집 실패: {e}", file=sys.stderr)

        if book.get("yes24"):
            try:
                snap["yes24"] = collect_yes24(book["yes24"])
            except Exception as e:
                snap["yes24Error"] = f"{type(e).__name__}: {e}"
                print(f"  YES24 수집 실패: {e}", file=sys.stderr)

        try:
            snap["aladin"] = collect_aladin(isbn)
        except Exception as e:
            snap["aladinError"] = f"{type(e).__name__}: {e}"
            print(f"  알라딘 수집 실패: {e}", file=sys.stderr)

        latest[book["id"]] = snap
        history_lines.append(json.dumps(snap, ensure_ascii=False, separators=(",", ":")))
        time.sleep(0.45)

    if books_changed:
        BOOKS_PATH.write_text(json.dumps(books, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    latest_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (DATA / "history.jsonl").open("a", encoding="utf-8") as f:
        for line in history_lines:
            f.write(line + "\n")

    print(f"완료: {now.isoformat()} / {len(books)}권")


if __name__ == "__main__":
    main()
