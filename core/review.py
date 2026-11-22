"""이미 쓴 원고 보강: ⓪예비심사 리뷰 ①불러오기 ②인용 전체 점검 ③근거 없는 문장 ④논의 보강 ⑤최신 연구 ⑥참고문헌 대조."""
import datetime
import hashlib
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


def text_hash(content):
    """원고 글의 지문 (인용 표시·메모는 빼고 글자만) — 바뀌지 않았으면 점검을 다시 돌리지 않는다."""
    soup = BeautifulSoup(content or "", "html.parser")
    for c in soup.select("span.cite, p.ai-note"):
        c.decompose()
    t = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]


def para_hash(t):
    return hashlib.sha1(re.sub(r"\s+", " ", t).strip().encode("utf-8")).hexdigest()[:12]


def unchanged(pid, tool):
    """지난번 이 점검을 돌린 뒤 원고 글이 그대로인가."""
    p = db.q("SELECT content, reports FROM projects WHERE id=?", (pid,), one=True)
    rep = json.loads(p["reports"] or "{}")
    prev = rep.get(tool)
    if not prev or not prev.get("hash"):
        return False
    if tool == "panel" and (prev.get("data") or {}).get("target") != (rep.get("panel_target") or "학위논문 예비심사"):
        return False
    return prev["hash"] == text_hash(p["content"])


def _cache_get(pid, key):
    p = db.q("SELECT reports FROM projects WHERE id=?", (pid,), one=True)
    return json.loads(p["reports"] or "{}").get(key) or {}


def _cache_put(pid, key, cache):
    p = db.q("SELECT reports FROM projects WHERE id=?", (pid,), one=True)
    r = json.loads(p["reports"] or "{}")
    r[key] = cache
    db.x("UPDATE projects SET reports=? WHERE id=?", (json.dumps(r, ensure_ascii=False), pid))


def save_report(pid, tool, data, summary, log):
    p = db.q("SELECT reports, content FROM projects WHERE id=?", (pid,), one=True)
    r = json.loads(p["reports"] or "{}")
    r[tool] = {"ran_at": now(), "engine": llm.engine(), "summary": summary, "data": data, "hash": text_hash(p["content"])}
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


PLACEHOLDER = re.compile(r"^\W*$|출처\s*필요|저자\s*미상|연도\s*미상")


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
        if isinstance(node, NavigableString):
            other = node.find_parent("span", class_="cite")
            # 다른 인용 칸: 괄호 인용은 빼고, '서희진(2009a)'처럼 문장의 일부인 인용은 글자를 살린다 (빼면 '황선환과 이'처럼 문장이 깨짐)
            if other is not None and (other is el or not re.search(r"\(\s*" + YEAR + r"\s*\)$", other.get_text().strip())):
                continue
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
    # 참고문헌 목록 안에 잘못 붙은 인용 표시는 글자로 되돌림
    head = next((h for h in soup.find_all(["h1", "h2", "h3", "h4"]) if re.fullmatch(r"\s*(?:" + REF_HEAD.pattern + r")\s*", h.get_text(), re.I)), None)
    if head:
        for c in head.find_all_next("span", class_="cite"):
            if not c.get("data-chunk"):
                c.replace_with(c.get_text())
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
                if not good and sent and author:  # 예전에 붙은 위치가 틀렸을 수 있음 → 인용한 논문 안에서 다시 찾아본다
                    ids = [x["id"] for x in pdfindex.find_papers_by_cite(author, year)]
                    hit = agent.find_in_paper(sent, ids) if ids else None
                    if hit and agent.verify_batch([(sent, hit["quote"])])[0]:
                        c["data-chunk"], c["data-quote"] = str(hit["chunk_id"]), hit["quote"]
                        log("   같은 논문에서 더 맞는 근거 위치를 찾아 고침")
                        good = True
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
                if not author:  # 저자가 없던 자리: AI가 고른 자료 → 확인 표시 (내가 쓴 저자 표기는 절대 안 바꿈)
                    before = c.get_text()
                    if PLACEHOLDER.search(before):  # '(출처 필요)' 같은 빈자리일 때만 표기를 채움
                        c.string = r["cite"]
                    set_state(c, "warn", f"AI가 고른 자료예요 (원래 표기: {before}). 맞는 근거인지 확인해 주세요")
                    item["status"], item["note"] = "warn", f"AI가 고른 자료 {r['cite']} — 확인 필요"
                else:
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
    hit = agent.find_in_paper(sent, ids) if ids else None
    if hit:
        return {"status": "linked", **hit}
    if author and not ids:
        return None  # 인용한 논문 PDF가 없음 → 다른 논문을 몰래 붙이지 않음
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
    para = BeautifulSoup(
        f'<p class="ai-draft" data-ai="{html.escape(sentence, quote=True)}">{html.escape(sentence.rstrip("."))} {sp}.</p>', "html.parser"
    )
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
    r = llm.light(
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


# ---------- ⓪ 예비심사 리뷰 (일목요연함 중심) ----------
RQ_RE = re.compile(r"연구\s*문제|연구\s*질문|가설|연구\s*목적|research\s+questions?|hypothes", re.I)
CONC_HEAD = re.compile(r"결론|논의|제언|conclusion|discussion|고찰", re.I)
CATS = ["일목요연함", "연구문제-결론 일치", "선행연구와 차별성", "연구방법 타당성", "결과 해석", "한계와 제언", "형식"]
REVIEWER = (
    "당신은 이 분야 학위논문 예비심사를 여러 번 맡아 본 엄격하지만 따뜻한 지도교수입니다. "
    "가장 중요하게 보는 것은 논문이 일목요연한가입니다: 연구문제 → 선행연구 → 방법 → 결과 → 결론이 하나의 줄기로 이어지는지, "
    "각 장·문단이 그 줄기에 기여하는지, 문단마다 중심 문장이 분명한지. 근거 없이 칭찬하거나 사소한 맞춤법을 지적하지 마세요. "
    "모든 답은 한국어로 씁니다."
)


def _para_blocks(soup):
    """[(장 제목, [(P번호, 요소, 글)])] — 참고문헌·AI 메모 제외, 문단마다 data-rv 번호를 붙인다."""
    out, n = [], 0
    for title, bl in sections(soup):
        if title and REF_HEAD.search(title):
            continue
        paras = []
        for b in bl:
            if "ai-note" in (b.get("class") or []):
                continue
            t = _clean(re.sub(r"\s+", " ", b.get_text("")).strip())  # 인용 표시 앞뒤 공백을 원고 그대로 (넣지도 빼지도 않음)
            if len(t) < 25:
                continue
            n += 1
            b["data-rv"] = f"P{n}"
            paras.append((f"P{n}", b, t))
        out.append((title, paras))
    return out


def _add_note(el, note):
    try:
        notes = json.loads(el.get("data-review") or "[]")
    except Exception:
        notes = []
    if any(x.get("issue") == note.get("issue") for x in notes):
        return
    notes.append(note)
    el["data-review"] = json.dumps(notes, ensure_ascii=False)


SKEL_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "paragraphs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "topic": {"type": "string"}, "problem": {"type": "string"}},
                "required": ["id", "topic", "problem"],
            },
        },
    },
    "required": ["claim", "paragraphs"],
}
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "thread": {"type": "string"},
        "verdict": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "rq": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "answered": {"type": "string", "enum": ["예", "부분", "아니오"]},
                    "where": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["question", "answered", "where", "comment"],
            },
        },
        "critiques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["중요", "보통", "사소"]},
                    "category": {"type": "string", "enum": CATS},
                    "para": {"type": "string"},
                    "issue": {"type": "string"},
                    "fix": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["severity", "category", "para", "issue", "fix", "question"],
            },
        },
    },
    "required": ["thread", "verdict", "strengths", "rq", "critiques"],
}


