"""인터넷 조사: OpenAlex(전 세계 학술 논문 2억여 건 공개 색인)에서 찾고, 무료 공개 PDF를 내려받는다.
무료가 아닌 논문은 대학 도서관 로그인(library.py)을 거쳐 시도한다."""
import re
import threading
from pathlib import Path

import httpx

from . import db

OA = "https://api.openalex.org"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
_lock = threading.Lock()


def _abstract(inv):
    if not inv:
        return ""
    pos = {}
    for w, idx in inv.items():
        for i in idx:
            pos[i] = w
    return " ".join(pos[i] for i in sorted(pos))[:1500]


def _work(w):
    oa = w.get("best_oa_location") or {}
    pdf = oa.get("pdf_url") or ""
    land = (w.get("open_access") or {}).get("oa_url") or ""
    src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    return {
        "openalex": w.get("id", ""),
        "title": w.get("display_name") or "",
        "authors": [a["author"]["display_name"] for a in w.get("authorships", []) if a.get("author")][:20],
        "year": str(w.get("publication_year") or ""),
        "doi": doi,
        "venue": src,
        "volume": (w.get("biblio") or {}).get("volume") or "",
        "issue": (w.get("biblio") or {}).get("issue") or "",
        "first_page": (w.get("biblio") or {}).get("first_page") or "",
        "last_page": (w.get("biblio") or {}).get("last_page") or "",
        "cited": w.get("cited_by_count", 0),
        "is_oa": bool((w.get("open_access") or {}).get("is_oa")),
        "pdf_url": pdf,
        "oa_url": land,
        "abstract": _abstract(w.get("abstract_inverted_index")),
        "lang": w.get("language") or "",
    }


def openalex_search(query, n=20, oa_only=False, year_from=None):
    filt = []
    if oa_only:
        filt.append("is_oa:true")
    if year_from:
        filt.append(f"from_publication_date:{year_from}-01-01")
    params = {"search": query, "per_page": min(n, 50)}
    if filt:
        params["filter"] = ",".join(filt)
    try:
        r = httpx.get(OA + "/works", params=params, headers=UA, timeout=30)
        r.raise_for_status()
        return [_work(w) for w in r.json().get("results", [])]
    except Exception:
        return []


def openalex_by_doi(doi):
    try:
        r = httpx.get(f"{OA}/works/https://doi.org/{doi}", headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        return _work(r.json())
    except Exception:
        return None


def _fname(meta):
    from .pdfindex import _surname

    a = _surname(meta["authors"][0]) if meta.get("authors") else "unknown"
    t = re.sub(r"[^\w가-힣]+", "_", meta.get("title", ""))[:50].strip("_")
    return f"{meta.get('year') or 'nd'}_{a}_{t}.pdf".replace(" ", "")


def _save(content, meta):
    from . import users

    path = users.papers_dir() / _fname(meta)
    i = 1
    while path.exists():
        path = path.with_name(path.stem + f"_{i}.pdf")
        i += 1
    path.write_bytes(content)
    return path


def download_open(meta, log=print):
    """무료 공개 PDF 내려받기. 성공하면 저장 경로."""
    urls = [u for u in (meta.get("pdf_url"), meta.get("oa_url")) if u]
    for url in urls:
        try:
            with httpx.Client(headers=UA, follow_redirects=True, timeout=httpx.Timeout(40, connect=10)) as c:
                r = c.get(url)
                if r.content[:5] == b"%PDF-":
                    return _save(r.content, meta)
                m = re.search(r'name="citation_pdf_url"\s+content="([^"]+)"', r.text or "")
                if m:
                    r2 = c.get(m.group(1))
                    if r2.content[:5] == b"%PDF-":
                        return _save(r2.content, meta)
        except Exception as e:
            log(f"    내려받기 실패({type(e).__name__})")
    return None


def _uid():
    from . import users

    return users.uid()


def already_have(meta):
    if meta.get("doi"):
        r = db.q("SELECT id FROM papers WHERE lower(doi)=lower(?) AND COALESCE(user_id,0)=?", (meta["doi"], _uid()), one=True)
        if r:
            return r["id"]
    if meta.get("title"):
        r = db.q("SELECT id FROM papers WHERE lower(title)=lower(?) AND COALESCE(user_id,0)=?", (meta["title"], _uid()), one=True)
        if r:
            return r["id"]
    return None


def fetch_paper(meta, log=print, allow_library=True):
    """공개 PDF → 안 되면 도서관 경유. 성공 시 라이브러리에 등록된 paper id."""
    from . import library, pdfindex

    from . import users

    if users.is_guest():
        allow_library = False  # 학교 도서관 로그인은 주인 계정이라 다른 사람은 못 씀
    have = already_have(meta)
    if have:
        return have
    path = download_open(meta, log)
    via = "인터넷(공개)"
    if not path and allow_library and meta.get("doi") and library.configured():
        log("    공개본 없음 → 대학 도서관 경유 시도")
        with _lock:
            content = library.fetch_pdf(meta["doi"], log)
        if content:
            path = _save(content, meta)
            via = "도서관"
    if not path:
        return None
    log(f"    ✔ 저장({via}): {path.name}")
    return pdfindex.index_pdf(path, meta=meta, source="web", log=log)
