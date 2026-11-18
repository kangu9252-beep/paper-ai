"""PDF를 읽어 문단 조각으로 나누고, 각 줄의 위치(좌표)까지 저장한다.
그래야 나중에 '이 문장이 논문 몇 쪽 어디에서 왔는지'를 형광펜으로 보여줄 수 있다."""
import difflib
import json
import math
import os
import re
import threading
from pathlib import Path

import numpy as np
import pymupdf

from . import db, llm

CHUNK_CHARS = 700
_emb_cache = {"ids": None, "papers": None, "mat": None}
_emb_lock = threading.Lock()


# ---------- 추출 ----------
REF_HEAD = re.compile(r"^\s*(references?|bibliography|works cited|literature cited|참\s*고\s*문\s*헌|인용\s*문헌)\s*$", re.I)


def is_reflist(text):
    """참고문헌 목록 조각인지 (연도·doi가 빽빽함). 근거로 쓰면 안 되므로 색인에서 뺀다."""
    years = len(re.findall(r"\(?(?:19|20)\d{2}[a-z]?\)?[.,]", text))
    dois = len(re.findall(r"doi|https?://", text, re.I))
    return years / max(1, len(text) / 100) > 0.9 or dois >= 3


OCR_DPI = 200


def _ocr_lines(page):
    """스캔본 쪽: Windows 내장 글자 인식(한국어)으로 줄과 위치를 얻는다. 못 하면 []."""
    try:
        import io as _io

        import winocr
        from PIL import Image
    except Exception:
        return []
    pix = page.get_pixmap(dpi=OCR_DPI)
    try:
        r = winocr.recognize_pil_sync(Image.open(_io.BytesIO(pix.tobytes("png"))), "ko")
    except Exception:
        return []
    k = 72 / OCR_DPI
    out, prev = [], None
    for ln in r.get("lines", []):
        ws = [w["bounding_rect"] for w in ln.get("words", [])]
        t = (ln.get("text") or "").strip()
        if not ws or not t:
            continue
        x0 = min(w["x"] for w in ws) * k
        y0 = min(w["y"] for w in ws) * k
        x1 = max(w["x"] + w["width"] for w in ws) * k
        y1 = max(w["y"] + w["height"] for w in ws) * k
        if prev and (y0 < prev[1] - 2 or y0 - prev[3] > (prev[3] - prev[1]) * 1.2):
            out.append(None)  # 단 바뀜이나 문단 사이 간격 → 블록 경계
        out.append((t, (x0, y0, x1, y1)))
        prev = (x0, y0, x1, y1)
    return out


def extract_chunks(path):
    doc = pymupdf.open(path)
    out, in_refs = [], False
    for pno, page in enumerate(doc):
        if in_refs:
            break
        pw, ph = page.rect.width, page.rect.height
        lines = []  # (text, rect)
        for b in page.get_text("dict", sort=True)["blocks"]:
            if b.get("type") != 0:
                continue
            blines = []
            for ln in b["lines"]:
                t = "".join(s["text"] for s in ln["spans"]).strip()
                if t:
                    blines.append((t, ln["bbox"]))
            btxt = " ".join(t for t, _ in blines)
            if len(btxt) < 4:  # 쪽번호 같은 잡음
                continue
            if REF_HEAD.match(btxt) and pno >= len(doc) * 0.4:  # 여기부터 참고문헌 목록
                in_refs = True
                break
            lines.extend(blines)
            lines.append(None)  # 블록 경계
        if not in_refs and sum(len(x[0]) for x in lines if x) < 30 and page.get_images():
            ocr = _ocr_lines(page)  # 글자가 없는 스캔본 쪽
            for item in ocr:
                if item and REF_HEAD.match(item[0]) and pno >= len(doc) * 0.4:
                    in_refs = True
                    break
                lines.append(item)
        cur, cur_len = [], 0
        for item in lines + [None]:
            if item is None:
                if cur_len >= CHUNK_CHARS * 0.6:
                    out.append(_mk(pno, pw, ph, cur))
                    cur, cur_len = [], 0
                continue
            cur.append(item)
            cur_len += len(item[0]) + 1
            if cur_len >= CHUNK_CHARS * 1.4:
                out.append(_mk(pno, pw, ph, cur))
                cur, cur_len = [], 0
        if cur:
            out.append(_mk(pno, pw, ph, cur))
    n = len(doc)
    doc.close()
    return [c for c in out if not is_reflist(c["text"])], n