def structure_review(pid, log, stop=lambda: False):
    p, soup = load(pid)
    for el in soup.select("[data-review]"):
        del el["data-review"]
    secs = _para_blocks(soup)
    byid = {k: el for _, ps in secs for k, el, _ in ps}
    if not byid:
        save_report(pid, "structure", {}, "검토할 본문 문단을 찾지 못했어요.", log)
        return
    topic = p.get("topic") or p.get("title") or ""
    claude = llm.engine() == "claude"
    cut, batch = (1500, 25) if claude else (450, 10)

    # 1단계: 장마다 중심 주장과 문단별 중심 내용 (논문 뼈대)
    skeleton, prev = [], ""
    todo = [(t, ps) for t, ps in secs if ps]
    for i, (title, ps) in enumerate(todo):
        if stop():
            return
        log(f"[뼈대 {i + 1}/{len(todo)}] {title or '(제목 없음)'} 읽는 중…")
        claim, items = "", []
        for k in range(0, len(ps), batch):
            part = ps[k : k + batch]
            block = "\n\n".join(f"{pp}: {t[:cut]}" for pp, _, t in part)
            r = llm.chat(
                f"논문 주제: {topic}\n앞 장의 중심 주장: {prev or '(첫 장)'}\n지금 장: {title or '(제목 없음)'}\n\n[문단들]\n{block}\n\n"
                "1) claim: 이 장이 말하려는 중심 주장을 한 문장으로.\n"
                "2) paragraphs: 문단마다 id(P번호 그대로), topic(그 문단의 중심 내용 한 줄, 20자 안팎), "
                "problem(분명한 문제가 있을 때만 한 문장. 예: 중심 문장이 없어 무엇을 말하려는지 흐림 / 이 장의 주제와 관계가 약함 / "
                "앞 문단과 연결이 끊김 / 앞 내용의 반복. 문제가 없으면 빈 문자열).",
                system=REVIEWER,
                schema=SKEL_SCHEMA,
                num_ctx=8192,
                num_predict=1500,
            )
            claim = claim or (r.get("claim") or "").strip()
            known = {pp for pp, _, _ in part}
            for x in r.get("paragraphs", []):
                xid = (x.get("id") or "").strip().upper()
                if xid in known:
                    items.append({"id": xid, "topic": (x.get("topic") or "").strip(), "problem": (x.get("problem") or "").strip()})
        skeleton.append({"title": title, "claim": claim, "paras": items})
        prev = claim

    # 2단계: 전체 줄기 · 연구문제-결론 일치 · 심사 비판
    if stop():
        return
    log("논문 전체 줄기와 심사 의견 정리 중…")
    sk_text = "\n".join(
        f"## {s['title'] or '(제목 없음)'} — {s['claim']}\n" + "\n".join(f"  {x['id']}: {x['topic']}" for x in s["paras"]) for s in skeleton
    )
    if claude:
        full = "\n\n".join(f"## {title}\n" + "\n".join(f"{pp}: {t}" for pp, _, t in ps) for title, ps in secs if ps)
        extra = f"[원고 전문]\n{full[:120000]}"
        sk_text = sk_text[:40000]
    else:
        rq = [f"{pp}: {t[:600]}" for _, ps in secs for pp, _, t in ps if RQ_RE.search(t)][:4]
        conc = [f"{pp}: {t[:500]}" for title, ps in secs if title and CONC_HEAD.search(title) for pp, _, t in ps]
        extra = "[연구문제가 나오는 문단]\n" + "\n".join(rq) + "\n\n[결론·논의 일부]\n" + "\n".join(conc)[:2500]
        sk_text = sk_text[:9000]
    r = llm.deep(
        f"논문 주제: {topic}\n\n[논문 뼈대: 장별 중심 주장과 문단별 중심 내용]\n{sk_text}\n\n{extra}\n\n"
        "예비심사 위원으로서 다음을 작성하세요.\n"
        "- thread: 이 논문 전체를 관통하는 핵심 주장 한 문장 (원고에서 읽히는 그대로. 줄기가 흐리면 흐리다고 쓰기)\n"
        "- verdict: 일목요연함에 대한 전체 평 2~3문장\n"
        "- strengths: 잘된 점 1~3개 (구체적으로)\n"
        "- rq: 원고의 연구문제마다 question(연구문제), answered(결과·결론에서 답했는지: 예/부분/아니오), "
        "where(답한 곳 P번호, 없으면 빈칸), comment(한 문장)\n"
        "- critiques: 심사에서 실제로 나올 비판·수정 요청 5~10개. 각각 severity(중요/보통/사소), category, "
        "para(가장 관련된 문단 P번호, 논문 전체에 대한 것이면 빈칸), issue(무엇이 문제인지), "
        "fix(어떻게 고치면 되는지 구체적으로), question(심사위원이 할 법한 질문 한 문장). "
        "일목요연함과 연구문제-결론 일치를 먼저 보세요.\n"
        "※ 원고가 아직 결과가 없는 연구계획서(프로포절)라면, rq의 answered는 '연구 설계·분석 방법이 그 연구문제에 답할 수 있게 짜였는지'로 판단하고, "
        "결과·결론이 없다는 것 자체는 비판하지 마세요.",
        system=REVIEWER,
        schema=JUDGE_SCHEMA,
        num_ctx=8192 if claude else 12288,
        num_predict=3000,
    )
    crit = []
    for c in r.get("critiques", []):
        c = {k: (c.get(k) or "").strip() for k in ("severity", "category", "para", "issue", "fix", "question")}
        if not c["issue"]:
            continue
        m = re.search(r"P\d+", c["para"].upper())
        c["para"] = m.group(0) if m and m.group(0) in byid else ""
        crit.append(c)
    order = {"중요": 0, "보통": 1, "사소": 2}
    crit.sort(key=lambda c: order.get(c["severity"], 1))
    # 원고 옆 💬 메모로 달기
    for c in crit:
        if c["para"]:
            _add_note(byid[c["para"]], {"sev": c["severity"], "cat": c["category"], "issue": c["issue"], "fix": c["fix"], "q": c["question"]})
    flow = 0
    for s in skeleton:
        for x in s["paras"]:
            if x["problem"]:
                flow += 1
                _add_note(byid[x["id"]], {"sev": "보통", "cat": "일목요연함", "issue": x["problem"], "fix": "", "q": ""})
    save(pid, soup)
    rqs = []
    for x in r.get("rq", []):
        m = re.search(r"P\d+", (x.get("where") or "").upper())
        rqs.append(x | {"where": m.group(0) if m and m.group(0) in byid else ""})
    data = {
        "thread": r.get("thread", ""),
        "verdict": r.get("verdict", ""),
        "strengths": r.get("strengths", [])[:3],
        "rq": rqs,
        "critiques": crit,
        "skeleton": skeleton,
    }
    n_imp = sum(c["severity"] == "중요" for c in crit)
    save_report(
        pid, "structure", data,
        f"예비심사 리뷰: 심사 의견 {len(crit)}개(중요 {n_imp}) · 흐름이 걸리는 문단 {flow}개 — 원고 옆 💬 메모로도 달았어요",
        log,
    )


