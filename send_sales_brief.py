"""WONTECH Sales Morning Brief - 영업부문 평일 아침 메일.
기사는 직전 아침 이후(월요일은 금요일 아침 이후), 상단 숫자 카드는 최근 7일(대시보드 '주간'과 같은 기준).

구성
  ① 오늘의 영업 포인트 3줄
  ② 경쟁사 동향 (회사별)
  ③ 원텍 언급 기사
  ④ 해외 시장 소식 (국가별)
  ⑤ 시장 지표 카드 (생산·수출·수입·경쟁사 매출·의원 수)
  ⑥ 이번 주·다음 주 학회·전시회

환경변수 (GitHub Secrets)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD : 회사 메일 발송 계정 (있으면 우선 사용)
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD  : 회사 메일 설정이 없을 때 쓰는 Gmail 계정
  SALES_EMAIL_TO                     : 영업부문 수신자 (쉼표로 구분)
  SALES_EMAIL_TEST_TO                : 테스트 수신자 (쉼표로 구분)
  SALES_EMAIL_REPLY_TO               : (선택) 답장 받을 회사 메일
  GEMINI_API_KEY                     : (선택) Google AI Studio 무료 키 → 요약·영업 포인트 작성
  SEND_MODE                          : all(기본) / test / preview(발송 없이 미리보기 파일만)
"""
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr, getaddresses, parsedate_to_datetime

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "collectors"))
from utils import load_config  # noqa: E402

KST = timezone(timedelta(hours=9))
WEEKDAY = "월화수목금토일"

# ── 디자인 토큰 (원텍 CI) ──
OR = "#FF4700"      # WONTECH orange
OR_SOFT = "#FFF1EA"
INK = "#1A1A1A"
SUB = "#6B6B6B"
MUTED = "#9A9A94"
LINE = "#ECE8E3"
BG = "#F4F2EF"
FONT = "'Malgun Gothic','Apple SD Gothic Neo',Arial,sans-serif"

DEFAULT_GROUPS = {
    "클래시스": ["클래시스", "Classys", "CLASSYS", "슈링크", "Shurink", "울트라포머", "Ultraformer", "볼뉴머", "Volnewmer", "쿼드세이"],
    "제이시스메디칼": ["제이시스", "Jeisys", "덴서티", "포텐자", "Potenza", "리니어지", "LinearZ"],
    "루트로닉·사이노슈어": ["루트로닉", "Lutronic", "사이노슈어", "Cynosure", "세르프", "XERF", "피코플러스", "PicoPlus"],
    "멀츠 (울쎄라)": ["멀츠", "Merz", "울쎄라", "Ultherapy"],
    "솔타 (써마지)": ["솔타", "Solta", "써마지", "Thermage", "프락셀", "Fraxel"],
    "인모드": ["인모드", "InMode", "모피어스", "Morpheus8"],
    "비올메디컬": ["비올", "VIOL", "실펌", "Sylfirm", "셀리뉴"],
    "하이로닉": ["하이로닉", "Hironic", "더블로", "Doublo", "브이로", "V-RO"],
    "아스테라시스": ["아스테라시스", "Asterasys", "리프테라", "Liftera", "쿨페이즈", "쿨소닉"],
}
PRIORITY_CATS = {"신제품·출시": 3, "인허가·규제": 3, "시장동향": 2, "글로벌사업": 2, "학회·임상": 1}


# ───────────────────────── 데이터 ─────────────────────────
def load_json(name, default):
    path = os.path.join(BASE, "data", name)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def pub_dt(item):
    try:
        dt = parsedate_to_datetime(item.get("pub_date", ""))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def window_items(items, now, hours=None):
    # 기사: 월요일은 금요일 아침 이후(약 74시간), 그 외는 직전 아침 이후(약 26시간)
    if hours is None:
        hours = 74 if now.weekday() == 0 else 26
    since = now - timedelta(hours=hours)
    return [i for i in items if (pub_dt(i) or since) > since], hours


def text_of(i):
    return f"{i.get('title', '')} {i.get('summary', '')}"


def score(item, terms):
    t = item.get("title", "").lower()
    s = 3 if any(x.lower() in t for x in terms) else 1
    cats = item.get("categories", [])
    s += max([PRIORITY_CATS.get(c, 0) for c in cats] or [0])
    if cats == ["기타"]:
        s -= 2
    # 병의원 홍보성 칼럼·연예 기사는 영업 정보 가치가 낮아 뒤로
    if re.search(r"원장|칼럼|맞춤|중요|고려할|도움|주의|비결|고백|미모|동안", item.get("title", "")):
        s -= 2
    s += min(item.get("_dup", 0), 3) * 0.5  # 여러 매체가 보도한 이슈일수록 가산
    return s


def _grams(t):
    t = re.sub(r"[^0-9a-z가-힣]", "", (t or "").lower())
    return {t[k:k + 2] for k in range(len(t) - 1)}


def dedupe(items):
    """같은 사건을 다룬 기사(제목 유사도 0.45 이상)는 1건으로 묶고 _dup에 중복 수 기록."""
    reps = []
    for i in sorted(items, key=lambda x: pub_dt(x) or datetime.min.replace(tzinfo=timezone.utc)):
        g = _grams(i.get("title"))
        sg = _grams(i.get("summary", "")[:120])
        for r in reps:
            rg, rs = r["_g"], r["_sg"]
            same_title = g and rg and len(g & rg) / len(g | rg) >= 0.45
            same_body = sg and rs and len(sg & rs) / len(sg | rs) >= 0.5
            if same_title or same_body:
                r["_dup"] = r.get("_dup", 0) + 1
                break
        else:
            i = dict(i, _g=g, _sg=sg, _dup=0)
            reps.append(i)
    return reps


