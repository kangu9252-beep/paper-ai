"""문헌분석 보조 도구: 연구 지도(인용 관계) · 근거 저울(연구들의 답 분포) · 원문 쉽게 설명 · 연구모형 변인 정리."""
import datetime
import html
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


# ---------- 🎯 투고 학술지 추천 ----------
def journals(pid, log, stop=lambda: False):
    p = db.q("SELECT topic, title FROM projects WHERE id=?", (pid,), one=True)
    rs = matrix.rows(pid)
    counts, examples = {}, {}

    def add(venue, title, w=1):
        v = (venue or "").strip()
        if len(v) < 3:
            return
        counts[v] = counts.get(v, 0) + w
        examples.setdefault(v, [])
        if title and len(examples[v]) < 3 and title not in examples[v]:
            examples[v].append(title)

    for r in rs:  # 표의 논문이 실린 학술지 (가중치 2)
        x = db.q("SELECT venue, title FROM papers WHERE id=?", (r["paper_id"],), one=True) or {}
        add(x.get("venue"), x.get("title"), 2)
    rep = json.loads((db.q("SELECT reports FROM projects WHERE id=?", (pid,), one=True) or {}).get("reports") or "{}")
    for w in (rep.get("map") or {}).get("later", []):
        add(w.get("venue"), w.get("title"))
    log("주제와 비슷한 최근 논문이 실린 학술지를 찾는 중…")
    q = agent._en_query(p.get("topic") or p.get("title") or "")
    year_from = datetime.date.today().year - 6
    for w in web.openalex_search(q, n=50, year_from=year_from):
        add(w.get("venue"), w.get("title"))
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:15]
    data = {"query": q, "items": [{"venue": v, "score": c, "examples": examples.get(v, [])} for v, c in top], "ran_at": now()}
    matrix._save_report(pid, "journals", data)
    log(f"학술지 후보 {len(top)}개")


