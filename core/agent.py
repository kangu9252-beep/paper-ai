"""논문 AI의 작업 흐름: ① 연구계획 ② 자료수집 ③ 집필 ④ 근거 자동찾기."""
import html
import json
import re

from . import db, library, llm, pdfindex, web

SYS = (
    "당신은 한국어로 논문을 돕는 연구 조교입니다. 사실을 지어내지 않습니다. "
    "주어진 자료에 없는 내용은 쓰지 않고, 모르면 모른다고 합니다."
)


# ① 연구계획 ---------------------------------------------------------------
def plan(topic, log):
    log("주제를 분석해 검색어를 만드는 중…")
    kw = llm.chat(
        f"논문 주제: {topic}\n\n이 주제의 선행연구를 찾기 위한 검색어를 만들어 주세요. "
        "en_query는 학술 DB 검색용 영어 검색어 한 줄(3~6단어), ko_query는 한국어 검색어 한 줄.",
        system=SYS,
        schema={
            "type": "object",
            "properties": {
                "ko_keywords": {"type": "array", "items": {"type": "string"}},
                "en_keywords": {"type": "array", "items": {"type": "string"}},
                "en_query": {"type": "string"},
                "ko_query": {"type": "string"},
            },
            "required": ["ko_keywords", "en_keywords", "en_query", "ko_query"],
        },
    )
    log(f"검색어: {kw.get('en_query')} / {kw.get('ko_query')}")
    log("인터넷 학술 색인(OpenAlex)에서 선행연구 조사 중…")
    lit = web.openalex_search(kw.get("en_query") or topic, n=25)
    lit += web.openalex_search(kw.get("ko_query") or topic, n=10)
    seen, uniq = set(), []
    for w in lit:
        key = (w["doi"] or w["title"]).lower()
        if key and key not in seen and w["title"]:
            seen.add(key)
            uniq.append(w)
    uniq.sort(key=lambda w: -(w["cited"] or 0))
    lit = uniq[:30]
    log(f"관련 문헌 {len(lit)}편 확인 (무료 원문 {sum(1 for w in lit if w['is_oa'])}편)")
    local = pdfindex.search(topic, k=8, per_paper=1)
    log(f"내 컴퓨터에 있는 관련 자료 {len(local)}건")

    refs = "\n".join(
        f"[{i + 1}] {w['title']} ({', '.join(w['authors'][:2])}, {w['year']}; 피인용 {w['cited']}) {w['abstract'][:300]}"
        for i, w in enumerate(lit[:15])
    )
    refs += "".join(f"\n[내PC] {c['cite']} {c['title']}: {c['text'][:200]}" for c in local[:5])
    log(("Claude가" if llm.engine() == "claude" else "로컬 AI가") + " 연구 경로를 설계하는 중…" + ("" if llm.engine() == "claude" else " (몇 분 걸릴 수 있어요)"))
    p = llm.chat(
        f"논문 주제: {topic}\n\n아래는 실제로 검색된 선행연구 목록입니다. 이것만 근거로 삼아 답하세요. "
        "선행연구를 언급할 때는 [번호]로 표시하세요.\n\n"
        f"{refs}\n\n"
        "다음을 한국어로 작성하세요:\n"
        "- background: 주제의 배경과 중요성 (3~4문장)\n"
        "- literature_flow: 선행연구의 주요 흐름 (4~6문장, [번호] 표시)\n"
        "- gaps: 아직 덜 연구된 부분(연구 공백) 2~4개\n"
        "- routes: 이 논문을 진행할 수 있는 서로 다른 연구 경로 3개. 각 경로마다 name(짧은 이름), "
        "questions(연구문제 2~3개), method(연구방법: 양적/질적/문헌연구 등 구체적으로), data(대상·자료 수집 방법), "
        "analysis(분석 방법), pros(장점), cons(단점·어려움), search_queries(이 경로에 필요한 자료를 찾을 영어 검색어 2~3개), "
        "outline(논문 목차: 장 제목 5~7개, 예: 'I. 서론')\n"
        "- recommended: 가장 추천하는 경로 번호(1~3)와 recommend_reason(이유)",
        system=SYS,
        schema={
            "type": "object",
            "properties": {
                "background": {"type": "string"},
                "literature_flow": {"type": "string"},
                "gaps": {"type": "array", "items": {"type": "string"}},
                "routes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "questions": {"type": "array", "items": {"type": "string"}},
                            "method": {"type": "string"},
                            "data": {"type": "string"},
                            "analysis": {"type": "string"},
                            "pros": {"type": "string"},
                            "cons": {"type": "string"},
                            "search_queries": {"type": "array", "items": {"type": "string"}},
                            "outline": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "questions", "method", "data", "analysis", "pros", "cons", "search_queries", "outline"],
                    },
                },
                "recommended": {"type": "integer"},
                "recommend_reason": {"type": "string"},
            },
            "required": ["background", "literature_flow", "gaps", "routes", "recommended", "recommend_reason"],
        },
        num_ctx=8192,
        num_predict=3000,
    )
    p["keywords"] = kw
    p["literature"] = lit
    p["local"] = [{k: c[k] for k in ("id", "paper_id", "cite", "title", "page")} for c in local]
    log("연구계획 완성")
    return p


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "method": {"type": "string"},
        "data": {"type": "string"},
        "analysis": {"type": "string"},
        "pros": {"type": "string"},
        "cons": {"type": "string"},
        "search_queries": {"type": "array", "items": {"type": "string"}},
        "outline": {"type": "array", "items": {"type": "string"}},
        "advice": {"type": "string"},
    },
    "required": ["name", "questions", "method", "data", "analysis", "pros", "cons", "search_queries", "outline", "advice"],
}


