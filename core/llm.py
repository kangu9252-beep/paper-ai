"""로컬 AI(Ollama) 호출."""
import copy
import json
import re
import threading

import httpx
import numpy as np

from . import db

CLAUDE_MODEL = "claude-opus-5-5"
_ctx = threading.local()  # 작업(스레드)마다 쓸 엔진: "local" 또는 "claude"


def set_engine(name):
    _ctx.engine = name


def engine():
    return getattr(_ctx, "engine", "local")


def claude_key():
    from . import secret

    return secret.unprotect(db.get_settings().get("claude_key", ""))


def engine_for(project_id=None, night=False):
    """이번 작업에 쓸 엔진. 새벽 예약 작업과 '로컬만' 프로젝트는 항상 로컬."""
    s = db.get_settings()
    if night or s.get("engine") != "claude" or not claude_key():
        return "local"
    if project_id:
        p = db.q("SELECT local_only FROM projects WHERE id=?", (project_id,), one=True)
        if p and p.get("local_only"):
            return "local"
    return "claude"


def _url():
    return db.get_settings()["ollama_url"].rstrip("/")


def models():
    try:
        r = httpx.get(_url() + "/api/tags", timeout=5)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return None


def chat(prompt, system=None, schema=None, temperature=0.2, num_ctx=8192, model=None, num_predict=1500):
    """schema(JSON Schema)를 주면 그 형식의 dict를 돌려준다. num_predict는 폭주 방지용 출력 상한(로컬)."""
    if engine() == "claude":
        return _claude(prompt, system, schema)
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {
        "model": model or db.get_settings()["model"],
        "messages": msgs,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict, "repeat_penalty": 1.1},
    }
    if schema:
        body["format"] = schema
    r = httpx.post(_url() + "/api/chat", json=body, timeout=900)
    r.raise_for_status()
    text = r.json()["message"]["content"]
    if not schema:
        return text
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}


_embed_ok = None
_embed_fail_at = 0.0


def embed(texts):
    """문장들을 의미 벡터로. 검색 모델이 없으면 None. (꺼져 있으면 1분 뒤 다시 시도)"""
    global _embed_ok, _embed_fail_at
    import time

    if not texts or (_embed_ok is False and time.time() - _embed_fail_at < 60):
        return None
    try:
        r = httpx.post(
            _url() + "/api/embed",
            json={"model": db.get_settings()["embed_model"], "input": [t[:2000] for t in texts]},
            timeout=600,
        )
        r.raise_for_status()
        _embed_ok = True
        v = np.array(r.json()["embeddings"], dtype=np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
        return v
    except Exception:
        _embed_ok = False
        _embed_fail_at = time.time()
        return None


def reset_embed_check():
    global _embed_ok
    _embed_ok = None


def log_use(project_id, feature):
    """AI 사용 내역 기록 (투고 시 'AI 사용 명시' 문구의 근거)."""
    if not project_id or not feature:
        return
    eng = engine()
    model = CLAUDE_MODEL if eng == "claude" else db.get_settings()["model"]
    db.x("INSERT INTO ai_log(project_id, feature, engine, model) VALUES(?, ?, ?, ?)", (project_id, feature, eng, model))


# ---------- Claude (인터넷) ----------
_client = None
_client_key = None


def _strict(schema):
    """구조화 출력은 모든 object에 additionalProperties:false 가 필요."""
    sc = copy.deepcopy(schema)

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object":
                n["additionalProperties"] = False
                n.setdefault("required", list(n.get("properties", {}).keys()))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(sc)
    return sc


def _claude(prompt, system=None, schema=None):
    import anthropic

    global _client, _client_key
    key = claude_key()
    if not key:
        raise RuntimeError("Claude API 키가 없습니다. 설정에서 넣어주세요.")
    if _client is None or _client_key != key:
        _client = anthropic.Anthropic(api_key=key, timeout=600)
        _client_key = key
    kw = {}
    if system:
        kw["system"] = system
    out_cfg = {"effort": "medium"}
    if schema:
        out_cfg["format"] = {"type": "json_schema", "schema": _strict(schema)}
    try:
        resp = _client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config=out_cfg,
            messages=[{"role": "user", "content": prompt}],
            **kw,
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("Claude API 키가 맞지 않거나 만료되었습니다. 설정에서 새 키를 넣어주세요.")
    except anthropic.RateLimitError:
        raise RuntimeError("Claude 사용량 한도에 걸렸습니다. 잠시 후 다시 시도하거나 로컬 AI로 바꿔주세요.")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude 오류({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise RuntimeError("인터넷 연결 문제로 Claude에 접속하지 못했습니다.")
    try:  # 사용량 기록 (설정 화면의 '이번 달 사용 금액' 표시용)
        u = resp.usage
        inp = (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
        db.x("INSERT INTO usage(model, input, output) VALUES(?, ?, ?)", (resp.model, inp, u.output_tokens or 0))
    except Exception:
        pass
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude가 이 요청을 거절했습니다. 로컬 AI로 다시 시도해 보세요.")
    text = "".join(b.text for b in resp.content if b.type == "text")
    if not schema:
        return text
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}


# 추정 요금 (claude-opus-5-5: 입력 $4 / 출력 $20, 100만 토큰당) — 실제 청구액은 콘솔 기준
PRICE_IN, PRICE_OUT, WON_PER_USD = 4.0, 20.0, 1400


def usage_this_month():
    r = db.q(
        "SELECT COALESCE(SUM(input),0) i, COALESCE(SUM(output),0) o, COUNT(*) n FROM usage WHERE ts >= strftime('%Y-%m-01', 'now', 'localtime')",
        one=True,
    )
    usd = r["i"] / 1e6 * PRICE_IN + r["o"] / 1e6 * PRICE_OUT
    return {"calls": r["n"], "input": r["i"], "output": r["o"], "usd": round(usd, 2), "won": int(round(usd * WON_PER_USD, -1))}
