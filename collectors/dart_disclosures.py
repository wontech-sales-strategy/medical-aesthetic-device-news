"""경쟁사 공시 수집 (금융감독원 DART 오픈API) → data/disclosures.json, docs/data/disclosures.json

- Secret DART_API_KEY 필요 (opendart.fss.or.kr 무료 발급)
- 대상: 국내 상장 경쟁사(주가 카드와 같은 회사) + 원텍
- 공시 제목으로 유형(실적·계약·투자·자금·지배구조)을 붙이고, 지분 변동 보고 같은 반복 공시는 '참고'로 분류
- 공시는 공개 정보이므로 대시보드·메일에 그대로 게재 가능
"""
import io
import json
import os
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
OUTS = [BASE / "data" / "disclosures.json", BASE / "docs" / "data" / "disclosures.json"]
CORP_MAP = BASE / "data" / "dart_corp_map.json"
API = "https://opendart.fss.or.kr/api"

# 종목코드 기준 (주가 카드와 같은 회사 + 비상장 전환 여부 확인용 제이시스·비올)
# 원텍 자체 공시는 제외 (사내에서 이미 공유되는 내용 + 공개 대시보드에 자사 공시를 올리지 않기 위함)
COMPANIES = [
    {"stock": "214150", "name": "클래시스"},
    {"stock": "287410", "name": "제이시스메디칼"},
    {"stock": "149980", "name": "하이로닉"},
    {"stock": "199550", "name": "레이저옵텍"},
    {"stock": "462510", "name": "라메디텍"},
    {"stock": "335890", "name": "비올"},
]

# (유형, 제목에 들어가는 말) — 위에서부터 먼저 맞는 것
TYPES = [
    ("실적", ["영업(잠정)실적", "매출액또는손익구조", "사업보고서", "반기보고서", "분기보고서", "결산실적"]),
    ("계약", ["단일판매", "공급계약"]),
    ("투자", ["신규시설투자", "타법인주식", "유형자산", "특허권"]),
    ("자금", ["유상증자", "무상증자", "전환사채", "신주인수권부사채", "교환사채", "자기주식", "주식소각", "현금ㆍ현물배당", "현금·현물배당"]),
    ("지배구조", ["최대주주", "합병", "분할", "영업양수", "영업양도", "공개매수", "상장폐지", "주식교환", "대표이사", "대표집행임원"]),
]
# 위 유형에 안 걸리는 공시는 '기타'로 두고 대시보드·메일에는 표시하지 않음 (주식매수선택권, 주주명부 등)
ROUTINE = ["임원ㆍ주요주주특정증권등소유상황보고서", "주식등의대량보유상황보고서", "기업설명회", "주주총회소집",
           "참고서류", "증권발행실적보고서", "투자설명서", "일괄신고"]


def classify(title):
    for label, words in TYPES:
        if any(w in title for w in words):
            return label, True
    if any(w in title for w in ROUTINE):
        return "참고", False
    return "기타", False


def corp_codes(key):
    """종목코드 → DART 고유번호. 한 번 받아 두고, 회사가 바뀌면 다시 받음."""
    try:
        cached = json.loads(CORP_MAP.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cached = {}
    if all(c["stock"] in cached for c in COMPANIES):
        return cached
    r = requests.get(f"{API}/corpCode.xml", params={"crtfc_key": key}, timeout=60)
    r.raise_for_status()
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        raise RuntimeError(f"DART 고유번호 파일을 받지 못했습니다: {r.text[:200]}")
    root = ET.fromstring(z.read(z.namelist()[0]))
    want = {c["stock"] for c in COMPANIES}
    found = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc in want:
            found[sc] = (el.findtext("corp_code") or "").strip()
    CORP_MAP.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    return found


def fetch(key, corp_code, bgn, end):
    out, page = [], 1
    while True:
        r = requests.get(f"{API}/list.json", params={"crtfc_key": key, "corp_code": corp_code, "bgn_de": bgn,
                                                     "end_de": end, "page_no": page, "page_count": 100}, timeout=30)
        j = r.json()
        if j.get("status") == "013":  # 조회된 데이터 없음
            return out
        if j.get("status") != "000":
            raise RuntimeError(f"DART 오류 {j.get('status')}: {j.get('message')}")
        out += j.get("list", [])
        if page >= int(j.get("total_page") or 1):
            return out
        page += 1
        time.sleep(0.3)


def main():
    key = os.environ.get("DART_API_KEY")
    if not key:
        print("[공시] DART_API_KEY 없음 → 건너뜀")
        return
    now = datetime.now(KST)
    try:
        prev = json.loads(OUTS[0].read_text(encoding="utf-8")).get("items", [])
    except (FileNotFoundError, ValueError):
        prev = []
    first_run = not prev
    seen = {i["rcept_no"]: i for i in prev}
    bgn = (now - timedelta(days=180 if first_run else 30)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")

    codes = corp_codes(key)
    items = {}
    for c in COMPANIES:
        cc = codes.get(c["stock"])
        if not cc:
            print(f"[공시] {c['name']}({c['stock']}): DART 고유번호 없음 → 건너뜀", file=sys.stderr)
            continue
        try:
            rows = fetch(key, cc, bgn, end)
        except Exception as e:  # noqa: BLE001
            print(f"[공시] {c['name']}: {e}", file=sys.stderr)
            continue
        for r in rows:
            no = r["rcept_no"]
            title = " ".join((r.get("report_nm") or "").split())
            label, important = classify(title)
            items[no] = {
                "company": c["name"], "own": c.get("own", False), "title": title, "type": label, "important": important,
                "date": r.get("rcept_dt", ""), "filer": r.get("flr_nm", ""), "rcept_no": no,
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={no}",
                "is_new": (not first_run) and no not in seen,
                "found": seen.get(no, {}).get("found") or now.strftime("%Y-%m-%d"),
            }
        print(f"[공시] {c['name']}: {len(rows)}건")
        time.sleep(0.3)

    # 이번에 조회하지 않은 기간(30일 이전)의 기존 공시는 180일까지 유지
    cutoff = (now - timedelta(days=180)).strftime("%Y%m%d")
    names = {c["name"] for c in COMPANIES}
    for no, old in seen.items():
        if no not in items and old.get("date", "") >= cutoff and old.get("company") in names:
            old["is_new"] = False
            items[no] = old
    out = sorted(items.values(), key=lambda x: (x["date"], x["rcept_no"]), reverse=True)
    doc = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "source": "금융감독원 전자공시시스템(DART) 오픈API",
        "companies": [c["name"] for c in COMPANIES],
        "items": out,
    }
    for p in OUTS:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[공시] 저장 {len(out)}건 · 주요 {sum(1 for i in out if i['important'])}건 · 신규 {sum(1 for i in out if i['is_new'])}건")


if __name__ == "__main__":
    main()
