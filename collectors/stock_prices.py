"""경쟁사 주가 수집 → data/stocks.json, docs/data/stocks.json

- 국내: 금융위원회_주식시세정보 (공공데이터포털, Secret DATA_GO_KR_KEY)
  · 기준일 다음 영업일 13시 이후 갱신 → 아침에는 보통 '전전 영업일' 종가가 최신입니다.
  · 이용조건: 공공누리 제4유형(출처표시·상업적 이용금지·변경금지)
- 해외(인모드 INMD): Alpha Vantage 무료 API (Secret ALPHAVANTAGE_KEY) — 키가 없으면 건너뜀
- 조회 결과가 없는 종목(상장폐지 등)은 자동으로 빠지고 로그에 표시됩니다.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]
OUTS = [BASE / "data" / "stocks.json", BASE / "docs" / "data" / "stocks.json"]
KST = timezone(timedelta(hours=9))

# 표시 순서대로. own=True 는 원텍
# 비올(335890)·제이시스메디칼(287410)은 시세 조회 결과가 없어(상장폐지 추정) 제외 — 2026.10.27~28 확인
KR_STOCKS = [
    {"code": "336570", "name": "원텍", "own": True},
    {"code": "214150", "name": "클래시스"},
    {"code": "149980", "name": "하이로닉"},
    {"code": "199550", "name": "레이저옵텍"},
    {"code": "462510", "name": "라메디텍"},
]
US_STOCKS = [{"symbol": "INMD", "name": "인모드", "market": "NASDAQ"}]

KR_ENDPOINTS = [
    "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo",
    "https://apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2",
]
HISTORY_DAYS = 100  # 미니 그래프용 (약 3개월)


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch_kr(code, key):
    rows, err = [], None
    for attempt in range(3):
        rows, err = _fetch_kr_once(code, key)
        if rows:
            return rows, None
        time.sleep(3 * (attempt + 1))
    return rows, err


def _fetch_kr_once(code, key):
    end = datetime.now(KST)
    begin = end - timedelta(days=HISTORY_DAYS + 40)
    params = {"serviceKey": key, "resultType": "json", "numOfRows": 300, "pageNo": 1,
              "likeSrtnCd": code, "beginBasDt": begin.strftime("%Y%m%d")}
    last_err = None
    for url in KR_ENDPOINTS:
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code} {r.text[:120]!r}"
                continue
            body = r.json()["response"]["body"]
            items = body.get("items") or {}
            items = items.get("item", []) if isinstance(items, dict) else []
            if isinstance(items, dict):
                items = [items]
            rows = [i for i in items if str(i.get("srtnCd", "")).lstrip("A") == code]
            return rows, None
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
    return [], last_err


def build_kr(meta, rows):
    rows = sorted(rows, key=lambda i: i["basDt"])
    last = rows[-1]
    hist = [{"d": i["basDt"], "c": num(i.get("clpr"))} for i in rows][-70:]
    return {
        "name": meta["name"], "code": meta["code"], "own": meta.get("own", False),
        "market": last.get("mrktCtg", "KRX"), "currency": "KRW", "api_name": last.get("itmsNm"),
        "date": last["basDt"], "close": num(last.get("clpr")), "change": num(last.get("vs")),
        "pct": num(last.get("fltRt")), "mcap": num(last.get("mrktTotAmt")),
        "history": hist,
    }


def fetch_us(meta, key):
    r = requests.get("https://www.alphavantage.co/query",
                     params={"function": "TIME_SERIES_DAILY", "symbol": meta["symbol"], "outputsize": "compact", "apikey": key},
                     timeout=30)
    ts = r.json().get("Time Series (Daily)")
    if not ts:
        raise RuntimeError(str(r.json())[:200])
    days = sorted(ts.items())
    hist = [{"d": d.replace("-", ""), "c": float(v["4. close"])} for d, v in days][-70:]
    last, prev = hist[-1]["c"], hist[-2]["c"]
    return {
        "name": meta["name"], "code": meta["symbol"], "own": False, "market": meta["market"], "currency": "USD",
        "date": hist[-1]["d"], "close": last, "change": round(last - prev, 2), "pct": round((last - prev) / prev * 100, 2),
        "history": hist,
    }


def main():
    key = os.environ.get("DATA_GO_KR_KEY")
    out, skipped = [], []
    if key:
        for m in KR_STOCKS:
            rows, err = fetch_kr(m["code"], key)
            if rows:
                item = build_kr(m, rows)
                out.append(item)
                print(f"[주가] {m['name']}({m['code']}) {item['date']} 종가 {item['close']:,.0f} ({item['pct']:+.2f}%) · API명 {item['api_name']}")
            else:
                skipped.append(f"{m['name']}({m['code']})" + (f": {err}" if err else ": 조회 결과 없음(상장폐지·코드 확인 필요)"))
            time.sleep(0.3)
    else:
        print("[주가] DATA_GO_KR_KEY 없음 → 국내 주가 건너뜀")
    av = os.environ.get("ALPHAVANTAGE_KEY")
    for m in US_STOCKS:
        if not av:
            skipped.append(f"{m['name']}({m['symbol']}): ALPHAVANTAGE_KEY 없음")
            continue
        try:
            item = fetch_us(m, av)
            out.append(item)
            print(f"[주가] {m['name']}({m['symbol']}) {item['date']} 종가 ${item['close']:,.2f} ({item['pct']:+.2f}%)")
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{m['name']}({m['symbol']}): {e}")
    for s in skipped:
        print(f"[주가] 제외: {s}", file=sys.stderr)
    try:
        prev = json.loads(OUTS[0].read_text(encoding="utf-8")).get("items", [])
    except (FileNotFoundError, ValueError):
        prev = []
    got = {i["code"] for i in out}
    order = [m["code"] for m in KR_STOCKS] + [m["symbol"] for m in US_STOCKS]
    for p_ in prev:
        if p_["code"] not in got and not p_.get("delisted"):
            p_["stale"] = True  # 이번 조회 실패 → 직전 값 유지(기준일은 이전 날짜 그대로 표시)
            out.append(p_)
            print(f"[주가] {p_['name']}: 이번 조회 실패 → 직전 값({p_.get('date')}) 유지", file=sys.stderr)
    for i in out:
        if i["code"] in got:
            i.pop("stale", None)
    out.sort(key=lambda i: order.index(i["code"]) if i["code"] in order else 99)
    if not out:
        print("[주가] 수집된 종목이 없어 기존 파일을 그대로 둡니다.", file=sys.stderr)
        return
    data = {
        "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "source_kr": "금융위원회_주식시세정보(공공데이터포털) · 공공누리 제4유형",
        "source_us": "Alpha Vantage",
        "items": out,
    }
    for p in OUTS:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[주가] 저장 {len(out)}종목")


if __name__ == "__main__":
    main()
