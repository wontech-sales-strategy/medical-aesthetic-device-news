"""해외 뉴스 수집기 (Google News RSS 검색, 국가별 판).

config.yaml > sources.overseas_rss.google_news 의 검색식을 국가판(edition)별로 조회합니다.
- 별도 API 키가 필요 없습니다.
- 영문 기사: 관련도 필터(주제어+제외어) 적용
- 현지어 기사(local: true): 제외어만 적용 (주제어 목록이 한·영 위주라서)
- 국가명(country)이 기사에 기록되어 대시보드에 표시됩니다.
"""
import re
import sys
import time
from urllib.parse import quote

import feedparser
import requests
from utils import is_relevant, load_config, retag, save_news, strip_html

URL = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def fetch(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200 and "<rss" in r.text[:500]:
                return r.text
            print(f"  [재시도 {i + 1}] 응답 {r.status_code}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  [재시도 {i + 1}] {e}", file=sys.stderr)
        time.sleep(3 * (i + 1))
    return ""


def clean_title(title, publisher):
    # Google News 제목 끝의 " - 매체명" 제거
    if publisher and title.endswith(f" - {publisher}"):
        return title[: -len(f" - {publisher}")].strip()
    return re.sub(r"\s+-\s+[^-]{2,60}$", "", title).strip()


def collect(text, label, country, local, cfg, limit):
    d = feedparser.parse(text)
    out = []
    for e in d.entries[:limit]:
        publisher = ""
        if e.get("source"):
            publisher = e.source.get("title", "") if hasattr(e.source, "get") else ""
        title = clean_title(strip_html(e.get("title", "")), publisher)
        summary = strip_html(e.get("summary", ""))
        if publisher and summary.endswith(publisher):
            summary = summary[: -len(publisher)].strip()
        link = e.get("link")
        if not title or not link:
            continue
        if not is_relevant(f"{title} {summary}", cfg, local=local, overseas=True):
            continue
        item = {
            "title": title,
            "summary": summary if summary != title else "",
            "link": link,
            "source": publisher or label,
            "pub_date": e.get("published") or e.get("updated") or "",
            "matched_keyword": label,
            "is_overseas": True,
            "country": country,
            "local_lang": bool(local),
        }
        out.append(retag(item, cfg))
    return out


def run():
    cfg = load_config()
    src = cfg.get("sources", {}).get("overseas_rss", {})
    if not src.get("enabled", False):
        print("overseas_rss 비활성화")
        return
    limit = int(src.get("per_query_limit", 40))

    items = {}
    ok = 0
    for q in src.get("google_news", []):
        hl, gl, ceid = (q.get("edition") or "en-US|US|US:en").split("|")
        url = URL.format(q=quote(q["query"]), hl=hl, gl=gl, ceid=ceid)
        text = fetch(url)
        if text:
            ok += 1
        got = collect(text, f"해외: {q.get('name', '')}", q.get("country", "글로벌"),
                      q.get("local", False), cfg, limit) if text else []
        print(f"Google News [{q.get('name')}/{gl}]: {len(got)}건")
        for it in got:
            items.setdefault(it["link"], it)
        time.sleep(2)

    # 같은 기사가 여러 매체·국가로 중복되면 제목 기준 1건만
    by_title = {}
    for it in items.values():
        by_title.setdefault(it["title"].lower(), it)

    merged = save_news(list(by_title.values()))
    print(f"완료: 해외 {len(by_title)}건 (성공 검색 {ok}/{len(src.get('google_news', []))}) / 누적 {len(merged)}건")


if __name__ == "__main__":
    run()
