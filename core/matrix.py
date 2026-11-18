"""③ 문헌분석: 선행연구 문헌분석표(칸마다 원문 근거) → 연구 동향·공백 검증 → 서론 초안."""
import datetime
import html
import json
import re

from bs4 import BeautifulSoup

from . import agent, db, llm, pdfindex

# (키, 표 머리글, 이 칸을 찾을 때 논문 안에서 검색할 말)
FIELDS = [
    ("purpose", "연구 목적·문제", "연구 목적 연구문제 가설 목표 purpose aim hypothesis"),
    ("sample", "연구대상(N)", "연구대상 참여자 표본 대상자 명을 대상으로 participants sample recruited"),
    ("variables", "주요 변인", "독립변인 종속변인 매개 조절 변수 영향 관계 variables mediator moderator"),
    ("measures", "측정도구", "측정도구 척도 문항 신뢰도 Cronbach 개발한 scale measured items"),
    ("method", "분석 방법", "분석 방법 회귀분석 구조방정식 매개효과 검증 부트스트랩 SPSS analysis regression"),
    ("findings", "주요 결과", "연구 결과 나타났다 유의한 영향 효과 결과 results found significant"),
    ("limits", "한계·제언", "연구의 한계 제언 후속 연구 limitation future research"),
]
LABEL = {k: t for k, t, _ in FIELDS}
NONE = "원문에서 확인 안 됨"


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def load(pid):
    p = db.q("SELECT * FROM projects WHERE id=?", (pid,), one=True)
    return p, json.loads(p.get("matrix") or "{}")


def save(pid, m):
    db.x("UPDATE projects SET matrix=? WHERE id=?", (json.dumps(m, ensure_ascii=False), pid))


def _paper(paper_id):
    p = db.q("SELECT id, title, authors, year, status FROM papers WHERE id=?", (paper_id,), one=True)
    if p:
        p["authors"] = json.loads(p["authors"] or "[]")
        p["cite"] = pdfindex.cite_label(p["authors"], p["year"])
    return p


# ---------- 후보: 원고에 인용된 논문을 먼저 ----------
def candidates(pid):
    p, m = load(pid)
    soup = BeautifulSoup(p.get("content") or "", "html.parser")
    cited, why = set(), {}
    chunk_ids = [int(c["data-chunk"]) for c in soup.select("span.cite[data-chunk]") if (c.get("data-chunk") or "").isdigit()]
    if chunk_ids:
        qs = ",".join("?" * len(chunk_ids))
        for r in db.q(f"SELECT DISTINCT paper_id FROM chunks WHERE id IN ({qs})", tuple(chunk_ids)):
            cited.add(r["paper_id"])
            why[r["paper_id"]] = "원고에 근거로 연결됨"
    for c in soup.select("span.cite:not([data-chunk])"):
        mm = re.match(r"^\(?\s*(.+?)\s*,\s*((?:19|20)\d{2})", c.get_text().strip()) or re.match(r"^(.+?)\s?\(((?:19|20)\d{2})", c.get_text().strip())
        if mm:
            for hit in pdfindex.find_papers_by_cite(mm.group(1), mm.group(2))[:1]:
                cited.add(hit["id"])
                why.setdefault(hit["id"], f"원고에 인용됨 {c.get_text().strip()}")
    out, seen = [], set()
    for r in db.q("SELECT id, title, authors, year FROM papers WHERE status='ok' ORDER BY id DESC"):
        key = (re.sub(r"\W", "", r["title"] or "")[:40], r["year"])
        if key in seen:  # 같은 PDF가 여러 폴더에 있으면 하나만
            continue
        seen.add(key)
        a = json.loads(r["authors"] or "[]")
        out.append({
            "id": r["id"], "title": r["title"], "year": r["year"], "cite": pdfindex.cite_label(a, r["year"]),
            "suggested": r["id"] in cited, "why": why.get(r["id"], ""), "done": str(r["id"]) in m,
        })
    out.sort(key=lambda x: (not x["suggested"], not x["done"]))
    return out


