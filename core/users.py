"""여러 사람이 웹 주소로 함께 쓸 때: 로그인, 사람별 자료 구분.
- 이 컴퓨터에서 직접 연 경우(주인) = 사용자 0. 로그인 없이 모든 기능.
- 웹 주소(터널)로 들어온 사람 = 사용자 1, 2, … 자기 프로젝트·PDF·Claude 키만. 내 컴퓨터 폴더·설정은 못 봄.
"""
import contextvars
import hashlib
import hmac
import secrets
import time

from . import db, secret

_uid = contextvars.ContextVar("uid", default=0)
ANON = -1  # 로그인 전 (아무 자료도 안 보임)

db.x("""CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, login TEXT UNIQUE, name TEXT, pw TEXT, salt TEXT,
  claude_key TEXT DEFAULT '', created TEXT DEFAULT (datetime('now','localtime')), last_seen TEXT
)""")
db.x("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER, created REAL)")
for table in ("projects", "papers", "usage"):
    try:
        db.x(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER DEFAULT 0")
    except Exception:
        pass  # 이미 있음


def uid():
    return _uid.get()


def set_uid(u):
    return _uid.set(u)


def is_guest():
    """웹 주소로 들어온 사람(주인이 아님)"""
    return uid() != 0


def papers_dir():
    if not is_guest():
        return db.PAPERS_DIR
    d = db.DATA / "users" / str(uid())
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash(pw, salt):
    return hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()


def invite_code():
    import os

    if os.environ.get("INVITE_CODE"):
        return os.environ["INVITE_CODE"].strip().upper()
    s = db.get_settings()
    code = s.get("invite_code")
    if not code:
        code = secrets.token_hex(3).upper()
        db.set_settings({"invite_code": code})
    return code


def signup(login, name, pw, code):
    login = (login or "").strip().lower()
    if not login or len(login) > 40 or not pw or len(pw) < 6:
        raise ValueError("아이디와 6자 이상 비밀번호를 넣어주세요")
    if not hmac.compare_digest((code or "").strip().upper(), invite_code()):
        raise ValueError("초대 코드가 맞지 않아요")
    if db.q("SELECT id FROM users WHERE login=?", (login,), one=True):
        raise ValueError("이미 있는 아이디예요")
    salt = secrets.token_hex(16)
    return db.x("INSERT INTO users(login, name, pw, salt) VALUES(?, ?, ?, ?)", (login, (name or login).strip()[:30], _hash(pw, salt), salt))


_fails = {}


def login(login, pw, ip=""):
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    if len(recent) >= 8:
        raise ValueError("로그인을 너무 많이 틀렸어요. 10분 뒤에 다시 해 주세요")
    u = db.q("SELECT * FROM users WHERE login=?", ((login or "").strip().lower(),), one=True)
    if not u or not hmac.compare_digest(_hash(pw or "", u["salt"]), u["pw"]):
        _fails[ip] = recent + [now]
        raise ValueError("아이디나 비밀번호가 맞지 않아요")
    tok = secrets.token_urlsafe(32)
    db.x("INSERT INTO sessions(token, user_id, created) VALUES(?, ?, ?)", (tok, u["id"], now))
    return tok


def session_user(tok):
    if not tok:
        return None
    r = db.q("SELECT user_id, created FROM sessions WHERE token=?", (tok,), one=True)
    if not r or time.time() - r["created"] > 30 * 86400:
        return None
    if not db.q("SELECT id FROM users WHERE id=?", (r["user_id"],), one=True):
        return None
    return r["user_id"]


def logout(tok):
    db.x("DELETE FROM sessions WHERE token=?", (tok,))


def me():
    u = db.q("SELECT id, login, name, claude_key FROM users WHERE id=?", (uid(),), one=True) if is_guest() else None
    if not u:
        return {"guest": False}
    return {"guest": True, "id": u["id"], "login": u["login"], "name": u["name"], "has_key": bool(u["claude_key"])}


def set_key(key):
    db.x("UPDATE users SET claude_key=? WHERE id=?", (secret.protect(key.strip()) if key.strip() else "", uid()))


def key():
    u = db.q("SELECT claude_key FROM users WHERE id=?", (uid(),), one=True)
    return secret.unprotect(u["claude_key"]) if u and u["claude_key"] else ""


def list_users():
    return db.q("SELECT u.id, u.login, u.name, u.created, (SELECT COUNT(*) FROM projects p WHERE p.user_id=u.id) projects, (SELECT COUNT(*) FROM papers p WHERE p.user_id=u.id) papers FROM users u ORDER BY u.id")


def delete_user(user_id):
    import shutil

    for p in db.q("SELECT id FROM papers WHERE user_id=?", (user_id,)):
        db.x("DELETE FROM chunks WHERE paper_id=?", (p["id"],))
    db.x("DELETE FROM papers WHERE user_id=?", (user_id,))
    db.x("DELETE FROM projects WHERE user_id=?", (user_id,))
    db.x("DELETE FROM sessions WHERE user_id=?", (user_id,))
    db.x("DELETE FROM users WHERE id=?", (user_id,))
    shutil.rmtree(db.DATA / "users" / str(user_id), ignore_errors=True)


def owns_project(pid):
    r = db.q("SELECT user_id FROM projects WHERE id=?", (pid,), one=True)
    return bool(r) and (r["user_id"] or 0) == uid()


def owns_paper(paper_id):
    r = db.q("SELECT user_id FROM papers WHERE id=?", (paper_id,), one=True)
    return bool(r) and (r["user_id"] or 0) == uid()


def owns_chunk(chunk_id):
    r = db.q("SELECT p.user_id FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.id=?", (chunk_id,), one=True)
    return bool(r) and (r["user_id"] or 0) == uid()