# ---------- 📖 내 자료에 묻기 ----------
def ask(pid, question, scope, log, stop=lambda: False):
    ids = [r["paper_id"] for r in matrix.rows(pid)] if scope == "matrix" else None
    if scope == "matrix" and not ids:
        raise RuntimeError("문헌분석표가 비어 있어요. '내 자료 전체'로 물어보세요.")
    if llm.embed(["확인"]) is None:
        raise RuntimeError("근거를 찾는 로컬 검색 모델(Ollama)이 꺼져 있어요. 컴퓨터에서 Ollama를 켠 뒤 다시 눌러주세요.")
    log("내 자료에서 관련 부분을 찾는 중…")
    srcs = pdfindex.search(question, k=10, paper_ids=ids, per_paper=2)
    if not srcs:
        raise RuntimeError("관련된 내용을 내 자료에서 찾지 못했어요.")
    block = "\n\n".join(f"[{i + 1}] {c['cite']}\n{c['text'][:700]}" for i, c in enumerate(srcs))
    log("답을 쓰는 중…")
    text = llm.chat(
        f"[질문] {question}\n\n[내 자료에서 찾은 원문]\n{block}\n\n"
        "위 원문만 근거로 질문에 한국어로 답하세요. 3~8문장, 한 줄에 한 문장. 원문에서 가져온 내용을 쓴 문장 끝에는 자료 번호를 붙이세요. 예) ~로 나타났다 [2].\n"
        "원문에 답이 없으면 없다고 말하고, 여러 연구의 결과가 다르면 다르다고 쓰세요. 저자 이름이나 (저자, 연도)는 쓰지 말고 번호만.",
        system=agent.SYS,
        num_ctx=10240,
        num_predict=1200,
        temperature=0.2,
    )
    claims = agent.parse_claims(text)
    pend = []
    for sent, idx in claims:
        if not (0 <= idx < len(srcs)):
            pend.append((sent, None, []))
            continue
        c = srcs[idx]
        opts = agent.split_sents(c["text"]) or [c["text"][:300]]
        v = llm.embed([sent] + opts)
        ranked = sorted(range(len(opts)), key=lambda i: -float(v[i + 1] @ v[0])) if v is not None else range(len(opts))
        cand = []
        for i in list(ranked)[:3]:
            loc = pdfindex.locate(c["text"], opts[i])
            a, b = (loc[0], loc[1]) if loc else (0, len(c["text"]))
            cand.append((opts[i], c["text"][max(0, a - 350): b + 350]))
        pend.append((sent, c, cand))
    chosen = [None] * len(pend)
    for t in range(3):  # 문맥으로 검증, 실패하면 다음 구절
        todo = [i for i, (s, c, cand) in enumerate(pend) if c and chosen[i] is None and len(cand) > t]
        if not todo:
            break
        for i, ok in zip(todo, agent.verify_batch([(pend[i][0], pend[i][2][t][1]) for i in todo])):
            if ok:
                chosen[i] = pend[i][2][t][0]
    parts, n_ok, n_bad = [], 0, 0
    for (sent, c, _), q in zip(pend, chosen):
        end = "" if sent.endswith(("?", "!")) else "."
        if c is None:
            parts.append(html.escape(sent) + end)
        elif q:
            parts.append(f"{html.escape(sent)} {agent.cite_span(c, q)}{end}")
            n_ok += 1
        else:
            parts.append(f"{html.escape(sent)} {agent.need_span(c['cite'])}{end}")
            n_bad += 1
    item = {"question": question, "scope": scope, "html": "<p>" + " ".join(parts) + "</p>", "ok": n_ok, "need": n_bad, "ran_at": now(), "engine": llm.engine()}
    hist = json.loads((db.q("SELECT reports FROM projects WHERE id=?", (pid,), one=True) or {}).get("reports") or "{}").get("ask", [])
    hist = [h for h in hist if h.get("question") != question][:9]
    matrix._save_report(pid, "ask", [item] + hist)
    log(f"답 완성 — 근거 확인 {n_ok}문장, 확인 필요 {n_bad}문장")


# ---------- 📤 참고문헌 내보내기 (Zotero·EndNote·Mendeley용 RIS) ----------
def ris(pid):
    rs = matrix.rows(pid)
    ids = [r["paper_id"] for r in rs] + [c["id"] for c in matrix.candidates(pid) if c["suggested"]]
    seen, out = set(), []
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        p = db.q("SELECT title, authors, year, doi, venue FROM papers WHERE id=?", (i,), one=True)
        if not p:
            continue
        out.append("TY  - JOUR")
        for a in json.loads(p["authors"] or "[]"):
            out.append(f"AU  - {a}")
        out += [f"TI  - {p['title'] or ''}", f"PY  - {p['year'] or ''}"]
        if p.get("venue"):
            out.append(f"JO  - {p['venue']}")
        if p.get("doi"):
            out.append(f"DO  - {p['doi']}")
        out.append("ER  - ")
        out.append("")
    return "\n".join(out)


