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


# 키워드별 출원번호·출원연도 보관 (합계 중복 제거용, 대시보드에는 안 올림)
APPS = BASE / "data" / "patent_apps.json"
SEG_OUTS = [
    BASE / "data" / "patent_trend_seg.json",
    BASE / "docs" / "data" / "patent_trend_seg.json",
]
YEARS = range(2021, 2027)


def _write(paths, obj):
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def run():
    src = CFG.get("sources", {}).get("kipris_patents", {})
    if not src.get("enabled", False):
        print("KIPRIS 특허 수집 비활성화")
        return

    segs = {k: "에스테틱" for k in src.get("keywords", [])}
    segs.update({k: "써지컬" for k in src.get("surgical_keywords", [])})
    if not segs:
        raise RuntimeError("config.yaml에 특허 검색 키워드가 없습니다.")

    try:
        apps = json.loads(APPS.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        apps = {}

    # 주 1회 전체 갱신. 그 사이에는 새로 추가된 키워드만 조회 (KIPRIS 무료 호출 한도 절약)
    full = True
    try:
        last = datetime.fromisoformat(json.loads(META.read_text(encoding="utf-8"))["updated_at"])
        full = datetime.now(timezone.utc) - last >= timedelta(days=REFRESH_DAYS) or os.environ.get("KIPRIS_FORCE") == "1"
    except (FileNotFoundError, KeyError, ValueError):
        pass
    todo = list(segs) if full else [k for k in segs if k not in apps]
    if not todo:
        print("KIPRIS: 최근 갱신 후 7일 이내 · 새 키워드 없음 → 건너뜀")
        return

    api_key = os.environ.get("KIPRIS_API_KEY")
    if not api_key:
        raise RuntimeError("KIPRIS_API_KEY가 없습니다.")

    try:
        for keyword in todo:
            print(f"[수집] {keyword} ({segs[keyword]})")
            got = {}
            for item in fetch_keyword(keyword, api_key):
                no, dt = item["applicationNumber"], item["applicationDate"]
                if no and len(dt) >= 4 and dt[:4].isdigit():
                    got[no] = int(dt[:4])
            apps[keyword] = got
            time.sleep(0.4)
    finally:
        # 한도 초과로 중간에 멈춰도 받은 키워드까지는 저장
        apps = {k: v for k, v in apps.items() if k in segs}
        APPS.write_text(json.dumps(apps, ensure_ascii=False), encoding="utf-8")
        kw_rows, totals = [], {"전체": {}, "에스테틱": {}, "써지컬": {}}
        for k, seg in segs.items():
            if k not in apps:
                continue
            per = {y: 0 for y in YEARS}
            for no, y in apps[k].items():
                if y in per:
                    per[y] += 1
                    totals["전체"][no] = y
                    totals[seg][no] = y
            kw_rows += [{"keyword": k, "seg": seg, "year": y, "count": per[y]} for y in YEARS]
        trend = {s: [{"year": y, "count": sum(1 for v in d.values() if v == y)} for y in YEARS] for s, d in totals.items()}
        if all(k in apps for k in segs):  # 모든 키워드가 모였을 때만 대시보드 파일 교체
            _write(KW_OUTS, kw_rows)
            _write(SEG_OUTS, trend)
            _write(OUTS, trend["에스테틱"])  # 기존 형식 유지 (에스테틱 합계)
            META.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
            print("[완료]", {s: [x["count"] for x in v] for s, v in trend.items()})
        else:
            print(f"[보류] 아직 못 받은 키워드 {[k for k in segs if k not in apps]} → 기존 대시보드 데이터 유지, 다음 수집 때 이어서 조회")


if __name__ == "__main__":
    try:
        run()
    except RuntimeError as e:
        if "LIMITED_NUMBER" in str(e) or "EXCEEDS" in str(e):
            # 무료 호출 한도 초과: 기존 특허 데이터를 그대로 두고 정상 종료 (다음 주기에 다시 시도)
            print(f"[안내] KIPRIS 무료 호출 한도 초과 → 기존 데이터 유지: {e}", file=sys.stderr)
            sys.exit(0)
        raise