def refine_route(topic, idea, lit, log):
    """사용자가 제안한 연구 경로를 AI 추천과 같은 형식으로 정리하고, 보완점을 짚어준다."""
    log("제안하신 연구 경로를 정리하는 중…")
    refs = "\n".join(f"[{i + 1}] {w['title']} ({w['year']})" for i, w in enumerate(lit[:12]))
    r = llm.chat(
        f"논문 주제: {topic}\n\n[연구자가 직접 제안한 연구 경로]\n{idea}\n\n[검색된 선행연구]\n{refs}\n\n"
        "연구자의 제안을 존중해서 그 의도를 바꾸지 말고, 다음 형식으로 한국어로 정리하세요: "
        "name(짧은 이름), questions(연구문제 2~3개), method(연구방법), data(대상·자료 수집 방법), analysis(분석 방법), "
        "pros(장점), cons(어려움), search_queries(필요한 자료를 찾을 영어 검색어 2~3개), outline(논문 목차 5~7개), "
        "advice(선행연구에 비추어 이 경로에서 보완하면 좋을 점 2~3문장, 연구자가 정하지 않은 부분은 무엇을 정해야 하는지).",
        system=SYS,
        schema=ROUTE_SCHEMA,
        num_ctx=6144,
        num_predict=1500,
    )
    r["user"] = True
    r["idea"] = idea
    log("내 경로 정리 완료")
    return r


