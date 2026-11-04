"""대학 도서관 로그인 & 원문 다운로드 (실제 크롬/엣지 창을 띄워 자동 조작).

- 로그인 정보는 이 PC 안의 브라우저 프로필(data/browser)에 쿠키로 남는다.
- 아이디/비밀번호를 설정에 넣으면 자동 입력을 시도하고, 2단계 인증 등은 사람이 창에서 마저 하면 된다.
"""
import re
import time
from urllib.parse import urljoin

from . import db, secret

PROFILE = db.DATA / "browser"


def configured():
    s = db.get_settings()
    return bool(s.get("lib_proxy_prefix") or s.get("lib_login_url"))


def _launch(p, headless=False):
    last = None
    for ch in ("chrome", "msedge"):
        try:
            return p.chromium.launch_persistent_context(
                str(PROFILE), channel=ch, headless=headless, accept_downloads=True,
                viewport={"width": 1200, "height": 850},
            )
        except Exception as e:
            last = e
    raise RuntimeError(f"크롬/엣지를 열 수 없습니다: {last}")


def _pw_field(page):
    for f in page.query_selector_all("input[type=password]"):
        if f.is_visible():
            return f
    return None


def _auto_login(page, log):
    s = db.get_settings()
    uid, pw = s.get("lib_id"), secret.unprotect(s.get("lib_pw", ""))
    pf = _pw_field(page)
    if not pf:
        return False
    if not (uid and pw):
        log("    로그인 화면입니다. 열린 창에서 직접 로그인해 주세요 (최대 3분 대기).")
        return False
    # 아이디 칸 찾기: 이름이 id/user/login 같은 칸 우선, 없으면 비밀번호 칸 바로 앞의 입력칸 (검색창 오인 방지)
    idf, before = None, None
    for f in page.query_selector_all("input"):
        if f.evaluate("(e, p) => e === p", pf):
            break
        if (f.get_attribute("type") or "text") in ("text", "email", "tel") and f.is_visible():
            before = f
            key = " ".join(filter(None, [f.get_attribute("name"), f.get_attribute("id"), f.get_attribute("placeholder")])).lower()
            if any(k in key for k in ("userid", "user", "login", "account", "아이디", "학번")) or key in ("id", "uid"):
                idf = f
    idf = idf or before
    if idf:
        idf.fill(uid)
    pf.fill(pw)
    pf.press("Enter")
    log("    아이디/비밀번호 자동 입력함")
    return True


def _wait_logged_in(page, seconds=180):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if page.is_closed():
                return False
            if not _pw_field(page):
                time.sleep(2)
                if not _pw_field(page):
                    return True
        except Exception:
            return False
        time.sleep(1.5)
    return False