TOOLS = {
    "structure": structure_review,
    "cites": check_citations,
    "uncited": uncited_claims,
    "discuss": discussion_boost,
    "recent": recent_research,
    "refs": check_references,
}


def full_review(pid, log, stop=lambda: False):
    for name in ("structure", "refs", "cites", "uncited", "recent", "discuss"):
        if stop():
            break
        if unchanged(pid, name):
            log(f"=== {name}: 원고가 그대로라 지난 결과를 써요 (0원) ===")
            continue
        log(f"=== {name} ===")
        try:
            TOOLS[name](pid, log, stop)
        except Exception as e:
            log(f"  {name} 실패: {e}")


# ---------- AI 사용 기록 · 공개 문구 ----------
FEATURES = {
    "plan": ("연구계획·선행연구 정리", "lit"),
    "route": ("연구 경로 정리", "lit"),
    "collect": ("자료 수집", "lit"),
    "recent": ("최신 연구 찾기", "lit"),
    "autofind": ("인용 근거 찾기", "cite"),
    "cites": ("인용 전체 점검", "cite"),
    "uncited": ("근거 없는 문장 찾기", "cite"),
    "structure": ("예비심사 리뷰(구조 점검)", "struct"),
    "write": ("본문 초안 작성", "draft"),
    "discuss": ("논의 보강 제안", "draft"),
    "discuss_insert": ("AI 제안 문장을 논의에 넣음", "draft"),
    "matrix": ("문헌분석표 만들기", "lit"),
    "gaps": ("연구 동향·공백 분석", "lit"),
    "intro": ("서론 초안 작성", "draft"),
    "meter": ("근거 저울(연구 결과 비교)", "lit"),
    "explain": ("원문 쉽게 설명·번역", "lit"),
    "model": ("연구모형 변인 정리", "struct"),
    "panel": ("심사위원 3인 모의 심사", "struct"),
    "panel_respond": ("모의 심사 재심사", "struct"),
    "style": ("문장 다듬기(교정)", "polish"),
    "aitone": ("AI 문체 점검·문장 수정안", "polish"),
    "journals": ("투고 학술지 추천", "lit"),
    "ask": ("내 자료에 묻기", "lit"),
}
PURPOSE = {
    "lit": ("선행연구 탐색과 정리", "literature search and organization"),
    "cite": ("인용 근거 확인", "verification of citations against source texts"),
    "struct": ("원고의 논리 구조 점검", "checking the logical structure of the manuscript"),
    "draft": ("일부 문장의 초안 작성", "drafting of some sentences"),
    "polish": ("문장 표현 다듬기", "polishing of wording and style"),
}


