"""논문 AI — 로컬 웹 서버. 실행: start.bat (브라우저에서 http://127.0.0.1:8765)"""
import json
import sys
import shutil
import threading
import traceback
import uuid
import webbrowser
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from core import agent, db, explore, library, llm, matrix, night, pdfindex, review, secret

app = FastAPI()
STATIC = Path(__file__).parent / "static"
JOBS = {}


# ---------- 백그라운드 작업 ----------
class Job:
    def __init__(self, kind, fn, engine="local", pid=None, feature=None):
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.status, self.log, self.result, self.emitted, self.stopped = kind, "running", [], None, [], False
        JOBS[self.id] = self

        self.engine = engine

        def run():
            llm.set_engine(engine)  # 이 작업에서 쓸 AI: local(내 컴퓨터) / claude(인터넷)
            if engine == "claude":
                self.say("Claude로 작업합니다")
            try:
                self.result = fn(self)
                self.status = "stopped" if self.stopped else "done"
                for f in (FULL_REVIEW if feature == "full" else [feature]):
                    llm.log_use(pid, f)
            except Exception as e:
                traceback.print_exc()
                self.log.append(f"오류: {e}")
                self.status = "error"

        threading.Thread(target=run, daemon=True).start()

    def say(self, msg):
        self.log.append(str(msg))

    def stop(self):
        return self.stopped


FULL_REVIEW = ["structure", "cites", "uncited", "recent", "discuss"]  # 전체 검토 중 AI를 쓰는 도구


def job_out(j):
    return {"id": j.id, "kind": j.kind, "status": j.status, "log": j.log[-200:], "result": j.result, "emitted": j.emitted}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404)
    return job_out(j)


@app.post("/api/jobs/{jid}/stop")
def stop_job(jid: str):
    if jid in JOBS:
        JOBS[jid].stopped = True
    return {"ok": True}


# ---------- 상태 / 설정 ----------
@app.get("/api/status")
def status():
    s = db.get_settings()
    s["lib_pw"] = "********" if s.get("lib_pw") else ""
    s["claude_key"] = "********" if s.get("claude_key") else ""
    return {
        "models": llm.models(),
        "papers": db.q("SELECT COUNT(*) n FROM papers WHERE status='ok'", one=True)["n"],
        "chunks": db.q("SELECT COUNT(*) n FROM chunks", one=True)["n"],
        "no_emb": db.q("SELECT COUNT(*) n FROM chunks WHERE emb IS NULL", one=True)["n"],
        "settings": s,
        "jobs": [job_out(j) | {"log": j.log[-1:]} for j in JOBS.values() if j.status == "running"],
    }


@app.post("/api/settings")
def save_settings(d: dict = Body(...)):
    for k in ("claude_key",):
        if k in d:
            if d[k] == "********":
                d.pop(k)
            else:
                d[k] = secret.protect(d[k].strip())
    if "lib_pw" in d:
        if d["lib_pw"] == "********":
            d.pop("lib_pw")
        else:
            d["lib_pw"] = secret.protect(d["lib_pw"])
    db.set_settings(d)
    llm.reset_embed_check()
    return {"ok": True}


# ---------- 프로젝트 ----------
@app.get("/api/projects")
def projects():
    return db.q("SELECT id, title, topic, updated FROM projects ORDER BY updated DESC")


@app.post("/api/projects")
def new_project(d: dict = Body(...)):
    topic = d.get("topic", "").strip()
    pid = db.x("INSERT INTO projects(title, topic) VALUES(?, ?)", (d.get("title") or topic[:40] or "새 논문", topic))
    return {"id": pid}


@app.get("/api/projects/{pid}")
def get_project(pid: int):
    p = db.q("SELECT * FROM projects WHERE id=?", (pid,), one=True)
    if not p:
        raise HTTPException(404)
    p["plan"] = json.loads(p["plan"]) if p["plan"] else None
    p["reports"] = json.loads(p.get("reports") or "{}")
    p["engine"] = llm.engine_for(pid)
    return p


@app.put("/api/projects/{pid}")
def put_project(pid: int, d: dict = Body(...)):
    for k in ("title", "topic", "content", "route", "local_only"):
        if k in d:
            db.x(f"UPDATE projects SET {k}=?, updated=datetime('now','localtime') WHERE id=?", (d[k], pid))
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def del_project(pid: int):
    db.x("DELETE FROM projects WHERE id=?", (pid,))
    return {"ok": True}