# ---------- 한 편 정리 ----------
def _gather(paper_id):
    got, seen = [], set()
    first = db.q("SELECT id, paper_id, page, text FROM chunks WHERE paper_id=? ORDER BY page, id LIMIT 2", (paper_id,))
    for _, _, q in [(None, None, None)] + FIELDS:
        rows = first if q is None else pdfindex.search(q, k=2, paper_ids=[paper_id], per_paper=2)
        for c in rows:
            if c["id"] not in seen:
                seen.add(c["id"])
                got.append(c)
    return got[:14]


ROW_SCHEMA = {
    "type": "object",
    "properties": {
        k: {
            "type": "object",
            "properties": {"value": {"type": "string"}, "src": {"type": "integer"}, "quote": {"type": "string"}},
            "required": ["value", "src", "quote"],
        }
        for k, _, _ in FIELDS
    },
    "required": [k for k, _, _ in FIELDS],
}


def build_row(paper_id, log):
    p = _paper(paper_id)
    chunks = _gather(paper_id)
    if not chunks:
        return None
    block = "\n\n".join(f"[{i + 1}] ({c['page'] + 1}쪽) {c['text'][:700]}" for i, c in enumerate(chunks))
    r = llm.chat(
        f"논문: {p['cite']} {p['title']}\n\n[논문 원문 조각]\n{block}\n\n"
        "이 논문의 문헌분석표를 만드세요. 칸: " + ", ".join(f"{k}({t})" for k, t, _ in FIELDS) + ".\n"
        "각 칸마다 value(한국어 1~2문장. 표본 수·신뢰도·계수 같은 숫자는 원문 그대로. 원문 조각에 없으면 정확히 '" + NONE + "'), "
        "src(근거가 된 조각 번호, 없으면 0), quote(그 조각에서 근거 부분을 글자 그대로 10~40단어 복사, 번역 금지, 없으면 빈칸).\n"
        "원문에 없는 내용을 추측해서 채우지 마세요.",
        system=agent.SYS,
        schema=ROW_SCHEMA,
        num_ctx=10240,
        num_predict=1800,
    )
    fields = {}
    for k, _, _ in FIELDS:
        x = r.get(k) or {}
        v = (x.get("value") or "").strip() or NONE
        q = (x.get("quote") or "").strip()
        i = int(x.get("src") or 0) - 1
        hit = None
        if q and v != NONE:
            order = ([chunks[i]] if 0 <= i < len(chunks) else []) + chunks
            hit = next((c for c in order if pdfindex.locate(c["text"], q)), None)
        fields[k] = {"v": v, "chunk": hit["id"] if hit else None, "quote": q if hit else ""}
    ok = sum(1 for f in fields.values() if f["chunk"])
    log(f"  → {sum(1 for f in fields.values() if f['v'] != NONE)}칸 채움, 그중 {ok}칸 원문 위치 확인")
    return {"paper_id": paper_id, "cite": p["cite"], "title": p["title"], "year": p["year"], "fields": fields, "ran_at": now(), "engine": llm.engine()}


def build(pid, paper_ids, log, stop=lambda: False):
    _, m = load(pid)
    for n, pp in enumerate(paper_ids):
        if stop():
            break
        p = _paper(pp)
        if not p:
            continue
        log(f"[{n + 1}/{len(paper_ids)}] {p['cite']} {p['title'][:50]}")
        try:
            row = build_row(pp, log)
        except Exception as e:
            log(f"  실패: {e}")
            continue
        if row:
            _, m = load(pid)  # 그사이 지운 줄이 있으면 반영
            m[str(pp)] = row
            save(pid, m)
    log(f"문헌분석표 {len(m)}편")


def rows(pid):
    _, m = load(pid)
    return sorted(m.values(), key=lambda r: (r.get("year") or "9999", r.get("cite") or ""))


def csv_text(pid):
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["번호", "인용", "제목", "연도"] + [t for _, t, _ in FIELDS])
    for i, r in enumerate(rows(pid), 1):
        w.writerow([i, r["cite"], r["title"], r["year"]] + [r["fields"].get(k, {}).get("v", "") for k, _, _ in FIELDS])
    return "﻿" + buf.getvalue()  # 엑셀에서 한글이 깨지지 않게