def _mk(pno, pw, ph, items):
    text, lines = "", []
    for t, r in items:
        if text:
            if text.endswith("-") and t[:1].islower():
                text = text[:-1]
            else:
                text += " "
        lines.append([len(text)] + [round(v, 1) for v in r])
        text += t
    return {"page": pno, "pw": pw, "ph": ph, "text": text, "lines": lines}


DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;]+)", re.I)


NAME_RE = re.compile(
    r"^(?:\[[^\]]*\]\s*)?(?:\d{1,3}[\s._-]+)?"  # [IV] 같은 꼬리표, 02 같은 번호
    r"(?P<a>[가-힣A-Za-z][^()\[\]]{0,60}?)\s*\(\s*(?P<y>(?:19|20)\d{2})[a-z]?\s*\)\s*(?P<t>.*)$"
)


NOTE_RE = re.compile(r"α|문항|\d점|공개본|번안|원척도|척도\s*원문|\bitem", re.I)  # 파일 이름이 제목이 아니라 메모일 때
# 내 원고·과제·결과지 등 근거로 쓰면 안 되는 파일
SKIP_NAME_RE = re.compile(r"카피킬러|결과확인서|수업계획서|랩미팅|제출용|문헌분석표|참고문헌\s*정리|중간고사|기말|초안|^20\d{6}[\s_]")


def meta_from_name(path):
    """'02 김미량·김재운 외(2010) 진지한 여가척도….pdf' 처럼 파일 이름에 저자(연도) 제목이 있으면 그대로 쓴다."""
    m = NAME_RE.match(Path(path).stem.strip())
    if not m:
        return None
    raw = re.sub(r"\s*(외|등|et\s*al\.?)\s*$", "", m.group("a").strip(" ,_-"))
    authors = [a.strip() for a in re.split(r"\s*(?:·|,|&|\band\b)\s*", raw) if a.strip()]
    if not authors or any(len(a) > 30 for a in authors):
        return None
    title = re.sub(r"\s*\[[^\]]*\]\.*\s*$", "", m.group("t")).strip(" ._-") or Path(path).stem
    return {"title": title, "authors": authors[:10], "year": m.group("y"), "doi": ""}


def guess_meta(path, chunks):
    first = " ".join(c["text"] for c in chunks if c["page"] < 2)[:6000]
    m = DOI_RE.search(first)
    if m:
        from . import web

        doi = m.group(1).rstrip(".)]")
        meta = web.openalex_by_doi(doi)
        if meta:
            return meta
    named = meta_from_name(path)
    if named and not NOTE_RE.search(named["title"]):  # 파일 이름이 정보를 주면 AI에게 묻지 않음 (빠르고 정확)
        return named
    head = " ".join(c["text"] for c in chunks if c["page"] == 0)[:2000]
    if len(head) > 100:
        try:
            r = llm.chat(
                "다음은 학술 논문 PDF 첫 쪽의 텍스트입니다. 논문 제목, 저자 이름 목록(원문 표기 그대로), 출판 연도를 뽑아주세요. "
                "모르면 빈 값으로 두세요.\n\n" + head,
                schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "authors": {"type": "array", "items": {"type": "string"}},
                        "year": {"type": "string"},
                    },
                    "required": ["title", "authors", "year"],
                },
                num_ctx=4096,
            )
            yr = re.search(r"(19|20)\d{2}", str(r.get("year", "")))
            if named:  # 저자·연도는 파일 이름이 더 정확, 제목만 AI가 읽은 것
                return named | {"title": r.get("title") or named["title"]}
            return {
                "title": r.get("title") or Path(path).stem,
                "authors": [a for a in r.get("authors", []) if a][:20],
                "year": yr.group(0) if yr else "",
                "doi": "",
            }
        except Exception:
            pass
    if named:  # AI가 실패해도 파일 이름의 저자·연도는 살린다
        return named
    return {"title": Path(path).stem, "authors": [], "year": "", "doi": ""}


