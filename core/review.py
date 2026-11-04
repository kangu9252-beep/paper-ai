"""이미 쓴 원고 보강: ①불러오기 ②인용 전체 점검 ③근거 없는 문장 ④논의 보강 ⑤최신 연구 ⑥참고문헌 대조."""
import datetime
import html
import io
import json
import re
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString

from . import agent, db, llm, pdfindex, web

YEAR = r"(?:19|20)\d{2}[a-z]?"
# (저자, 2020) / (저자, 2020; 저자2 외, 2019) / (Smith & Lee, 2020, p. 3)
PAREN_RE = re.compile(r"\(([^()]*?" + YEAR + r"[^()]*?)\)")
PART_RE = re.compile(r"^\s*([^,;()\d][^;()]*?),?\s+(" + YEAR + r")(?:\s*,\s*pp?\.\s*[\d\-–]+)?\s*$")
# 저자(2020) / Neff et al.(2003) / 홍길동 외(2019)
NARR_RE = re.compile(
    r"((?<![가-힣A-Za-z0-9])[가-힣]{2,4}(?:\s?외)?|[A-Z][A-Za-z\-']+(?:\s(?:et al\.|&\s[A-Z][A-Za-z\-']+|and\s[A-Z][A-Za-z\-']+))?)\s?\((" + YEAR + r")\)"
)
SKIP_HEAD = re.compile(r"연구\s*방법|방법|결과|results?|methods?|참고\s*문헌|references?|부록|appendix|abstract|초록|목차", re.I)
REF_HEAD = re.compile(r"참\s*고\s*문\s*헌|references?|bibliography|인용\s*문헌", re.I)
RESULT_HEAD = re.compile(r"결과|results?", re.I)
DISC_HEAD = re.compile(r"논의|discussion|고찰", re.I)
SENT_END = re.compile(r"(?<=[다요음함됨임것])\.(?=\s|$)|(?<=[a-z\)\]])\.(?=\s+[A-Z가-힣])|[!?](?=\s|$)")


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------- ① 원고 불러오기 ----------
def _docx_paras(data):
    import docx

    d = docx.Document(io.BytesIO(data))
    out = []
    for p in d.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        st = (p.style.name or "").lower() if p.style is not None else ""
        out.append(("h" if ("heading" in st or "제목" in st or st.startswith("title")) else "p", t))
    return out


