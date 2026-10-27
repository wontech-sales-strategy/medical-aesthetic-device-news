"""경쟁사 글로벌 인허가 Tracker (1단계: 미국 FDA 510(k))
→ data/regulatory.json, docs/data/regulatory.json

- 출처: openFDA Device 510(k) API (무료·키 불필요, FDA 주 1회 갱신)
- FDA에는 브랜드가 아니라 '제조사 법인명'으로 등록되므로 아래 WATCH_LIST의 applicant로 매칭합니다.
  · DEKA 레이저 = El.En S.p.A. (동명 회사 'Deka Research and Development'는 인슐린 펌프 회사라 제외)
  · 울쎄라(Merz) = Ulthera, Inc.
- 새로 잡힌 허가는 is_new=True 로 표시 → 아침 메일 알림에 사용
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]
OUTS = [BASE / "data" / "regulatory.json", BASE / "docs" / "data" / "regulatory.json"]
KST = timezone(timedelta(hours=9))
SINCE = "20230101"  # 이 날짜 이후 허가만 보관

# 회사(표시명) → FDA 검색어, 신청인명에 반드시 포함될 단어, 제외 단어
WATCH_LIST = [
    {"company": "원텍", "own": True, "q": "wontech", "must": ["wontech"]},
    {"company": "클래시스", "q": "classys", "must": ["classys"]},
    {"company": "인모드", "q": "inmode", "must": ["inmode"]},
    {"company": "멀츠(울쎄라)", "q": "ulthera", "must": ["ulthera"]},
    {"company": "솔타메디칼", "q": "solta", "must": ["solta"]},
    {"company": "소프웨이브", "q": "sofwave", "must": ["sofwave"]},
    {"company": "DEKA(El.En.)", "q": "el.en", "must": ["el.en", "el. en"], "exclude": ["deka research"]},
    {"company": "제이시스메디칼", "q": "jeisys", "must": ["jeisys"]},
    {"company": "알마레이저", "q": "\"alma lasers\"", "must": ["alma lasers"]},
    {"company": "사이노슈어 루트로닉", "q": "lutronic", "must": ["lutronic"]},
]

# 제품코드 → 분류 (FDA 분류 기준을 단순화)
CODE_GROUP = {"GEI": "RF", "GEX": "Laser", "ONG": "Laser", "ONF": "Laser", "OHV": "초음파(HIFU)", "IPF": "EMS",
              "NGX": "EMS", "PBX": "기타", "OAP": "레이저(홈케어)", "PKT": "기타"}


def fetch(q):
    url = "https://api.fda.gov/device/510k.json"
    params = {"search": f"applicant:{q} AND decision_date:[{SINCE} TO 29991231]", "sort": "decision_date:desc", "limit": 100}
    for attempt in range(3):
        r = requests.get(url, params=params, timeout=40)
        if r.status_code == 404:  # 결과 없음
            return []
        if r.status_code == 200:
            return r.json().get("results", [])
        time.sleep(5 * (attempt + 1))
    r.raise_for_status()
    return []


def main():
    prev = {}
    try:
        prev = {i["k_number"] for i in json.loads(OUTS[0].read_text(encoding="utf-8")).get("items", [])}
    except (FileNotFoundError, ValueError, KeyError):
        prev = set()
    first_run = not prev
    items, seen = [], set()
    for w in WATCH_LIST:
        try:
            rows = fetch(w["q"])
        except Exception as e:  # noqa: BLE001
            print(f"[FDA] {w['company']} 조회 실패: {e}")
            continue
        n = 0
        for r in rows:
            app = (r.get("applicant") or "").lower()
            if not any(m in app for m in w["must"]) or any(x in app for x in w.get("exclude", [])):
                continue
            k = r.get("k_number")
            if not k or k in seen:
                continue
            seen.add(k)
            d = r.get("decision_date", "")
            items.append({
                "company": w["company"], "own": w.get("own", False), "applicant": r.get("applicant"),
                "device": r.get("device_name"), "product_code": r.get("product_code"),
                "group": CODE_GROUP.get(r.get("product_code"), "기타"),
                "country": "미국", "type": "FDA 510(k)", "decision": r.get("decision_description"),
                "date": d, "k_number": k,
                "url": f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID={k}",
                "is_new": (not first_run) and k not in prev,
                "found": datetime.now(KST).strftime("%Y-%m-%d"),
            })
            n += 1
        print(f"[FDA] {w['company']}: {n}건 (2023년 이후)")
        time.sleep(1)
    # 이전에 '새로 발견'된 날짜는 유지
    try:
        old = {i["k_number"]: i for i in json.loads(OUTS[0].read_text(encoding="utf-8")).get("items", [])}
        for i in items:
            if i["k_number"] in old:
                i["found"] = old[i["k_number"]].get("found", i["found"])
    except (FileNotFoundError, ValueError, KeyError):
        pass
    if not items:
        print("[FDA] 수집 결과가 없어 기존 파일을 유지합니다.")
        return
    items.sort(key=lambda i: i["date"], reverse=True)
    data = {"updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
            "source": "U.S. FDA 510(k) Premarket Notification Database (openFDA)",
            "note": "FDA DB는 제조사 법인명 기준 · DEKA는 El.En S.p.A., 울쎄라는 Ulthera, Inc. 명의",
            "items": items}
    for p in OUTS:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[FDA] 저장 {len(items)}건 · 신규 {sum(i['is_new'] for i in items)}건")


if __name__ == "__main__":
    main()
