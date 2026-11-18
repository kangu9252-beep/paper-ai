"""SQLite 저장소: 논문(PDF), 문단 조각(chunk), 집필 프로젝트, 설정."""
import json
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PAPERS_DIR = DATA / "papers"
DATA.mkdir(exist_ok=True)
PAPERS_DIR.mkdir(exist_ok=True)
DB_PATH = DATA / "app.db"

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")

_conn.executescript(
    """
CREATE TABLE IF NOT EXISTS papers (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE,
  title TEXT, authors TEXT, year TEXT, doi TEXT, venue TEXT,
  source TEXT, status TEXT, pages INTEGER, mtime REAL,
  added TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  paper_id INTEGER, page INTEGER, text TEXT,
  lines TEXT, pw REAL, ph REAL, emb BLOB
);
CREATE INDEX IF NOT EXISTS chunks_paper ON chunks(paper_id);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  title TEXT, topic TEXT, plan TEXT, route INTEGER,
  content TEXT DEFAULT '', found TEXT DEFAULT '[]',
  updated TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')),
  model TEXT, input INTEGER, output INTEGER
);
CREATE TABLE IF NOT EXISTS ai_log (
  id INTEGER PRIMARY KEY, project_id INTEGER, feature TEXT, engine TEXT, model TEXT,
  ts TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS schedule (
  id INTEGER PRIMARY KEY,
  project_id INTEGER, kind TEXT, args TEXT DEFAULT '{}',
  status TEXT DEFAULT 'waiting', log TEXT DEFAULT '',
  created TEXT DEFAULT (datetime('now','localtime')), ran_at TEXT
);
"""
)
for col, ddl in (("local_only", "INTEGER DEFAULT 0"), ("reports", "TEXT DEFAULT '{}'"), ("matrix", "TEXT DEFAULT '{}'"), ("ref_folders", "TEXT DEFAULT '[]'")):
    try:
        _conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
    except sqlite3.OperationalError:
        pass  # 이미 있음
_conn.commit()


def q(sql, args=(), one=False):
    with _lock:
        cur = _conn.execute(sql, args)
        rows = cur.fetchall()
    rows = [dict(r) for r in rows]
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


def xmany(sql, rows):
    with _lock:
        _conn.executemany(sql, rows)
        _conn.commit()


DEFAULTS = {
    "model": "qwen2.5:7b-instruct",
    "embed_model": "bge-m3",
    "embed_backend": "builtin",  # builtin: 앱 내장 작은 모델(빠름) / ollama: bge-m3(조금 더 정확, 느림)
    "ollama_url": "http://127.0.0.1:11434",
    "scan_folders": [str(PAPERS_DIR), str(Path.home() / "Documents"), str(Path.home() / "Downloads")],
    "strict_verify": True,
    "lib_login_url": "",
    "lib_proxy_prefix": "",
    "lib_id": "",
    "lib_pw": "",
    "max_downloads": 15,
    "engine": "local",  # local | claude
    "claude_key": "",
    "scan_with_claude": True,  # PDF 저자·연도 읽기를 Claude(가장 싼 Haiku)로 — 한 편 1원 미만
    "monthly_cap_won": 0,  # 이번 달 Claude 사용 한도(원), 0이면 없음
}


def get_settings():
    s = dict(DEFAULTS)
    for r in q("SELECT k, v FROM settings"):
        try:
            s[r["k"]] = json.loads(r["v"])
        except Exception:
            pass
    return s


def set_settings(d):
    for k, v in d.items():
        if k in DEFAULTS:
            x("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (k, json.dumps(v, ensure_ascii=False)))
