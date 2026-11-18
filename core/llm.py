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


# Claude를 쓸 때 일의 무게에 따라 모델을 나눈다 (비용 절약)
TIERS = {
    "deep": ("claude-opus-5-5", "medium"),  # 판단: 심사·연구계획·연구 공백·서론
    "standard": ("claude-sonnet-5-5", "medium"),  # 정리·작성: 집필·문헌분석표·근거 찾기·문장 점검
    "light": ("claude-haiku-5-5", "low"),  # 단순: 저자·연도 읽기·검색어
}
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}  # 100만 토큰당 $
WON_PER_USD = 1400


def chat(prompt, system=None, schema=None, temperature=0.2, num_ctx=8192, model=None, num_predict=1500, tier="standard"):
    """schema(JSON Schema)를 주면 그 형식의 dict를 돌려준다. num_predict는 폭주 방지용 출력 상한(로컬)."""
    if engine() == "claude":
        return _claude(prompt, system, schema, tier)
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


BUILTIN_MODEL = "minishlab/potion-multilingual-128M"  # 앱에 들어 있는 작은 다국어 모델: 설치 없이 빠름(CPU로 초당 수십 조각)
_builtin = {"m": None}
_builtin_lock = threading.Lock()


def embed_backend():
    return db.get_settings().get("embed_backend") or "builtin"


def embed_name():
    """지금 쓰는 검색 모델 이름 (벡터를 섞어 쓰지 않도록 구분용)"""
    return BUILTIN_MODEL if embed_backend() == "builtin" else db.get_settings()["embed_model"]


_dim = {}


def embed_dim():
    n = embed_name()
    if n not in _dim:
        v = embed(["확인"])
        if v is None:
            return None
        _dim[n] = v.shape[1]
    return _dim[n]


def _embed_builtin(texts):
    from pathlib import Path

    with _builtin_lock:
        if _builtin["m"] is None:
            from fastembed import TextEmbedding

            cache = Path(__file__).resolve().parent.parent / "data" / "models"
            cache.mkdir(parents=True, exist_ok=True)
            _builtin["m"] = TextEmbedding(BUILTIN_MODEL, cache_dir=str(cache))
        v = np.array(list(_builtin["m"].embed([t[:2000] for t in texts], batch_size=32)), dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
    return v


def embed(texts):
    """문장들을 의미 벡터로. 검색 모델이 없으면 None. (꺼져 있으면 1분 뒤 다시 시도)"""
    global _embed_ok, _embed_fail_at
    import time

    if not texts or (_embed_ok is False and time.time() - _embed_fail_at < 60):
        return None
    if embed_backend() == "builtin":
        try:
            v = _embed_builtin(texts)
            _embed_ok = True
            return v
        except Exception:
            import traceback

            traceback.print_exc()
            _embed_ok = False
            _embed_fail_at = time.time()
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


def deep(*a, **k):
    return chat(*a, tier="deep", **k)


def light(*a, **k):
    return chat(*a, tier="light", **k)


def _claude(prompt, system=None, schema=None, tier="standard"):
    import anthropic

    global _client, _client_key
    key = claude_key()
    if not key:
        raise RuntimeError("Claude API 키가 없습니다. 설정에서 넣어주세요.")
    if _client is None or _client_key != key:
        _client = anthropic.Anthropic(api_key=key, timeout=600)
        _client_key = key
    cap = int(db.get_settings().get("monthly_cap_won") or 0)
    if cap and usage_this_month()["won"] >= cap:
        raise RuntimeError(f"이번 달 Claude 사용 한도({cap:,}원)에 도달해 멈췄어요. ⚙ 설정에서 한도를 올리거나 로컬 AI로 바꿔 주세요.")
    model, effort = TIERS.get(tier, TIERS["standard"])
    kw = {}
    if system:
        kw["system"] = system
    if model != "claude-haiku-5-5":  # Haiku는 서버 쪽 대체(fallback)가 없음
        kw["betas"] = ["server-side-fallback-2026-07-01"]
        kw["fallbacks"] = "default"
    out_cfg = {"effort": effort}
    if schema:
        out_cfg["format"] = {"type": "json_schema", "schema": _strict(schema)}
    try:
        resp = _client.beta.messages.create(
            model=model,
            max_tokens=16000,
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


def cost_usd(model, inp, out):
    pi, po = next((v for k, v in PRICES.items() if (model or "").startswith(k)), PRICES["claude-opus-5-5"])
    return inp / 1e6 * pi + out / 1e6 * po


def usage_this_month():
    """이번 달 이 앱에서 쓴 Claude 금액 (모델별 요금으로 계산한 어림값 — 실제 청구액은 콘솔 기준)."""
    rows = db.q(
        "SELECT model, COALESCE(SUM(input),0) i, COALESCE(SUM(output),0) o, COUNT(*) n FROM usage WHERE ts >= strftime('%Y-%m-01', 'now', 'localtime') GROUP BY model"
    )
    usd = sum(cost_usd(r["model"], r["i"], r["o"]) for r in rows)
    return {"calls": sum(r["n"] for r in rows), "input": sum(r["i"] for r in rows), "output": sum(r["o"] for r in rows),
            "usd": round(usd, 2), "won": int(round(usd * WON_PER_USD, -1))}


def estimate_won(tier, inp_tokens, out_tokens):
    """실행 전 예상 금액(원). 생각(thinking)도 출력으로 청구되므로 출력은 넉넉히 잡는다."""
    model = TIERS[tier][0]
    return max(1, int(round(cost_usd(model, inp_tokens, out_tokens * (1 if tier == "light" else 1.8)) * WON_PER_USD)))
