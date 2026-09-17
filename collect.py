#!/usr/bin/env python3
"""ISBN 기반 국내 서점 모니터링 수집기.

- books.json의 ISBN으로 교보문고/YES24 상품 ID를 자동 탐색한다.
- 교보문고: 분야 순위, 온라인/매장 재고, 리뷰, 찜, 판매가
- YES24: 판매지수, 분야 순위, 리뷰, 평점
- 알라딘: Sales Point, 리뷰/100자평, 분야 순위
- data/latest.json / data/history.jsonl 에 저장한다.

주의: 공개 웹 페이지/엔드포인트 구조가 바뀌면 파서 수정이 필요할 수 있다.
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
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


# ---------- 상품 ID 자동 탐색 ----------
def discover_yes24(isbn: str) -> str | None:
    q = urllib.parse.quote(isbn)
    url = f"https://www.yes24.com/Product/Search?domain=ALL&query={q}"
    html = fetch(url)
    patterns = [
        r'href=["\']/Product/Goods/(\d+)',
        r'href=["\']/product/goods/(\d+)',
        r'/Goods/Detail/(\d+)',
        r'/goods/detail/(\d+)',
    ]
    for pat in patterns:
        ids = re.findall(pat, html, re.I)
        if ids:
            return ids[0]
    return None


def discover_kyobo(isbn: str) -> str | None:
    q = urllib.parse.quote(isbn)
    urls = [
        f"https://search.kyobobook.co.kr/search?keyword={q}",
        f"https://search.kyobobook.co.kr/search?gbCode=TOT&target=total&keyword={q}",
    ]
    for url in urls:
        try:
            html = fetch(url)
        except Exception:
            continue
        patterns = [
            r'product\.kyobobook\.co\.kr/detail/(S\d+)',
            r'/detail/(S\d+)',
            r'prodInfoClick\([^)]*["\'](S\d+)["\']',
            r'cmdtCode["\']?\s*[:=]\s*["\'](S\d+)',
        ]
        for pat in patterns:
            ids = re.findall(pat, html, re.I)
            if ids:
                return ids[0]
    return None


# ---------- 교보문고 ----------
def collect_kyobo(pid: str) -> dict:
    base = f"https://product.kyobobook.co.kr/api/gw/pdt/v2/product/{pid}"
    out: dict = {"id": pid, "url": f"https://product.kyobobook.co.kr/detail/{pid}"}

    top_url = f"https://product.kyobobook.co.kr/api/gw/pdt/v2/product/component/{pid}/top"
    top = fetch_json(top_url).get("data", {}).get("top", {})
    info = top.get("info") or {}
    order = top.get("order") or {}
    out["rank"] = [
        {"label": r.get("label"), "rank": r.get("rank")}
        for r in (info.get("weeklyBest") or [])
        if r.get("rank") is not None
    ]
    review = info.get("review") or {}
    out["reviewCount"] = review.get("count")
    out["reviewScore"] = review.get("score")
    out["price"] = (order.get("price") or {}).get("discountPrice")
    out["status"] = (order.get("status") or {}).get("value")

    pdata = fetch_json(base).get("data") or {}
    pinfo = pdata.get("info") or {}
    policy = pdata.get("policy") or {}
    out["onlineStock"] = pinfo.get("realInvnQntt")
    out["likeCount"] = policy.get("likeCount")

    try:
        inv = fetch_json(f"{base}/location-inventory").get("data") or []
        stores = []
        for group in inv:
            for s in group.get("list") or []:
                stores.append({
                    "name": s.get("strName"),
                    "stock": s.get("realInvnQntt", 0),
                    "address": (s.get("strAdrs") or "").strip(),
                })
        out["storeStockTotal"] = sum(x["stock"] for x in stores if isinstance(x.get("stock"), int))
        out["stores"] = stores
    except Exception as e:
        out["inventoryError"] = f"{type(e).__name__}: {e}"
    return out


# ---------- YES24 ----------
def parse_rank_text(text: str) -> list[dict]:
    ranks = []
    seen = set()
    for label, num in re.findall(r"([가-힣A-Za-z0-9/()·&\- ]{1,45}?)\s*([\d,]+)위", text):
        label = re.sub(r"^베스트\s*", "", label.strip())
        if not label or len(label) > 45:
            continue
        key = (label, num)
        if key in seen:
            continue
        seen.add(key)
        ranks.append({"label": label, "rank": int(num.replace(",", ""))})
        if len(ranks) >= 4:
            break
    return ranks


def collect_yes24(gid: str) -> dict:
    url = f"https://www.yes24.com/product/goods/{gid}"
    html = fetch(url)
    text = strip_html(html)
    out: dict = {
        "id": gid,
        "url": url,
        "cover": f"https://image.yes24.com/goods/{gid}/XL",
    }
    m = re.search(r"판매지수\s*([\d,]+)", text)
    out["salesIndex"] = int(m.group(1).replace(",", "")) if m else None

    # 리뷰/평점: HTML 구조와 텍스트 두 경로를 모두 시도
    m = re.search(r'gd_reviewCount[^>]*>.*?([\d,]+)\s*건', html, re.S)
    if not m:
        m = re.search(r"종이책 리뷰\s*\(?([\d,]+)건", text)
    out["reviewCount"] = int(m.group(1).replace(",", "")) if m else None

    m = re.search(r'gd_rating[^>]*>.*?<em[^>]*>([\d.]+)</em>', html, re.S)
    if not m:
        m = re.search(r"리뷰 총점\s*([\d.]+)", text)
    out["reviewScore"] = float(m.group(1)) if m else None

    ranks = []
    m = re.search(r'BestSellerRank_Book/(\d+)/\?categoryNumber=(\d+)', html)
    if m:
        try:
            module = fetch(
                f"https://www.yes24.com/Product/addModules/BestSellerRank_Book/{m.group(1)}/?categoryNumber={m.group(2)}&FreePrice=N",
                referer=url,
            )
            ranks = parse_rank_text(strip_html(module))
        except Exception:
            pass
    if not ranks:
        ranks = parse_rank_text(text)
    out["rank"] = ranks
    return out


# ---------- 알라딘 ----------
def collect_aladin(isbn: str) -> dict:
    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={urllib.parse.quote(isbn)}"
    html = fetch(url)
    text = strip_html(html)
    out: dict = {"url": url}
    m = re.search(r"ItemId=(\d+)", html, re.I)
    if m:
        out["itemId"] = m.group(1)
    m = re.search(r"Sales\s*Point\s*:?\s*([\d,]+)", text, re.I)
    out["salesPoint"] = int(m.group(1).replace(",", "")) if m else None
    m = re.search(r"리뷰\(([\d,]+)\)", text)
    out["reviewCount"] = int(m.group(1).replace(",", "")) if m else None
    m = re.search(r"100자평\(([\d,]+)\)", text)
    out["commentCount"] = int(m.group(1).replace(",", "")) if m else None
    ranks = []
    head = text[: text.find("기본정보")] if "기본정보" in text else text[:25000]
    for label, period, num in re.findall(r"([가-힣A-Za-z0-9/ ]{1,28}?)\s*(주간|월간)\s*([\d,]+)위", head):
        ranks.append({"label": f"{label.strip()} {period}", "rank": int(num.replace(",", ""))})
        if len(ranks) >= 4:
            break
    out["rank"] = ranks
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

        # 없는 상품 ID는 ISBN으로 자동 탐색 후 books.json에 캐시
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
        time.sleep(0.35)

    if books_changed:
        BOOKS_PATH.write_text(json.dumps(books, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (DATA / "history.jsonl").open("a", encoding="utf-8") as f:
        for line in history_lines:
            f.write(line + "\n")
    print(f"완료: {now.isoformat()} / {len(books)}권")


if __name__ == "__main__":
    main()
