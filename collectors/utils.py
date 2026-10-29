"""공용 유틸리티: 설정 로드, 카테고리/영역/회사/제품 분류, 중복 제거, JSON 저장."""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
DATA_PATH = os.path.join(BASE_DIR, "data", "news.json")
DOCS_DATA_PATH = os.path.join(BASE_DIR, "docs", "data", "news.json")
RETENTION_DAYS = 120
OVERSEAS_RETENTION_DAYS = 45  # 해외 기사는 양이 많아 45일만 보관


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def strip_html(text):
    text = re.sub(r"<.*?>", "", text or "")
    return html.unescape(text).strip()


def classify_categories(text, cfg):
    lowered = text.lower()
    matched = []
    for cat, keywords in cfg.get("categories", {}).items():
        if any(kw.lower() in lowered for kw in keywords):
            matched.append(cat)
    return matched or ["기타"]


def classify_segments(text, cfg):
    """써지컬 단어가 있으면 '써지컬'. 에스테틱은 기본값(기존과 동일)이며,
    써지컬 기사는 에스테틱 고유 단어('레이저'처럼 양쪽 공통어 제외)가 있을 때만 함께 표시."""
    lowered = text.lower()
    segs = cfg.get("segments", {})
    common = {t.lower() for t in cfg.get("segment_common_terms", [])}
    surg = any(kw.lower() in lowered for kw in segs.get("써지컬", []))
    if not surg:
        return ["에스테틱"]
    brand = cfg.get("brand", {})
    surg_terms = {t.lower() for t in segs.get("써지컬", [])}
    names = [t for t in brand.get("own_products", []) + brand.get("competitor_companies", []) + brand.get("competitor_products", [])
             if t.lower() not in surg_terms]  # 에스테틱 브랜드명(써지컬 전용 브랜드 제외)
    aes_terms = segs.get("에스테틱", []) + cfg.get("relevance", {}).get("specific_terms", []) + names
    aes = any(kw.lower() in lowered for kw in aes_terms if kw.lower() not in common)
    return ["에스테틱", "써지컬"] if aes else ["써지컬"]


def _hits(text, values):
    lowered = text.lower()
    return [v for v in values if v and not v.startswith("TODO") and v.lower() in lowered]


def find_mentions(text, cfg):
    brand = cfg.get("brand", {})
    return {
        "own_company_mentions": _hits(text, brand.get("own_company", [])),
        "own_product_mentions": _hits(text, brand.get("own_products", [])),
        "competitor_company_mentions": _hits(text, brand.get("competitor_companies", [])),
        "competitor_product_mentions": _hits(text, brand.get("competitor_products", [])),
    }


def is_relevant(text, cfg, local=False, overseas=False):
    """관련도 필터(규칙). 제외어가 있으면 탈락.
    - 국내 기사: 아래 중 하나를 만족해야 통과
      A) 업체명(원텍·경쟁사) 또는 미용의료 전용어(피부과·에스테틱·HIFU 등)가 있음
      B) 경쟁제품명 + 일반어(시술·리프팅·레이저 등)가 함께 있음
         ※ '더블로·온다·티타늄'처럼 일상어와 겹치는 제품명은 단독으로는 인정하지 않음
      C) 서로 다른 일반어가 2개 이상 있음
    - 해외 영문 기사: 주제어(context_terms) 1개 이상
    - 해외 현지어 기사: 제외어만 적용 (AI 검수에서 다시 확인)
    - 써지컬 전용어(relevance.surgical_terms)가 있으면 통과 (전립선·암 등 surgical_exempt 제외어는 이때 적용 안 함)"""
    rel = cfg.get("relevance", {})
    lowered = (text or "").lower()
    brand = cfg.get("brand", {})
    ambig = {t.lower() for t in rel.get("ambiguous_names", [])}
    own = [t for t in brand.get("own_company", []) + brand.get("own_products", []) if t.lower() not in ambig]
    if any(t.lower() in lowered for t in own):  # 원텍 기사는 항상 통과
        return True
    surgical = any(t.lower() in lowered for t in rel.get("surgical_terms", []) if t)
    exempt = {t.lower() for t in rel.get("surgical_exempt", [])} if surgical else set()
    if any(t.lower() in lowered for t in rel.get("exclude_terms", []) if t and t.lower() not in exempt):
        return False
    if local or surgical:  # 써지컬 전용어(홀뮴·EVLA 등)가 있으면 통과
        return True
    if overseas:
        contexts = [t.lower() for t in rel.get("context_terms", []) if t]
        return not contexts or any(t in lowered for t in contexts)
    has = lambda terms: [t for t in terms if t and t.lower() in lowered]
    names = [t for t in brand.get("own_company", []) + brand.get("own_products", []) + brand.get("competitor_companies", [])
             if t.lower() not in ambig]
    if has(names) or has(rel.get("specific_terms", [])):
        return True
    generic = set(has(rel.get("generic_terms", [])))
    if generic and has(brand.get("competitor_products", [])):
        return True
    return len(generic) >= 2