# ---------- 💬 도우미 채팅 (앱 사용법 + 논문 진행 길잡이) ----------
ANCHORS = {
    "plan": ("plan", "연구계획 화면"),
    "#scanBtn": ("collect", "내 컴퓨터 PDF 찾아 읽기"),
    "#upBtn": ("collect", "PDF 파일 추가"),
    "#moreBtn": ("collect", "인터넷에서 더 수집"),
    "#mxPick": ("matrix", "문헌분석표 만들기"),
    "#mxGap": ("matrix", "연구 공백 찾기"),
    "#mxIntro": ("matrix", "서론 초안 쓰기"),
    "#akQ": ("matrix", "내 자료에 묻기"),
    "#mtQ": ("matrix", "근거 저울"),
    "#mapGo": ("matrix", "연구 지도"),
    "#jnGo": ("matrix", "투고 학술지 추천"),
    "#mdSvg": ("matrix", "연구모형 그림"),
    "#aiWrite": ("write", "AI로 집필"),
    "#autoAll": ("write", "출처 필요(?) 모두 자동 찾기"),
    "#refsBtn": ("write", "참고문헌 만들기"),
    "#impFile": ("review", "원고 불러오기"),
    "#tool-panel": ("review", "심사위원 3인 모의 심사"),
    "#tool-structure": ("review", "예비심사 리뷰(일목요연함)"),
    "#tool-cites": ("review", "인용 전체 점검"),
    "#tool-uncited": ("review", "근거 없는 문장 찾기"),
    "#tool-discuss": ("review", "논의 보강"),
    "#tool-recent": ("review", "최신 연구 찾기"),
    "#tool-refs": ("review", "참고문헌 대조"),
    "#tool-style": ("review", "문장 다듬기"),
    "#tool-aitone": ("review", "AI 문체 점검"),
    "#aiUse": ("review", "AI 사용 기록·공개 문구"),
    "settings": ("settings", "설정(AI 엔진·Claude 키·논문 폴더·도서관 로그인)"),
}
APP_MAP = """[논문 AI 앱의 기능과 위치]
① 연구계획(plan): 주제를 적으면 선행연구를 조사하고 연구 경로 3개를 추천. 내가 생각한 경로를 제안할 수도 있음.
② 자료수집(collect): 내 컴퓨터·외장하드 PDF 찾아 읽기(#scanBtn), PDF 추가(#upBtn), 승인한 경로로 인터넷에서 논문 내려받기(#moreBtn). 스캔본 PDF도 글자 인식으로 읽음.
③ 문헌분석(matrix): 문헌분석표(#mxPick, 칸마다 원문 근거, 엑셀·Zotero 내보내기), 연구 공백·이론적 가정 찾기(#mxGap), 서론 초안(#mxIntro), 내 자료에 묻기(#akQ), 근거 저울(#mtQ, 연구들이 예/일부/아니오로 어떻게 답하는지), 연구 지도(#mapGo, 인용 관계로 핵심·최신 논문), 투고 학술지 추천(#jnGo), 연구모형 그림(#mdSvg, PNG·SVG).
④ 집필(write): AI 장별 집필(#aiWrite, 문장마다 근거), 출처 필요(?) 자동 찾기(#autoAll), 참고문헌 만들기(#refsBtn). 편집기에서 (저자, 연도)를 쓰면 자동 인식하고, 누르면 오른쪽에 원문 위치가 열림. AI 초안은 보라색으로 표시되고 고쳐 쓰면 사라짐.
⑤ 원고 점검(review): 원고 불러오기(#impFile, hwpx·docx·txt), 심사위원 3인 모의 심사와 답변서 재심사(#tool-panel), 예비심사 리뷰(#tool-structure), 인용 전체 점검(#tool-cites), 근거 없는 문장(#tool-uncited), 논의 보강(#tool-discuss), 최신 연구(#tool-recent), 참고문헌 대조(#tool-refs), 문장 다듬기(#tool-style), AI 문체 점검(#tool-aitone), AI 사용 기록과 투고용 공개 문구(#aiUse). 상담 사례처럼 민감한 원고는 '로컬 AI만 사용'을 켤 것.
설정(settings): AI 엔진(내 컴퓨터 로컬 AI / Claude), Claude 키 연결 도우미, 논문 폴더, 대학 도서관 로그인, 새벽 자동 실행.
근거 문장 맞추기에는 컴퓨터의 Ollama가 켜져 있어야 함."""
ASSIST_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}, "anchor": {"type": "string", "enum": list(ANCHORS)}}, "required": ["label", "anchor"]}},
    },
    "required": ["answer", "actions"],
}
PERSONA = {"start": "처음 시작(주제만 있음)", "draft": "초안이 있음(쓰는 중)", "revise": "심사·수정을 앞둠(거의 완성)"}


