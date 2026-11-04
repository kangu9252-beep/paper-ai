"""새벽 2시 자동 작업: 낮에 예약해 둔 일을 밤에 로컬 AI로 처리한다(무료).

- 앱이 켜져 있으면 앱 안의 타이머가 02:00~07:00 사이에 예약을 실행.
- Windows 예약 작업을 등록하면 01:55에 컴퓨터를 깨워 앱을 조용히 켜고, 일을 마치면 스스로 꺼진다.
"""
import datetime
import json
import os
import subprocess
import sys
import threading
import time
import traceback

from . import agent, db, llm, review

TASK_NAME = "PaperAI-Night"
START_HOUR, END_HOUR = 2, 7
KINDS = {"collect", "write", "review"}
_running = threading.Lock()


def in_window(now=None):
    h = (now or datetime.datetime.now()).hour
    return START_HOUR <= h < END_HOUR


def next_run_text():
    now = datetime.datetime.now()
    if in_window(now):
        return "지금(새벽 작업 시간)"
    return "오늘 새벽 2시" if now.hour < START_HOUR else "내일 새벽 2시"


def _run_one(row):
    logs = []

    def log(m):
        logs.append(str(m))
        db.x("UPDATE schedule SET log=? WHERE id=?", ("\n".join(logs[-80:]), row["id"]))

    args = json.loads(row["args"] or "{}")
    pid = row["project_id"]
    llm.set_engine("local")  # 새벽 작업은 항상 내 컴퓨터 AI로 (무료)
    db.x("UPDATE schedule SET status='running', ran_at=datetime('now','localtime') WHERE id=?", (row["id"],))
    try:
        if row["kind"] == "collect":
            agent.collect(pid, int(args.get("route", 0)), log, lambda: False)
        elif row["kind"] == "write":
            def emit(h):
                p = db.q("SELECT content FROM projects WHERE id=?", (pid,), one=True)
                db.x("UPDATE projects SET content=?, updated=datetime('now','localtime') WHERE id=?", ((p["content"] or "") + h, pid))

            agent.write(pid, args.get("sections", []), log, lambda: False, emit)
        elif row["kind"] == "review":
            tool = args.get("tool", "full")
            (review.full_review if tool == "full" else review.TOOLS[tool])(pid, log)
        db.x("UPDATE schedule SET status='done' WHERE id=?", (row["id"],))
    except Exception as e:
        traceback.print_exc()
        log(f"오류: {e}")
        db.x("UPDATE schedule SET status='error' WHERE id=?", (row["id"],))


def run_due(force=False):
    """기다리는 예약을 차례로 실행. force=True면 시간과 관계없이 지금 실행."""
    if not (force or in_window()):
        return 0
    if not _running.acquire(blocking=False):
        return 0
    n = 0
    try:
        while True:
            row = db.q("SELECT * FROM schedule WHERE status='waiting' ORDER BY id LIMIT 1", one=True)
            if not row or not (force or in_window()):
                break
            _run_one(row)
            n += 1
    finally:
        _running.release()
    return n


def start_scheduler():
    night_mode = "--night" in sys.argv

    def loop():
        db.x("UPDATE schedule SET status='waiting' WHERE status='running'")  # 중간에 꺼졌던 작업 다시
        started = time.time()
        while True:
            try:
                run_due()
            except Exception:
                traceback.print_exc()
            if night_mode:
                waiting = db.q("SELECT COUNT(*) n FROM schedule WHERE status='waiting'", one=True)["n"]
                # 예약 작업이 다 끝났거나 새벽 시간이 지나면 조용히 종료
                if (in_window() and not waiting) or (not in_window() and time.time() - started > 3600):
                    os._exit(0)
            time.sleep(60)

    threading.Thread(target=loop, daemon=True).start()


# ---------- Windows 예약 작업 ----------
def _ps(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, encoding="utf-8", errors="ignore")
    return r.returncode, (r.stdout + r.stderr).strip()


def register_task():
    root = str(db.ROOT)
    pyw = os.path.join(root, ".venv", "Scripts", "pythonw.exe")
    cmd = (
        f"$a = New-ScheduledTaskAction -Execute '{pyw}' -Argument 'server.py --night' -WorkingDirectory '{root}';"
        "$t = New-ScheduledTaskTrigger -Daily -At 1:55am;"
        "$s = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable:$false -ExecutionTimeLimit (New-TimeSpan -Hours 6);"
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s "
        "-Description '논문 AI: 새벽 2시 예약 작업(자료수집·집필·원고검토)을 로컬 AI로 실행' -Force | Out-Null; 'OK'"
    )
    code, out = _ps(cmd)
    _status_cache["t"] = 0
    if code == 0 and "OK" in out:
        return "등록 완료 — 매일 01:55에 컴퓨터를 깨워 예약된 작업을 처리하고 스스로 꺼집니다. (컴퓨터 전원은 켜두거나 절전 상태로 두세요)"
    return f"등록 실패: {out[-300:]}"


def unregister_task():
    _status_cache["t"] = 0
    code, out = _ps(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false; 'OK'")
    return "해제했습니다." if code == 0 else f"해제 실패: {out[-200:]}"


_status_cache = {"t": 0, "v": "none"}


def task_status():
    """Windows 예약 작업 등록 여부 (PowerShell이 느려서 1분간 기억)."""
    if time.time() - _status_cache["t"] > 60:
        code, out = _ps(f"(Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue).State")
        _status_cache.update(t=time.time(), v="registered" if out.strip() in ("Ready", "Running") else "none")
    return _status_cache["v"]
