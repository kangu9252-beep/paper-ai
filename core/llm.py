"""로컬 AI(Ollama) 호출."""
import copy
import json
import re
import threading

import httpx
import numpy as np

from . import db

CLAUDE_MODEL = "claude-opus-5-5"
_clients = {}
_clients_lock = threading.Lock()
_ctx = threading.local()  # 작업(스레드)마다 쓸 엔진: "local" 또는 "claude"


def set_engine(name):
    _ctx.engine = name


def engine():
    return getattr(_ctx, "engine", "local")


def claude_key():
    from . import secret, users

    if users.is_guest():  # 웹 주소로 들어온 사람은 자기 키만
        return users.key()
    return secret.unprotect(db.get_settings().get("claude_key", ""))


def engine_for(project_id=None, night=False):
    """이번 작업에 쓸 엔진. 새벽 예약 작업과 '로컬만' 프로젝트는 항상 로컬."""
    from . import users

    if users.is_guest():
        return "claude"  # 웹으로 쓰는 사람은 Claude만 (키가 없으면 실행할 때 안내)
    s = db.get_settings()
    if night or s.get("engine") != "claude" or not (claude_key() or via_subscription()):
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


# 맞장구(동조)·감싸 주기(온정주의)를 줄이는 공통 원칙 — 검토·도우미 지시문에 붙인다
OBJECTIVE = (
    " [객관 진단 원칙] 연구자의 기분을 맞추려고 동의하거나 칭찬하지 않습니다. '좋은 질문이에요', '훌륭합니다' 같은 인사말로 시작하지 않습니다. "
    "지도교수·심사위원(레드팀)의 눈으로 약점을 먼저, 가장 많이 다룹니다(답의 절반 정도). 그다음 지금 할 수 있는 현실적인 보완책을 구체적으로 제시하고(30% 정도), "
    "마지막으로 근거에 비춘 객관적 판단과 그 확신 정도를 밝힙니다(20% 정도). 잘된 점은 근거가 분명할 때만 짧게 말합니다. "
    "자료·논문·수치를 지어내지 않고, 확인할 수 없는 것은 '확인 필요'라고 씁니다."
)


# AI 답에 섞여 나오는 마크다운·HTML 기호(**굵게**, ## 제목, <br> 등)를 지워 읽기 쉽게 한다.
# 원문 인용(quote)·근거 문장은 원문과 글자가 같아야 하므로 건드리지 않는다.
_TAG_RE = re.compile(r"</?(?:br|p|b|strong|i|em|u|ul|ol|li|h[1-6]|span|div|table|tr|td|th|hr)\b[^>]*>", re.I)


def tidy(t):
    if not isinstance(t, str) or not t:
        return t
    t = re.sub(r"<br\s*/?>|</p>|</li>|</h[1-6]>", "\n", t, flags=re.I)
    t = re.sub(r"<li\b[^>]*>", "• ", t, flags=re.I)
    t = _TAG_RE.sub("", t)
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t, flags=re.S)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", t)  # ## 제목
    t = re.sub(r"(?m)^\s*[-*]\s+", "• ", t)  # - 목록 → •
    t = re.sub(r"(?m)^\s*(?:---+|\*\*\*+)\s*$", "", t)  # 가로줄
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    t = re.sub(r"&nbsp;", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


_KEEP = re.compile(r"quote|sentence|original|text|label|anchor|id$|chunk|doi|url|author|title|venue|path|query|keyword", re.I)


def _tidy_all(v, key=""):
    if isinstance(v, str):
        return v if _KEEP.search(key) else tidy(v)
    if isinstance(v, list):
        return [_tidy_all(x, key) for x in v]
    if isinstance(v, dict):
        return {k: _tidy_all(x, k) for k, x in v.items()}
    return v


def chat(prompt, system=None, schema=None, temperature=0.2, num_ctx=8192, model=None, num_predict=1500, tier="standard"):
    """schema(JSON Schema)를 주면 그 형식의 dict를 돌려준다. 답 속 마크다운·HTML 기호는 지운다."""
    r = _chat_raw(prompt, system, schema, temperature, num_ctx, model, num_predict, tier)
    return _tidy_all(r) if schema else r


def _chat_raw(prompt, system=None, schema=None, temperature=0.2, num_ctx=8192, model=None, num_predict=1500, tier="standard"):
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


_en_cache = {}


def to_english(texts):
    """한국어 문장을 영어로 (영어 논문에서 근거를 찾을 때 같은 언어끼리 비교하려고).
    Claude일 때 가장 싼 Haiku로 한 번에 — 문장 10개에 1원 안팎. 로컬 AI면 너무 느려서 건너뜀(None)."""
    out = [None] * len(texts)
    need = [i for i, t in enumerate(texts) if re.search(r"[가-힣]", t or "") and t not in _en_cache]
    if need and engine() == "claude":
        for s in range(0, len(need), 20):
            part = need[s : s + 20]
            block = "\n".join(f"{k + 1}. {texts[i][:600]}" for k, i in enumerate(part))
            try:
                r = light(
                    f"다음 한국어 문장을 각각 자연스러운 학술 영어 한 문장으로 옮기세요. 고유명사·척도명은 영어 원래 이름으로.\n{block}",
                    schema={"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer"}, "en": {"type": "string"}}, "required": ["n", "en"]}}}, "required": ["items"]},
                )
                for x in r.get("items", []):
                    k = int(x.get("n", 0)) - 1
                    if 0 <= k < len(part) and x.get("en"):
                        _en_cache[texts[part[k]]] = x["en"]
            except Exception:
                pass
    for i, t in enumerate(texts):
        out[i] = _en_cache.get(t)
    return out