def login(log=print):
    from playwright.sync_api import sync_playwright

    s = db.get_settings()
    url = s.get("lib_login_url")
    if not url:
        return "설정에서 '도서관 로그인 주소'를 먼저 넣어주세요."
    with sync_playwright() as p:
        ctx = _launch(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        _auto_login(page, log)
        ok = _wait_logged_in(page)
        time.sleep(2)
        ctx.close()
    return "로그인 완료 — 이 PC 브라우저 프로필에 저장되었습니다." if ok else "로그인을 확인하지 못했습니다(시간 초과 또는 창 닫힘)."


def _publisher_url(doi):
    """DOI → 출판사 주소. 도서관 프록시는 doi.org를 통과시키지 않으므로 출판사 주소로 바꿔서 접속한다."""
    import httpx

    url = f"https://doi.org/{doi}"
    try:
        for _ in range(3):
            r = httpx.get(url, follow_redirects=False, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            loc = r.headers.get("location")
            if not loc:
                break
            url = urljoin(url, loc)
            if "doi.org" not in url:
                break
    except Exception:
        pass
    # Elsevier 중간 페이지(linkinghub)는 오래 머무므로 ScienceDirect 논문 주소로 바로
    m = re.search(r"linkinghub\.elsevier\.com/retrieve/pii/([^/?#]+)", url)
    if m:
        url = f"https://www.sciencedirect.com/science/article/pii/{m.group(1)}"
    return url


def _fresh_login(ctx, page, log):
    """도서관에 새로 로그인해서 이번 브라우저 세션에 학외접속 권한을 받는다.
    (이미 로그인된 상태면 도서관이 학외접속 연결 단계를 건너뛰기 때문에 매번 새로 로그인)"""
    home = re.match(r"https?://[^/#]+", db.get_settings().get("lib_login_url") or "")
    if home:
        page.goto(home.group(0), wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
        ctx.clear_cookies(domain=home.group(0).split("//")[1])
    page.goto(db.get_settings()["lib_login_url"], wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)
    _auto_login(page, log)
    ok = _wait_logged_in(page, 180)
    time.sleep(6)  # 로그인 후 학외접속 연결(#/proxy) 단계가 끝날 때까지
    return ok


PDF_TEXT = re.compile(r"^(view pdf|pdf|download pdf|pdf download|full text pdf|get pdf|원문보기|원문 보기|pdf 다운로드|pdf보기|다운로드)$", re.I)


def _settle(page, quiet=4, limit=40):
    """중간 이동 페이지(예: Elsevier linkinghub)를 지나 주소가 멈출 때까지 기다린다."""
    last, since, end = page.url, time.time(), time.time() + limit
    while time.time() < end:
        time.sleep(1)
        if page.url != last:
            last, since = page.url, time.time()
        elif time.time() - since >= quiet:
            break
    time.sleep(2)


def _grab_pdf(ctx, page, log):
    """현재 논문 페이지에서 PDF를 받아온다: 메타 정보 → PDF 버튼 클릭(새 창·다운로드까지 감지)."""
    got = {}

    def on_resp(r):
        try:
            if "pdf" in (r.headers.get("content-type") or "") and "body" not in got:
                b = r.body()
                if b[:5] == b"%PDF-":
                    got["body"] = b
        except Exception:
            pass

    def on_dl(d):
        try:
            got.setdefault("body", open(d.path(), "rb").read())
        except Exception:
            pass

    ctx.on("response", on_resp)
    ctx.on("page", lambda pg: pg.on("download", on_dl))
    page.on("download", on_dl)

    def wait(sec):
        end = time.time() + sec
        while time.time() < end and "body" not in got:
            time.sleep(0.5)
        return got.get("body")

    # 이 논문의 식별자 (추천 논문 등 다른 논문의 PDF 버튼을 누르지 않도록)
    ids = [x for x in re.findall(r"(?:pii/|doi/(?:abs/|full/)?)([^/?#]+(?:/[^/?#]+)?)", page.url) if len(x) > 5]

    def mine(href):
        return not ids or any(i.lower() in href.lower() for i in ids)

    def try_get(u):
        try:
            r = ctx.request.get(u, timeout=60000)
            b = r.body()
            return b if b[:5] == b"%PDF-" else None
        except Exception:
            return None

    cands = []
    meta = page.query_selector("meta[name=citation_pdf_url]")
    if meta and meta.get_attribute("content"):
        cands.append(meta.get_attribute("content"))
    links = []
    for el in page.query_selector_all("a[href]"):
        href = el.get_attribute("href") or ""
        txt = re.sub(r"\s+", " ", el.inner_text() or "").strip()
        if PDF_TEXT.match(txt) or re.search(r"/pdf|pdfft|\.pdf($|\?)", href, re.I):
            links.append(urljoin(page.url, href))
    cands += [h for h in links if mine(h)]
    expanded = []
    for c in cands:  # Wiley: /doi/pdf/ 는 뷰어 화면 → 실제 파일은 /doi/pdfdirect/
        if re.search(r"/doi/e?pdf/", c):
            expanded.append(re.sub(r"/doi/e?pdf/", "/doi/pdfdirect/", c).split("?")[0] + "?download=true")
        expanded.append(c)
    for c in dict.fromkeys(expanded):
        b = try_get(c)
        if b:
            return b
    # 직접 주소로 안 되면 이 논문의 PDF 버튼을 실제로 눌러봄
    for el in page.query_selector_all("a, button"):
        try:
            txt = re.sub(r"\s+", " ", el.inner_text() or "").strip()
            href = el.get_attribute("href") or ""
            if not el.is_visible() or not PDF_TEXT.match(txt) or (href and not mine(href)):
                continue
            el.click(timeout=5000)
            body = wait(25)
            if body:
                return body
        except Exception:
            continue
    return wait(3)


def fetch_pdf(doi, log=print):
    """대학 도서관 학외접속으로 구독 논문 PDF를 받는다. 성공 시 PDF bytes."""
    from playwright.sync_api import sync_playwright

    s = db.get_settings()
    pub = _publisher_url(doi)
    url = (s.get("lib_proxy_prefix") or "") + pub
    host = re.sub(r"^https?://([^/]+).*", r"\1", pub)
    try:
        with sync_playwright() as p:
            ctx = _launch(p)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                if s.get("lib_login_url") and not _fresh_login(ctx, page, log):
                    log("    도서관 로그인 실패")
                    return None
                for attempt in range(2):
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    _settle(page)
                    body_text = page.inner_text("body")[:500] if page.query_selector("body") else ""
                    if "미등록" in body_text:
                        log(f"    학교 도서관이 이 출판사({host})를 구독하지 않음")
                        return None
                    if _pw_field(page) or "login" in page.url:  # 세션이 풀렸으면 한 번 더 로그인
                        if attempt == 0 and _fresh_login(ctx, page, log):
                            continue
                        return None
                    break
                pdf = _grab_pdf(ctx, page, log)
                if not pdf and "sciencedirect" in host:
                    log(f"    Elsevier(ScienceDirect)는 자동 다운로드를 막아둠 → 평소 쓰는 크롬에서 직접 받아 '＋ PDF 추가'로 넣어주세요: https://doi.org/{doi}")
                elif not pdf:
                    log(f"    {host}에서 PDF를 받지 못함(구독 범위 밖이거나 버튼 형식이 다름)")
                return pdf
            finally:
                ctx.close()
    except Exception as e:
        log(f"    도서관 경유 실패: {e}")
        return None