def index_pdf(path, meta=None, source="local", log=print):
    path = str(Path(path).resolve())
    mtime = os.path.getmtime(path)
    old = db.q("SELECT id, mtime, title, authors, year, doi, venue, status FROM papers WHERE path=?", (path,), one=True)
    if old and meta is None and old["title"] and old["status"] == "ok":  # 다시 읽을 때 사용자가 고친 인용 정보 유지
        meta = {**old, "authors": json.loads(old["authors"] or "[]")}
    if old and abs((old["mtime"] or 0) - mtime) < 1 and old["status"] != "notext":  # notext: 글자 인식 전 스캔본 → 다시 읽기
        return old["id"]
    try:
        chunks, npages = extract_chunks(path)
    except Exception as e:
        log(f"  읽기 실패: {Path(path).name} ({e})")
        return None
    total = sum(len(c["text"]) for c in chunks)
    status = "ok" if total > 300 else "noocr"  # noocr: 글자 인식으로도 못 읽음
    if meta is None:
        meta = guess_meta(path, chunks) if status == "ok" else (meta_from_name(path) or {"title": Path(path).stem, "authors": [], "year": "", "doi": ""})
    if old:
        pid = old["id"]
        db.x("DELETE FROM chunks WHERE paper_id=?", (pid,))
        db.x(
            "UPDATE papers SET title=?, authors=?, year=?, doi=?, venue=?, status=?, pages=?, mtime=? WHERE id=?",
            (meta.get("title"), json.dumps(meta.get("authors", []), ensure_ascii=False), meta.get("year", ""),
             meta.get("doi", ""), meta.get("venue", ""), status, npages, mtime, pid),
        )
    else:
        pid = db.x(
            "INSERT INTO papers(path,title,authors,year,doi,venue,source,status,pages,mtime) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (path, meta.get("title"), json.dumps(meta.get("authors", []), ensure_ascii=False), meta.get("year", ""),
             meta.get("doi", ""), meta.get("venue", ""), source, status, npages, mtime),
        )
    if status != "ok":
        log(f"  글자를 읽을 수 없는 PDF(스캔본일 수 있음): {Path(path).name}")
        return pid
    rows = []
    for i in range(0, len(chunks), 32):
        part = chunks[i : i + 32]
        vec = llm.embed([c["text"] for c in part])
        for j, c in enumerate(part):
            emb = vec[j].tobytes() if vec is not None else None
            rows.append((pid, c["page"], c["text"], json.dumps(c["lines"]), c["pw"], c["ph"], emb))
    db.xmany("INSERT INTO chunks(paper_id,page,text,lines,pw,ph,emb) VALUES(?,?,?,?,?,?,?)", rows)
    invalidate()
    return pid


def is_own_draft(fn, mine):
    """내 논문 프로젝트와 제목이 같은 PDF는 내 원고(초안·결과본)로 보고 근거에서 뺀다."""
    k = re.sub(r"\W", "", Path(fn).stem)
    return SKIP_NAME_RE.search(fn) or any(t and t in k for t in mine)