def _state(pid):
    if not pid:
        return "아직 논문 프로젝트를 열지 않음."
    p = db.q("SELECT * FROM projects WHERE id=?", (pid,), one=True)
    if not p:
        return "프로젝트 없음."
    rep = json.loads(p.get("reports") or "{}")
    text = BeautifulSoup(p.get("content") or "", "html.parser")
    cites = text.select("span.cite")
    m = json.loads(p.get("matrix") or "{}")
    from . import users

    papers = db.q("SELECT COUNT(*) n FROM papers WHERE status='ok' AND COALESCE(user_id,0)=?", (users.uid(),), one=True)["n"]
    lines = [
        f"논문 제목: {p.get('title')}", f"주제: {p.get('topic')}",
        f"길잡이 유형: {PERSONA.get((rep.get('guide') or {}).get('persona'), '아직 안 고름')}",
        f"연구계획: {'있음' if p.get('plan') else '없음'}", f"내 자료 PDF: {papers}편", f"문헌분석표: {len(m)}편",
        f"원고: {len(text.get_text())}자, 인용 {len(cites)}개(근거 연결 {sum(1 for c in cites if c.get('data-chunk'))}개)",
        f"AI 엔진: {llm.engine_for(pid)}", f"로컬 AI만 사용: {'예' if p.get('local_only') else '아니오'}",
    ]
    for k, label in (("structure", "예비심사 리뷰"), ("panel", "3인 모의 심사"), ("cites", "인용 점검"), ("refs", "참고문헌 대조"), ("style", "문장 다듬기"), ("aitone", "AI 문체 점검")):
        if rep.get(k):
            lines.append(f"{label} 결과: {rep[k].get('summary', '')[:160]}")
    if rep.get("panel", {}).get("data", {}).get("required"):
        req = rep["panel"]["data"]["required"]
        lines.append("모의 심사 수정 요구: " + " / ".join(f"{it['id']}. {it['issue'][:60]}({it.get('status') or '미답변'})" for it in req[:10]))
    if rep.get("gaps"):
        lines.append("연구 공백: " + " / ".join(g["gap"][:60] for g in rep["gaps"].get("gaps", [])[:4]))
    return "\n".join(lines)


def assist(pid, messages, log):
    convo = "\n".join(f"{'연구자' if m.get('role') == 'user' else '도우미'}: {m.get('content', '')[:1500]}" for m in messages[-12:])
    r = llm.chat(
        f"{APP_MAP}\n\n[지금 연구자의 진행 상황]\n{_state(pid)}\n\n[대화]\n{convo}\n\n"
        "마지막 연구자 말에 답하세요. answer는 쉬운 한국어로, 짧은 문단이나 번호 목록으로(길어도 10줄). 무엇을, 왜, 어떤 순서로 하면 되는지 구체적으로. "
        "앱 기능이 도움이 되면 actions에 바로가기 버튼을 1~3개 넣으세요(label은 버튼 글자, anchor는 위 기능 위치). "
        "논문 방법론 질문에는 일반적인 원칙으로 답하되, 학교·학과 규정과 지도교수 판단을 확인하라고 덧붙이세요. 모르는 것은 모른다고 하세요.",
        system="당신은 '논문 AI' 앱 안의 도우미입니다. 상담심리 등 사회과학 대학원생이 학위논문을 쓰도록 돕는 친절한 선배 연구자이자 앱 안내자입니다. "
        "연구자는 개발자가 아니므로 전문용어는 풀어서 설명합니다.",
        schema=ASSIST_SCHEMA,
        num_ctx=8192,
        num_predict=900,
        temperature=0.3,
    )
    acts = [{"label": a["label"], "anchor": a["anchor"], "tab": ANCHORS[a["anchor"]][0]} for a in r.get("actions", []) if a.get("anchor") in ANCHORS][:3]
    return {"answer": r.get("answer", ""), "actions": acts}