def _hwpx_paras(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    names = sorted(n for n in z.namelist() if re.match(r"Contents/section\d+\.xml", n))
    out = []
    for n in names:
        xml = z.read(n).decode("utf-8", "ignore")
        for para in re.findall(r"<hp:p[ >].*?</hp:p>", xml, re.S):
            t = "".join(re.findall(r"<hp:t(?: [^>]*)?>(.*?)</hp:t>", para, re.S))
            t = html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
            if t:
                out.append(("p", t))
    return out


def _looks_heading(t):
    return len(t) < 45 and not t.endswith(("다.", ".", "다")) and bool(
        re.match(r"^(제\s*\d+\s*[장절]|[IVX]+\s*\.|\d+(\.\d+)*\.?\s|[가-하]\.\s|\d+\)\s|서론|결론|논의|참고\s*문헌|references|abstract|초록)", t, re.I)
    )


def to_html(paras):
    out, in_refs = [], False
    for kind, t in paras:
        if kind == "h" or _looks_heading(t):
            in_refs = bool(REF_HEAD.search(t))
            out.append(f"<h2>{html.escape(t)}</h2>")
        else:
            body = html.escape(t)
            out.append(f"<p>{body if in_refs else mark_cites_text(body)}</p>")
    return "\n".join(out)


def span(label):
    return f'<span class="cite need" contenteditable="false">{label}</span>'


def mark_cites_text(escaped):
    """HTML 이스케이프된 문단 텍스트 안의 인용 표기를 인용 칸(span)으로 바꾼다."""

    def paren(m):
        parts = html.unescape(m.group(1)).split(";")
        if not all(PART_RE.match(x) for x in parts):
            return m.group(0)
        if len(parts) == 1:
            return span(m.group(0))
        return "(" + "; ".join(span(html.escape(x.strip())) for x in parts) + ")"

    out = PAREN_RE.sub(paren, escaped)
    # 서술형 인용은 이미 만든 span 밖에서만
    pieces = re.split(r"(<span[^>]*>.*?</span>)", out)
    for i in range(0, len(pieces), 2):
        pieces[i] = NARR_RE.sub(lambda m: span(m.group(0)), pieces[i])
    return "".join(pieces)


def import_manuscript(filename, data=None, text=None):
    name = (filename or "").lower()
    if text is not None:
        paras = [("p", ln.strip()) for ln in text.splitlines() if ln.strip()]
    elif name.endswith(".docx"):
        paras = _docx_paras(data)
    elif name.endswith(".hwpx"):
        paras = _hwpx_paras(data)
    elif name.endswith(".hwp"):
        raise ValueError("한글(.hwp) 파일은 직접 읽을 수 없어요. 한글에서 '다른 이름으로 저장 → .hwpx 또는 .docx'로 저장해서 올려주세요.")
    elif name.endswith((".txt", ".md")):
        paras = [("p", ln.strip()) for ln in data.decode("utf-8", "ignore").splitlines() if ln.strip()]
    else:
        raise ValueError("지원 형식: .docx, .hwpx, .txt (또는 글 붙여넣기)")
    if not paras:
        raise ValueError("파일에서 글을 찾지 못했어요.")
    return to_html(paras)


# ---------- 공통: 원고 구조 ----------
def load(pid):
    p = db.q("SELECT * FROM projects WHERE id=?", (pid,), one=True)
    return p, BeautifulSoup(p["content"] or "", "html.parser")


def save(pid, soup):
    db.x("UPDATE projects SET content=?, updated=datetime('now','localtime') WHERE id=?", (str(soup), pid))


def save_report(pid, tool, data, summary, log):
    p = db.q("SELECT reports FROM projects WHERE id=?", (pid,), one=True)
    r = json.loads(p["reports"] or "{}")
    r[tool] = {"ran_at": now(), "engine": llm.engine(), "summary": summary, "data": data}
    db.x("UPDATE projects SET reports=? WHERE id=?", (json.dumps(r, ensure_ascii=False), pid))
    log(summary)


def sections(soup):
    """[(제목, [블록...])] — h1~h3 기준으로 나눔."""
    out, cur = [], ("", [])
    for el in soup.find_all(["h1", "h2", "h3", "p", "li"], recursive=True):
        if el.name in ("h1", "h2", "h3"):
            out.append(cur)
            cur = (el.get_text(" ", strip=True), [])
        elif not el.find_parent(["p", "li"]):
            cur[1].append(el)
    out.append(cur)
    return [s for s in out if s[0] or s[1]]


def parse_label(label):
    t = label.strip()
    m = re.match(r"^\(?\s*(.+?)\s*,\s*(" + YEAR + r"|연도미상)(?:\s*,\s*pp?\.\s*[\d\-–]+)?\s*\)?$", t) or re.match(
        r"^(.+?)\s?\((" + YEAR + r")\)$", t
    )
    if not m:
        return "", ""
    return m.group(1).strip(), ("" if m.group(2) == "연도미상" else m.group(2))


def first_surname(author):
    a = re.sub(r"\s*(외|et al\.?|등)\s*$", "", author.strip())
    a = re.split(r"\s*(?:,|&|\band\b|·)\s*", a)[0].strip()
    if re.match(r"[가-힣]", a):
        return a.replace(" ", "")
    return a.split()[-1].lower() if a.split() else a.lower()


def _clean(t):
    t = re.sub(r"\(\s*[;,\s]*\)", "", t)  # 인용을 뺀 뒤 남은 빈 괄호
    return re.sub(r"\s+([.,;:!?])", r"\1", t)


def cite_sentence(el):
    """인용 칸이 근거가 되는 문장. (저자, 연도)는 앞 문장, 저자(연도)는 그 인용이 들어 있는 문장."""
    block = el.find_parent(["p", "li"]) or el.parent
    before, after, seen = "", "", False
    for node in block.descendants:
        if node is el:
            seen = True
            continue
        if isinstance(node, NavigableString) and not node.find_parent("span", class_="cite"):
            if seen:
                after += str(node)
            else:
                before += str(node)
    before, after = _clean(before), _clean(after)
    parts = SENT_END.split(before)
    s = parts[-1].strip()
    narrative = re.search(r"\(\s*" + YEAR + r"\s*\)$", el.get_text().strip())
    if narrative:
        tail = SENT_END.split(after, 1)[0]
        s = f"{s} {el.get_text().strip()}{tail}".strip()
    elif len(s) < 10 and len(parts) > 1:
        s = (parts[-2].strip() + " " + s).strip()
    return re.sub(r"^[\s.;,)]+", "", s)


def all_cites(soup):
    cites = soup.find_all("span", class_="cite")
    n = max([int(c.get("data-cid")) for c in cites if (c.get("data-cid") or "").isdigit()] + [0])
    for c in cites:
        if not (c.get("data-cid") or "").isdigit():
            n += 1
            c["data-cid"] = str(n)
        c["contenteditable"] = "false"
    return cites


def set_state(c, state, note=""):
    cls = [x for x in (c.get("class") or []) if x not in ("need", "warn")]
    if state in ("need", "warn"):
        cls.append(state)
    c["class"] = cls
    if note:
        c["data-note"] = note
    elif c.has_attr("data-note"):
        del c["data-note"]


# ---------- ② 인용 전체 점검 ----------
def check_citations(pid, log, stop=lambda: False, use_internet=True):
    p, soup = load(pid)
    cites = all_cites(soup)
    save(pid, soup)
    log(f"인용 {len(cites)}개 점검 시작")
    items, ok, warn, need = [], 0, 0, 0
    for i, c in enumerate(cites):
        if stop():
            break
        label = c.get_text()
        if label.strip() == "(출처 필요)":
            continue
        author, year = parse_label(label)
        sent = cite_sentence(c)
        log(f"[{i + 1}/{len(cites)}] {label}")
        item = {"cid": c["data-cid"], "label": label, "sentence": sent[:200]}
        if c.get("data-chunk"):
            e = pdfindex.evidence(int(c["data-chunk"]), c.get("data-quote", ""))
            if not e:
                set_state(c, "need", "연결된 근거 PDF가 라이브러리에서 사라졌어요")
                item["status"], item["note"] = "need", "근거 PDF 없음"
            else:
                quote = c.get("data-quote") or e["text"][:600]
                good = agent.verify_batch([(sent, quote)])[0] if sent else True
                if good:
                    set_state(c, None)
                    item["status"] = "ok"
                else:
                    set_state(c, "warn", "원문과 내용이 다르거나 과장되었을 수 있어요")
                    item["status"], item["note"] = "warn", "원문과 다를 수 있음"
        else:
            if not sent:
                item["status"], item["note"] = "need", "앞 문장을 찾지 못함"
                need += 1
                items.append(item)
                continue
            r = agent.autofind(sent, author, year, lambda m: log("   " + m)) if use_internet else _local_find(sent, author, year)
            if r and r.get("status") == "linked":
                c["data-chunk"], c["data-quote"] = str(r["chunk_id"]), r.get("quote", "")
                if not author:
                    c.string = r["cite"]
                set_state(c, None)
                item["status"], item["note"] = "ok", "근거 위치를 찾아 연결함"
            elif r and r.get("status") == "alternative":
                c["data-sug-chunk"], c["data-sug-quote"], c["data-sug-label"] = str(r["chunk_id"]), r.get("quote", ""), r["cite"]
                set_state(c, "need", f"인용한 논문에서는 못 찾음 — 다른 자료 {r['cite']}에 근거 있음")
                item["status"], item["note"] = "need", f"대신 {r['cite']}에 근거"
            else:
                has = bool(author and pdfindex.find_papers_by_cite(author, year))
                set_state(c, "need", "논문은 있지만 이 내용을 찾지 못함" if has else "이 논문 PDF가 내 자료에 없음")
                item["status"], item["note"] = "need", ("논문 안에서 근거 못 찾음" if has else "PDF 없음")
        ok += item["status"] == "ok"
        warn += item["status"] == "warn"
        need += item["status"] == "need"
        items.append(item)
        if i % 5 == 4:
            save(pid, soup)
    save(pid, soup)
    save_report(pid, "cites", items, f"인용 점검: ✔ {ok}개 확인 · ⚠ {warn}개 내용 확인 필요 · ? {need}개 근거 없음", log)


def _local_find(sent, author, year):
    ids = [p["id"] for p in pdfindex.find_papers_by_cite(author, year)] if author else None
    hit = agent._pick(sent, pdfindex.search(sent, k=5, paper_ids=ids or None, per_paper=5 if ids else 2))
    return {"status": "linked", **hit} if hit else None


# ---------- ③ 근거 없는 문장 ----------
def uncited_claims(pid, log, stop=lambda: False):
    p, soup = load(pid)
    flagged, items = 0, []
    secs = sections(soup)
    for title, blocks in secs:
        if title and SKIP_HEAD.search(title):
            continue
        for b in blocks:
            if stop():
                break
            if "ai-note" in (b.get("class") or []):
                continue
            segs = []  # (text_node, start, end, sentence)
            for node in list(b.descendants):
                if not isinstance(node, NavigableString) or node.find_parent("span", class_="cite"):
                    continue
                s = str(node)
                pos = 0
                for m in SENT_END.finditer(s):
                    sent = re.sub(r"^[\s.;,)]+", "", s[pos:m.start()]).strip()
                    if len(sent) >= 15:
                        segs.append((node, m.start(), sent))
                    pos = m.end()
                tail = re.sub(r"^[\s.;,)]+", "", s[pos:]).strip()
                nxt = node.next_sibling
                cited_tail = getattr(nxt, "name", None) == "span" and "cite" in (nxt.get("class") or [])
                if len(tail) >= 15 and not cited_tail and re.search(r"[다요]$", tail):
                    segs.append((node, len(s.rstrip()), tail))
            if not segs:
                continue
            numbered = "\n".join(f"{i + 1}. {sg[2]}" for i, sg in enumerate(segs))
            r = llm.chat(
                f"다음은 논문 원고의 문장들입니다. 이 중 선행연구나 이론, 통계 같은 외부 근거(인용)가 필요한 주장 문장의 번호만 고르세요.\n"
                "연구자 자신의 연구 결과·연구 목적·계획·의견·연결 문장은 인용이 필요 없으므로 고르지 마세요.\n\n" + numbered,
                schema={"type": "object", "properties": {"need": {"type": "array", "items": {"type": "integer"}}}, "required": ["need"]},
                num_ctx=4096,
                num_predict=200,
                temperature=0,
            )
            need_idx = sorted({int(n) - 1 for n in r.get("need", []) if 0 < int(n) <= len(segs)}, reverse=True)
            for i in need_idx:  # 뒤에서부터 넣어야 위치가 안 밀림
                node, at, sent = segs[i]
                s = str(node)
                new = BeautifulSoup(f'{html.escape(s[:at].rstrip())} <span class="cite need" contenteditable="false" data-auto="1">(출처 필요)</span>{html.escape(s[at:])}', "html.parser")
                sp = new.find("span")
                sug = _suggest(sent)
                if sug:
                    sp["data-sug-chunk"], sp["data-sug-quote"], sp["data-sug-label"] = str(sug["id"]), sug["quote"], sug["cite"]
                    sp["data-note"] = f"추천 근거: {sug['cite']}"
                node.replace_with(new)
                flagged += 1
                items.append({"sentence": sent[:200], "section": title, "suggest": sug["cite"] if sug else ""})
            log(f"  '{title or '본문'}' 문단 검사: {len(need_idx)}곳 표시")
    all_cites(soup)
    save(pid, soup)
    save_report(pid, "uncited", list(reversed(items)), f"근거 없는 주장 문장 {flagged}곳에 '출처 필요' 표시 (추천 근거 {sum(1 for i in items if i['suggest'])}곳)", log)


def _suggest(sentence):
    """내 자료에서 의미가 가까운 근거 문장 추천 (검색 모델만 사용 — 빠름)."""
    cands = pdfindex.search(sentence, k=4, per_paper=1)
    if not cands:
        return None
    (chunk, quote, sim), = agent.match_quotes([(sentence, 0)], cands)
    if chunk and quote and sim >= 0.62:
        return {"id": chunk["id"], "quote": quote, "cite": chunk["cite"]}
    return None


# ---------- ④ 논의 보강 ----------
def discussion_boost(pid, log, stop=lambda: False):
    p, soup = load(pid)
    res = [b for t, bl in sections(soup) if RESULT_HEAD.search(t or "") for b in bl]
    text = "\n".join(b.get_text(" ", strip=True) for b in res)[:7000]
    if len(text) < 40:
        save_report(pid, "discuss", [], "결과 장(제목에 '결과' 또는 'Results')을 찾지 못했어요. 결과 장 제목을 확인해 주세요.", log)
        return
    log("연구 결과에서 핵심 결과를 뽑는 중…")
    r = llm.chat(
        f"[연구 결과]\n{text}\n\n이 연구의 핵심 결과를 3~6개 뽑으세요. finding은 한국어 한 문장, query는 이 결과와 관련된 선행연구를 찾을 영어 검색어(3~6단어).",
        schema={"type": "object", "properties": {"findings": {"type": "array", "items": {"type": "object", "properties": {"finding": {"type": "string"}, "query": {"type": "string"}}, "required": ["finding", "query"]}}}, "required": ["findings"]},
        num_predict=800,
    )
    out = []
    for f in r.get("findings", [])[:6]:
        if stop():
            break
        log(f"· {f['finding'][:60]}")
        cands = []
        for c in pdfindex.search(f["finding"], k=4, per_paper=1):
            cands.append({"src": "local", "cite": c["cite"], "title": c["title"], "text": c["text"][:600], "chunk": c})
        for w in web.openalex_search(f["query"], n=8):
            if w["abstract"] and not web.already_have(w):
                cands.append({"src": "web", "cite": pdfindex.cite_label(w["authors"], w["year"]), "title": w["title"], "text": w["abstract"][:600], "work": w})
        if not cands:
            out.append({"finding": f["finding"], "support": [], "contrast": []})
            continue
        block = "\n\n".join(f"[{i + 1}] {c['cite']} {c['title']}\n{c['text']}" for i, c in enumerate(cands))
        j = llm.chat(
            f"[이 연구의 결과]\n{f['finding']}\n\n[선행연구]\n{block}\n\n각 선행연구가 이 결과와 같은 방향의 결과를 보고했으면 support, 반대 방향이면 contrast, "
            "주제가 다르거나 판단할 근거가 부족하면 none으로 엄격하게 판정하세요. 애매하면 none입니다.\n"
            "support/contrast인 경우 논의 장에 쓸 문장(sentence)을 한 문장으로 쓰세요. 규칙: 선행연구에 실제로 적힌 내용만 쓰고, "
            "연구 대상(예: 청소년, 대학생)은 선행연구에 적힌 대로 쓰며, '~이다', '~하였다', '~와 일치한다'처럼 논문 문체로 끝내고 '~합니다'는 쓰지 마세요. 인용 표기는 쓰지 마세요.",
            schema={"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer"}, "relation": {"type": "string", "enum": ["support", "contrast", "none"]}, "reason": {"type": "string"}, "sentence": {"type": "string"}}, "required": ["n", "relation", "reason", "sentence"]}}}, "required": ["items"]},
            num_predict=1500,
        )
        entry = {"finding": f["finding"], "support": [], "contrast": []}
        for it in j.get("items", []):
            k = int(it.get("n", 0)) - 1
            if not (0 <= k < len(cands)) or it.get("relation") not in ("support", "contrast"):
                continue
            c = cands[k]
            rec = {"cite": c["cite"], "title": c["title"], "reason": it.get("reason", ""), "sentence": it.get("sentence", "")}
            if c["src"] == "local":
                ch, q, _ = agent.match_quotes([(rec["sentence"], 0)], [c["chunk"]])[0]
                rec.update(chunk_id=ch["id"], quote=q)
            else:
                w = c["work"]
                rec.update(doi=w["doi"], is_oa=w["is_oa"])
                pid2 = web.fetch_paper(w, log=lambda m: log("   " + m)) if w["is_oa"] else None
                if pid2:
                    hits = pdfindex.search(rec["sentence"], k=3, paper_ids=[pid2], per_paper=3)
                    if hits:
                        ch, q, _ = agent.match_quotes([(rec["sentence"], 0)], hits)[0]
                        rec.update(chunk_id=ch["id"], quote=q, cite=ch["cite"])
            entry[it["relation"]].append(rec)
            if len(entry["support"]) >= 3 and len(entry["contrast"]) >= 3:
                break
        for rel in ("support", "contrast"):  # 원문이 연결된 것 먼저, 결과마다 각각 3건까지
            entry[rel] = sorted(entry[rel], key=lambda r: not r.get("chunk_id"))[:3]
        out.append(entry)
    n_s = sum(len(e["support"]) for e in out)
    n_c = sum(len(e["contrast"]) for e in out)
    save_report(pid, "discuss", out, f"논의 보강: 핵심 결과 {len(out)}개 · 일치하는 연구 {n_s}건 · 상반되는 연구 {n_c}건", log)