def scan_folders(folders, log=print, stop=lambda: False):
    mine = {re.sub(r"\W", "", r["t"] or "")[:18] for r in db.q("SELECT title t FROM projects UNION SELECT topic FROM projects")}
    mine = {t for t in mine if len(t) >= 12}
    found = []
    for f in folders:
        if not os.path.isdir(f):
            continue
        for dirpath, dirnames, files in os.walk(f):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "AppData", ".venv")]
            for fn in files:
                if fn.lower().endswith(".pdf") and not is_own_draft(fn, mine):
                    p = os.path.join(dirpath, fn)
                    try:
                        if os.path.getsize(p) < 80 * 1024 * 1024:
                            found.append(p)
                    except OSError:
                        pass
    found = list(dict.fromkeys(str(Path(p).resolve()) for p in found))
    known = {r["path"]: r["mtime"] for r in db.q("SELECT path, mtime FROM papers")}
    retry = {r["path"] for r in db.q("SELECT path FROM papers WHERE status='notext'")}  # 스캔본 → 글자 인식으로 다시
    todo = [p for p in found if p not in known or p in retry or abs(known[p] - os.path.getmtime(p)) >= 1]
    # 파일 이름에 저자(연도)가 있는 논문 먼저 (AI 없이 빨리 읽힘) → 바로 검색되게 한 뒤 나머지
    quick = lambda p: (m := meta_from_name(p)) is not None and not NOTE_RE.search(m["title"])
    todo.sort(key=lambda p: not quick(p))
    n_quick = sum(1 for p in todo if quick(p))
    log(f"PDF {len(found)}개 발견, 새로 읽을 것 {len(todo)}개 (이름에 저자·연도가 있는 {n_quick}개 먼저)")
    for i, p in enumerate(todo):
        if i == n_quick and n_quick:
            log("먼저 읽은 논문을 검색할 수 있게 정리하는 중…")
            embed_missing(log=log)
        if stop():
            break
        log(f"[{i + 1}/{len(todo)}] {Path(p).name}")
        index_pdf(p, log=log)
    # 지워진 파일 정리 — 외장하드가 빠져 있을 때는 지우지 않음
    offline = [f.lower() for f in folders if not os.path.isdir(f)]
    for path in known:
        if os.path.exists(path) or not os.path.exists(Path(path).anchor) or any(path.lower().startswith(f) for f in offline):
            continue
        r = db.q("SELECT id FROM papers WHERE path=?", (path,), one=True)
        db.x("DELETE FROM chunks WHERE paper_id=?", (r["id"],))
        db.x("DELETE FROM papers WHERE id=?", (r["id"],))
    invalidate()
    return len(todo)


def embed_missing(log=print):
    rows = db.q("SELECT id, text FROM chunks WHERE emb IS NULL")
    if not rows:
        return
    llm.reset_embed_check()
    log(f"검색용 벡터가 없는 조각 {len(rows)}개 처리")
    for i in range(0, len(rows), 32):
        part = rows[i : i + 32]
        vec = llm.embed([r["text"] for r in part])
        if vec is None:
            log("검색 모델(bge-m3)을 쓸 수 없어 건너뜀")
            return
        db.xmany("UPDATE chunks SET emb=? WHERE id=?", [(vec[j].tobytes(), r["id"]) for j, r in enumerate(part)])
    invalidate()


# ---------- 검색 ----------
def invalidate():
    with _emb_lock:
        _emb_cache["ids"] = None


def _load_emb():
    with _emb_lock:
        if _emb_cache["ids"] is None:
            rows = db.q("SELECT id, paper_id, emb FROM chunks WHERE emb IS NOT NULL")
            if rows:
                _emb_cache["ids"] = np.array([r["id"] for r in rows])
                _emb_cache["papers"] = np.array([r["paper_id"] for r in rows])
                _emb_cache["mat"] = np.vstack([np.frombuffer(r["emb"], dtype=np.float32) for r in rows])
            else:
                _emb_cache["ids"] = np.array([], dtype=int)
                _emb_cache["papers"] = np.array([], dtype=int)
                _emb_cache["mat"] = None
        return _emb_cache["ids"], _emb_cache["papers"], _emb_cache["mat"]