def ai_usage(pid):
    p, soup = load(pid)
    rows = db.q(
        "SELECT feature, engine, model, COUNT(*) n, MIN(ts) first, MAX(ts) last FROM ai_log WHERE project_id=? GROUP BY feature, engine, model ORDER BY MIN(ts)",
        (pid,),
    )
    for r in rows:
        r["label"] = FEATURES.get(r["feature"], (r["feature"], ""))[0]
    drafts = len(soup.select("p.ai-draft"))
    kinds = [k for k in PURPOSE if any(FEATURES.get(r["feature"], ("", ""))[1] == k for r in rows)]
    tools_ko, tools_en = [], []
    for eng, model in sorted({(r["engine"], r["model"]) for r in rows if r["engine"]}):
        if eng == "claude":
            tools_ko.append(f"Anthropic Claude({model})")
            tools_en.append(f"Anthropic Claude ({model})")
        else:
            tools_ko.append(f"로컬 언어모델 {model}(Ollama)")
            tools_en.append(f"a locally run language model, {model} (Ollama)")
    if not kinds:
        return {"rows": rows, "drafts": drafts, "ko": "", "en": ""}
    used_draft = "draft" in kinds or drafts > 0
    ko = (
        f"본 연구는 {', '.join(PURPOSE[k][0] for k in kinds)}에 인공지능 도구인 {', '.join(tools_ko) or 'AI 도구'}를 보조적으로 활용하였다. "
        "AI가 제시한 문헌과 인용은 모두 연구자가 원문과 대조하여 확인하였다. "
        + ("AI가 작성한 초안은 연구자가 검토하고 수정하였다. " if used_draft else "")
        + ("AI가 제안한 문장 수정안은 연구자가 선택적으로 검토하여 반영하였다. " if "polish" in kinds else "")
        + "연구의 설계, 자료 분석과 해석, 최종 원고의 내용에 대한 책임은 연구자에게 있다."
    )
    en = (
        f"AI tools, namely {' and '.join(tools_en) or 'general-purpose AI tools'}, were used to assist with {', '.join(PURPOSE[k][1] for k in kinds)}. "
        "All literature and citations suggested by AI were checked by the author against the original sources. "
        + ("Drafts produced by AI were reviewed and revised by the author. " if used_draft else "")
        + ("Wording suggestions from AI were reviewed and selectively adopted by the author. " if "polish" in kinds else "")
        + "The author is fully responsible for the study design, data analysis and interpretation, and the content of the final manuscript."
    )
    return {"rows": rows, "drafts": drafts, "ko": ko, "en": en}


# ---------- 심사위원 3인 모의 심사 · 답변서 · 재심사 ----------
VERDICTS = ["통과", "조건부 통과", "수정 후 재심", "불통과"]
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "concerns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["중요", "보통", "사소"]},
                    "where": {"type": "string"},
                    "issue": {"type": "string"},
                    "request": {"type": "string"},
                },
                "required": ["severity", "where", "issue", "request"],
            },
        },
        "decision": {"type": "string", "enum": VERDICTS},
    },
    "required": ["summary", "strengths", "concerns", "decision"],
}
DEBATE_SCHEMA = {
    "type": "object",
    "properties": {
        "agreements": {"type": "array", "items": {"type": "string"}},
        "disagreements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"issue": {"type": "string"}, "r1": {"type": "string"}, "r2": {"type": "string"}, "r3": {"type": "string"}, "resolution": {"type": "string"}},
                "required": ["issue", "r1", "r2", "r3", "resolution"],
            },
        },
        "verdict": {"type": "string", "enum": VERDICTS},
        "verdict_reason": {"type": "string"},
        "required": {
            "type": "array",
            "items": {"type": "object", "properties": {"issue": {"type": "string"}, "by": {"type": "array", "items": {"type": "integer"}}, "why": {"type": "string"}}, "required": ["issue", "by", "why"]},
        },
    },
    "required": ["agreements", "disagreements", "verdict", "verdict_reason", "required"],
}
PANEL_ROLES = [
    ("심사위원 1", "연구방법·통계 전문가", "연구 설계, 표본, 측정도구의 타당도·신뢰도, 분석 방법이 연구문제에 맞는지, 결과 해석이 자료를 넘어서지 않는지를 가장 엄격하게 봅니다."),
    ("심사위원 2", "이 주제 분야의 이론 전문가", "이론적 배경이 탄탄한지, 선행연구와 무엇이 다른지(독창성), 변인 간 관계의 이론적 근거, 학문적·실천적 기여를 봅니다."),
    ("심사위원 3", "지도교수형 심사위원", "논문이 일목요연한지(연구문제→선행연구→방법→결과→결론이 한 줄기인지), 학위논문 요건과 형식, 연구자가 디펜스할 수 있는지를 봅니다."),
]