def _table_text(rs, cut=220):
    return "\n".join(
        f"[{i + 1}] {r['cite']} {r['title'][:60]}\n" + "\n".join(f"   - {LABEL[k]}: {r['fields'].get(k, {}).get('v', '')[:cut]}" for k, _, _ in FIELDS)
        for i, r in enumerate(rs)
    )


# ---------- 연구 동향 · 공백 검증 · 이론적 가정 ----------
GAP_SCHEMA = {
    "type": "object",
    "properties": {
        "trends": {"type": "array", "items": {"type": "object", "properties": {"point": {"type": "string"}, "rows": {"type": "array", "items": {"type": "integer"}}}, "required": ["point", "rows"]}},
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gap": {"type": "string"},
                    "evidence": {"type": "string"},
                    "rows": {"type": "array", "items": {"type": "integer"}},
                    "strength": {"type": "string", "enum": ["확실", "보통", "약함"]},
                    "check": {"type": "string"},
                },
                "required": ["gap", "evidence", "rows", "strength", "check"],
            },
        },
        "assumptions": {"type": "array", "items": {"type": "object", "properties": {"assumption": {"type": "string"}, "critique": {"type": "string"}, "rows": {"type": "array", "items": {"type": "integer"}}}, "required": ["assumption", "critique", "rows"]}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["trends", "gaps", "assumptions", "questions"],
}


def gaps(pid, log, stop=lambda: False):
    p, _ = load(pid)
    rs = rows(pid)
    if len(rs) < 3:
        raise RuntimeError("문헌분석표에 논문이 3편 이상 있어야 연구 공백을 볼 수 있어요.")
    log(f"문헌 {len(rs)}편으로 연구 동향과 공백을 분석하는 중…")
    r = llm.chat(
        f"내 연구 주제: {p.get('topic') or p.get('title')}\n\n[문헌분석표]\n{_table_text(rs)}\n\n"
        "이 표만 근거로 다음을 한국어로 쓰세요. rows에는 근거가 된 표의 번호를 넣으세요.\n"
        "- trends: 선행연구의 흐름 3~5개 (대상·변인·방법·결과의 경향)\n"
        "- gaps: 내 연구 주제와 관련된 연구 공백 2~4개. evidence에는 표에서 확인되는 근거를 숫자로(예: '15편 중 성인 댄서 대상은 2편, 병렬매개 검증은 0편'), "
        "strength(확실/보통/약함: 표의 편수가 적거나 검색 범위가 좁으면 '약함'), check(이 공백을 주장하기 전에 추가로 확인할 검색어·자료).\n"
        "- assumptions: 선행연구들이 공통으로 깔고 있는 이론적 가정 1~3개와 그 가정에 대한 비판(이 연구가 파고들 지점)\n"
        "- questions: 위 공백에서 나올 수 있는 연구문제 2~3개\n"
        "표에 없는 사실을 지어내지 마세요. 표가 다룬 범위 밖의 일은 '표의 범위에서는'이라고 한정하세요.",
        system=agent.SYS,
        schema=GAP_SCHEMA,
        num_ctx=12288,
        num_predict=2500,
    )
    ids = [x["paper_id"] for x in rs]

    def fix(lst):
        for it in lst:
            it["rows"] = [ids[i - 1] for i in it.get("rows", []) if isinstance(i, int) and 0 < i <= len(ids)]
        return lst

    data = {
        "n": len(rs),
        "trends": fix(r.get("trends", [])),
        "gaps": fix(r.get("gaps", [])),
        "assumptions": fix(r.get("assumptions", [])),
        "questions": r.get("questions", []),
        "ran_at": now(),
        "engine": llm.engine(),
    }
    _save_report(pid, "gaps", data)
    log(f"연구 동향 {len(data['trends'])}개 · 공백 {len(data['gaps'])}개 · 이론적 가정 {len(data['assumptions'])}개")