# ───────── AI 검수 (Google Gemini 무료 등급) ─────────
AI_CHECK_PROMPT = (
    "당신은 한국 미용의료기기 회사 원텍의 뉴스 클리핑 검수자입니다. 아래 기사 목록에서 "
    "'미용의료기기(RF·HIFU·레이저·마이크로니들 등 에스테틱 장비)와 피부미용·에스테틱 업계"
    "(장비 회사, 피부과·에스테틱 시술·병의원, 관련 인허가·규제·학회·전시회·시장·수출, 필러·톡신·스킨부스터 등 업계 동향)', "
    "그리고 '수술용·치료용 레이저 장비와 그 시장(홀뮴 레이저 전립선비대증·요로결석, 하지정맥류·치핵 레이저(EVLA), "
    "여성 질 레이저·CO2 프랙셔널, 통증 치료 레이저(PBM), 관련 회사·인허가·학회)'와 실질적으로 관련 없는 기사의 id만 고르세요.\n"
    "반드시 제외: 스포츠(더블더블 등), 연예·패션·화보·포토콜(제품명과 같은 단어가 우연히 들어간 경우 포함), "
    "연예인이 시술을 받았다는 가십, 정치·사회 일반, 주가·거래량·시황 단순 나열, "
    "미용과 무관한 레이저(산업·반도체·공항·군사·안과 라식/라섹), 레이저·에너지 장비와 무관한 일반 의료·제약(정형외과 임플란트·치과·안과·진단·신약 등), "
    "자동차·타이어(브리지스톤 포텐자 등), 화장품만 다루는 기사.\n"
    "위 제외 대상이 아니고 업계와 관련 있어 보이면 남기세요.\n"
    '형식: {"drop":[id, ...]}\n기사:\n'
)


def _gemini(prompt, key, models, deadline):
    url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
    import requests
    last = None
    for model in models:
        for attempt in range(4):
            if time.time() > deadline:
                raise RuntimeError("AI 검수 시간 한도 초과")
            try:
                r = requests.post(url.format(model), headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                                  json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
                                  timeout=90)
                if r.status_code == 429 and ("PerDay" in r.text or "retryDelay" not in r.text):
                    raise RuntimeError("AI 하루 무료 한도 소진 → 이번 수집은 AI 검수 생략")
                if r.status_code in (429, 500, 503) and attempt < 3:
                    time.sleep(20 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code} {r.text[:150]!r}")
                text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
                m = re.search(r"\{.*\}", text, re.S)
                time.sleep(4)
                return json.loads(m.group(0)) if m else {}
            except Exception as e:  # noqa: BLE001
                last = e
                break
    raise RuntimeError(last)