def _manuscript_text(pid, soup):
    secs = _para_blocks(soup)
    full = "\n\n".join(f"## {t}\n" + "\n".join(f"{k}: {x}" for k, _, x in ps) for t, ps in secs if ps)
    if llm.engine() == "claude":
        return full[:110000]
    row = db.q("SELECT reports FROM projects WHERE id=?", (pid,), one=True) or {}
    sk = (json.loads(row.get("reports") or "{}").get("structure") or {}).get("data", {}).get("skeleton")
    if sk:  # 로컬 AI는 긴 원고를 다 못 읽으므로 예비심사 리뷰의 뼈대를 쓴다
        return "\n".join(f"## {s['title']} — {s['claim']}\n" + "\n".join(f"  {x['id']}: {x['topic']}" for x in s["paras"]) for s in sk)[:9000]
    return full[:7000]


def panel_review(pid, log, stop=lambda: False):
    p, soup = load(pid)
    rep = json.loads(p.get("reports") or "{}")
    target = rep.get("panel_target") or "학위논문 예비심사"
    text = _manuscript_text(pid, soup)
    save(pid, soup)  # 문단 번호(data-rv) 저장
    if len(text) < 300:
        save_report(pid, "panel", {}, "심사할 원고가 너무 짧아요.", log)
        return
    topic = p.get("topic") or p.get("title") or ""
    reviews = []
    for i, (name, role, focus) in enumerate(PANEL_ROLES):
        if stop():
            return
        log(f"[{i + 1}/3] {name}({role}) 심사 중…")
        r = llm.deep(
            f"심사 종류: {target}\n논문 주제: {topic}\n\n[원고 (문단마다 P번호)]\n{text}\n\n"
            f"당신은 {name}, {role}입니다. {focus} 다른 심사위원과 상의하지 말고 독립적으로 심사하세요.\n"
            "- summary: 이 원고에 대한 총평 2~3문장\n- strengths: 강점 1~3개\n"
            "- concerns: 지적사항 4~8개. where(P번호나 장 이름), issue(문제), request(연구자에게 요구하는 수정)\n"
            "- decision: 통과/조건부 통과/수정 후 재심/불통과\n"
            "원고가 결과가 없는 연구계획서라면 결과·결론이 없다는 것 자체는 지적하지 마세요. 근거 없이 칭찬하지 마세요.",
            system=f"당신은 {target}을 여러 번 맡아 본 {role}입니다. 엄격하지만 연구자가 성장하도록 구체적으로 씁니다. 한국어로 답합니다.",
            schema=REVIEW_SCHEMA,
            num_ctx=12288,
            num_predict=2200,
            temperature=0.4,
        )
        reviews.append({"name": name, "role": role, **{k: r.get(k) for k in REVIEW_SCHEMA["properties"]}})
    if stop():
        return
    log("세 심사위원이 의견을 나누고 최종 판정하는 중…")
    block = "\n\n".join(
        f"[{rv['name']} · {rv['role']}] 판정: {rv['decision']}\n총평: {rv['summary']}\n"
        + "\n".join(f"- ({c['severity']}, {c['where']}) {c['issue']} → {c['request']}" for c in rv["concerns"] or [])
        for rv in reviews
    )
    d = llm.deep(
        f"심사 종류: {target}\n논문 주제: {topic}\n\n[세 심사위원의 독립 심사]\n{block}\n\n"
        "심사위원장으로서 세 심사위원의 토론을 정리하세요.\n"
        "- agreements: 세 사람이 공통으로 지적한 점\n"
        "- disagreements: 의견이 갈린 쟁점마다 r1, r2, r3의 입장(한 문장씩, 해당 없으면 의견 없음)과 resolution(위원회의 정리)\n"
        "- verdict: 최종 판정(통과/조건부 통과/수정 후 재심/불통과), verdict_reason: 이유 2문장\n"
        "- required: 연구자가 반드시 고쳐야 할 수정 요구 4~10개 (겹치는 지적은 합치고, by에 제기한 심사위원 번호 1~3)",
        system="당신은 공정한 심사위원장입니다. 한국어로 답합니다.",
        schema=DEBATE_SCHEMA,
        num_ctx=12288,
        num_predict=2200,
    )
    req = [{"id": i + 1, "issue": x.get("issue", ""), "by": x.get("by", []), "why": x.get("why", ""), "response": "", "status": "", "comment": ""}
           for i, x in enumerate(d.get("required", []))]
    data = {"target": target, "reviews": reviews, "agreements": d.get("agreements", []), "disagreements": d.get("disagreements", []),
            "verdict": d.get("verdict", ""), "verdict_reason": d.get("verdict_reason", ""), "required": req, "round": 1, "history": []}
    save_report(pid, "panel", data, f"모의 심사({target}): 최종 판정 '{data['verdict']}' · 수정 요구 {len(req)}개 — 답변서를 쓰면 재심사를 받을 수 있어요", log)