def pick_groups(items, groups, per_group=3):
    used, out = set(), []
    for name, terms in groups.items():
        # 제목에 회사·제품명이 들어간 기사만 (본문 스치듯 언급 제외)
        hits = [i for i in items if i["link"] not in used and any(x.lower() in i.get("title", "").lower() for x in terms)]
        hits.sort(key=lambda i: (score(i, terms), pub_dt(i) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        sel = []
        for i in hits:  # 같은 회사 안에서 비슷한 이야기는 1건만
            g = i.get("_g") or _grams(i.get("title"))
            if all(len(g & (j.get("_g") or set())) / max(1, len(g | (j.get("_g") or set()))) < 0.3 for j in sel):
                sel.append(i)
            if len(sel) >= per_group:
                break
        for i in sel:
            used.add(i["link"])
        if sel:
            out.append((name, sel, len(hits)))
    others = [i for i in items if i["link"] not in used and i.get("competitor_mentions")
              and any(m.lower() in i.get("title", "").lower() for m in i.get("competitor_mentions", []))]
    others.sort(key=lambda i: score(i, i.get("competitor_mentions", [])), reverse=True)
    pick, seen_co = [], set()
    for i in others:  # 기타 경쟁사는 회사당 1건
        key = (i.get("competitor_company_mentions") or i.get("competitor_mentions"))[0].lower()
        if key not in seen_co:
            pick.append(i)
            seen_co.add(key)
        if len(pick) >= 4:
            break
    if pick:
        out.append(("기타 경쟁사", pick, len(others)))
        used.update(i["link"] for i in pick)
    return out, used


# ───────────────────────── AI 요약 (Google Gemini 무료 등급) ─────────────────────────
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


AI_STATE = {"start": None, "down": False}
AI_BUDGET_SEC = 240  # AI에 쓰는 시간은 최대 4분. 넘으면 AI 없이 바로 발송


def _chat(prompt, models, key):
    """Gemini API 무료 등급 호출. 모델을 바꿔가며 재시도."""
    if AI_STATE["start"] is None:
        AI_STATE["start"] = time.time()
    if AI_STATE["down"]:
        raise RuntimeError("앞선 AI 호출 실패 → 이번 발송은 AI 생략")
    last = None
    for model in models:
        for attempt in range(4):
            try:
                r = requests.post(
                    GEMINI_URL.format(model=model),
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json={"systemInstruction": {"parts": [{"text": SYSTEM}]},
                          "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}},
                    timeout=90,
                )
                if time.time() - AI_STATE["start"] > AI_BUDGET_SEC:
                    AI_STATE["down"] = True
                    raise RuntimeError(f"AI 시간 한도({AI_BUDGET_SEC}초) 초과 → AI 생략")
                if r.status_code in (500, 503) and attempt < 3:  # 구글 서버 혼잡(일시적) → 20·40·60초 대기 후 재시도
                    wait = 20 * (attempt + 1)
                    print(f"  [AI] {model} 서버 혼잡({r.status_code}) → {wait}초 후 재시도", file=sys.stderr)
                    time.sleep(wait)
                    continue
                if r.status_code == 429 and attempt < 3:  # 무료 한도 초과 → 안내된 시간만큼 대기 후 재시도
                    wait = re.search(r'"retryDelay":\s*"(\d+)', r.text)
                    wait = min(int(wait.group(1)) + 2, 65) if wait else 35
                    print(f"  [AI] {model} 한도 초과(429) → {wait}초 대기: {r.text[:160]!r}", file=sys.stderr)
                    time.sleep(wait)
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code} {r.text[:200]!r}")
                text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
                m = re.search(r"\{.*\}", text, re.S)
                print(f"  [AI] 성공: {model}")
                time.sleep(5)  # 무료 등급 분당 한도 보호
                return json.loads(m.group(0)) if m else None
            except Exception as e:
                last = e
                print(f"  [AI] {model} 실패: {e}", file=sys.stderr)
                break
        if AI_STATE["down"]:
            break
    AI_STATE["down"] = True
    raise RuntimeError(last)


SYSTEM = ("당신은 한국 미용의료기기 회사 원텍(WONTECH, 제품: 올리지오·타이탄·피코케어·라비앙)의 영업전략팀 애널리스트입니다. "
          "주어진 기사 정보만 근거로 쓰고, 기사에 없는 사실·숫자는 절대 만들지 마세요. 추정이면 '~로 보임'처럼 밝히세요. "
          "한국어, 간결한 존댓말. 반드시 JSON만 출력하세요.")


def _ai_models(cfg):
    ai_cfg = cfg.get("sales_email", {}).get("ai", {})
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not ai_cfg.get("enabled", True):
        return None, None
    models = [m for m in (ai_cfg.get("models") or []) if m.startswith("gemini")] or ["gemini-flash-latest", "gemini-flash-lite-latest"]
    return models, key


def ai_dedupe(cfg, comp_groups, per_group):
    """같은 회사 안에서 같은 사건을 다룬 기사를 AI로 묶어 1건만 남김. 실패하면 앞에서부터 per_group건."""
    models, key = _ai_models(cfg)
    flat = [(gi, i) for gi, (_, its, _) in enumerate(comp_groups) for i in its]
    drop = set()
    if models and flat:
        rows = [{"id": n, "group": comp_groups[gi][0], "title": i.get("title", ""), "summary": i.get("summary", "")[:100]}
                for n, (gi, i) in enumerate(flat)]
        try:
            data = _chat(
                "두 가지를 하세요.\n"
                "1) 같은 group 안에서 '같은 사건·같은 발표'를 다룬 기사끼리 묶으세요. 제목이 달라도 내용이 같은 소식이면 같은 묶음입니다. "
                "각 묶음의 첫 id는 정보가 가장 충실한 기사로 하세요. 혼자인 기사는 쓰지 마세요.\n"
                "2) 미용의료기기·에스테틱 업계(또는 수술용 레이저 장비)와 실질적으로 관련 없는 기사 id를 off에 넣으세요. "
                "예: 스포츠, 연예·패션·화보(제품명과 같은 단어가 우연히 들어간 경우), 연예인 시술 가십, 정치, 주가·거래량 단순 나열, "
                "미용과 무관한 레이저·의료, 자동차·타이어.\n"
                '형식: {"same":[[id,id,...], ...], "off":[id, ...]}\n기사:\n' + json.dumps(rows, ensure_ascii=False), models, key) or {}
            for grp in data.get("same") or []:
                ids = [k for k in grp if isinstance(k, int) and 0 <= k < len(flat)]
                if len(ids) < 2:
                    continue
                keep = flat[ids[0]][1]
                for k in ids[1:]:
                    if flat[k][0] == flat[ids[0]][0] and flat[k][1] is not keep:
                        drop.add(id(flat[k][1]))
                        keep["_dup"] = keep.get("_dup", 0) + 1 + flat[k][1].get("_dup", 0)
            off = [k for k in (data.get("off") or []) if isinstance(k, int) and 0 <= k < len(flat)]
            for k in off:
                drop.add(id(flat[k][1]))
                print(f"  [AI] 무관 기사 제외: {flat[k][1].get('title', '')[:60]}")
            print(f"  [AI] 중복·무관 기사 {len(drop)}건 제외")
        except Exception as ex:
            print(f"[경고] AI 중복 묶기 실패 → 기존 방식 사용: {ex}", file=sys.stderr)
    out = []
    for name, its, total in comp_groups:
        keep = [i for i in its if id(i) not in drop][:(4 if name == "기타 경쟁사" else per_group)]
        if keep:
            out.append((name, keep, total))
    return out


def ai_enrich(cfg, comp_groups, own_items, overseas):
    models, token = _ai_models(cfg)
    if not models:
        return None
    catalog = [(g, i) for g, its, _ in comp_groups for i in its]
    catalog += [("원텍", i) for i in own_items] + [(f"해외·{i.get('country', '')}", i) for i in overseas]
    if not catalog:
        return None
    by_link = {}
    try:
        # 1) 기사별 요약·영업 포인트 (무료 등급 일일 호출 수를 아끼려고 한 번에 처리)
        for start in range(0, len(catalog), 60):
            chunk = catalog[start:start + 60]
            rows = [{"id": k, "group": g, "title": i.get("title", ""), "summary": i.get("summary", "")[:160]}
                    for k, (g, i) in enumerate(chunk)]
            data = _chat(
                "각 기사에 대해 영업사원용 요약과 영업 포인트를 쓰세요.\n"
                "[요약] 기사의 핵심 사실(누가·무엇을·숫자)을 1~2문장으로.\n"
                "[영업 포인트] 원텍 영업사원이 병원 원장님 상담이나 경쟁 대응에서 바로 쓸 수 있는 구체적인 1문장만 쓰세요. "
                "기사 속 사실(제품명·숫자·시장·시술명)을 반드시 근거로 하고, 원텍 제품(올리지오·타이탄·피코케어·라비앙 등)과 연결될 때만 쓰세요. "
                "예: '포텐자 팁 누적 400만 개 → 소모품 비용을 묻는 원장님께 장비별 시술당 소모품 비용 비교 자료로 대응'. "
                "'참고해야', '모니터링', '대응해야 할 것으로 보입니다', '강조하는 방향으로' 같은 일반론은 금지합니다. "
                "구체적으로 쓸 내용이 없으면 반드시 빈 문자열로 두세요(대부분의 기사는 빈 문자열이어도 됩니다).\n"
                '형식: {"items":{"<id>":{"summary":"...","point":"..."}}}\n'
                "해외 기사(영문 등)도 요약은 한국어로 쓰되 제목은 번역하지 마세요.\n기사:\n"
                + json.dumps(rows, ensure_ascii=False), models, token) or {}
            for k, (g, i) in enumerate(chunk):
                by_link[i["link"]] = (data.get("items") or {}).get(str(k)) or {}
        # 2) 오늘의 영업 포인트 3줄 (제목·요약만 사용)
        brief = [{"id": n, "group": g, "title": i.get("title", ""),
                  "summary": (by_link.get(i["link"], {}).get("summary") or i.get("summary", ""))[:120]}
                 for n, (g, i) in enumerate(catalog[:30])]
        data = _chat(
            "아래 기사 중 오늘 아침 영업사원이 꼭 알아야 할 이슈 3개를 고르고, 각각 60자 내외 1문장으로 쓰세요. "
            "경쟁사 신제품·인허가·실적, 원텍 소식, 시장 변화 순으로 우선하세요.\n"
            '형식: {"issues":[{"text":"...","id":기사id}, ... 3개]}\n기사:\n' + json.dumps(brief, ensure_ascii=False),
            models, token) or {}
    except Exception as e:  # AI 실패 시 규칙 기반으로 발송
        print(f"[경고] AI 요약 실패 → 규칙 기반으로 작성: {e}", file=sys.stderr)
        return {"issues": [], "items": by_link} if by_link else None
    issues = []
    for it in (data.get("issues") or [])[:3]:
        k = it.get("id")
        link = catalog[k][1]["link"] if isinstance(k, int) and 0 <= k < len(catalog) else ""
        if it.get("text"):
            issues.append({"text": it["text"], "link": link})
    return {"issues": issues, "items": by_link}


# ───────────────────────── HTML 조각 ─────────────────────────
def e(s):
    return html.escape(str(s or ""))


def fmt_date(i):
    d = pub_dt(i)
    return d.astimezone(KST).strftime("%m/%d") if d else ""


def section(num, title, sub=""):
    return f"""
<tr><td style="padding:30px 32px 10px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td style="font:800 11px {FONT};color:{OR};letter-spacing:1px;width:28px;vertical-align:middle">{num}</td>
    <td style="font:800 17px {FONT};color:{INK};vertical-align:middle">{e(title)}</td>
    <td align="right" style="font:400 11px {FONT};color:{MUTED};vertical-align:middle">{e(sub)}</td>
  </tr></table>
  <div style="height:2px;background:{INK};margin-top:10px;line-height:2px;font-size:0">&nbsp;</div>
</td></tr>"""


def article(i, info=None, accent=False):
    info = info or {}
    title = i.get("title", "")
    orig = ""
    summ = info.get("summary") or i.get("summary", "")
    summ = summ[:180] + ("…" if len(summ) > 180 else "")
    point = info.get("point", "")
    tag = ""
    if i.get("is_overseas"):
        tag = f'<span style="display:inline-block;background:{INK};color:#fff;font:700 10px {FONT};padding:2px 7px;border-radius:3px;margin-right:6px">{e(i.get("country") or "해외")}</span>'
    border = OR if accent else LINE
    return f"""
<tr><td style="padding:0 32px 12px">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {LINE};border-left:3px solid {border};background:#fff">
  <tr><td style="padding:14px 16px">
   <a href="{e(i.get('link'))}" target="_blank" style="font:700 14px/1.45 {FONT};color:{INK};text-decoration:none">{tag}{e(title)}</a>
   {f'<div style="font:400 11px {FONT};color:{MUTED};margin-top:3px">{e(orig)}</div>' if orig else ''}
   <div style="font:400 11px {FONT};color:{MUTED};margin-top:5px">{e(i.get('source'))} · {fmt_date(i)}{f' · 유사 보도 {i["_dup"]}건' if i.get('_dup') else ''}</div>
   {f'<div style="font:400 13px/1.65 {FONT};color:#333;margin-top:8px">{e(summ)}</div>' if summ else ''}
   {f'<div style="margin-top:9px;background:{OR_SOFT};padding:8px 11px;font:400 12px/1.6 {FONT};color:#7a2a07"><b style="color:{OR}">▶ 영업 포인트</b>&nbsp; {e(point)}</div>' if point else ''}
  </td></tr>
 </table>
</td></tr>"""


def group_header(name, total):
    return f"""
<tr><td style="padding:6px 32px 8px">
  <span style="display:inline-block;background:{OR_SOFT};color:{OR};font:800 12px {FONT};padding:4px 10px;border-radius:12px">{e(name)}</span>
  <span style="font:400 11px {FONT};color:{MUTED};margin-left:6px">관련 기사 {total}건</span>
</td></tr>"""


def region_map(market, dash):
    """대시보드 지도와 같은 '글로벌 EBD 시장 지역 비중' 이미지 + 지역별 수치 (공개 자료만 사용)."""
    regs = market.get("regions") or []
    if not regs:
        return ""
    cells = "".join(
        f'<td width="20%" align="center" style="padding:8px 2px;border-left:{"0" if i == 0 else "1px solid " + LINE}">'
        f'<div style="font:400 11px {FONT};color:{SUB}">{e(r["name"])}</div>'
        f'<div style="font:800 15px {FONT};color:{INK};margin-top:2px">{r["share"]:.1f}%</div>'
        f'<div style="font:400 10px {FONT};color:{MUTED};margin-top:1px">CAGR {r.get("cagr", "-")}%</div></td>'
        for i, r in enumerate(regs))
    img = (f'<a href="{e(dash)}" target="_blank"><img src="{e(dash)}map_regions.png" width="616" alt="글로벌 EBD 시장 지역 비중 지도" '
           f'style="display:block;width:100%;max-width:616px;height:auto;border:0"></a>') if dash else ""
    return f"""
<tr><td style="padding:0 32px 6px">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid {LINE}">
  <tr><td style="padding:12px 12px 4px">{img}</td></tr>
  <tr><td style="padding:0 8px 6px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table></td></tr>
 </table>
 <div style="font:400 12px {FONT};color:{INK};margin-top:8px">대시보드에서는 시장 비중 외에 <b>시장 규모·CAGR</b>과 <b>국가별 시장 비중</b>도 확인할 수 있습니다.
  {f'<a href="{e(dash)}" target="_blank" style="color:{OR};text-decoration:none;white-space:nowrap">대시보드 지도 보기 →</a>' if dash else ''}</div>
 <div style="font:400 10px/1.6 {FONT};color:{MUTED};margin-top:4px">출처: {e(market.get('source', ''))} · 2025년 매출 기준 · CAGR 2026–2033</div>
</td></tr>"""


def empty(msg):
    return f'<tr><td style="padding:0 32px 12px"><div style="border:1px solid {LINE};background:#fff;padding:14px 16px;font:400 13px {FONT};color:{MUTED}">{e(msg)}</div></td></tr>'


def kpi_strip(cells):
    tds = "".join(
        f'<td width="{100 // len(cells)}%" style="padding:14px 6px;text-align:center;border-right:1px solid {LINE}">'
        f'<div style="font:400 11px {FONT};color:{SUB}">{e(k)}</div>'
        f'<div style="font:800 22px {FONT};color:{OR if hl else INK};margin-top:4px">{e(v)}</div></td>'
        for k, v, hl in cells)
    return f'<tr><td style="padding:18px 32px 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid {LINE}"><tr>{tds}</tr></table></td></tr>'


def mini_bars(hist, color=OR):
    vals = [float(h.get("value") or 0) for h in hist]
    mx = max(vals + [1])
    cells = ""
    for h, v in zip(hist, vals):
        px = max(4, int(v / mx * 40))
        cells += (f'<td valign="bottom" align="center" style="padding:0 3px">'
                  f'<div style="font:700 9px {FONT};color:{INK}">{v:,.0f}</div>'
                  f'<div style="width:14px;height:{px}px;background:{color};line-height:{px}px;font-size:0;margin:2px auto 0">&nbsp;</div>'
                  f'<div style="font:400 9px {FONT};color:{MUTED};margin-top:2px">\'{str(h.get("year"))[-2:]}</div></td>')
    return f'<table role="presentation" cellpadding="0" cellspacing="0" align="center"><tr>{cells}</tr></table>'


def metric_card(title, unit, x):
    hist = (x or {}).get("history", [])
    if not hist:
        return f'<td width="33%" style="padding:6px"></td>'
    last, prev = hist[-1]["value"], hist[-2]["value"] if len(hist) > 1 else None
    yoy = f'{(last - prev) / prev * 100:+.1f}%' if prev else ""
    return (f'<td width="33%" valign="top" style="padding:6px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid {LINE}"><tr><td style="padding:12px 10px">'
            f'<div style="font:700 11px {FONT};color:{SUB}">{e(title)}</div>'
            f'<div style="margin-top:4px"><span style="font:800 18px {FONT};color:{INK}">{last:,.0f}</span> '
            f'<span style="font:400 10px {FONT};color:{MUTED}">{e(unit)}</span> '
            f'<span style="font:700 11px {FONT};color:{OR}">{yoy}</span></div>'
            f'<div style="margin-top:8px">{mini_bars(hist)}</div>'
            f'<div style="font:400 9px {FONT};color:{MUTED};margin-top:6px">{e(x.get("source", ""))}</div>'
            f'</td></tr></table></td>')


def competitor_card(x):
    items = sorted((x or {}).get("items", []), key=lambda it: -(it["history"][-1]["value"]))
    if not items:
        return '<td width="33%"></td>'
    mx = max(it["history"][-1]["value"] for it in items)
    rows = ""
    for it in items:
        h = it["history"]
        v, p = h[-1]["value"], (h[-2]["value"] if len(h) > 1 else None)
        own = it.get("own")
        w = max(6, int(v / mx * 100))
        rows += (f'<tr><td style="font:{"800" if own else "400"} 11px {FONT};color:{OR if own else SUB};padding:3px 4px 3px 0;white-space:nowrap">{e(it["name"])}</td>'
                 f'<td width="100%" style="padding:3px 4px"><div style="background:{LINE};height:8px;line-height:8px;font-size:0"><div style="width:{w}%;background:{OR if own else "#9A9A94"};height:8px;line-height:8px;font-size:0">&nbsp;</div></div></td>'
                 f'<td align="right" style="font:700 11px {FONT};color:{INK};padding:3px 0;white-space:nowrap">{v:,.0f}</td>'
                 f'<td align="right" style="font:700 10px {FONT};color:{OR};padding:3px 0 3px 5px;white-space:nowrap">{((v - p) / p * 100):+.1f}%</td></tr>' if p else '')
    return (f'<td width="66%" colspan="2" valign="top" style="padding:6px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid {LINE}"><tr><td style="padding:12px 14px">'
            f'<div style="font:700 11px {FONT};color:{SUB}">주요 경쟁사 매출 (2025, 억원)</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">{rows}</table>'
            f'<div style="font:400 9px {FONT};color:{MUTED};margin-top:6px">DART · 제이시스 별도, 그 외 연결</div>'
            f'</td></tr></table></td>')


# ───────────────────────── 본문 조립 ─────────────────────────
def build(cfg, now):
    se = cfg.get("sales_email", {})
    news = load_json("news.json", [])
    metrics = load_json("market_metrics.json", {})
    events = load_json("exhibitions.json", [])
    items, hours = window_items(news, now)
    # 상단 숫자 카드: 최근 7일 (대시보드 '주간'과 같은 기준)
    wk_since = now - timedelta(days=7)
    week = [i for i in news if pub_dt(i) is None or pub_dt(i) >= wk_since]  # 대시보드와 동일 기준
    k_total = len(week)
    k_comp = sum(1 for i in week if i.get("competitor_mentions") or i.get("competitor_company_mentions") or i.get("competitor_product_mentions"))
    k_own = sum(1 for i in week if i.get("own_brand_mentions") or i.get("own_company_mentions") or i.get("own_product_mentions"))
    k_ov = sum(1 for i in week if i.get("is_overseas"))
    items = dedupe(items)
    domestic = [i for i in items if not i.get("is_overseas")]
    overseas = [i for i in items if i.get("is_overseas")]
    overseas_total = len(overseas)

    groups = se.get("competitor_groups") or DEFAULT_GROUPS
    per_group = int(se.get("per_group", 3))
    comp_groups, _ = pick_groups(items, groups, per_group=per_group + 3)  # 후보 넉넉히
    comp_groups = ai_dedupe(cfg, comp_groups, per_group)
    used_links = [i["link"] for _, its, _ in comp_groups for i in its]
    own = [i for i in items if i.get("own_brand_mentions") or i.get("own_company_mentions")]
    used_links = set(used_links) | {i["link"] for i in own}
    own.sort(key=lambda i: pub_dt(i) or now, reverse=True)
    own_total = len(own)
    own = own[: int(se.get("max_own", 5))]
    DEVICE = re.compile(r"hifu|ultherapy|thermage|xerf|inmode|morpheus|potenza|sofwave|laser|radiofrequency|"
                        r"ultrasound|microneedl|aesthetic device|energy-based|ウルセラ|サーマ|ハイフ|レーザー|"
                        r"海芙|海扶|電波|皮秒|雷射|激光|音波|熱瑪吉|láser|laser|lazer|ليزر", re.I)
    overseas = [i for i in overseas if i["link"] not in used_links and (
        i.get("competitor_mentions") or i.get("own_brand_mentions")
        or DEVICE.search(i.get("title", ""))
        or (not i.get("local_lang") and i.get("categories") not in (["기타"], None)))]
    ov = sorted(overseas, key=lambda i: (bool(i.get("competitor_mentions")), not i.get("local_lang"), pub_dt(i) or now), reverse=True)
    seen_c, ov_pick = {}, []
    for i in ov:  # 국가당 최대 2건
        c = i.get("country", "글로벌")
        if seen_c.get(c, 0) < 2:
            ov_pick.append(i)
            seen_c[c] = seen_c.get(c, 0) + 1
        if len(ov_pick) >= int(se.get("max_overseas", 8)):
            break

    ai = ai_enrich(cfg, comp_groups, own, ov_pick)
    info = (ai or {}).get("items", {})

    # ① 오늘의 영업 포인트
    if ai and len(ai.get("issues") or []) >= 2:
        issues = ai["issues"]
    else:
        # 규칙 기반: 원텍 1건 + 경쟁사는 회사마다 대표 기사 1건씩, 점수 높은 순으로 3건
        cands = [(score(i, i.get("own_brand_mentions", [])) + 2, "원텍", i) for i in own[:1]]
        for g, its, _ in comp_groups:
            if g != "기타 경쟁사" and its:
                cands.append((score(its[0], groups.get(g, [])) - (2 if its[0].get("local_lang") else 0), g, its[0]))
        cands.sort(key=lambda x: x[0], reverse=True)
        issues = [{"text": f"[{g}] {i['title']}", "link": i["link"]} for _, g, i in cands[:3]]

    date_s = f"{now:%Y.%m.%d}({WEEKDAY[now.weekday()]})"
    span = "금요일 아침 이후" if hours > 30 else "최근 24시간"
    comp_cnt = sum(1 for i in items if i.get("competitor_mentions"))
    dash = cfg.get("telegram", {}).get("dashboard_url", "")

    rows = []
    # 헤더
    rows.append(f"""
<tr><td style="background:{OR};padding:22px 32px">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
  <td style="vertical-align:middle"><img src="{e(dash)}logo-wontech-wordmark.png" width="128" alt="wontech" style="display:block;border:0"></td>
  <td align="right" style="font:400 12px {FONT};color:#fff;vertical-align:middle">{date_s}<br><span style="opacity:.85">영업부문 데일리</span></td>
 </tr></table>
 <div style="font:800 26px {FONT};color:#fff;margin-top:18px;letter-spacing:-0.3px">Sales Morning Brief</div>
 <div style="font:400 12px {FONT};color:#fff;opacity:.9;margin-top:4px">미용의료기기 시장 · 경쟁사 · 해외 동향 &nbsp;|&nbsp; 영업전략팀</div>
</td></tr>""")
    rows.append(kpi_strip([
        ("수집 기사 · 최근 7일", f"{k_total:,}건", False),
        ("경쟁사 언급 · 7일", f"{k_comp:,}건", False),
        ("원텍 언급 · 7일", f"{k_own:,}건", True),
        ("해외 기사 · 7일", f"{k_ov:,}건", False),
    ]))

    # ① 이슈
    use_ai_issues = bool(ai and len(ai.get("issues") or []) >= 2)
    sec_no = iter(f"{i:02d}" for i in range(1, 20))
    rows.append(section(next(sec_no), "Today’s Top 3",
                        "AI 요약" if use_ai_issues else "회사별 대표 기사"))
    for n, it in enumerate(issues[:3], 1):
        link = f'<a href="{e(it.get("link"))}" style="font:400 11px {FONT};color:{OR};text-decoration:none;white-space:nowrap">&nbsp;원문 →</a>' if it.get("link") else ""
        rows.append(f"""
<tr><td style="padding:0 32px 8px">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid {LINE}"><tr>
  <td width="44" align="center" style="background:{INK};font:800 18px {FONT};color:#fff;padding:12px 0">{n}</td>
  <td style="padding:12px 16px;font:700 14px/1.55 {FONT};color:{INK}">{e(it.get('text'))}{link}</td>
 </tr></table>
</td></tr>""")
    if not issues:
        rows.append(empty("오늘은 특기할 이슈가 없습니다."))

    # ② 글로벌 시장 지역 비중 (대시보드 지도와 동일, 공개 자료만)
    market = load_json("market_regions.json", {})
    if market.get("regions"):
        rows.append(section(next(sec_no), "글로벌 시장 지역 비중", "EBD 시장 · 2025"))
        rows.append(region_map(market, cfg.get("telegram", {}).get("dashboard_url", "")))

    # ② 경쟁사
    rows.append(section(next(sec_no), "경쟁사 동향", f"{len(comp_groups)}개사"))
    if comp_groups:
        for name, its, total in comp_groups:
            rows.append(group_header(name, total))
            for i in its:
                rows.append(article(i, info.get(i["link"])))
    else:
        rows.append(empty("최근 경쟁사 관련 기사가 없습니다."))

    # ③ 원텍
    rows.append(section(next(sec_no), "원텍 언급 기사", f"{own_total}건 중 {len(own)}건"))
    if own:
        for i in own:
            rows.append(article(i, info.get(i["link"]), accent=True))
    else:
        rows.append(empty("최근 원텍 언급 기사가 없습니다."))

    # ④ 해외
    rows.append(section(next(sec_no), "해외 시장 소식", f"{len(seen_c)}개국"))
    if ov_pick:
        for i in ov_pick:
            rows.append(article(i, info.get(i["link"])))
    else:
        rows.append(empty("최근 해외 기사가 없습니다."))

    # ⑤ 지표
    rows.append(section(next(sec_no), "시장 지표", "공식 통계·공시 기준"))
    rows.append(f"""
<tr><td style="padding:0 26px 6px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">
 <tr>{metric_card("피부개선 의료기기 생산", "억원", metrics.get("production"))}{metric_card("수출", "백만$", metrics.get("export"))}{metric_card("수입", "백만$", metrics.get("market"))}</tr>
 <tr>{competitor_card(metrics.get("competitors"))}{metric_card("피부과·성형외과 의원", "개소", metrics.get("clinic"))}</tr>
</table></td></tr>""")

    # ⑥ 학회
    upcoming = []
    for ev in events:
        try:
            s = datetime.strptime(ev["start_date"], "%Y-%m-%d").replace(tzinfo=KST)
            t = datetime.strptime(ev["end_date"], "%Y-%m-%d").replace(tzinfo=KST)
        except (KeyError, ValueError):
            continue
        if t.date() >= now.date() and s.date() <= (now + timedelta(days=int(se.get("event_days", 21)))).date():
            upcoming.append((s, t, ev))
    upcoming.sort(key=lambda x: x[0])
    rows.append(section(next(sec_no), "다가오는 학회·전시회", f"향후 {se.get('event_days', 21)}일"))
    if upcoming:
        trs = "".join(
            f'<tr><td style="padding:10px 12px;border-bottom:1px solid {LINE};font:800 12px {FONT};color:{OR};white-space:nowrap">{s:%m.%d}{"–" + t.strftime("%m.%d") if t != s else ""}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid {LINE};font:700 13px {FONT};color:{INK}"><a href="{e(ev.get("source"))}" style="color:{INK};text-decoration:none">{e(ev.get("name"))}</a></td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid {LINE};font:400 12px {FONT};color:{SUB}">{e(ev.get("location"))}</td></tr>'
            for s, t, ev in upcoming)
        rows.append(f'<tr><td style="padding:0 32px 6px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid {LINE}">{trs}</table></td></tr>')
    else:
        rows.append(empty("표시 기간 내 등록된 행사가 없습니다."))

    # 대시보드 버튼 + 푸터
    rows.append(f"""
<tr><td align="center" style="padding:28px 32px 8px">
 <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="background:{INK};border-radius:4px">
  <a href="{e(dash)}" target="_blank" style="display:inline-block;padding:13px 28px;font:700 14px {FONT};color:#fff;text-decoration:none">대시보드에서 전체 기사 보기 →</a>
 </td></tr></table>
</td></tr>
<tr><td style="padding:18px 32px 28px;font:400 11px/1.7 {FONT};color:{MUTED}">
 영업전략팀 발행 · 평일 오전 9시 자동 발송 · 기사: {span} · 숫자 카드: 최근 7일 · 수집 범위: 네이버 뉴스, Google News 22개국판, DART, 식약처, 건강보험심사평가원<br>
 {'요약·영업 포인트는 원문 기사를 바탕으로 AI(Google Gemini)가 작성했습니다. 정확한 내용은 원문을 확인해 주세요.<br>' if ai else ''}
 문의·수신 변경: 영업전략팀
</td></tr>""")

    body = "".join(rows)
    doc = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WONTECH Sales Morning Brief</title></head>
<body style="margin:0;padding:0;background:{BG}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG}"><tr><td align="center" style="padding:24px 8px">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;background:#FAF9F7;border:1px solid {LINE}">
{body}
</table></td></tr></table></body></html>"""

    plain = [f"WONTECH Sales Morning Brief {date_s}", ""]
    plain += [f"{n}. {it.get('text')}" for n, it in enumerate(issues[:3], 1)]
    plain += ["", f"대시보드: {dash}"]
    subject = f"{se.get('subject_prefix', '[영업전략팀] WONTECH Sales Morning Brief')} · {now:%m/%d}({WEEKDAY[now.weekday()]})"
    return subject, doc, "\n".join(plain)


# ───────────────────────── 발송 ─────────────────────────
def send(subject, html_body, text_body, recipients, from_addr, app_password):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"WONTECH 영업전략팀 <{from_addr}>"
    # 받는사람(To)에 수신자를 그대로 표시. "이름 <주소>" 형식이면 이름도 함께 표시
    pairs = [(n, a) for n, a in getaddresses(recipients) if a]
    msg["To"] = ", ".join(formataddr((str(Header(n, "utf-8")), a)) if n else a for n, a in pairs)
    reply_to = os.environ.get("SALES_EMAIL_REPLY_TO")
    if reply_to:  # 답장은 회사 메일로 받기
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 587)
    rcpts = list(dict.fromkeys([a for _, a in pairs] + [from_addr]))  # 보낸 계정에도 1부
    errors = []
    # 회사 메일 서버는 오래된 보안 방식을 쓰는 경우가 있어, 안 되면 순서대로 다른 방식으로 재시도
    attempts = [(port, False)] + [(p, True) for p in (port, 465, 587, 25)]
    seen = set()
    for p, legacy in attempts:
        if (p, legacy) in seen:
            continue
        seen.add((p, legacy))
        ctx = ssl.create_default_context()
        if legacy:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            try:
                ctx.minimum_version = ssl.TLSVersion.TLSv1
            except (ValueError, AttributeError):
                pass
            if os.environ.get("SMTP_INSECURE") == "1":
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        try:
            if p == 465:
                with smtplib.SMTP_SSL(host, p, timeout=30, context=ctx) as s:
                    s.login(from_addr, app_password)
                    s.sendmail(from_addr, rcpts, msg.as_string())
            else:
                with smtplib.SMTP(host, p, timeout=30) as s:
                    s.ehlo()
                    if s.has_extn("starttls"):
                        s.starttls(context=ctx)
                        s.ehlo()
                    s.login(from_addr, app_password)
                    s.sendmail(from_addr, rcpts, msg.as_string())
            print(f"SMTP 발송 성공: {host}:{p} ({'호환 모드' if legacy else '기본'})")
            return
        except Exception as e:
            errors.append(f"{host}:{p} {'호환' if legacy else '기본'} → {type(e).__name__}: {e}")
            print("  [SMTP 시도 실패] " + errors[-1], file=sys.stderr)
    raise RuntimeError("모든 SMTP 방식 실패:\n" + "\n".join(errors))


def run():
    cfg = load_config()
    se = cfg.get("sales_email", {})
    mode = (os.environ.get("SEND_MODE") or "all").strip()
    now = datetime.now(KST)

    subject, html_body, text_body = build(cfg, now)
    out = os.path.join(BASE, "out")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "sales_brief_preview.html"), "w", encoding="utf-8") as f:
        f.write(html_body)
    print(f"미리보기 저장: out/sales_brief_preview.html / 제목: {subject}")

    if mode == "preview":
        return
    if not se.get("enabled", False) and mode != "test":
        print("sales_email.enabled: false → 발송 생략")
        return
    env_key = "SALES_EMAIL_TEST_TO" if mode == "test" else "SALES_EMAIL_TO"
    recipients = [a.strip() for a in (os.environ.get(env_key) or "").split(",") if a.strip()]
    # 회사 메일(SMTP_*) 시크릿이 있으면 우선 사용, 없으면 Gmail(GMAIL_*) 사용
    from_addr = os.environ.get("SMTP_USER") or os.environ.get("GMAIL_ADDRESS")
    pw = os.environ.get("SMTP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD")
    if not recipients or not from_addr or not pw:
        print(f"[오류] {env_key} / GMAIL_ADDRESS / GMAIL_APP_PASSWORD 시크릿을 확인하세요.", file=sys.stderr)
        sys.exit(1)
    try:
        send(subject, html_body, text_body, recipients, from_addr, pw)
    except Exception as e:
        g_addr, g_pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
        if os.environ.get("SMTP_USER") and g_addr and g_pw:
            print(f"[경고] 회사 메일 발송 실패 → Gmail로 대신 발송합니다.\n{e}", file=sys.stderr)
            os.environ["SMTP_HOST"], os.environ["SMTP_PORT"] = "smtp.gmail.com", "587"
            send(subject, html_body, text_body, recipients, g_addr, g_pw)
        else:
            raise
    print(f"발송 완료({mode}): {len(recipients)}명")


if __name__ == "__main__":
    run()