def ai_check(items, cfg):
    """아직 AI 검수 안 된 기사(ai_ok 없음)를 최신순으로 검수해 ai_ok True/False 표시.
    키가 없거나 실패하면 표시하지 않고 넘어감(다음 수집 때 다시 시도)."""
    key = os.environ.get("GEMINI_API_KEY")
    ai_cfg = cfg.get("relevance", {}).get("ai_check", {})
    if not key or not ai_cfg.get("enabled", True):
        return
    models = ai_cfg.get("models") or ["gemini-flash-latest", "gemini-flash-lite-latest"]
    brand = cfg.get("brand", {})
    own = [t.lower() for t in brand.get("own_company", []) + brand.get("own_products", [])]
    for i in items:  # 원텍 언급 기사는 검수 없이 유지
        if "ai_ok" not in i and any(t in f"{i.get('title', '')} {i.get('summary', '')}".lower() for t in own):
            i["ai_ok"] = True
    # 최근 기사만 검수 (오래된 기사까지 검수하면 무료 하루 한도를 다 써서 아침 메일 AI가 멈춤)
    age = int(ai_cfg.get("max_age_days", 10))
    cut = datetime.now(timezone.utc) - timedelta(days=age)

    def recent(i):
        try:
            d = parsedate_to_datetime(i.get("pub_date", ""))
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)) >= cut
        except (TypeError, ValueError):
            return True
    todo = [i for i in items if "ai_ok" not in i and recent(i)][: int(os.environ.get("AI_CHECK_MAX") or ai_cfg.get("max_per_run", 480))]
    deadline = time.time() + int(os.environ.get("AI_CHECK_SECONDS") or ai_cfg.get("max_seconds", 420))
    batch = int(ai_cfg.get("batch", 60))
    done = dropped = 0
    for s in range(0, len(todo), batch):
        chunk = todo[s:s + batch]
        rows = [{"id": k, "title": i.get("title", ""), "summary": (i.get("summary") or "")[:90]} for k, i in enumerate(chunk)]
        try:
            data = _gemini(AI_CHECK_PROMPT + json.dumps(rows, ensure_ascii=False), key, models, deadline)
        except Exception as e:  # noqa: BLE001
            print(f"[AI 검수] 중단: {e}", file=sys.stderr)
            break
        drop = {k for k in (data.get("drop") or []) if isinstance(k, int)}
        for k, i in enumerate(chunk):
            i["ai_ok"] = k not in drop
        done += len(chunk)
        dropped += len(drop)
    print(f"[AI 검수] {done}건 확인, {dropped}건 제외 (남은 미검수 {sum(1 for i in items if 'ai_ok' not in i)}건)")


def retag(item, cfg):
    """현재 config 기준으로 카테고리·영역·언급 정보를 다시 계산 (기존 누적 기사 포함)."""
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    mentions = find_mentions(text, cfg)
    item.update(mentions)
    item["categories"] = classify_categories(text, cfg)
    item["segments"] = classify_segments(text, cfg)
    item["own_brand_mentions"] = mentions["own_company_mentions"] + mentions["own_product_mentions"]
    item["competitor_mentions"] = mentions["competitor_company_mentions"] + mentions["competitor_product_mentions"]
    return item


def load_existing():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_news(items):
    existing = load_existing()
    by_link = {item["link"]: item for item in existing}
    for item in items:
        by_link[item["link"]] = item

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    cutoff_overseas = datetime.now(timezone.utc) - timedelta(days=OVERSEAS_RETENTION_DAYS)

    def is_recent(item):
        try:
            limit = cutoff_overseas if item.get("is_overseas") else cutoff
            return parsedate_to_datetime(item["pub_date"]) >= limit
        except (KeyError, TypeError, ValueError):
            return True

    cfg = load_config()

    def sort_key(item):
        try:
            dt = parsedate_to_datetime(item.get("pub_date", ""))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return datetime(1970, 1, 1, tzinfo=timezone.utc)

    # 기간 내 + 관련도 통과 기사만 남기고, 최신 기준으로 태그 재계산
    merged = sorted(
        (retag(v, cfg) for v in by_link.values()
         if is_recent(v) and is_relevant(f"{v.get('title', '')} {v.get('summary', '')}", cfg,
                                         local=v.get("local_lang", False), overseas=v.get("is_overseas", False))),
        key=sort_key,
        reverse=True,
    )
    # 2차: AI 검수에서 '무관'으로 판정된 기사 제외
    ai_check(merged, cfg)
    merged = [v for v in merged if v.get("ai_ok") is not False]

    for path in (DATA_PATH, DOCS_DATA_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged
