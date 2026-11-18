"""문헌분석 보조 도구: 연구 지도(인용 관계) · 근거 저울(연구들의 답 분포) · 원문 쉽게 설명 · 연구모형 변인 정리."""
import datetime
import difflib
import json
import re

import httpx
from bs4 import BeautifulSoup

from . import agent, db, llm, matrix, pdfindex, web


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _oa(path, params=None):
    try:
        r = httpx.get(web.OA + path, params=params or {}, headers=web.UA, timeout=30)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _same_title(a, b):
    n = lambda s: re.sub(r"\W", "", (s or "").lower())
    return difflib.SequenceMatcher(None, n(a), n(b)).ratio()


def _find_work(row):
    """문헌분석표의 논문 → OpenAlex 원본 (DOI가 있으면 DOI로, 없으면 제목으로)."""
    p = db.q("SELECT title, year, doi FROM papers WHERE id=?", (row["paper_id"],), one=True) or {}
    if p.get("doi"):
        w = _oa(f"/works/https://doi.org/{p['doi']}")
        if w:
            return w
    title = p.get("title") or row.get("title") or ""
    if len(title) < 8:
        return None
    res = (_oa("/works", {"search": title[:200], "per_page": 5}) or {}).get("results", [])
    for w in res:
        if _same_title(w.get("display_name"), title) >= 0.85 and (not p.get("year") or str(w.get("publication_year")) == str(p["year"])[:4]):
            return w
    return None


def _item(w, count=0):
    m = web._work(w)
    m["count"] = count
    m["have"] = bool(web.already_have(m))
    m.pop("abstract", None)
    return m


# ---------- 🕸 연구 지도 ----------
def research_map(pid, log, stop=lambda: False):
    rs = matrix.rows(pid)
    if len(rs) < 2:
        raise RuntimeError("문헌분석표에 논문이 2편 이상 있어야 연구 지도를 그릴 수 있어요.")
    seeds, refs = [], {}
    for r in rs:
        if stop():
            return
        w = _find_work(r)
        if not w:
            log(f"  {r['cite']}: 인용 정보 없음 (국내 학술지는 빠진 경우가 많아요)")
            continue
        seeds.append(w["id"])
        for x in w.get("referenced_works", []):
            refs[x] = refs.get(x, 0) + 1
        log(f"  {r['cite']}: 참고문헌 {len(w.get('referenced_works', []))}개")
    if len(seeds) < 2:
        raise RuntimeError("인용 정보를 찾은 논문이 2편 미만이에요. 영어 논문이나 DOI가 있는 논문을 표에 더 넣어 주세요.")
    seed_set = set(seeds)
    # 1) 내 논문들이 공통으로 인용한 고전·핵심 문헌
    top = [k for k, c in sorted(refs.items(), key=lambda kv: -kv[1]) if c >= 2 and k not in seed_set][:30]
    prior = []
    for i in range(0, len(top), 25):
        ids = "|".join(t.rsplit("/", 1)[-1] for t in top[i : i + 25])
        for w in (_oa("/works", {"filter": f"openalex_id:{ids}", "per_page": 25}) or {}).get("results", []):
            prior.append(_item(w, refs.get(w["id"], 0)))
    prior.sort(key=lambda x: (-x["count"], -(x["cited"] or 0)))
    log(f"공통으로 인용된 문헌 {len(prior)}편")
    # 2) 내 논문들을 인용한 후속 연구 (여러 편을 인용할수록 내 주제와 가까움)
    ids = "|".join(s.rsplit("/", 1)[-1] for s in seeds[:50])
    year_from = datetime.date.today().year - 6
    later = []
    for w in (_oa("/works", {"filter": f"cites:{ids},from_publication_date:{year_from}-01-01", "per_page": 50, "sort": "cited_by_count:desc"}) or {}).get("results", []):
        c = len(seed_set & set(w.get("referenced_works", [])))
        if w["id"] not in seed_set:
            later.append(_item(w, c))
    later.sort(key=lambda x: (-x["count"], -int(x["year"] or 0), -(x["cited"] or 0)))
    later = later[:25]
    log(f"내 논문들을 인용한 최근 연구 {len(later)}편")
    data = {"seeds": len(seeds), "total": len(rs), "prior": prior[:20], "later": later, "ran_at": now(), "engine": llm.engine()}
    matrix._save_report(pid, "map", data)


# ---------- ⚖ 근거 저울 ----------
METER_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["예", "일부", "아니오", "관련없음"]},
                    "reason": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["n", "verdict", "reason", "quote"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["results", "summary"],
}


def meter(pid, question, log, stop=lambda: False):
    rs = matrix.rows(pid)
    if not rs:
        raise RuntimeError("먼저 문헌분석표를 만들어 주세요.")
    blocks, pools = [], []
    for i, r in enumerate(rs):
        f = r["fields"].get("findings", {})
        pool = matrix._row_chunks(r) + pdfindex.search(question, k=2, paper_ids=[r["paper_id"]], per_paper=2)
        pool = list({c["id"]: c for c in pool}.values())[:4]
        pools.append(pool)
        snip = "\n".join(f"   · {c['text'][:450]}" for c in pool)
        blocks.append(f"[{i + 1}] {r['cite']} — 대상: {r['fields'].get('sample', {}).get('v', '')[:120]}\n   결과 요약: {f.get('v', '')[:300]}\n{snip}")
    log(f"문헌 {len(rs)}편이 이 질문에 어떻게 답하는지 보는 중…")
    r = llm.chat(
        f"[질문] {question}\n\n[문헌]\n" + "\n\n".join(blocks) + "\n\n"
        "각 문헌의 결과가 질문에 어떻게 답하는지 판정하세요. verdict: 예(지지) / 일부(조건부·부분적·혼재) / 아니오(반대 또는 유의하지 않음) / 관련없음(이 질문을 다루지 않음). "
        "reason은 한 문장(대상·조건 포함), quote는 판정 근거를 위 원문에서 글자 그대로 10~40단어 복사(관련없음이면 빈칸). "
        "summary에는 전체 경향을 2문장으로, 표본·설계의 한계도 함께. 결과를 과장하지 마세요.",
        system=agent.SYS,
        schema=METER_SCHEMA,
        num_ctx=12288,
        num_predict=2500,
    )
    items = []
    for x in r.get("results", []):
        i = int(x.get("n") or 0) - 1
        if not (0 <= i < len(rs)):
            continue
        q = (x.get("quote") or "").strip()
        hit = next((c for c in pools[i] if q and pdfindex.locate(c["text"], q)), None)
        items.append({"paper_id": rs[i]["paper_id"], "cite": rs[i]["cite"], "verdict": x.get("verdict", "관련없음"),
                      "reason": x.get("reason", ""), "chunk": hit["id"] if hit else None, "quote": q if hit else ""})
    counts = {k: sum(1 for it in items if it["verdict"] == k) for k in ("예", "일부", "아니오", "관련없음")}
    p = db.q("SELECT reports FROM projects WHERE id=?", (pid,), one=True)
    hist = json.loads(p["reports"] or "{}").get("meter", [])
    hist = [h for h in hist if h.get("question") != question][:4]
    hist.insert(0, {"question": question, "items": items, "counts": counts, "summary": r.get("summary", ""), "ran_at": now(), "engine": llm.engine()})
    matrix._save_report(pid, "meter", hist)
    log(f"예 {counts['예']} · 일부 {counts['일부']} · 아니오 {counts['아니오']} · 관련없음 {counts['관련없음']}")


# ---------- 🇰🇷 원문 쉽게 설명 ----------
def explain(chunk_id, quote, log):
    c = db.q("SELECT c.text, p.title FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id=?", (chunk_id,), one=True)
    if not c:
        raise RuntimeError("원문을 찾지 못했어요.")
    log("원문을 쉽게 풀어 쓰는 중…")
    r = llm.chat(
        f"논문: {c['title']}\n[원문]\n{c['text'][:2500]}\n\n[특히 볼 부분]\n{quote or '(전체)'}\n\n"
        "상담심리 대학원생이 읽기 쉽게 한국어로 답하세요.\n"
        "- translation: '특히 볼 부분'의 정확한 한국어 번역(원문이 한국어면 빈칸)\n"
        "- plain: 이 부분이 무슨 말인지 쉬운 말로 2~4문장 (통계 수치가 있으면 무슨 뜻인지)\n"
        "- terms: 어려운 용어 1~4개와 짧은 뜻\n원문에 없는 내용을 덧붙이지 마세요.",
        system=agent.SYS,
        schema={
            "type": "object",
            "properties": {
                "translation": {"type": "string"},
                "plain": {"type": "string"},
                "terms": {"type": "array", "items": {"type": "object", "properties": {"term": {"type": "string"}, "meaning": {"type": "string"}}, "required": ["term", "meaning"]}},
            },
            "required": ["translation", "plain", "terms"],
        },
        num_ctx=6144,
        num_predict=900,
    )
    return r


# ---------- 🗺 연구모형 변인 정리 ----------
MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "iv": {"type": "array", "items": {"type": "string"}},
        "mediators": {"type": "array", "items": {"type": "string"}},
        "dvs": {"type": "array", "items": {"type": "string"}},
        "moderators": {"type": "array", "items": {"type": "string"}},
        "covariates": {"type": "array", "items": {"type": "string"}},
        "mediation": {"type": "string", "enum": ["병렬", "직렬", "단순", "없음"]},
        "title": {"type": "string"},
    },
    "required": ["iv", "mediators", "dvs", "moderators", "covariates", "mediation", "title"],
}


def model_suggest(pid, log):
    p = db.q("SELECT topic, title, content, plan, route FROM projects WHERE id=?", (pid,), one=True)
    text = BeautifulSoup(p["content"] or "", "html.parser").get_text(" ", strip=True)
    rq = " ".join(re.findall(r"[^.]*(?:연구\s*문제|가설|연구\s*모형|매개|조절)[^.]*\.", text))[:3000]
    plan = ""
    if p.get("plan"):
        try:
            rt = json.loads(p["plan"])["routes"][p.get("route") or 0]
            plan = f"연구문제: {' / '.join(rt.get('questions', []))}\n방법: {rt.get('method', '')}"
        except Exception:
            pass
    log("연구 주제와 원고에서 변인을 찾는 중…")
    r = llm.chat(
        f"논문 주제: {p.get('topic') or p.get('title')}\n{plan}\n[원고에서 연구문제·가설·모형 관련 문장]\n{rq or '(없음)'}\n\n"
        "이 연구의 연구모형 변인을 역할별로 정리하세요. iv(독립변인), mediators(매개변인, 순서대로), dvs(종속변인), moderators(조절변인), "
        "covariates(통제변인), mediation(매개 방식: 병렬/직렬/단순/없음), title(모형 이름, 예: '병렬매개모형'). 변인 이름은 원고에 쓴 그대로 짧게.",
        system=agent.SYS,
        schema=MODEL_SCHEMA,
        num_ctx=6144,
        num_predict=500,
    )
    matrix._save_report(pid, "model", {k: r.get(k) for k in MODEL_SCHEMA["properties"]} | {"ran_at": now()})
    log("변인 정리 완료 — 그림에서 이름을 고칠 수 있어요")