JOSA = re.compile(r"(으로써|으로서|에서는|에게서|으로|에서|에게|까지|부터|이라|라는|하는|하여|하고|은|는|이|가|을|를|의|에|도|와|과|로|만)$")


def tokens(text):
    out = []
    for w in re.findall(r"[가-힣]{2,}|[A-Za-z][A-Za-z\-]{2,}", text):
        if re.match(r"[가-힣]", w) and len(w) >= 3:
            w = JOSA.sub("", w) or w
        if len(w) >= 2 and w.lower() not in STOP:
            out.append(w.lower())
    return list(dict.fromkeys(out))[:12]


STOP = set("the and for with that this from are was were have has been which their there these those into than also such using use used study research 연구 대한 있다 있는 통해 위해 따라 그리고 하지만 또한 이러한 것으로 것이 대해".split())


def search(query, k=10, paper_ids=None, per_paper=3):
    where, args = "", []
    if paper_ids:
        where = f" AND paper_id IN ({','.join('?' * len(paper_ids))})"
        args = list(paper_ids)
    total = db.q("SELECT COUNT(*) n FROM chunks WHERE 1=1" + where, args, one=True)["n"] or 1
    scores = {}
    for t in tokens(query):
        hits = db.q(f"SELECT id FROM chunks WHERE lower(text) LIKE ?{where} LIMIT 5000", ["%" + t + "%"] + args)
        if not hits or len(hits) > total * 0.5 and total > 20:
            continue
        idf = math.log(1 + total / len(hits))
        for h in hits:
            scores[h["id"]] = scores.get(h["id"], 0) + idf
    kw_max = max(scores.values()) if scores else 1
    comb = {cid: 0.35 * s / kw_max for cid, s in scores.items()}
    ids, papers, mat = _load_emb()
    if mat is not None and len(ids):
        qv = llm.embed([query])
        if qv is not None:
            sims = mat @ qv[0]
            if paper_ids:
                mask = np.isin(papers, list(paper_ids))
                sims = np.where(mask, sims, -1)
            top = np.argsort(-sims)[: k * 8]
            for i in top:
                if sims[i] <= 0:
                    continue
                cid = int(ids[i])
                comb[cid] = comb.get(cid, 0) + float(sims[i])
    ranked = sorted(comb.items(), key=lambda kv: -kv[1])
    out, count, seen = [], {}, set()
    for cid, sc in ranked:
        c = db.q(
            "SELECT c.id, c.paper_id, c.page, c.text, p.title, p.authors, p.year FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id=?",
            (cid,), one=True,
        )
        if not c:
            continue
        if count.get(c["paper_id"], 0) >= per_paper:
            continue
        key = re.sub(r"\s+", "", c["text"])[:300]
        if key in seen:  # 같은 PDF가 여러 폴더에 있을 때 같은 문단은 한 번만
            continue
        seen.add(key)
        count[c["paper_id"]] = count.get(c["paper_id"], 0) + 1
        c["score"] = round(sc, 3)
        c["authors"] = json.loads(c["authors"] or "[]")
        c["cite"] = cite_label(c["authors"], c["year"])
        out.append(c)
        if len(out) >= k:
            break
    return out


# ---------- 인용 표기 ----------
def _surname(name):
    name = name.strip()
    if re.match(r"[가-힣]", name):
        return name.replace(" ", "")
    if "," in name:
        return name.split(",")[0].strip()
    parts = name.split()
    return parts[-1] if parts else name


def cite_label(authors, year):
    yr = year or "연도미상"
    if not authors:
        return f"(저자미상, {yr})"
    ko = bool(re.match(r"[가-힣]", authors[0]))
    names = [_surname(a) for a in authors]
    if len(names) == 1:
        a = names[0]
    elif len(names) == 2:
        a = f"{names[0]}, {names[1]}" if ko else f"{names[0]} & {names[1]}"
    else:
        a = f"{names[0]} 외" if ko else f"{names[0]} et al."
    return f"({a}, {yr})"