def insert_discussion(pid, sentence, chunk_id, quote, label):
    """논의 장 끝(없으면 원고 끝)에 근거 달린 문장을 넣는다."""
    p, soup = load(pid)
    sp = f'<span class="cite{"" if chunk_id else " need"}" contenteditable="false"' + (f' data-chunk="{int(chunk_id)}" data-quote="{html.escape(quote or "", quote=True)}"' if chunk_id else "") + f">{html.escape(label)}</span>"
    para = BeautifulSoup(f"<p>{html.escape(sentence.rstrip('.'))} {sp}.</p>", "html.parser")
    heads = soup.find_all(["h1", "h2", "h3"])
    disc = next((i for i, h in enumerate(heads) if DISC_HEAD.search(h.get_text())), None)
    refs = next((h for h in heads if REF_HEAD.search(h.get_text())), None)
    if disc is not None and disc + 1 < len(heads):
        heads[disc + 1].insert_before(para)  # 논의 장의 끝
    elif disc is not None or refs is None:
        soup.append(para)
    else:
        refs.insert_before(para)
    all_cites(soup)
    save(pid, soup)


# ---------- ⑤ 최신 연구 ----------
def recent_research(pid, log, stop=lambda: False):
    p, soup = load(pid)
    blocks = [(t, _clean(b.get_text(" ", strip=True))) for t, bl in sections(soup) if not (t and REF_HEAD.search(t)) for b in bl]
    blocks = [(t, x) for t, x in blocks if len(x) > 60]
    body = " ".join(x for _, x in blocks)[:4000]
    topic = p.get("topic") or p.get("title") or ""
    log("원고 주제로 검색어 만드는 중…")
    r = llm.chat(
        f"논문 주제: {topic}\n원고 일부: {body[:2500]}\n\n이 원고와 관련된 최신 선행연구를 찾기 위한 영어 학술 검색어 2개(각 3~6단어)를 만드세요.",
        schema={"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}}, "required": ["queries"]},
        num_predict=200,
    )
    year_from = datetime.date.today().year - 3
    seen, works = set(), []
    for qq in r.get("queries", [])[:3] or [topic]:
        log(f"검색: {qq} ({year_from}년 이후)")
        for w in web.openalex_search(qq, n=20, year_from=year_from):
            key = (w["doi"] or w["title"]).lower()
            if key in seen or web.already_have(w):
                continue
            seen.add(key)
            works.append(w)
    cited = {(first_surname(a), y[:4]) for a, y in (parse_label(c.get_text()) for c in soup.find_all("span", class_="cite")) if a}
    works = [w for w in works if not (w["authors"] and (first_surname(w["authors"][0]), w["year"]) in cited)]
    works.sort(key=lambda w: -(w["cited"] or 0))
    works = works[:15]
    # 원고의 어느 문단과 가장 관련 있는지 (검색 모델)
    if works and blocks:
        wv = llm.embed([w["title"] + ". " + w["abstract"][:500] for w in works])
        bv = llm.embed([x[:800] for _, x in blocks])
        if wv is not None and bv is not None:
            sims = wv @ bv.T
            for i, w in enumerate(works):
                j = int(sims[i].argmax())
                w["where"] = f"{blocks[j][0] or '본문'} — “{blocks[j][1][:50]}…”"
    items = [{k: w.get(k, "") for k in ("title", "authors", "year", "venue", "cited", "is_oa", "doi", "pdf_url", "oa_url", "where")} | {"abstract": w["abstract"][:400]} for w in works]
    save_report(pid, "recent", items, f"최신 연구({year_from}년 이후) {len(items)}편 — 원고에 아직 없는 논문", log)


# ---------- ⑥ 참고문헌 대조 ----------
def ref_entries(soup):
    out, on = [], False
    for el in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        if el.name in ("h1", "h2", "h3"):
            on = bool(REF_HEAD.search(el.get_text()))
            continue
        if on:
            t = el.get_text(" ", strip=True)
            if len(t) > 10:
                out.append(t)
    return out


def parse_ref(t):
    m = re.search(r"\(\s*(" + YEAR + r")\s*\)|[,.]\s*(" + YEAR + r")\s*[.,]", t)
    if not m:
        return None, None
    year = (m.group(1) or m.group(2))
    head = t[: m.start()].strip()
    if re.match(r"[가-힣]", head):
        a = re.match(r"[가-힣]{2,5}", head.replace(" ", ""))
        return (a.group(0) if a else head[:4]), year
    return head.split(",")[0].strip().split()[-1].lower() if head else None, year


def check_references(pid, log, stop=lambda: False):
    p, soup = load(pid)
    refs = ref_entries(soup)
    if not refs:
        save_report(pid, "refs", {}, "참고문헌 장(제목 '참고문헌' 또는 'References')을 찾지 못했어요.", log)
        return
    ref_keys = []
    for t in refs:
        a, y = parse_ref(t)
        ref_keys.append({"text": t[:200], "author": a, "year": y})
    cites = {}
    for c in soup.find_all("span", class_="cite"):
        a, y = parse_label(c.get_text())
        if a:
            cites.setdefault((first_surname(a), y[:4]), c.get_text())

    def same(a1, a2):
        return a1 and a2 and (a1 == a2 or (re.match(r"[a-z]", a1 or "") and a1 == a2.lower()))

    missing, wrong_year, used = [], [], set()
    for (a, y), label in cites.items():
        hit = [i for i, r in enumerate(ref_keys) if same(a, r["author"]) and r["year"] and r["year"][:4] == y]
        if hit:
            used.update(hit)
            continue
        other = [r for r in ref_keys if same(a, r["author"])]
        if other:
            wrong_year.append({"label": label, "refs": [r["text"] for r in other]})
            used.update(i for i, r in enumerate(ref_keys) if same(a, r["author"]))
        else:
            missing.append(label)
    unused = [r["text"] for i, r in enumerate(ref_keys) if i not in used]
    data = {"missing": missing, "wrong_year": wrong_year, "unused": unused, "n_refs": len(refs), "n_cites": len(cites)}
    save_report(
        pid, "refs", data,
        f"참고문헌 대조: 본문 인용 {len(cites)}개 · 목록 {len(refs)}개 → 목록에 없음 {len(missing)} · 연도 불일치 {len(wrong_year)} · 본문에서 안 쓰임 {len(unused)}",
        log,
    )


TOOLS = {
    "cites": check_citations,
    "uncited": uncited_claims,
    "discuss": discussion_boost,
    "recent": recent_research,
    "refs": check_references,
}


def full_review(pid, log, stop=lambda: False):
    for name in ("refs", "cites", "uncited", "recent", "discuss"):
        if stop():
            break
        log(f"=== {name} ===")
        try:
            TOOLS[name](pid, log, stop)
        except Exception as e:
            log(f"  {name} 실패: {e}")