@app.post("/api/projects/{pid}/plan")
def make_plan(pid: int, d: dict = Body(default={})):
    pr = db.q("SELECT topic FROM projects WHERE id=?", (pid,), one=True)
    idea = (d.get("idea") or "").strip()

    def fn(j):
        p = agent.plan(pr["topic"], j.say)
        rec = max(0, min(len(p.get("routes", [])) - 1, int(p.get("recommended") or 1) - 1))
        if idea:  # 사용자가 생각해 둔 경로가 있으면 함께 정리해서 기본 선택
            p.setdefault("routes", []).append(agent.refine_route(pr["topic"], idea, p.get("literature", []), j.say))
            rec = len(p["routes"]) - 1
        db.x("UPDATE projects SET plan=?, route=? WHERE id=?", (json.dumps(p, ensure_ascii=False), rec, pid))
        return {"ok": True}

    return {"job": Job("plan", fn, llm.engine_for(pid), pid, "plan").id}


@app.post("/api/projects/{pid}/route_add")
def route_add(pid: int, d: dict = Body(...)):
    idea = (d.get("idea") or "").strip()
    if not idea:
        raise HTTPException(400, "제안 내용을 적어주세요")

    def fn(j):
        pr = db.q("SELECT topic, plan FROM projects WHERE id=?", (pid,), one=True)
        p = json.loads(pr["plan"]) if pr["plan"] else {"routes": [], "literature": []}
        r = agent.refine_route(pr["topic"], idea, p.get("literature", []), j.say)
        p.setdefault("routes", []).append(r)
        db.x("UPDATE projects SET plan=?, route=? WHERE id=?", (json.dumps(p, ensure_ascii=False), len(p["routes"]) - 1, pid))
        return {"ok": True}

    return {"job": Job("route", fn, llm.engine_for(pid), pid, "route").id}


@app.post("/api/projects/{pid}/route_del")
def route_del(pid: int, d: dict = Body(...)):
    pr = db.q("SELECT plan, route FROM projects WHERE id=?", (pid,), one=True)
    p = json.loads(pr["plan"])
    i = int(d.get("index", -1))
    if 0 <= i < len(p["routes"]) and p["routes"][i].get("user"):
        p["routes"].pop(i)
        db.x("UPDATE projects SET plan=?, route=? WHERE id=?", (json.dumps(p, ensure_ascii=False), 0, pid))
    return {"ok": True}


@app.post("/api/projects/{pid}/collect")
def collect(pid: int, d: dict = Body(...)):
    route = int(d.get("route", 0))
    db.x("UPDATE projects SET route=? WHERE id=?", (route, pid))
    return {"job": Job("collect", lambda j: agent.collect(pid, route, j.say, j.stop), llm.engine_for(pid), pid, "collect").id}


@app.post("/api/projects/{pid}/write")
def write(pid: int, d: dict = Body(...)):
    secs = d.get("sections") or []
    return {"job": Job("write", lambda j: agent.write(pid, secs, j.say, j.stop, j.emitted.append), llm.engine_for(pid), pid, "write").id}


# ---------- 라이브러리 ----------
@app.get("/api/papers")
def papers():
    rows = db.q("SELECT id, path, title, authors, year, doi, source, status, pages, added FROM papers ORDER BY added DESC")
    for r in rows:
        r["authors"] = json.loads(r["authors"] or "[]")
        r["cite"] = pdfindex.cite_label(r["authors"], r["year"])
    return rows


@app.put("/api/papers/{pid}")
def edit_paper(pid: int, d: dict = Body(...)):
    db.x(
        "UPDATE papers SET title=?, authors=?, year=? WHERE id=?",
        (d.get("title", ""), json.dumps(d.get("authors", []), ensure_ascii=False), d.get("year", ""), pid),
    )
    return {"ok": True}


@app.delete("/api/papers/{pid}")
def del_paper(pid: int):
    db.x("DELETE FROM chunks WHERE paper_id=?", (pid,))
    db.x("DELETE FROM papers WHERE id=?", (pid,))
    pdfindex.invalidate()
    return {"ok": True}


