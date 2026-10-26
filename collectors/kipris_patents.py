import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict
import yaml

BASE = Path(__file__).resolve().parents[1]

CFG = yaml.safe_load(
    (BASE / "config.yaml").read_text(encoding="utf-8")
)

API_URL = (
    "http://plus.kipris.or.kr/kipo-api/kipi/"
    "patUtiModInfoSearchSevice/getWordSearch"
)

OUTS = [
    BASE / "data" / "patent_trend.json",
    BASE / "docs" / "data" / "patent_trend.json",
]

# 마지막 갱신 시각 (특허 통계는 천천히 바뀌므로 주 1회만 갱신해 KIPRIS 무료 호출 한도를 아낌)
META = BASE / "data" / "patent_meta.json"
REFRESH_DAYS = 7

# 키워드별 연도 건수 (대시보드 특허 카드 하단 상세용)
KW_OUTS = [
    BASE / "data" / "patent_keywords.json",
    BASE / "docs" / "data" / "patent_keywords.json",
]


def fetch_keyword(keyword, api_key):
    page = 1
    rows = 500
    results = []

    while True:
        # ServiceKey를 마지막에 보내도록 순서 유지
        params = [
            ("word", keyword),
            ("year", 10),
            ("patent", "true"),
            ("utility", "false"),
            ("numOfRows", rows),
            ("pageNo", page),
            ("ServiceKey", api_key),
        ]

        r = requests.get(API_URL, params=params, timeout=60)
        r.raise_for_status()

        root = ET.fromstring(r.text)

        result_code = root.findtext(".//resultCode")
        if result_code and result_code not in ("00", "0"):
            raise RuntimeError(
                f"KIPRIS API 오류: {result_code} "
                f"{root.findtext('.//resultMsg')}"
            )

        items = root.findall(".//items/item")

        for item in items:
            results.append({
                "applicationNumber":
                    item.findtext("applicationNumber") or "",
                "applicationDate":
                    item.findtext("applicationDate") or "",
                "title":
                    item.findtext("inventionTitle") or "",
            })

        total = int(root.findtext(".//count/totalCount") or 0)

        if page * rows >= total:
            break

        page += 1
        time.sleep(0.25)

    return results


def run():
    src = CFG.get("sources", {}).get("kipris_patents", {})

    try:
        last = datetime.fromisoformat(json.loads(META.read_text(encoding="utf-8"))["updated_at"])
        if datetime.now(timezone.utc) - last < timedelta(days=REFRESH_DAYS) and os.environ.get("KIPRIS_FORCE") != "1":
            print(f"KIPRIS: 최근 갱신 {last:%Y-%m-%d} → {REFRESH_DAYS}일 이내라 이번엔 건너뜀")
            return
    except (FileNotFoundError, KeyError, ValueError):
        pass

    if not src.get("enabled", False):
        print("KIPRIS 특허 수집 비활성화")
        return

    api_key = os.environ.get("KIPRIS_API_KEY")

    if not api_key:
        raise RuntimeError("KIPRIS_API_KEY가 없습니다.")

    keywords = src.get("keywords", [])

    if not keywords:
        raise RuntimeError("config.yaml에 특허 검색 키워드가 없습니다.")

    # 출원번호 기준 중복 제거
    patents = {}
    kw_rows = []

    for keyword in keywords:
        print(f"[수집] {keyword}")

        rows = fetch_keyword(keyword, api_key)

        # 키워드 안에서만 출원번호 중복 제거 후 연도별 건수
        kw_yearly = defaultdict(int)
        seen = set()
        for item in rows:
            app_no = item["applicationNumber"]
            dt = item["applicationDate"]
            if not app_no or app_no in seen:
                continue
            seen.add(app_no)
            if len(dt) >= 4 and dt[:4].isdigit():
                kw_yearly[int(dt[:4])] += 1
        for year in range(2021, 2027):
            kw_rows.append({"keyword": keyword, "year": year, "count": kw_yearly.get(year, 0)})

        for item in rows:
            app_no = item["applicationNumber"]

            if app_no:
                patents[app_no] = item

        time.sleep(0.4)

    yearly = defaultdict(int)

    for item in patents.values():
        dt = item["applicationDate"]

        if len(dt) >= 4 and dt[:4].isdigit():
            year = int(dt[:4])

            if 2021 <= year <= 2026:
                yearly[year] += 1

    output = [
        {
            "year": year,
            "count": yearly.get(year, 0)
        }
        for year in range(2021, 2027)
    ]

    for path in OUTS:
        path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    for path in KW_OUTS:
        path.write_text(
            json.dumps(kw_rows, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    META.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    print(f"[완료] 중복 제거 특허 {len(patents)}건")
    print(output)


if __name__ == "__main__":
    try:
        run()
    except RuntimeError as e:
        if "LIMITED_NUMBER" in str(e) or "EXCEEDS" in str(e):
            # 무료 호출 한도 초과: 기존 특허 데이터를 그대로 두고 정상 종료 (다음 주기에 다시 시도)
            print(f"[안내] KIPRIS 무료 호출 한도 초과 → 기존 데이터 유지: {e}", file=sys.stderr)
            sys.exit(0)
        raise