def find_papers_by_cite(author, year):
    """'(이기태, 2026)' 같은 표기에서 해당 논문 후보를 라이브러리에서 찾는다."""
    author = re.sub(r"\s*(외|et al\.?|등)\s*$", "", author.strip())
    first = re.split(r"\s*(?:,|&|and|·)\s*", author)[0].strip()
    out = []
    for p in db.q("SELECT id, title, authors, year, path FROM papers WHERE status='ok'"):
        if year and p["year"] and p["year"][:4] != year[:4]:
            continue
        if year and not p["year"] and year[:4] not in Path(p["path"]).stem:  # 연도를 모르는 논문은 파일 이름의 연도라도 맞아야
            continue
        auths = json.loads(p["authors"] or "[]")
        blob = " ".join(auths) + " " + Path(p["path"]).stem
        if first and first.lower() in blob.lower():
            p["authors"] = auths
            out.append(p)
    return out


# ---------- 근거 위치 ----------
def _norm(s):
    chars, mp = [], []
    prev_space = True
    for i, ch in enumerate(s):
        if ch.isspace() or ch in "-‐–—":
            if not prev_space:
                chars.append(" ")
                mp.append(i)
            prev_space = True
            continue
        chars.append(ch.lower())
        mp.append(i)
        prev_space = False
    return "".join(chars), mp


def locate(text, quote):
    """quote가 text 안 어디에 있는지 (시작, 끝, 일치도). 없으면 None."""
    if not quote or not text:
        return None
    nt, mp = _norm(text)
    nq, _ = _norm(quote)
    nq = nq.strip()
    if len(nq) < 6:
        return None
    i = nt.find(nq)
    if i >= 0:
        return mp[i], mp[min(i + len(nq) - 1, len(mp) - 1)] + 1, 1.0
    sm = difflib.SequenceMatcher(None, nt, nq, autojunk=False)
    m = sm.find_longest_match(0, len(nt), 0, len(nq))
    ratio = m.size / len(nq)
    if m.size >= 12 and ratio >= 0.55:
        s = max(0, m.a - m.b)
        e = min(len(nt), s + len(nq))
        return mp[s], mp[e - 1] + 1, round(ratio, 2)
    return None


def evidence(chunk_id, quote=""):
    c = db.q(
        "SELECT c.*, p.title, p.authors, p.year, p.path, p.pages, p.doi FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id=?",
        (chunk_id,), one=True,
    )
    if not c:
        return None
    lines = json.loads(c["lines"])
    loc = locate(c["text"], quote)
    hl, qs, qe = [], None, None
    if loc:
        qs, qe = loc[0], loc[1]
    for i, ln in enumerate(lines):
        start = ln[0]
        end = lines[i + 1][0] if i + 1 < len(lines) else len(c["text"])
        strong = loc is not None and start < qe and end > qs
        hl.append({"r": ln[1:], "strong": strong})
    c.pop("emb", None)
    c.pop("lines", None)
    c["authors"] = json.loads(c["authors"] or "[]")
    c["cite"] = cite_label(c["authors"], c["year"])
    c["hl"] = hl
    c["quote_range"] = [qs, qe] if loc else None
    c["match"] = loc[2] if loc else 0
    return c


def render_page(paper_id, page, zoom=1.6):
    p = db.q("SELECT path FROM papers WHERE id=?", (paper_id,), one=True)
    if not os.path.exists(p["path"]):
        raise FileNotFoundError(f"PDF 파일을 찾을 수 없어요. 외장하드에 있는 논문이면 외장하드를 연결해 주세요: {p['path']}")
    doc = pymupdf.open(p["path"])
    pg = doc[max(0, min(page, len(doc) - 1))]
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    data = pix.tobytes("png")
    w, h = pg.rect.width, pg.rect.height
    doc.close()
    return data, w, h