@app.post("/api/papers/upload")
async def upload(files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            continue
        dest = db.PAPERS_DIR / Path(f.filename).name
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(dest)

    def fn(j):
        for i, p in enumerate(saved):
            j.say(f"[{i + 1}/{len(saved)}] {p.name} 읽는 중…")
            pdfindex.index_pdf(p, log=j.say)
        j.say("완료")

    return {"job": Job("index", fn).id}


@app.post("/api/scan")
def scan():
    def fn(j):
        pdfindex.scan_folders(db.get_settings()["scan_folders"], log=j.say, stop=j.stop)
        pdfindex.embed_missing(log=j.say)
        j.say("완료")

    return {"job": Job("scan", fn).id}


@app.get("/api/search")
def search(q: str, author: str = "", year: str = "", k: int = 12):
    ids = None
    if author:
        ps = pdfindex.find_papers_by_cite(author, year)
        ids = [p["id"] for p in ps] or None
    res = pdfindex.search(q, k=k, paper_ids=ids, per_paper=4 if ids else 2)
    return {"results": res, "restricted": bool(ids)}


@app.get("/api/lookup")
def lookup(author: str, year: str = ""):
    return [{"id": p["id"], "title": p["title"]} for p in pdfindex.find_papers_by_cite(author, year)]


@app.get("/api/evidence")
def evidence(chunk: int, quote: str = ""):
    e = pdfindex.evidence(chunk, quote)
    if not e:
        raise HTTPException(404, "근거 자료가 라이브러리에서 사라졌습니다.")
    return e


@app.get("/api/paper/{pid}/page/{page}")
def page_info(pid: int, page: int):
    p = db.q("SELECT id, title, authors, year, pages FROM papers WHERE id=?", (pid,), one=True)
    return p


@app.get("/api/page/{pid}/{page}.png")
def page_png(pid: int, page: int):
    data, _, _ = pdfindex.render_page(pid, page)
    return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/pdf/{pid}")
def pdf(pid: int):
    p = db.q("SELECT path FROM papers WHERE id=?", (pid,), one=True)
    return FileResponse(p["path"], media_type="application/pdf")


@app.post("/api/autofind")
def autofind(d: dict = Body(...)):
    return {"job": Job("autofind", lambda j: agent.autofind(d.get("sentence", ""), d.get("author", ""), d.get("year", ""), j.say), llm.engine_for(d.get("project_id")), d.get("project_id"), "autofind").id}


@app.post("/api/references")
def references(d: dict = Body(...)):
    return agent.references(d.get("chunks", []))


@app.post("/api/library/login")
def lib_login():
    return {"job": Job("login", lambda j: j.say(library.login(j.say))).id}



@app.get("/api/claude/usage")
def claude_usage():
    return llm.usage_this_month()


@app.post("/api/claude/test")
def claude_test():
    """저장된 키로 Claude에 짧게 물어보고, 구조화 출력까지 되는지 확인."""
    import time

    if not llm.claude_key():
        return {"ok": False, "msg": "저장된 Claude API 키가 없어요. 키를 넣고 '저장'을 먼저 눌러주세요."}
    llm.set_engine("claude")
    t = time.time()
    try:
        r = llm.chat(
            "'자기자비'를 학술적으로 한 문장으로 정의하고, 영어 용어를 함께 알려주세요.",
            schema={"type": "object", "properties": {"definition": {"type": "string"}, "english": {"type": "string"}}, "required": ["definition", "english"]},
        )
        return {"ok": True, "msg": f"연결 성공 ({time.time() - t:.1f}초) — {r.get('english', '')}: {r.get('definition', '')}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    finally:
        llm.set_engine("local")


# ---------- 원고 보강 ----------
@app.post("/api/import")
async def import_new(file: UploadFile | None = File(None), text: str = Form(""), title: str = Form("")):
    try:
        if file is not None:
            content = review.import_manuscript(file.filename, data=await file.read())
            name = title or Path(file.filename).stem
        else:
            content = review.import_manuscript("", text=text)
            name = title or (text.strip().splitlines() or ["불러온 원고"])[0][:40]
    except ValueError as e:
        raise HTTPException(400, str(e))
    pid = db.x("INSERT INTO projects(title, topic, content) VALUES(?, ?, ?)", (name, name, content))
    return {"id": pid}


@app.post("/api/projects/{pid}/import")
async def import_into(pid: int, file: UploadFile | None = File(None), text: str = Form("")):
    try:
        content = review.import_manuscript(file.filename, data=await file.read()) if file is not None else review.import_manuscript("", text=text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.x("UPDATE projects SET content=?, updated=datetime('now','localtime') WHERE id=?", (content, pid))
    return {"ok": True}


@app.post("/api/projects/{pid}/review/{tool}")
def run_review(pid: int, tool: str):
    if tool != "full" and tool not in review.TOOLS:
        raise HTTPException(404)
    fn = review.full_review if tool == "full" else review.TOOLS[tool]
    feature = None if tool == "refs" else tool  # 참고문헌 대조는 AI를 쓰지 않음
    return {"job": Job("review", lambda j: fn(pid, j.say, j.stop), llm.engine_for(pid), pid, feature).id}


# ---------- ③ 문헌분석 ----------
@app.get("/api/projects/{pid}/matrix")
def matrix_get(pid: int):
    return {"rows": matrix.rows(pid), "candidates": matrix.candidates(pid), "fields": [[k, t] for k, t, _ in matrix.FIELDS]}


@app.post("/api/projects/{pid}/matrix")
def matrix_build(pid: int, d: dict = Body(...)):
    ids = [int(x) for x in d.get("paper_ids", [])][:60]
    if not ids:
        raise HTTPException(400, "정리할 논문을 골라주세요")
    return {"job": Job("matrix", lambda j: matrix.build(pid, ids, j.say, j.stop), llm.engine_for(pid), pid, "matrix").id}


@app.delete("/api/projects/{pid}/matrix/{paper_id}")
def matrix_del(pid: int, paper_id: int):
    _, m = matrix.load(pid)
    m.pop(str(paper_id), None)
    matrix.save(pid, m)
    return {"ok": True}


@app.get("/api/projects/{pid}/matrix.csv")
def matrix_csv(pid: int):
    from fastapi.responses import Response
    from urllib.parse import quote

    name = quote(f"문헌분석표_{pid}.csv")
    return Response(matrix.csv_text(pid), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


@app.post("/api/projects/{pid}/gaps")
def matrix_gaps(pid: int):
    return {"job": Job("gaps", lambda j: matrix.gaps(pid, j.say, j.stop), llm.engine_for(pid), pid, "gaps").id}


@app.post("/api/projects/{pid}/intro")
def matrix_intro(pid: int):
    return {"job": Job("intro", lambda j: matrix.intro(pid, j.say, j.stop), llm.engine_for(pid), pid, "intro").id}


@app.post("/api/projects/{pid}/map")
def research_map(pid: int):
    return {"job": Job("map", lambda j: explore.research_map(pid, j.say, j.stop), llm.engine_for(pid), pid, None).id}


@app.post("/api/projects/{pid}/meter")
def meter(pid: int, d: dict = Body(...)):
    q = (d.get("question") or "").strip()
    if len(q) < 5:
        raise HTTPException(400, "질문을 적어주세요")
    return {"job": Job("meter", lambda j: explore.meter(pid, q, j.say, j.stop), llm.engine_for(pid), pid, "meter").id}


@app.post("/api/explain")
def explain(d: dict = Body(...)):
    pid = d.get("project_id")
    return {"job": Job("explain", lambda j: explore.explain(int(d["chunk"]), d.get("quote", ""), j.say), llm.engine_for(pid), pid, "explain").id}


@app.post("/api/projects/{pid}/model_suggest")
def model_suggest(pid: int):
    return {"job": Job("model", lambda j: explore.model_suggest(pid, j.say), llm.engine_for(pid), pid, "model").id}


@app.put("/api/projects/{pid}/model")
def model_save(pid: int, d: dict = Body(...)):
    matrix._save_report(pid, "model", {k: d.get(k) for k in explore.MODEL_SCHEMA["properties"]})
    return {"ok": True}


@app.get("/api/projects/{pid}/ai_usage")
def ai_usage(pid: int):
    return review.ai_usage(pid)


@app.post("/api/projects/{pid}/insert")
def insert_sentence(pid: int, d: dict = Body(...)):
    review.insert_discussion(pid, d.get("sentence", ""), d.get("chunk_id"), d.get("quote", ""), d.get("label", "(출처 필요)"))
    db.x("INSERT INTO ai_log(project_id, feature, engine, model) VALUES(?, 'discuss_insert', '', '')", (pid,))
    return {"ok": True}


@app.post("/api/fetch_work")
def fetch_work(d: dict = Body(...)):
    from core import web

    return {"job": Job("fetch", lambda j: {"paper": web.fetch_paper(d, log=j.say)}).id}


# ---------- 새벽 예약 ----------
@app.get("/api/schedule")
def schedule_list():
    rows = db.q("SELECT s.*, p.title FROM schedule s LEFT JOIN projects p ON p.id=s.project_id ORDER BY s.id DESC LIMIT 50")
    return {"rows": rows, "next": night.next_run_text(), "task": night.task_status()}


@app.post("/api/schedule")
def schedule_add(d: dict = Body(...)):
    if d.get("kind") not in night.KINDS:
        raise HTTPException(400, "알 수 없는 작업")
    sid = db.x("INSERT INTO schedule(project_id, kind, args) VALUES(?, ?, ?)", (d.get("project_id"), d["kind"], json.dumps(d.get("args", {}), ensure_ascii=False)))
    return {"id": sid, "next": night.next_run_text()}


@app.delete("/api/schedule/{sid}")
def schedule_del(sid: int):
    db.x("DELETE FROM schedule WHERE id=? AND status='waiting'", (sid,))
    return {"ok": True}


@app.post("/api/night/register")
def night_register():
    return {"msg": night.register_task()}


@app.post("/api/night/unregister")
def night_unregister():
    return {"msg": night.unregister_task()}


@app.post("/api/night/run_now")
def night_run_now():
    threading.Thread(target=night.run_due, kwargs={"force": True}, daemon=True).start()
    return {"ok": True}

app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    if "--night" in sys.argv or sys.stdout is None:  # 창 없이 켜질 때(새벽 예약)는 기록을 파일로
        sys.stdout = sys.stderr = open(db.DATA / "night.log", "a", encoding="utf-8", buffering=1)

    import os

    night.start_scheduler()
    if not os.environ.get("NO_BROWSER") and "--night" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8765")).start()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