def _save_report(pid, key, data):
    p = db.q("SELECT reports FROM projects WHERE id=?", (pid,), one=True)
    rep = json.loads(p["reports"] or "{}")
    rep[key] = data
    db.x("UPDATE projects SET reports=? WHERE id=?", (json.dumps(rep, ensure_ascii=False), pid))


# ---------- 서론 초안 (필요성 → 선행연구 흐름 → 공백 → 목적·연구문제) ----------
def _row_chunks(row):
    """문헌분석표에서 이미 원문 위치를 확인한 조각들 (서론 근거 후보로 먼저 쓴다)."""
    ids = [f["chunk"] for f in row["fields"].values() if f.get("chunk")]
    if not ids:
        return []
    got = db.q(f"SELECT c.id, c.paper_id, c.page, c.text FROM chunks c WHERE c.id IN ({','.join('?' * len(ids))})", tuple(ids))
    for c in got:
        c["cite"] = row["cite"]
    return got


def _best_quote(sent, row):
    """서론 문장의 근거 후보 3개 (문헌분석표에서 확인한 구절 우선) → [(조각, 구절, 유사도, 검증용 문맥)]"""
    cands = _row_chunks(row) + pdfindex.search(sent, k=4, paper_ids=[row["paper_id"]], per_paper=4)
    cands = list({c["id"]: c for c in cands}.values())
    byid = {c["id"]: c for c in cands}
    opts = [(byid[f["chunk"]], f["quote"], 0.03) for f in row["fields"].values() if f.get("chunk") in byid and f.get("quote")]
    opts += [(c, q, 0.0) for c in cands for q in agent.split_sents(c["text"])]
    if not opts:
        return []
    v = llm.embed([sent] + [q for _, q, _ in opts])
    if v is None:
        return []
    sims = v[1:] @ v[0]
    out, seen = [], set()
    for k in sorted(range(len(opts)), key=lambda i: -(sims[i] + opts[i][2])):
        c, q, _ = opts[k]
        loc = pdfindex.locate(c["text"], q)
        a, b = (loc[0], loc[1]) if loc else (0, len(c["text"]))
        ctx = c["text"][max(0, a - 350): b + 350]
        if ctx in seen:
            continue
        seen.add(ctx)
        out.append((c, q, float(sims[k]), ctx))
        if len(out) == 3:
            break
    return out