RESPOND_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "status": {"type": "string", "enum": ["해결", "부분 해결", "미해결"]}, "comment": {"type": "string"}}, "required": ["id", "status", "comment"]}},
        "verdict": {"type": "string", "enum": VERDICTS},
        "verdict_reason": {"type": "string"},
    },
    "required": ["items", "verdict", "verdict_reason"],
}


def panel_respond(pid, responses, log, stop=lambda: False):
    p, soup = load(pid)
    rep = json.loads(p.get("reports") or "{}")
    pn = (rep.get("panel") or {}).get("data")
    if not pn or not pn.get("required"):
        raise RuntimeError("먼저 모의 심사를 받아 주세요.")
    for it in pn["required"]:
        it["response"] = (responses.get(str(it["id"])) or it.get("response") or "").strip()
    todo = [it for it in pn["required"] if it["status"] != "해결"]
    if not any(it["response"] for it in todo):
        raise RuntimeError("답변을 하나 이상 적어 주세요.")
    text = _manuscript_text(pid, soup)
    log(f"재심사 중 (수정 요구 {len(todo)}개)…")
    block = "\n\n".join(f"[{it['id']}] 수정 요구: {it['issue']}\n    연구자 답변: {it['response'] or '(답변 없음)'}" for it in todo)
    r = llm.deep(
        f"심사 종류: {pn['target']}\n\n[지난 심사의 수정 요구와 연구자의 답변]\n{block}\n\n[수정된 원고]\n{text}\n\n"
        "심사위원회로서 재심사하세요. 각 항목마다 status(해결/부분 해결/미해결)와 comment(왜 그렇게 판단했는지, 남은 요구가 있으면 구체적으로). "
        "답변만 하고 원고에 반영하지 않았으면 부분 해결 이하로 보세요. 수정하지 않는 이유를 설득력 있게 설명했다면 해결로 볼 수 있습니다. "
        "마지막으로 verdict(최종 판정)와 verdict_reason(2문장).",
        system="당신은 공정하지만 엄격한 심사위원장입니다. 한국어로 답합니다.",
        schema=RESPOND_SCHEMA,
        num_ctx=12288,
        num_predict=1800,
    )
    st = {int(x["id"]): x for x in r.get("items", []) if str(x.get("id", "")).isdigit()}
    pn["history"].append({"round": pn["round"], "verdict": pn["verdict"], "required": [dict(it) for it in pn["required"]]})
    for it in todo:
        if it["id"] in st:
            it["status"], it["comment"] = st[it["id"]].get("status", ""), st[it["id"]].get("comment", "")
    pn["round"] += 1
    pn["verdict"], pn["verdict_reason"] = r.get("verdict", pn["verdict"]), r.get("verdict_reason", "")
    done = sum(1 for it in pn["required"] if it["status"] == "해결")
    save_report(pid, "panel", pn, f"재심사 {pn['round'] - 1}차: 판정 '{pn['verdict']}' · 해결 {done}/{len(pn['required'])}", log)


TOOLS["panel"] = panel_review


# ---------- 문장 다듬기 (비문·시제·과장·중복) ----------
STYLE_CATS = ["비문·호응", "시제·어미", "과장·단정", "중복·장황", "구어체", "맞춤법·띄어쓰기", "용어 일관성"]
STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "excerpt": {"type": "string"},
                    "category": {"type": "string", "enum": STYLE_CATS},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["id", "excerpt", "category", "problem", "fix"],
            },
        }
    },
    "required": ["items"],
}


