#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
아울북 도서 모니터링 수집기 v6
- 매시간 GitHub Actions에서 실행
- 교보 종합 주간 베스트 + 어린이(초등) 분야 베스트를 실제 목록에서 직접 탐색
- 교보/YES24/알라딘 공개 리뷰 본문을 브라우저 렌더링까지 포함해 수집
- 순위권 밖/미수집은 0이 아니라 null로 저장
"""
from __future__ import annotations

import json
import re
import time
import html as html_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BOOKS_PATH = ROOT / "books.json"
LATEST_PATH = DATA / "latest.json"
HISTORY_PATH = DATA / "history.jsonl"

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36")

session = requests.Session()
session.headers.update({
    "User-Agent": UA,
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
})

def get(url, *, timeout=25, referer=None):
    headers = {"Referer": referer} if referer else None
    r = session.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r

def text_clean(s):
    s = html_lib.unescape(s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def as_int(v):
    if v is None:
        return None
    m = re.search(r"-?\d[\d,]*", str(v))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except Exception:
        return None

def positive_rank(v):
    n = as_int(v)
    return n if n and n > 0 else None

def unique(seq):
    out, seen = [], set()
    for x in seq:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out

# ---------- 상품 ID ----------
def discover_kyobo(isbn):
    # JSON 검색을 1순위로 사용
    try:
        j = get(
            "https://www.kyobobook.co.kr/api/gw/aco/search/commodity"
            f"?keyword={quote(isbn)}&gbCode=TOT&page=1"
        ).json()
        docs = (((j or {}).get("data") or {}).get("resultDocuments") or [])
        for d in docs:
            if str(d.get("cmdtcode") or "") == isbn and d.get("sale_CMDTID"):
                return str(d["sale_CMDTID"])
        if docs and docs[0].get("sale_CMDTID"):
            return str(docs[0]["sale_CMDTID"])
    except Exception:
        pass

    try:
        h = get(f"https://search.kyobobook.co.kr/search?keyword={quote(isbn)}&gbCode=TOT&target=total").text
        ids = re.findall(r'(S\d{12,})', h)
        return ids[0] if ids else None
    except Exception:
        return None

def discover_yes24(isbn):
    try:
        h = get(f"https://www.yes24.com/Product/Search?domain=ALL&query={quote(isbn)}").text
        ids = re.findall(r"/Product/Goods/(\d+)", h, re.I)
        return ids[0] if ids else None
    except Exception:
        return None

def discover_aladin(isbn):
    try:
        h = get(f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={quote(isbn)}").text
        ids = re.findall(r"ItemId=(\d+)", h, re.I)
        return ids[0] if ids else None
    except Exception:
        return None

# ---------- 순위 ----------
def extract_ids_from_html(html, patterns):
    vals = []
    for p in patterns:
        vals.extend(re.findall(p, html, re.I))
    return unique(vals)

def crawl_rank(target_ids, url_builder, patterns, max_pages=20):
    """
    페이지의 실제 노출 순서를 순위로 사용.
    URL 자체가 해당 베스트 목록이므로 다른 카테고리 순위를 추정하지 않음.
    """
    target_ids = {str(x) for x in target_ids if x}
    if not target_ids:
        return {}
    found = {}
    seen = set()
    rank = 0
    no_new = 0

    for page in range(1, max_pages + 1):
        try:
            h = get(url_builder(page), timeout=30).text
        except Exception as e:
            print(f"[rank] page {page} error: {e}")
            continue

        ids = extract_ids_from_html(h, patterns)
        new = [x for x in ids if x not in seen]
        if not new:
            no_new += 1
            if no_new >= 2:
                break
            continue
        no_new = 0

        for pid in new:
            seen.add(pid)
            rank += 1
            if pid in target_ids and pid not in found:
                found[pid] = rank

        if target_ids.issubset(found.keys()):
            break
        time.sleep(.12)
    return found

def kyobo_rank_maps(books):
    ids = [b.get("kyobo") for b in books]
    patterns = [
        r'product\.kyobobook\.co\.kr/detail/(S\d+)',
        r'data-pid=["\'](S\d+)'
    ]

    # 종합 베스트 > 주간 > 전체
    overall = crawl_rank(
        ids,
        lambda p: f"https://store.kyobobook.co.kr/bestseller/total/weekly?page={p}&per=50&pcMode=on",
        patterns,
        20,
    )

    # 국내도서 > 어린이(초등) > 베스트셀러
    children = crawl_rank(
        ids,
        lambda p: f"https://store.kyobobook.co.kr/category/domestic/42/best?page={p}&per=50&pcMode=on",
        patterns,
        20,
    )
    return overall, children

def yes24_rank_maps(books):
    ids = [b.get("yes24") for b in books]
    patterns = [r'/Product/Goods/(\d+)']
    overall = crawl_rank(
        ids,
        lambda p: f"https://www.yes24.com/Product/Category/BestSeller?categoryNumber=001&pageNumber={p}&pageSize=120",
        patterns, 12
    )
    children = crawl_rank(
        ids,
        lambda p: f"https://www.yes24.com/Product/Category/BestSeller?categoryNumber=001001016&pageNumber={p}&pageSize=120",
        patterns, 12
    )
    return overall, children

def aladin_rank_maps(books):
    ids = [b.get("aladinItemId") for b in books]
    patterns = [r'wproduct\.aspx\?ItemId=(\d+)']
    overall = crawl_rank(
        ids,
        lambda p: ("https://www.aladin.co.kr/shop/common/wbest.aspx?"
                   f"BestType=Bestseller&BranchType=1&CID=0&page={p}&cnt=100"),
        patterns, 20
    )
    children = crawl_rank(
        ids,
        lambda p: ("https://www.aladin.co.kr/shop/common/wbest.aspx?"
                   f"BestType=Bestseller&BranchType=1&CID=1108&page={p}&cnt=100"),
        patterns, 20
    )
    return overall, children

# ---------- Selenium: 공개 리뷰 본문 ----------
_DRIVER = None

def get_driver():
    global _DRIVER
    if _DRIVER is not None:
        return _DRIVER

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opt = Options()
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--disable-gpu")
    opt.add_argument("--window-size=1440,1200")
    opt.add_argument(f"--user-agent={UA}")
    opt.add_argument("--lang=ko-KR")
    opt.page_load_strategy = "eager"
    _DRIVER = webdriver.Chrome(options=opt)
    _DRIVER.set_page_load_timeout(35)
    return _DRIVER

DATE_RE = re.compile(r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}")

def review_candidates_from_dom(driver, store, limit=5):
    """
    각 서점 리뷰 영역 주변의 실제 렌더링 DOM에서 리뷰 카드 후보를 찾는다.
    클래스명이 바뀌어도 review/comment/klover 키워드 + 날짜/본문 길이로 재필터링.
    """
    selectors = {
        "kyobo": [
            "[class*='klover'] [class*='comment']",
            "[class*='klover'] [class*='review']",
            "[id*='klover'] [class*='comment']",
            "[class*='review'] [class*='comment']",
            "[class*='review_item']", "[class*='comment_item']",
        ],
        "aladin": [
            "[class*='review']", "[id*='review']",
            "[class*='Ere_prod_mblog']", "[class*='blog']",
            "[class*='MyReview']", "[class*='myreview']",
        ],
        "yes24": [
            "[class*='review']", "[id*='review']",
            "[class*='cmt']", "[class*='rvw']",
        ],
    }[store]

    js = """
    const sels = arguments[0];
    const out = [];
    for (const s of sels) {
      for (const el of document.querySelectorAll(s)) {
        const t = (el.innerText || '').trim();
        if (t) out.push(t);
      }
    }
    return out;
    """
    raw = driver.execute_script(js, selectors) or []

    # body 전체의 날짜 기반 블록도 fallback 후보로 추가
    body = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    lines = [x.strip() for x in body.splitlines() if x.strip()]
    for i, line in enumerate(lines):
        if DATE_RE.search(line):
            block = "\n".join(lines[max(0, i-3):min(len(lines), i+10)])
            raw.append(block)

    out, seen = [], set()
    trash = (
        "리뷰 작성", "리뷰 신고", "구매자", "신고", "도움이", "펼쳐보기",
        "접어보기", "최신순", "공감순", "상품정보", "배송", "장바구니"
    )

    for txt in raw:
        txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
        if len(txt) < 18 or len(txt) > 3000:
            continue
        # 날짜가 있거나, 리뷰 클래스에서 뽑힌 충분한 문장만 허용
        if not DATE_RE.search(txt) and len(txt) < 45:
            continue

        lines2 = [x.strip() for x in txt.splitlines() if x.strip()]
        content_lines = []
        date = None
        author = None
        score = None

        for ln in lines2:
            if date is None:
                dm = DATE_RE.search(ln)
                if dm:
                    date = dm.group(0).replace(".", "-").replace("/", "-")
                    continue
            sm = re.search(r"(?:평점|별점)\s*([0-9.]+)", ln)
            if sm and score is None:
                try: score = float(sm.group(1))
                except: pass
                continue
            if len(ln) <= 32 and ("*" in ln or re.fullmatch(r"[A-Za-z0-9가-힣._*-]{2,32}", ln)):
                if author is None and not any(t in ln for t in trash):
                    author = ln
                    continue
            if any(ln == t for t in trash):
                continue
            if ln.startswith(("리뷰 ", "회원리뷰", "Klover 리뷰", "100자평")) and len(ln) < 40:
                continue
            content_lines.append(ln)

        content = " ".join(content_lines)
        content = re.sub(r"\s+", " ", content).strip()
        # 너무 긴 섹션 전체가 잡혔으면 버림
        if len(content) < 15 or len(content) > 1200:
            continue

        # 동일 리뷰 중복 제거
        key = re.sub(r"\W+", "", content)[:120]
        if len(key) < 12 or key in seen:
            continue
        seen.add(key)

        out.append({
            "author": author or f"{store} 회원",
            "date": date,
            "score": score,
            "content": content[:1000],
        })
        if len(out) >= limit:
            break
    return out

def selenium_reviews(store, url, review_link=None, limit=5):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    d = get_driver()
    target = review_link or url
    d.get(target)
    time.sleep(1.5)

    # lazy-load 리뷰가 내려가야 생기는 사이트 대응
    for ratio in (.35, .65, .9, 1.0):
        try:
            d.execute_script(f"window.scrollTo(0, document.body.scrollHeight*{ratio});")
            time.sleep(.45)
        except Exception:
            pass

    # 상품 페이지에 리뷰 버튼이 있으면 클릭
    if not review_link:
        labels = {
            "kyobo": ["Klover 리뷰", "리뷰"],
            "aladin": ["리뷰"],
            "yes24": ["회원리뷰", "리뷰"],
        }[store]
        for label in labels:
            try:
                els = d.find_elements(By.XPATH, f"//*[self::a or self::button][contains(normalize-space(.), '{label}')]")
                if els:
                    d.execute_script("arguments[0].click();", els[0])
                    time.sleep(1.2)
                    for ratio in (.7, 1.0):
                        d.execute_script(f"window.scrollTo(0, document.body.scrollHeight*{ratio});")
                        time.sleep(.4)
                    break
            except Exception:
                pass

    return review_candidates_from_dom(d, store, limit)

# ---------- 서점별 상세 ----------
def collect_kyobo(book, overall, children):
    pid = str(book["kyobo"])
    isbn = book["isbn"]
    url = f"https://product.kyobobook.co.kr/detail/{pid}"
    out = {
        "id": pid, "url": url,
        "overallRank": positive_rank(overall),
        "childrenRank": positive_rank(children),
        "rankBasis": "종합 주간 베스트 / 어린이(초등) 분야 베스트",
    }

    # 상단 JSON
    try:
        j = get(f"https://product.kyobobook.co.kr/api/gw/pdt/v2/product/component/{pid}/top").json()
        top = (((j or {}).get("data") or {}).get("top") or {})
        info = top.get("info") or {}
        review = info.get("review") or {}
        out["reviewCount"] = as_int(review.get("count"))
        try: out["reviewScore"] = float(review.get("score")) if review.get("score") is not None else None
        except: out["reviewScore"] = None
    except Exception as e:
        out["detailError"] = str(e)

    # 재고
    try:
        j = get(f"https://product.kyobobook.co.kr/api/gw/pdt/product/{pid}/location-inventory").json()
        stores = []
        for group in (j.get("data") or []):
            for s in (group.get("list") or []):
                stores.append({
                    "name": s.get("strName"),
                    "stock": as_int(s.get("realInvnQntt")) or 0,
                    "address": text_clean(s.get("strAdrs") or ""),
                })
        out["stores"] = stores
        out["storeStockTotal"] = sum(x["stock"] for x in stores)
    except Exception as e:
        out["inventoryError"] = str(e)

    try:
        j = get(f"https://product.kyobobook.co.kr/api/gw/pdt/v2/product/{pid}").json()
        p = (j.get("data") or {})
        info = p.get("info") or {}
        out["onlineStock"] = as_int(info.get("realInvnQntt"))
    except Exception:
        pass

    # 실제 렌더링된 Klover 리뷰
    try:
        out["reviews"] = selenium_reviews("kyobo", url, limit=5)
    except Exception as e:
        out["reviews"] = []
        out["reviewContentError"] = f"{type(e).__name__}: {e}"
    return out

def collect_yes24(book, overall, children):
    gid = str(book["yes24"])
    url = f"https://www.yes24.com/product/goods/{gid}"
    h = get(url).text
    soup = BeautifulSoup(h, "html.parser")
    t = text_clean(soup.get_text(" ", strip=True))

    out = {
        "id": gid, "url": url,
        "cover": f"https://image.yes24.com/goods/{gid}/XL",
        "overallRank": positive_rank(overall),
        "childrenRank": positive_rank(children),
        "rankBasis": "국내도서 종합 베스트 / 어린이 종합 베스트",
    }

    m = re.search(r"판매지수\s*([\d,]+)", t)
    out["salesIndex"] = as_int(m.group(1)) if m else None

    # YES24: 회원리뷰 총합이 아니라 '종이책 리뷰' 또는 리뷰/한줄평의 첫 숫자
    rc = None
    for pat in [
        r"종이책\s*리뷰\s*\(\s*([\d,]+)\s*건",
        r"리뷰/한줄평\s*\(\s*([\d,]+)\s*/\s*[\d,]+\s*\)",
        r"리뷰\s*\(\s*([\d,]+)\s*건\s*\)",
    ]:
        m = re.search(pat, t)
        if m:
            rc = as_int(m.group(1)); break
    out["reviewCount"] = rc

    m = re.search(r"(?:리뷰\s*)?총점\s*([0-9.]+)", t)
    try: out["reviewScore"] = float(m.group(1)) if m else None
    except: out["reviewScore"] = None

    # 먼저 리뷰 모듈, 실패 시 Selenium
    reviews = []
    try:
        sortno = "001001016"
        m = re.search(r'goodsSortNo["\']?\s*[:=]\s*["\']([0-9]+)', h, re.I)
        if m: sortno = m.group(1)
        params = f"goodsSortNo={sortno}&resourceKeyGb=01&goodsStateGb=02&goodsSetYn=N&Sort=1&PageNumber=1"
        review_url = f"https://www.yes24.com/Product/CommunityModules/GoodsReviewList/{gid}?{params}"
        rh = get(review_url, referer=url).text
        rsoup = BeautifulSoup(rh, "html.parser")
        # 모듈 내 카드 단위 후보
        for el in rsoup.select("li, .reviewInfoGrp, .review_cont, .reviewInfo, [class*='review']"):
            txt = text_clean(el.get_text("\n", strip=True))
            if len(txt) < 20 or len(txt) > 1500: continue
            dm = re.search(r"(20\d{2}-\d{2}-\d{2})", txt)
            if not dm: continue
            body = re.sub(r"\s+", " ", txt)
            key = re.sub(r"\W+", "", body)[:120]
            if any(re.sub(r"\W+", "", x["content"])[:120] == key for x in reviews): continue
            reviews.append({"author":"YES24 회원","date":dm.group(1),"content":body[:1000]})
            if len(reviews) >= 5: break
        if not reviews:
            reviews = selenium_reviews("yes24", url, review_link=review_url, limit=5)
    except Exception:
        try: reviews = selenium_reviews("yes24", url, limit=5)
        except Exception: reviews = []
    out["reviews"] = reviews
    return out

def collect_aladin(book, overall, children):
    isbn = book["isbn"]
    aid = str(book.get("aladinItemId") or "")
    url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={aid}" if aid else f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={quote(isbn)}"
    h = get(url).text
    soup = BeautifulSoup(h, "html.parser")
    t = text_clean(soup.get_text(" ", strip=True))

    out = {
        "url": url, "itemId": aid or None,
        "overallRank": positive_rank(overall),
        "childrenRank": positive_rank(children),
        "rankBasis": "주간 베스트 종합 / 어린이",
    }

    m = re.search(r"(?:Sales\s*Point|세일즈포인트)\s*:?\s*([\d,]+)", t, re.I)
    out["salesPoint"] = as_int(m.group(1)) if m else None

    m = re.search(r"100자평\s*\(\s*([\d,]+)\s*\)\s*리뷰\s*\(\s*([\d,]+)\s*\)", t)
    if m:
        out["commentCount"] = as_int(m.group(1))
        out["reviewCount"] = as_int(m.group(2))
    else:
        out["commentCount"] = None
        out["reviewCount"] = None

    m = re.search(r"별점\s*([0-9.]+)", t)
    try: out["reviewScore"] = float(m.group(1)) if m else None
    except: out["reviewScore"] = None

    # 상품 페이지의 '리뷰(N)' 링크를 실제로 따라감
    review_link = None
    for a in soup.find_all("a", href=True):
        label = text_clean(a.get_text(" ", strip=True))
        if re.search(r"리뷰\s*\(\s*\d+\s*\)", label):
            review_link = urljoin(url, a["href"])
            break

    try:
        out["reviews"] = selenium_reviews("aladin", url, review_link=review_link, limit=5)
        out["reviewSourceUrl"] = review_link
    except Exception as e:
        out["reviews"] = []
        out["reviewContentError"] = f"{type(e).__name__}: {e}"
    return out

def main():
    DATA.mkdir(exist_ok=True)
    books = json.loads(BOOKS_PATH.read_text(encoding="utf-8"))
    latest = {}
    if LATEST_PATH.exists():
        try: latest = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
        except: latest = {}

    changed = False
    for i, b in enumerate(books, 1):
        print(f"[ID {i}/{len(books)}] {b['title']}")
        if not b.get("kyobo"):
            x = discover_kyobo(b["isbn"])
            if x: b["kyobo"] = x; changed = True
        if not b.get("yes24"):
            x = discover_yes24(b["isbn"])
            if x: b["yes24"] = x; changed = True
        if not b.get("aladinItemId"):
            x = discover_aladin(b["isbn"])
            if x: b["aladinItemId"] = x; changed = True

    if changed:
        BOOKS_PATH.write_text(json.dumps(books, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("[RANK] Kyobo")
    ko, kc = kyobo_rank_maps(books)
    print("[RANK] YES24")
    yo, yc = yes24_rank_maps(books)
    print("[RANK] Aladin")
    ao, ac = aladin_rank_maps(books)

    now = datetime.now(KST).replace(microsecond=0)
    history_rows = []

    for i, b in enumerate(books, 1):
        print(f"[BOOK {i}/{len(books)}] {b['title']}")
        snap = {
            "t": now.isoformat(),
            "book": b["id"],
            "title": b["title"],
            "isbn": b["isbn"],
        }

        if b.get("kyobo"):
            try:
                pid = str(b["kyobo"])
                snap["kyobo"] = collect_kyobo(b, ko.get(pid), kc.get(pid))
            except Exception as e:
                snap["kyoboError"] = f"{type(e).__name__}: {e}"

        if b.get("yes24"):
            try:
                gid = str(b["yes24"])
                snap["yes24"] = collect_yes24(b, yo.get(gid), yc.get(gid))
            except Exception as e:
                snap["yes24Error"] = f"{type(e).__name__}: {e}"

        try:
            aid = str(b.get("aladinItemId") or "")
            snap["aladin"] = collect_aladin(b, ao.get(aid), ac.get(aid))
        except Exception as e:
            snap["aladinError"] = f"{type(e).__name__}: {e}"

        latest[b["id"]] = snap
        history_rows.append(json.dumps(snap, ensure_ascii=False, separators=(",", ":")))
        time.sleep(.18)

    LATEST_PATH.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        for row in history_rows:
            f.write(row + "\n")

    try:
        if _DRIVER is not None:
            _DRIVER.quit()
    except Exception:
        pass

    print(f"DONE {now.isoformat()} / {len(books)} books")

if __name__ == "__main__":
    main()