def claude_cli():
    """이 PC에 깔린 Claude Code 실행 파일 (없으면 None)"""
    import os
    import shutil

    exe = os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
    return exe if os.path.exists(exe) else shutil.which("claude")


def via_subscription():
    """주인 컴퓨터에서 Claude를 API 키 대신 내 Claude 구독(Claude Code)으로 돌릴지.
    웹으로 들어온 사람은 절대 안 됨 — 구독은 본인만 쓰는 것 (남과 나눠 쓰면 약관 위반)."""
    from . import users

    return not users.is_guest() and db.get_settings().get("claude_via") == "subscription" and bool(claude_cli())


CLI_MODEL = {"claude-opus-5-5": "opus", "claude-sonnet-5-5": "sonnet", "claude-haiku-5-5": "haiku"}


def _claude_cli(prompt, system, schema, model):
    """Claude Code(claude -p)로 한 번 묻기. 추가 비용 없음(구독 사용량 안에서), 대신 한 번에 5초쯤 더 걸림."""
    import os
    import subprocess
    import tempfile

    from . import users

    cmd = [claude_cli(), "-p", "--model", CLI_MODEL.get(model, "sonnet"), "--output-format", "json", "--tools", "", "--no-session-persistence",
           "--system-prompt", system or "당신은 학술 논문 작성을 돕는 연구 보조자입니다. 요청한 것만 답하세요."]
    if schema:
        cmd += ["--json-schema", json.dumps(_strict(schema), ensure_ascii=False)]
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}  # 키가 있으면 구독 대신 키로 청구되므로 뺀다
    try:
        r = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=900, env=env, cwd=tempfile.gettempdir(),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude(구독)가 15분 안에 답하지 않았어요. 잠시 후 다시 해 주세요.")
    out = r.stdout.decode("utf-8", "replace")
    try:
        d = json.loads(out)
    except Exception:
        err = (r.stderr.decode("utf-8", "replace") or out).strip()[-300:]
        if "login" in err.lower() or "auth" in err.lower():
            raise RuntimeError("Claude Code에 로그인되어 있지 않아요. 터미널에서 claude 를 한 번 실행해 로그인해 주세요.")
        raise RuntimeError(f"Claude(구독) 실행 오류: {err}")
    if d.get("is_error"):
        msg = str(d.get("result") or d.get("api_error_status") or "")
        if "limit" in msg.lower():
            raise RuntimeError("Claude 구독 사용량 한도에 걸렸어요. 한도가 풀린 뒤 다시 하거나 설정에서 'API 키로'로 바꿔 주세요.")
        raise RuntimeError(f"Claude(구독) 오류: {msg[:300]}")
    try:
        u = d.get("usage") or {}
        inp = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
        db.x("INSERT INTO usage(model, input, output, user_id) VALUES(?, ?, ?, ?)", ("sub:" + model, inp, u.get("output_tokens") or 0, users.uid()))
    except Exception:
        pass
    if not schema:
        return d.get("result") or ""
    so = d.get("structured_output")
    if isinstance(so, dict):
        return so
    text = d.get("result") or ""
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}


def _claude(prompt, system=None, schema=None, tier="standard"):
    import anthropic

    from . import users

    if via_subscription():
        return _claude_cli(prompt, system, schema, TIERS.get(tier, TIERS["standard"])[0])
    key = claude_key()
    if not key:
        raise RuntimeError("Claude API 키가 없습니다. " + ("오른쪽 위 '내 설정'에서 내 키를 넣어주세요." if users.is_guest() else "설정에서 넣어주세요."))
    with _clients_lock:  # 사람마다(키마다) 연결을 따로 — 다른 사람 키로 결제되는 일이 없게
        client = _clients.get(key)
        if client is None:
            client = _clients[key] = anthropic.Anthropic(api_key=key, timeout=600)
    cap = 0 if users.is_guest() else int(db.get_settings().get("monthly_cap_won") or 0)
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
        resp = client.beta.messages.create(
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
        db.x("INSERT INTO usage(model, input, output, user_id) VALUES(?, ?, ?, ?)", (resp.model, inp, u.output_tokens or 0, users.uid()))
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
    if (model or "").startswith("sub:"):
        return 0.0  # 구독으로 돌린 것은 추가 비용 없음
    pi, po = next((v for k, v in PRICES.items() if (model or "").startswith(k)), PRICES["claude-opus-5-5"])
    return inp / 1e6 * pi + out / 1e6 * po


def usage_this_month():
    from . import users

    """이번 달 이 앱에서 쓴 Claude 금액 (모델별 요금으로 계산한 어림값 — 실제 청구액은 콘솔 기준)."""
    rows = db.q(
        "SELECT model, COALESCE(SUM(input),0) i, COALESCE(SUM(output),0) o, COUNT(*) n FROM usage WHERE ts >= strftime('%Y-%m-01', 'now', 'localtime') AND COALESCE(user_id,0)=? GROUP BY model",
        (users.uid(),),
    )
    usd = sum(cost_usd(r["model"], r["i"], r["o"]) for r in rows)
    return {"calls": sum(r["n"] for r in rows), "input": sum(r["i"] for r in rows), "output": sum(r["o"] for r in rows),
            "usd": round(usd, 2), "won": int(round(usd * WON_PER_USD, -1))}


def estimate_won(tier, inp_tokens, out_tokens):
    """실행 전 예상 금액(원). 생각(thinking)도 출력으로 청구되므로 출력은 넉넉히 잡는다."""
    model = TIERS[tier][0]
    return max(1, int(round(cost_usd(model, inp_tokens, out_tokens * (1 if tier == "light" else 1.8)) * WON_PER_USD)))