def style_check(pid, log, stop=lambda: False):
    p, soup = load(pid)
    for el in soup.select("[data-review]"):  # 지난 문장 점검 메모만 지움 (심사 메모는 둠)
        try:
            notes = [n for n in json.loads(el["data-review"]) if n.get("cat") != "문장"]
        except Exception:
            notes = []
        if notes:
            el["data-review"] = json.dumps(notes, ensure_ascii=False)
        else:
            del el["data-review"]
    secs = _para_blocks(soup)
    paras = [(k, el, t) for title, ps in secs if not (title and SKIP_HEAD.search(title) and REF_HEAD.search(title)) for k, el, t in ps]
    byid = {k: el for k, el, _ in paras}
    plan = bool(re.search(r"계획|프로포절|proposal", (p.get("title") or "") + (p.get("topic") or "")))
    cache = _cache_get(pid, "style_cache")  # 문단 지문 → 지난 점검 결과
    found, step = [], 12
    for k, _, t in paras:
        for it in cache.get(para_hash(t), []):
            found.append(it | {"id": k})
    todo = [x for x in paras if para_hash(x[2]) not in cache]
    if len(todo) < len(paras):
        log(f"고친 문단 {len(todo)}개만 점검해요 (나머지 {len(paras) - len(todo)}개는 지난 결과 그대로)")
    paras_all, paras = paras, todo
    for i in range(0, len(paras), step):
        if stop():
            return
        part = paras[i : i + step]
        log(f"[{i // step + 1}/{(len(paras) + step - 1) // step}] 문장 점검 중…")
        r = llm.chat(
            "\n\n".join(f"{k}: {t[:1500]}" for k, _, t in part) + "\n\n"
            "학위논문 원고의 문장을 교정자처럼 점검하세요. 분명한 문제만 고르세요(문단당 0~3개). "
            "분류: " + ", ".join(STYLE_CATS) + ". "
            + ("이 원고는 연구계획서라 아직 하지 않은 일은 '~할 것이다/~할 예정이다'로 써야 합니다. " if plan else "")
            + "'완벽하게', '입증한다', '강력한'처럼 근거보다 센 표현, 같은 개념을 다른 용어로 부르는 것도 짚으세요.\n"
            "각 항목: id(P번호), excerpt(문제 부분을 원문에서 그대로 10~40자), problem(무엇이 문제인지 한 문장), fix(고친 문장 예시).",
            system="당신은 한국어 학술 논문 전문 교정자입니다. 원고의 내용은 바꾸지 않고 문장만 다듬습니다.",
            schema=STYLE_SCHEMA,
            num_ctx=8192,
            num_predict=2000,
        )
        for x in r.get("items", []):
            k = (x.get("id") or "").strip().upper()
            if k in byid and x.get("problem"):
                found.append({"id": k, "excerpt": x.get("excerpt", ""), "category": x.get("category", ""), "problem": x["problem"], "fix": x.get("fix", "")})
        for k, _, t in part:  # 점검한 문단은 문제가 없어도 기록 (다음에 건너뛰기)
            cache[para_hash(t)] = [{kk: v for kk, v in f.items() if kk != "id"} for f in found if f["id"] == k]
        _cache_put(pid, "style_cache", cache)
    paras = paras_all
    for f in found:
        _add_note(byid[f["id"]], {"sev": "사소", "cat": "문장", "issue": f"[{f['category']}] “{f['excerpt']}” — {f['problem']}", "fix": f["fix"], "q": ""})
    save(pid, soup)
    cnt = {}
    for f in found:
        cnt[f["category"]] = cnt.get(f["category"], 0) + 1
    save_report(pid, "style", found, "문장 다듬기: " + (" · ".join(f"{k} {v}" for k, v in cnt.items()) or "고칠 문장이 없어요") + " — 원고 옆 💬 메모로도 달았어요", log)


TOOLS["style"] = style_check


# ---------- AI 문체 점검 (AI가 쓴 티 찾기 · 자연스러운 수정안) ----------
AI_PATTERNS = [
    "문장 길이·구조 균일", "헤지어 과다", "기계적 전환어", "추상적 상투어", "셋씩 나열", "요약·정리 문장 반복",
    "같은 구문 반복", "대칭 구조 남용", "구체성 없는 일반화", "불필요한 메타 진술", "명사화 과다", "수식어 과다", "근거 없는 포괄 진술",
]
HEDGE_RE = re.compile(r"할 수 있(다|을 것이다)|것으로 보인다|것으로 판단된다|라고 할 수 있다|가능성이 있다|것으로 사료된다")
TRANS_RE = re.compile(r"^(또한|나아가|더불어|이러한 맥락에서|이와 같이|이처럼|따라서|결론적으로|요컨대|아울러|특히)")
AITONE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "sentence": {"type": "string"},
                    "score": {"type": "integer"},
                    "patterns": {"type": "array", "items": {"type": "string", "enum": AI_PATTERNS}},
                    "reason": {"type": "string"},
                    "rewrite": {"type": "string"},
                },
                "required": ["id", "sentence", "score", "patterns", "reason", "rewrite"],
            },
        }
    },
    "required": ["items"],
}


def _sents(t):
    return [s.strip() for s in re.split(r"(?<=[다요음함됨]\.)\s+|(?<=[.?!])\s+(?=[A-Z가-힣])", t) if len(s.strip()) >= 12]


def _tone_stats(paras):
    """AI 없이 세는 지표: 문장 길이의 고르기, 헤지어·전환어 비율."""
    sents = [s for _, _, t in paras for s in _sents(t)]
    if not sents:
        return {}
    lens = [len(s) for s in sents]
    mean = sum(lens) / len(lens)
    sd = (sum((x - mean) ** 2 for x in lens) / len(lens)) ** 0.5
    return {
        "sentences": len(sents),
        "len_mean": round(mean),
        "len_cv": round(sd / mean, 2) if mean else 0,  # 낮을수록 문장 길이가 기계적으로 고름
        "hedge": round(sum(1 for s in sents if HEDGE_RE.search(s)) / len(sents) * 100),
        "trans": round(sum(1 for s in sents if TRANS_RE.search(s)) / len(sents) * 100),
    }