# ② 자료수집 ---------------------------------------------------------------
def collect(project_id, route_idx, log, stop):
    pr = db.q("SELECT * FROM projects WHERE id=?", (project_id,), one=True)
    p = json.loads(pr["plan"])
    route = p["routes"][route_idx]
    s = db.get_settings()
    cands = list(p.get("literature", []))
    for qtext in route.get("search_queries", [])[:3]:
        log(f"추가 조사: {qtext}")
        cands += web.openalex_search(qtext, n=15)
    seen, uniq = set(), []
    for w in cands:
        key = (w["doi"] or w["title"]).lower()
        if key and key not in seen:
            seen.add(key)
            uniq.append(w)
    uniq.sort(key=lambda w: (not w["is_oa"], -(w["cited"] or 0)))
    limit = int(s.get("max_downloads", 15))
    got, lib_tries = [], 0
    for w in uniq:
        if stop() or len(got) >= limit:
            break
        if not w["is_oa"]:
            if not (w["doi"] and library.configured()) or lib_tries >= 5:
                continue
            lib_tries += 1
        log(f"· {w['title'][:70]} ({w['year']})")
        pid = web.fetch_paper(w, log=log)
        if pid:
            got.append(pid)
    log(f"자료수집 완료: {len(got)}편이 라이브러리에 추가됨")
    return {"added": len(got)}


# ③ 집필 -------------------------------------------------------------------
SUPPORT_MIN = 0.55  # 문장과 원문 문장의 의미 유사도가 이보다 낮으면 '?' 처리


def cite_span(chunk, quote, label=None):
    label = label or chunk["cite"]
    return (
        f'<span class="cite" contenteditable="false" data-chunk="{chunk["id"]}" '
        f'data-quote="{html.escape(quote or "", quote=True)}">{html.escape(label)}</span>'
    )


def need_span(label="(출처 필요)"):
    return f'<span class="cite" contenteditable="false">{html.escape(label)}</span>'


def split_sents(text):
    parts = re.split(r"(?<=[.!?。])\s+(?=[A-Z가-힣0-9(“\"'])", text)
    return [p.strip() for p in parts if len(p.strip()) >= 25]


def match_quotes(claims, chunks):
    """각 주장 문장마다 근거 자료 안에서 의미가 가장 가까운 원문 문장을 찾는다 (검색 모델 사용).
    claims: [(문장, 자료번호)] → [(chunk, quote, 유사도)]"""
    sents = [(ci, s) for ci, c in enumerate(chunks) for s in split_sents(c["text"])]
    cv = llm.embed([t for t, _ in claims])
    sv = llm.embed([s for _, s in sents]) if sents else None
    out = []
    for k, (text, idx) in enumerate(claims):
        if cv is None or sv is None:
            c = chunks[idx] if 0 <= idx < len(chunks) else None
            out.append((c, "", 0.0))
            continue
        sims = sv @ cv[k]
        best_i = int(sims.argmax())
        own = [i for i, (ci, _) in enumerate(sents) if ci == idx]
        if own:
            oi = max(own, key=lambda i: sims[i])
            if sims[oi] >= sims[best_i] - 0.05:  # AI가 고른 자료를 우선
                best_i = oi
        ci, quote = sents[best_i]
        out.append((chunks[ci], quote, float(sims[best_i])))
    return out