def intro(pid, log, stop=lambda: False):
    p, _ = load(pid)
    rs = rows(pid)
    rep = json.loads(p.get("reports") or "{}")
    g = rep.get("gaps")
    if not g:
        raise RuntimeError("먼저 '연구 공백 찾기'를 실행해 주세요.")
    if llm.embed(["확인"]) is None:
        raise RuntimeError("근거 문장을 맞추는 로컬 검색 모델(Ollama)이 꺼져 있어요. 컴퓨터에서 Ollama를 켠 뒤 다시 눌러주세요.")
    num = {r["paper_id"]: i + 1 for i, r in enumerate(rs)}
    gap_txt = "\n".join(f"- {x['gap']} (근거: {x['evidence']}; 관련 [{', '.join(str(num[i]) for i in x['rows'] if i in num)}])" for x in g["gaps"])
    q_txt = "\n".join(f"- {q}" for q in g["questions"])
    log("서론 초안을 쓰는 중…")
    text = llm.chat(
        f"내 연구 주제: {p.get('topic') or p.get('title')}\n\n[문헌분석표]\n{_table_text(rs, 300)}\n\n[연구 공백]\n{gap_txt}\n\n[연구문제 후보]\n{q_txt}\n\n"
        "위 자료만 써서 학위논문 서론 초안을 한국어 학술 문체(~이다, ~하였다)로 쓰세요. 흐름: "
        "① 연구의 필요성(현상과 중요성) → ② 선행연구의 흐름 → ③ 연구 공백 → ④ 연구 목적과 연구문제. 문단 4~5개, 전체 14~20문장.\n"
        "규칙: 한 줄에 한 문장. 선행연구 내용을 쓴 문장 끝에는 표 번호를 붙이세요. 예) 진지한 여가는 삶의 질과 정적 관계를 보였다 [3].\n"
        "저자 이름이나 (저자, 연도)는 쓰지 말고 번호만. 표에 없는 사실·수치는 지어내지 마세요.\n"
        "번호는 그 한 편의 논문이 직접 말한 내용에만 붙이세요. '선행연구 중 ~한 연구는 없었다'처럼 여러 편을 종합한 판단(연구 공백), "
        "연구 목적·연구문제처럼 내 연구에 대한 문장에는 번호를 붙이지 마세요. 문단이 바뀌는 곳에는 빈 줄을 넣으세요.\n"
        "자료가 부족해 못 쓴 내용이 있으면 본문이 다 끝난 뒤 맨 마지막 줄에 '[메모]'로 시작하는 한 줄로만 알려주세요.",
        system=agent.SYS,
        num_ctx=12288,
        num_predict=2200,
        temperature=0.3,
    )
    note = ""
    mm = re.search(r"(?m)^\s*(?:\[메모\]|-{3,}|\*{0,2}작성\s*메모|참고\s*:|※)", text)
    if mm:  # 본문과 AI 메모 분리
        note = re.sub(r"^\s*(?:\[메모\]|-{3,}|\*{0,2}작성\s*메모[^\n]*|참고\s*:|※)\s*", "", text[mm.start():]).strip()
        note = re.sub(r"\[(\d+)\]", lambda x: rs[int(x.group(1)) - 1]["cite"] if 0 < int(x.group(1)) <= len(rs) else "", re.sub(r"\s+", " ", note))
        text = text[: mm.start()]
    paras_out, strict = [], db.get_settings().get("strict_verify", True)
    total_ok = total_bad = 0
    for para in [x for x in re.split(r"\n\s*\n", text) if x.strip()]:
        claims = agent.parse_claims(para)
        parts, pend = [], []
        for sent, idx in claims:
            if idx < 0 or idx >= len(rs):  # 내 연구에 대한 문장 (근거 불필요)
                parts.append(html.escape(sent) + ("" if sent.endswith(("?", "!")) else "."))
                continue
            opts = [o for o in _best_quote(sent, rs[idx]) if o[2] >= agent.SUPPORT_MIN]
            pend.append((len(parts), sent, idx, opts))
            parts.append(None)
        chosen = [x[3][0] if x[3] else None for x in pend]
        if strict:  # 구절의 앞뒤 문맥으로 검증, 실패하면 다음 후보로 (최대 3번)
            chosen = [None] * len(pend)
            for t in range(3):
                todo = [i for i, x in enumerate(pend) if chosen[i] is None and len(x[3]) > t]
                if not todo:
                    break
                for i, v in zip(todo, agent.verify_batch([(pend[i][1], pend[i][3][t][3]) for i in todo])):
                    if v:
                        chosen[i] = pend[i][3][t]
        for (pos, sent, idx, _), pick in zip(pend, chosen):
            if pick:
                parts[pos] = f"{html.escape(sent)} {agent.cite_span(pick[0], pick[1])}."
                total_ok += 1
            else:
                parts[pos] = f"{html.escape(sent)} {agent.need_span(rs[idx]['cite'])}."
                total_bad += 1
        if parts:
            plain = html.escape(" ".join(s for s, _ in claims), quote=True)
            paras_out.append(f'<p class="ai-draft" data-ai="{plain}">{" ".join(parts)}</p>')
    memo = f'<p class="ai-note" contenteditable="false">💬 AI 메모: {html.escape(note)} <small>(확인 후 지워주세요)</small></p>' if note else ""
    out = "<h2>서론 (AI 초안)</h2>" + "".join(paras_out) + memo
    _save_report(pid, "intro", {"html": out, "ran_at": now(), "engine": llm.engine(), "ok": total_ok, "need": total_bad})
    log(f"서론 초안 완성 — 근거 확인 {total_ok}문장, 확인 필요(?) {total_bad}문장")