def ai_tone(pid, log, stop=lambda: False):
    p, soup = load(pid)
    for el in soup.select("[data-review]"):  # 지난 AI 문체 메모만 지움
        try:
            notes = [n for n in json.loads(el["data-review"]) if n.get("cat") != "AI 문체"]
        except Exception:
            notes = []
        if notes:
            el["data-review"] = json.dumps(notes, ensure_ascii=False)
        else:
            del el["data-review"]
    paras = [(k, el, t) for _, ps in _para_blocks(soup) for k, el, t in ps]
    byid = {k: el for k, el, _ in paras}
    stats = _tone_stats(paras)
    cache = _cache_get(pid, "aitone_cache")
    found, step = [], 10
    for k, _, t in paras:
        for it in cache.get(para_hash(t), []):
            found.append(it | {"id": k})
    todo = [x for x in paras if para_hash(x[2]) not in cache]
    if len(todo) < len(paras):
        log(f"고친 문단 {len(todo)}개만 점검해요 (나머지 {len(paras) - len(todo)}개는 지난 결과 그대로)")
    paras_all, paras = paras, todo
    for i in range(0, len(paras), step):
        if stop():
            return
        part = paras[i : i + step]
        log(f"[{i // step + 1}/{(len(paras) + step - 1) // step}] 문장마다 AI 문체 점검 중…")
        r = llm.chat(
            "\n\n".join(f"{k}: {t[:1600]}" for k, _, t in part) + "\n\n"
            "각 문장이 AI가 쓴 글처럼 보이는 정도를 판단하세요. 기준 13가지: " + ", ".join(AI_PATTERNS) + ".\n"
            "AI처럼 보이는 정도(score)가 40 이상인 문장만 고르세요(점수 0~100). 각 항목: id(P번호), sentence(원문 문장 그대로), score, "
            "patterns(해당 기준), reason(왜 AI처럼 보이는지 한 문장), rewrite(뜻·사실·인용 (저자, 연도)는 그대로 두고, 연구자가 직접 쓴 것처럼 "
            "구체적이고 자연스러운 한국어 학술 문장으로 고친 것. 없는 사실·수치를 더하지 마세요).",
            system="당신은 한국어 학술 글쓰기 교정 전문가입니다. 기계적인 문체를 사람의 문체로 다듬는 일을 돕습니다.",
            schema=AITONE_SCHEMA,
            num_ctx=8192,
            num_predict=2600,
            temperature=0.3,
        )
        for x in r.get("items", []):
            k = (x.get("id") or "").strip().upper()
            sc = max(0, min(100, int(x.get("score") or 0)))
            if k in byid and sc >= 40 and x.get("sentence"):
                found.append({"id": k, "sentence": x["sentence"], "score": sc, "label": "AI" if sc >= 70 else "의심",
                              "patterns": x.get("patterns", []), "reason": x.get("reason", ""), "rewrite": x.get("rewrite", "")})
        for k, _, t in part:
            cache[para_hash(t)] = [{kk: v for kk, v in f.items() if kk != "id"} for f in found if f["id"] == k]
        _cache_put(pid, "aitone_cache", cache)
    paras = paras_all
    for f in found:
        _add_note(byid[f["id"]], {"sev": "중요" if f["label"] == "AI" else "보통", "cat": "AI 문체",
                                  "issue": f"[AI {f['score']}%] “{f['sentence'][:80]}” — {f['reason']}", "fix": f["rewrite"], "q": ""})
    save(pid, soup)
    total = sum(len(t) for _, _, t in paras) or 1
    ai_chars = sum(len(f["sentence"]) for f in found if f["label"] == "AI")
    sus_chars = sum(len(f["sentence"]) for f in found if f["label"] == "의심")
    data = {"items": sorted(found, key=lambda f: -f["score"]), "stats": stats,
            "ai_pct": round(ai_chars / total * 100), "sus_pct": round(sus_chars / total * 100)}
    save_report(pid, "aitone", data,
                f"AI 문체 점검: AI로 보이는 문장 {sum(f['label'] == 'AI' for f in found)}개({data['ai_pct']}%) · 의심 {sum(f['label'] == '의심' for f in found)}개({data['sus_pct']}%) — 원고 옆 💬 메모에 수정안", log)


TOOLS["aitone"] = ai_tone



# ---------- 실행 전 예상 금액 ----------
def estimates(pid):
    p = db.q("SELECT content, matrix FROM projects WHERE id=?", (pid,), one=True)
    soup = BeautifulSoup(p["content"] or "", "html.parser")
    n_cites = len(soup.select("span.cite"))
    n_need = len(soup.select("span.cite:not([data-chunk])"))
    for c in soup.select("span.cite"):
        c.decompose()
    chars = len(soup.get_text())
    T = int(chars * 0.8)  # 한국어 글자 수 → 토큰 수 어림
    paras = max(1, len([x for x in soup.find_all(["p", "li"]) if len(x.get_text(strip=True)) >= 25]))
    rows = len(json.loads(p.get("matrix") or "{}"))
    E = llm.estimate_won
    est = {
        "structure": E("standard", int(T * 1.1), int(paras * 40)) + E("deep", int(T * 1.3), 3000),
        "panel": E("deep", int(T * 3.3), 9000),
        "style": E("standard", int(T * 1.2), max(1, paras // 12) * 1500),
        "aitone": E("standard", int(T * 1.2), max(1, paras // 10) * 2000),
        "cites": E("standard", n_cites * 5000, n_cites * 500),
        "uncited": E("standard", int(T * 1.1), 2500),
        "discuss": E("standard", 20000, 5000),
        "recent": E("light", 3000, 200),
        "refs": 0,
        "autoall": E("standard", n_need * 6000, n_need * 500),
        "matrix_per_paper": E("standard", 6500, 1500),
        "gaps": E("deep", rows * 700 + 1500, 3000),
        "intro": E("deep", rows * 800 + 3000, 2500) + E("standard", 6000 * 2, 400),
        "meter": E("standard", rows * 900 + 1000, 2000),
        "ask": E("standard", 7000, 1200),
        "explain": E("standard", 2500, 600),
        "scan_per_pdf": E("light", 2000, 200),
    }
    eng = llm.engine_for(pid)
    sub = eng == "claude" and llm.via_subscription()
    return {"engine": eng, "sub": sub, "won": est, "month": llm.usage_this_month(), "cap": int(db.get_settings().get("monthly_cap_won") or 0)}