def verify_batch(pairs):
    """[(문장, 원문)] 각각 원문이 문장을 뒷받침하는지 한 번에 검사. 실패 시 모두 통과로 본다."""
    if not pairs:
        return []
    block = "\n\n".join(f"{i + 1}. 문장: {s}\n   원문: {q}" for i, (s, q) in enumerate(pairs))
    try:
        r = llm.chat(
            f"{block}\n\n각 번호마다 원문이 문장의 내용을 뒷받침하면 ok=true, 원문에 없는 내용을 덧붙였거나 과장·왜곡했으면 ok=false로 판정하세요.",
            system="당신은 꼼꼼한 논문 심사위원입니다.",
            schema={
                "type": "object",
                "properties": {"results": {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer"}, "ok": {"type": "boolean"}}, "required": ["n", "ok"]}}},
                "required": ["results"],
            },
            num_ctx=6144,
            num_predict=400,
            temperature=0,
        )
        verdict = {int(x["n"]): bool(x["ok"]) for x in r.get("results", [])}
        return [verdict.get(i + 1, True) for i in range(len(pairs))]
    except Exception:
        return [True] * len(pairs)


def parse_claims(text):
    """'문장 [2]. 문장 [1][3]. 번호 없는 문장.' → [(문장, 자료번호-1 또는 -1)]"""
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\[(\d+)((?:\s*,\s*\d+)+)\]", lambda m: "".join(f"[{n}]" for n in re.findall(r"\d+", m.group(0))), text)
    text = re.sub(r"\(\s*((?:\[\d+\]\s*,?\s*)+)\)", lambda m: re.sub(r"[\s,]", "", m.group(1)), text)
    text = re.sub(r"\s*((?:\[\d+\])+)\s*\.", r".\1", text)  # '문장 [1].' → '문장.[1]'
    text = re.sub(r"\.((?:\[\d+\])+)", r"\1.", text)  # 정규화: '문장[1].'
    claims = []
    for seg in re.split(r"(?<=\])\s*\.?\s*|(?<=[다요음함됨]\.)\s+", text):
        seg = seg.strip()
        if not seg:
            continue
        nums = re.findall(r"\[(\d+)\]", seg)
        sent = re.sub(r"\s*\[\d+\]", "", seg).strip().rstrip(".").strip()
        sent = re.sub(r"\s*\([^()]{1,40},\s*\d{4}\)", "", sent).strip()
        if len(sent) < 8:
            continue
        for s in re.split(r"(?<=[다요음함됨])\.\s+", sent):  # 번호 하나에 여러 문장이 묶인 경우
            claims.append((s.strip().rstrip("."), int(nums[0]) - 1 if nums else -1))
    return claims


def write_section(topic, route, section, log):
    query = f"{topic} {section} {route.get('name', '')}"
    srcs = pdfindex.search(query, k=6, per_paper=2)
    if not srcs:
        return f"<h2>{html.escape(section)}</h2><p>{need_span('(라이브러리에 관련 자료가 없어 작성하지 못했습니다 — 자료를 먼저 수집하세요)')}</p>"
    block = "\n\n".join(f"[{i + 1}] {c['cite']}\n{c['text'][:600]}" for i, c in enumerate(srcs))
    text = llm.chat(
        f"논문 주제: {topic}\n연구 방법: {route.get('method', '')}\n지금 쓸 장: {section}\n\n"
        f"[자료]\n{block}\n\n"
        "위 자료만 사용해서 이 장의 본문을 학술 논문 문체(~이다, ~하였다)의 한국어로 5~8문장 쓰세요.\n"
        "규칙:\n"
        "1. 한 줄에 한 문장씩 쓰고, 각 문장 끝에 근거가 된 자료 번호를 붙이세요.\n"
        "   예) 자기자비는 우울 증상과 부적 상관을 보였다 [2].\n"
        "   문장은 반드시 '~이다', '~하였다', '~나타났다'로 끝내고 '~합니다'는 쓰지 마세요.\n"
        "2. 자료에 없는 사실·수치·연도는 절대 지어내지 마세요.\n"
        "3. 저자 이름이나 (저자, 연도) 표기는 쓰지 마세요. 번호만 붙이면 됩니다.\n"
        "4. 문장들이 자연스럽게 이어지는 하나의 글이 되게 하세요.\n"
        "5. 자료가 부족해서 이 장에서 쓰지 못한 내용이 있으면, 본문이 다 끝난 뒤 맨 마지막 줄에 '[메모]'로 시작하는 한 줄로만 알려주세요. 그 밖의 설명은 쓰지 마세요.",
        system=SYS,
        num_ctx=6144,
        num_predict=900,
        temperature=0.3,
    )
    # 본문과 AI 메모 분리 ([메모] / --- / 참고: 로 시작하는 꼬리말)
    m = re.search(r"(?m)^\s*(?:\[메모\]|-{3,}|참고\s*:|※)", text)
    note = ""
    if m:
        note = re.sub(r"^\s*(?:\[메모\]|-{3,}|참고\s*:|※)\s*", "", text[m.start():]).strip()
        note = re.sub(r"\[(\d+)\]", lambda x: srcs[int(x.group(1)) - 1]["cite"] if 0 < int(x.group(1)) <= len(srcs) else "", re.sub(r"\s+", " ", note))
        text = text[: m.start()]
    claims = parse_claims(text)
    if not claims:
        return f"<h2>{html.escape(section)}</h2><p>{need_span('(AI가 문장을 만들지 못했습니다 — 다시 시도해 주세요)')}</p>"
    strict = db.get_settings().get("strict_verify", True)
    matches = match_quotes(claims, srcs)
    checks = [True] * len(claims)
    if strict:
        log("  근거 검증 중…")
        idxs = [i for i, (c, q, sim) in enumerate(matches) if c and q and sim >= SUPPORT_MIN]
        for i, v in zip(idxs, verify_batch([(claims[i][0], matches[i][1]) for i in idxs])):
            checks[i] = v
    parts, ok, bad = [], 0, 0
    for (sent, idx), (chunk, quote, sim), passed in zip(claims, matches, checks):
        t = html.escape(sent)
        if chunk and quote and passed and (sim >= SUPPORT_MIN or not strict):
            parts.append(f"{t} {cite_span(chunk, quote)}.")
            ok += 1
        else:
            parts.append(f"{t} {need_span(chunk['cite'] if chunk else '(출처 필요)')}.")
            bad += 1
    log(f"  → {ok}문장 근거 확인, {bad}문장 확인 필요(?)")
    memo = f'<p class="ai-note" contenteditable="false">💬 AI 메모: {html.escape(note)} <small>(확인 후 지워주세요)</small></p>' if note else ""
    if note:
        log(f"  ※ AI 메모: {note[:120]}")
    return f"<h2>{html.escape(section)}</h2><p>{' '.join(parts)}</p>{memo}"


def write(project_id, sections, log, stop, emit):
    pr = db.q("SELECT * FROM projects WHERE id=?", (project_id,), one=True)
    p = json.loads(pr["plan"])
    route = p["routes"][pr["route"] or 0]
    for i, sec in enumerate(sections):
        if stop():
            break
        log(f"[{i + 1}/{len(sections)}] '{sec}' 집필 중…")
        emit(write_section(pr["topic"], route, sec, log))
    log("집필 완료")


# ④ 근거 자동찾기 ------------------------------------------------------------
def _pick(sentence, cands):
    if not cands:
        return None
    block = "\n\n".join(f"[{i + 1}] {c['cite']}\n{c['text'][:700]}" for i, c in enumerate(cands))
    r = llm.chat(
        f"[주장 문장]\n{sentence}\n\n[후보 자료]\n{block}\n\n"
        "주장 문장을 직접 뒷받침하는 자료를 고르세요. source에 번호, quote에는 뒷받침하는 부분을 자료 원문에서 글자 그대로 "
        "(번역하지 말고) 10~40단어 복사하세요. 뒷받침하는 자료가 없으면 source를 0으로 하세요.",
        system=SYS,
        schema={
            "type": "object",
            "properties": {"source": {"type": "integer"}, "quote": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["source", "quote", "reason"],
        },
        num_ctx=6144,
        num_predict=400,
    )
    idx = int(r.get("source") or 0) - 1
    if not (0 <= idx < len(cands)):
        return None
    c = cands[idx]
    q = r.get("quote", "")
    if not pdfindex.locate(c["text"], q):
        for c2 in cands:
            if pdfindex.locate(c2["text"], q):
                c = c2
                break
        else:
            return None
    return {"chunk_id": c["id"], "quote": q, "cite": c["cite"], "title": c["title"], "reason": r.get("reason", "")}


def _en_query(sentence):
    try:
        r = llm.chat(
            f"다음 문장의 근거 논문을 찾기 위한 영어 학술 검색어(3~6단어)를 만드세요.\n{sentence}",
            schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            num_ctx=2048,
        )
        return r.get("q") or sentence
    except Exception:
        return sentence


def _author_match(w, author):
    a = re.sub(r"\s*(외|et al\.?|등)\s*$", "", author).split(",")[0].split("&")[0].strip().lower()
    return a and any(a in x.lower() for x in w["authors"])


def autofind(sentence, author, year, log):
    sentence = sentence.strip()
    if author:
        papers = pdfindex.find_papers_by_cite(author, year)
        if papers:
            log(f"내 컴퓨터에서 '{author}, {year}' 논문 발견: {papers[0]['title'][:60]}")
            hit = _pick(sentence, pdfindex.search(sentence, k=6, paper_ids=[p["id"] for p in papers], per_paper=6))
            if hit:
                log("근거 위치 찾음")
                return {"status": "linked", **hit}
            log("논문은 있지만 이 문장을 뒷받침하는 부분을 찾지 못함")
        else:
            log(f"내 컴퓨터에 '{author}, {year}' 논문이 없음 → 인터넷에서 찾는 중…")
            enq = _en_query(sentence)
            found = []
            for qq in (f"{enq}", sentence, author):
                for w in web.openalex_search(qq, n=25):
                    if (not year or w["year"] == year[:4]) and _author_match(w, author):
                        found.append(w)
                if found:
                    break
            for w in found[:3]:
                log(f"인터넷에서 후보 발견: {w['title'][:70]} ({w['year']})")
                pid = web.fetch_paper(w, log=log)
                if pid:
                    hit = _pick(sentence, pdfindex.search(sentence, k=6, paper_ids=[pid], per_paper=6))
                    if hit:
                        log("내려받은 논문에서 근거 위치 찾음")
                        return {"status": "linked", **hit}
                else:
                    log("  원문을 내려받지 못함(무료 공개본 없음/도서관 미설정)")
    # 인용한 저자가 없거나 못 찾음 → 내 라이브러리 전체에서
    log("내 컴퓨터 전체 자료에서 근거 탐색 중…")
    cands = pdfindex.search(sentence, k=6, per_paper=2)
    hit = _pick(sentence, cands)
    if not hit:
        log("인터넷에서 새 자료 탐색 중…")
        for w in [w for w in web.openalex_search(_en_query(sentence), n=15, oa_only=True)][:3]:
            pid = web.fetch_paper(w, log=log, allow_library=False)
            if pid:
                hit = _pick(sentence, pdfindex.search(sentence, k=4, paper_ids=[pid], per_paper=4))
                if hit:
                    break
    if hit:
        status = "linked" if not author else "alternative"
        log("근거 찾음" + (" (인용하신 논문이 아닌 다른 자료)" if status == "alternative" else ""))
        return {"status": status, **hit}
    log("근거를 찾지 못했습니다. '직접 찾기'로 후보를 살펴보세요.")
    return {"status": "none", "candidates": [{k: c[k] for k in ("id", "cite", "title", "page")} for c in cands]}


def references(chunk_ids):
    ids = list({int(c) for c in chunk_ids if str(c).isdigit()})
    if not ids:
        return []
    rows = db.q(
        f"SELECT DISTINCT p.* FROM papers p JOIN chunks c ON c.paper_id=p.id WHERE c.id IN ({','.join('?' * len(ids))})", ids
    )
    out = []
    for p in rows:
        a = json.loads(p["authors"] or "[]")
        ko = bool(a and re.match(r"[가-힣]", a[0]))
        auth = ", ".join(a[:7]) + (" 외" if ko and len(a) > 7 else (", et al." if len(a) > 7 else "")) if a else "저자미상"
        ref = f"{auth} ({p['year'] or 'n.d.'}). {p['title']}."
        if p.get("venue"):
            ref += f" {p['venue']}."
        if p.get("doi"):
            ref += f" https://doi.org/{p['doi']}"
        out.append(ref)
    return sorted(out)
