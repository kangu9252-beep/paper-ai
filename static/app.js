// 논문 AI — 화면 동작
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// 인용 표기 인식: (저자, 2020) · (저자, 2020; 저자2 외, 2019) · (Smith, 2019, p. 3) · 저자(2020) · Neff et al.(2003)
const YR = "(?:19|20)\\d{2}[a-z]?";
const PAREN_RE = new RegExp(`\\(([^()]*?${YR}[^()]*?)\\)`, "g");
const PART_RE = new RegExp(`^\\s*([^,;()\\d][^;()]*?),?\\s+(${YR})(?:\\s*,\\s*pp?\\.\\s*[\\d\\-–]+)?\\s*$`);
const NARR_RE = new RegExp(`((?<![가-힣A-Za-z0-9])[가-힣]{2,4}(?:\\s?외)?|[A-Z][A-Za-z\\-']+(?:\\s(?:et al\\.|&\\s[A-Z][A-Za-z\\-']+|and\\s[A-Z][A-Za-z\\-']+))?)\\s?\\((${YR})\\)`, "g");
const CITE_RE = new RegExp(`\\([^()]*?${YR}[^()]*?\\)`, "g"); // 문장에서 인용 표기 지우기용

// 텍스트 안의 인용 표기 위치들: [{index, length, parts: [표기...], group}]
function findCites(text) {
  const out = [];
  for (const m of text.matchAll(PAREN_RE)) {
    const parts = m[1].split(";");
    if (!parts.every((x) => PART_RE.test(x))) continue;
    out.push({ index: m.index, length: m[0].length, parts: parts.length > 1 ? parts.map((x) => x.trim()) : [m[0]], group: parts.length > 1 });
  }
  for (const m of text.matchAll(NARR_RE)) {
    if (out.some((o) => m.index < o.index + o.length && o.index < m.index + m[0].length)) continue;
    out.push({ index: m.index, length: m[0].length, parts: [m[0]], group: false });
  }
  return out.sort((a, b) => a.index - b.index);
}

const S = { projects: [], cur: null, tab: "plan", status: null, routeSel: 0 };

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) {
    o.method = o.method || "POST";
    o.headers = { "Content-Type": "application/json" };
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  let r;
  try {
    r = await fetch(path, o);
  } catch {
    throw new Error("앱이 꺼져 있어요. 바탕화면 '논문 AI' 아이콘으로 다시 켠 뒤 이 페이지를 새로고침(F5) 해주세요.");
  }
  if (r.status === 401 && !path.startsWith("/api/auth/")) { showLogin(); throw new Error("로그인이 필요해요"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), ms);
}

// ---------- 백그라운드 작업 표시 ----------
// 실행 전 예상 금액 (Claude일 때만, 로컬 AI면 무료)
S.est = {};
async function loadEst(pid) {
  try { S.est[pid] = await api(`/api/projects/${pid}/estimates`); } catch { S.est[pid] = null; }
  return S.est[pid];
}
function costTag(pid, key, mult = 1) {
  const e = S.est[pid];
  if (!e) return "";
  if (e.engine !== "claude") return `<span class="cost free" title="내 컴퓨터 로컬 AI로 해요">무료</span>`;
  if (e.sub) return `<span class="cost free" title="내 Claude 구독(Claude Code)으로 해요 — 추가 요금 없음, 구독 사용량 안에서">구독</span>`;
  const w = Math.round((e.won[key] || 0) * mult);
  if (!w) return `<span class="cost free">0원</span>`;
  return `<span class="cost" title="Claude 사용 예상 금액(어림값). 원고가 그대로면 다시 돌리지 않아 0원이에요.">약 ${w.toLocaleString()}원</span>`;
}

function fmtEta(sec) {
  if (sec < 60) return `${sec}초`;
  const m = Math.round(sec / 60);
  return m < 60 ? `${m}분` : `${Math.floor(m / 60)}시간 ${m % 60}분`;
}

function runJob(id, title, { onEmit, onDone, logEl, onLog } = {}) {
  if (document.querySelector(`.job[data-id="${id}"]`)) {  // 이미 화면에 있는 작업 (예: 읽는 중에 폴더를 더 고름)
    toast("이미 진행 중인 작업이에요 — 끝나면 이어서 처리해요");
    return Promise.resolve(null);
  }
  const card = document.createElement("div");
  card.className = "job";
  card.dataset.id = id;
  card.innerHTML = `<div class="h"><span class="spin"></span><span class="grow">${esc(title)}</span><button class="icon" title="중지">■</button></div><div class="pg" hidden><div class="pg-bar"><span></span></div><div class="pg-t small"></div></div><div class="l" title="눌러서 전체 기록 보기">시작…</div><pre class="full" hidden></pre>`;
  $("#jobs").appendChild(card);
  $("button", card).onclick = () => api(`/api/jobs/${id}/stop`, { method: "POST" });
  $(".l", card).onclick = () => { const f = $(".full", card); f.hidden = !f.hidden; if (!f.hidden) f.scrollTop = f.scrollHeight; };
  let emitted = 0;
  return new Promise((resolve) => {
    const tick = async () => {
      let j;
      try { j = await api(`/api/jobs/${id}`); } catch (e) {
        if (/작업 없음/.test(e.message)) {  // 앱이 다시 켜지면서 끊긴 작업
          $(".spin", card)?.remove();
          $(".l", card).textContent = "앱이 다시 켜지면서 멈췄어요. 필요하면 다시 실행해 주세요.";
          $(".h", card).insertAdjacentHTML("afterbegin", "⏹");
          setTimeout(() => card.remove(), 6000);
          onDone && onDone(null, { status: "stopped", log: ["앱이 다시 켜지면서 멈췄어요"] });
          resolve(null);
          return;
        }
        setTimeout(tick, 2000);
        return;
      }
      const last = j.log[j.log.length - 1] || "";
      $(".l", card).textContent = last;
      const full = $(".full", card);
      if (!full.hidden) { full.textContent = j.log.slice(-40).join("\n"); full.scrollTop = full.scrollHeight; }
      const pg = j.progress;
      const pgText = pg ? `${pg.pct}% · ${pg.n}/${pg.total}${pg.eta ? ` · 약 ${fmtEta(pg.eta)} 남음` : ""}` : "";
      if (pg) { $(".pg", card).hidden = false; $(".pg-bar span", card).style.width = pg.pct + "%"; $(".pg-t", card).textContent = pgText; }
      const le = logEl && document.getElementById(logEl);
      if (le) {
        le.textContent = j.log.join("\n");
        le.scrollTop = le.scrollHeight;
        if (pg) {  // 점검 카드 안에도 진행 막대
          let bar = le.previousElementSibling;
          if (!bar || !bar.classList.contains("pg")) { bar = document.createElement("div"); bar.className = "pg inline"; bar.innerHTML = `<div class="pg-bar"><span></span></div><div class="pg-t small"></div>`; le.before(bar); }
          $(".pg-bar span", bar).style.width = pg.pct + "%";
          $(".pg-t", bar).textContent = pgText;
        }
      }
      onLog && onLog(last, j);
      while (onEmit && emitted < j.emitted.length) onEmit(j.emitted[emitted++]);
      if (j.status === "running") { setTimeout(tick, 1000); return; }
      $(".spin", card).remove();
      $(".h", card).insertAdjacentHTML("afterbegin", j.status === "done" ? "✅" : j.status === "error" ? "⚠️" : "⏹");
      if (j.status === "error") {  // 오류 상자는 직접 닫을 때까지 남김
        card.classList.add("err");
        const f = $(".full", card);
        f.textContent = j.log.slice(-40).join("\n");
        f.hidden = false;
        const b = $("button", card);
        b.textContent = "✕"; b.title = "닫기"; b.onclick = () => card.remove();
      } else setTimeout(() => card.remove(), 4000);
      refreshStatus();
      onDone && onDone(j.result, j);
      resolve(j);
    };
    tick();
  });
}

// ---------- 상태 ----------
async function refreshStatus() {
  const el = $("#status");
  try {
    const s = await api("/api/status");
    S.status = s;
    if (s.guest) {
      el.className = s.me?.has_key ? "status ok" : "status bad";
      el.textContent = s.me?.has_key ? `${s.me.name}님 · 내 자료 ${s.papers}편` : `${s.me?.name || ""}님 · Claude 키를 넣어 주세요`;
      return;
    }
    const claudeReady = s.settings.engine === "claude" && (s.settings.claude_key || s.settings.claude_via === "subscription");
    el.onclick = null;
    el.style.cursor = "";
    if (claudeReady) {
      el.className = "status ok";
      el.textContent = `Claude 연결됨 · 내 자료 ${s.papers}편`;
    } else if (!s.models) {  // Ollama도 Claude도 없음: 처음 쓰는 사람
      el.className = "status bad";
      el.textContent = s.cli ? "Claude 연결이 필요해요 (눌러서 연결)" : "Claude 키를 넣어 주세요 (눌러서 연결)";
      el.style.cursor = "pointer";
      el.onclick = () => (s.cli ? subWizard() : claudeWizard(() => refreshStatus()));
    } else if (!s.models.includes(s.settings.model)) {
      el.className = "status bad";
      el.textContent = `모델 ${s.settings.model} 없음 — 설정 확인`;
    } else {
      el.className = "status ok";
      el.textContent = `로컬 AI 연결됨 · 내 자료 ${s.papers}편`;
    }
  } catch {
    el.className = "status bad";
    el.textContent = "앱이 꺼져 있어요 — 아이콘으로 다시 켜주세요";
  }
}

// ---------- 프로젝트 목록 ----------
async function loadProjects() {
  S.projects = await api("/api/projects");
  $("#projectList").innerHTML = S.projects
    .map((p) => `<div class="proj ${S.cur && S.cur.id === p.id ? "active" : ""}" data-id="${p.id}">${esc(p.title)}<small>${esc(p.updated)}</small></div>`)
    .join("");
  $$(".proj").forEach((d) => (d.onclick = () => openProject(+d.dataset.id)));
}

async function createProject(topic, idea = "") {
  topic = topic.trim();
  if (!topic) return toast("주제를 적어주세요");
  const { id } = await api("/api/projects", { json: { topic } });
  await api(`/api/projects/${id}/guide`, { method: "PUT", json: { persona: "start" } });
  S.guideOpen = true;
  await openProject(id);
  startPlan(idea);
}

async function openProject(id, tab) {
  S.cur = await api(`/api/projects/${id}`);
  S.routeSel = S.cur.route || 0;
  S.tab = tab || (S.cur.content ? "write" : S.cur.plan ? "plan" : "plan");
  loadProjects();
  renderProject();
}

function showWelcome() {
  S.cur = null;
  loadProjects();
  $("#main").innerHTML = $("#tpl-welcome").innerHTML;
  S.persona = S.persona || "";
  const pick = (k) => {
    S.persona = k;
    $$(".persona", $("#main")).forEach((c) => c.classList.toggle("on", c.dataset.p === k));
    $("#wStart").style.display = k === "start" ? "" : "none";
    $("#wImport").style.display = k === "start" ? "none" : "";
    $("#wImpTitle").textContent = k === "revise" ? "심사·수정할 원고를 불러오세요" : "쓰고 있는 원고를 불러오세요";
  };
  $$(".persona", $("#main")).forEach((c) => (c.onclick = () => pick(c.dataset.p)));
  if (S.persona) pick(S.persona);
  $("#welcomeGo").onclick = () => createProject($("#welcomeTopic").value, $("#welcomeIdea").value);
  $("#wImpFile").onclick = () => $("#wImpInput").click();
  const done = async (fd) => {
    const { id } = await api("/api/import", { method: "POST", body: fd });
    await api(`/api/projects/${id}/guide`, { method: "PUT", json: { persona: S.persona === "revise" ? "revise" : "draft" } });
    S.guideOpen = true;
    await openProject(id, "review");
    toast("원고를 불러왔어요. 점검할 항목을 골라 실행하세요.");
  };
  $("#wImpInput").onchange = (e) => { const fd = new FormData(); fd.append("file", e.target.files[0]); done(fd); };
  $("#wImpPasteBtn").onclick = () => { $("#wImpPaste").style.display = "block"; $("#wImpText").focus(); };
  $("#wImpGo").onclick = () => {
    const t = $("#wImpText").value.trim();
    if (!t) return toast("원고를 붙여넣어 주세요");
    const fd = new FormData();
    fd.append("text", t);
    done(fd);
  };
}

function renderProject() {
  const p = S.cur;
  $("#main").innerHTML = `
    <div class="tabs">
      <div class="ptitle" title="눌러서 제목 바꾸기">${esc(p.title)}</div>
      <button class="tab" data-tab="plan">① 연구계획</button>
      <button class="tab" data-tab="collect">② 자료수집</button>
      <button class="tab" data-tab="matrix">③ 문헌분석</button>
      <button class="tab" data-tab="write">④ 집필</button>
      <button class="tab" data-tab="review">⑤ 원고 점검</button>
      <span class="grow"></span>
      <span class="badge ${p.engine === "claude" ? "" : "gray"}" title="이 프로젝트에서 쓰는 AI">${p.engine === "claude" ? "Claude" : "로컬 AI"}</span>
      <button class="ghost small" id="delProj">삭제</button>
    </div>
    <div id="guideBar"></div>
    <div id="tabBody"></div>`;
  renderGuide();
  $$(".tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === S.tab);
    b.onclick = () => { saveNow(); S.tab = b.dataset.tab; renderProject(); };
  });
  $(".ptitle").onclick = async () => {
    const t = prompt("논문 제목", p.title);
    if (t) { await api(`/api/projects/${p.id}`, { method: "PUT", json: { title: t } }); p.title = t; renderProject(); loadProjects(); }
  };
  $("#delProj").onclick = async () => {
    if (!confirm(`'${p.title}' 프로젝트를 삭제할까요? (내려받은 PDF는 그대로 남습니다)`)) return;
    await api(`/api/projects/${p.id}`, { method: "DELETE" });
    showWelcome();
  };
  ({ plan: renderPlan, collect: renderCollect, matrix: renderMatrix, write: renderWrite, review: renderReview })[S.tab]();
}

// ---------- 🧭 길잡이 (유형별 6단계) ----------
const mxCount = (p) => { try { return Object.keys(JSON.parse(p.matrix || "{}")).length; } catch { return 0; } };
const txtLen = (p) => (p.content || "").replace(/<[^>]+>/g, "").trim().length;
const has = (p, k) => !!(p.reports || {})[k];
const GUIDE = {
  start: { name: "🌱 처음 시작해요", sub: "주제만 있어요", steps: [
    { t: "주제 정하고 연구계획 세우기", why: "주제를 적으면 선행연구를 조사해 연구 경로 3개를 추천해요. 마음에 드는 경로를 고르거나 내 생각을 제안하세요.", tab: "plan", a: "plan", done: (p) => !!p.plan },
    { t: "논문 PDF 모으기", why: "고른 경로에 맞는 논문을 내려받고, 내 컴퓨터·외장하드의 PDF도 읽어 들여요.", tab: "collect", a: "#scanBtn", done: () => (S.status?.papers || 0) >= 10 },
    { t: "문헌분석표 만들기", why: "논문마다 목적·대상·변인·측정도구·결과를 표로 정리해요. 모든 칸에 원문 근거가 달려요.", tab: "matrix", a: "#mxPick", done: (p) => mxCount(p) >= 5 },
    { t: "연구 공백 찾고 연구모형 그리기", why: "선행연구가 아직 안 한 것을 숫자로 확인하고, 내 연구의 변인 관계를 그림으로 정리해요.", tab: "matrix", a: "#mxGap", done: (p) => has(p, "gaps") },
    { t: "서론부터 장별로 쓰기", why: "서론 초안과 장별 집필 모두 문장마다 근거가 달려요. AI 초안(보라색)은 꼭 내 말로 고쳐 쓰세요.", tab: "write", a: "#aiWrite", done: (p) => txtLen(p) > 3000 },
    { t: "예비심사 대비 점검", why: "일목요연한지 보고, 심사위원 3인 모의 심사로 나올 질문을 미리 받아봐요.", tab: "review", a: "#tool-panel", done: (p) => has(p, "panel") || has(p, "structure") },
  ] },
  draft: { name: "✍ 초안이 있어요", sub: "어느 정도 썼어요", steps: [
    { t: "원고 불러오기", why: "한글(.hwpx)·워드(.docx) 원고를 올리면 (저자, 연도) 인용을 자동으로 알아봐요.", tab: "review", a: "#impFile", done: (p) => txtLen(p) > 500 },
    { t: "참조할 논문 폴더 고르기", why: "인용한 논문 원문이 있는 폴더(외장하드·구글 드라이브 포함)를 고르면 그 안에서만 근거를 찾아요.", tab: "collect", a: "#refFolders", done: (p) => (p.ref_papers || 0) >= 5 },
    { t: "출처 확인하기", why: "원고 속 인용이 원문과 맞는지, 근거 없이 주장한 문장은 없는지 확인해요.", tab: "write", a: "#autoAll", done: (p) => has(p, "cites") || has(p, "uncited") },
    { t: "선행연구 보강하기", why: "문헌분석표로 선행연구를 정리하고, 최신 연구와 연구 공백을 확인해 서론·이론적 배경을 단단하게 해요.", tab: "matrix", a: "#mxPick", done: (p) => mxCount(p) >= 5 || has(p, "recent") },
    { t: "논문의 줄기 점검", why: "연구문제→선행연구→방법→결과→결론이 한 줄기인지 예비심사 리뷰로 봐요.", tab: "review", a: "#tool-structure", done: (p) => has(p, "structure") },
    { t: "참고문헌·문장 마무리", why: "본문 인용과 참고문헌 목록을 맞추고, 비문·시제·과장 표현을 다듬어요.", tab: "review", a: "#tool-refs", done: (p) => has(p, "refs") && has(p, "style") },
  ] },
  revise: { name: "🎓 심사·수정을 앞두고 있어요", sub: "거의 완성했어요", steps: [
    { t: "원고 불러오기", why: "최신 원고를 올려 주세요. 다시 올리면 지금 원고를 바꿔요.", tab: "review", a: "#impFile", done: (p) => txtLen(p) > 500 },
    { t: "심사위원 3인 모의 심사", why: "방법론·이론·지도교수형 심사위원이 각자 심사하고 토론해 판정해요. 심사 종류(예비심사·본심사·학술지)를 고르세요.", tab: "review", a: "#tool-panel", done: (p) => has(p, "panel") },
    { t: "답변서 쓰고 재심사 받기", why: "수정 요구마다 어떻게 고쳤는지 적으면 재심사로 해결됐는지 알려줘요. 실제 심사 답변 연습도 돼요.", tab: "review", a: "#tool-panel", done: (p) => (p.reports?.panel?.data?.round || 1) > 1 },
    { t: "인용·참고문헌 최종 확인", why: "가짜 인용이나 연도 불일치는 심사에서 치명적이에요. 원문 대조와 참고문헌 대조를 해요.", tab: "review", a: "#tool-cites", done: (p) => has(p, "cites") && has(p, "refs") },
    { t: "문장 다듬기 · AI 문체 점검", why: "비문·과장 표현을 고치고, AI가 쓴 티가 나는 문장을 자연스럽게 다듬어요.", tab: "review", a: "#tool-aitone", done: (p) => has(p, "style") || has(p, "aitone") },
    { t: "투고 준비", why: "투고할 학술지 후보를 보고, AI 사용 공개 문구를 투고 규정에 맞게 넣어요.", tab: "matrix", a: "#jnGo", done: (p) => has(p, "journals") },
  ] },
};

function renderGuide() {
  const box = $("#guideBar");
  const p = S.cur;
  if (!box || !p) return;
  const persona = p.reports?.guide?.persona;
  if (!persona) {
    box.innerHTML = `<div class="guide ask"><b>🧭 길잡이</b> 지금 어디쯤 계세요? 고르시면 해야 할 일을 순서대로 알려드려요.
      ${Object.entries(GUIDE).map(([k, g]) => `<button class="small" data-persona="${k}">${g.name} <span class="muted">${g.sub}</span></button>`).join("")}
      <button class="small ghost" id="guideHide">나중에</button></div>`;
  } else {
    const g = GUIDE[persona];
    const st = g.steps.map((x) => !!x.done(p));
    const cur = st.indexOf(false);
    const open = S.guideOpen;
    box.innerHTML = `<div class="guide"><div class="row"><b>🧭 ${g.name}</b>
        <span class="g-prog">${st.map((d, i) => `<i class="${d ? "d" : i === cur ? "c" : ""}" title="${esc(g.steps[i].t)}"></i>`).join("")}</span>
        <span class="small muted">${st.filter(Boolean).length}/${st.length}</span>
        ${cur >= 0 ? `<span class="grow small">지금 할 일: <b>${cur + 1}. ${esc(g.steps[cur].t)}</b></span><button class="small primary" data-go="${cur}">여기서 하기 →</button>` : `<span class="grow small">🎉 모든 단계를 마쳤어요!</span>`}
        <button class="small ghost" id="guideToggle">${open ? "접기 ▴" : "전체 단계 ▾"}</button></div>
      ${open ? `<ol class="g-steps">${g.steps.map((x, i) => `<li class="${st[i] ? "d" : i === cur ? "c" : ""}"><div class="row"><b class="grow">${st[i] ? "✔" : i + 1 + "."} ${esc(x.t)}</b><button class="small" data-go="${i}">여기서 하기</button></div><div class="small muted">${esc(x.why)}</div></li>`).join("")}</ol>
        <div class="small muted">유형 바꾸기: ${Object.entries(GUIDE).map(([k, gg]) => `<a href="#" data-persona="${k}">${gg.name}</a>`).join(" · ")} · 막히면 오른쪽 아래 <b>💬 도우미</b>에게 물어보세요.</div>` : ""}</div>`;
    $$("[data-go]", box).forEach((b) => (b.onclick = () => { const x = g.steps[+b.dataset.go]; goTo(x.tab, x.a); }));
    $("#guideToggle").onclick = () => { S.guideOpen = !S.guideOpen; renderGuide(); };
  }
  $$("[data-persona]", box).forEach((b) => (b.onclick = async (e) => {
    e.preventDefault();
    await api(`/api/projects/${p.id}/guide`, { method: "PUT", json: { persona: b.dataset.persona } });
    S.cur = await api(`/api/projects/${p.id}`);
    S.guideOpen = true;
    renderGuide();
  }));
  if ($("#guideHide")) $("#guideHide").onclick = () => (box.innerHTML = "");
}

// 기능 위치로 이동: 탭을 열고 그 버튼·카드를 깜빡여 보여줌
function goTo(tab, anchor) {
  if (tab === "settings") { renderSettings(); return; }
  if (!S.cur) { toast("먼저 논문 프로젝트를 열어 주세요"); return; }
  saveNow();
  if (S.tab !== tab) { S.tab = tab; renderProject(); }
  if (!anchor || anchor === tab) return;
  let n = 0;
  const t = setInterval(() => {
    const el = $(anchor);
    if (el || ++n > 30) {
      clearInterval(t);
      if (!el) return;
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      el.classList.add("g-flash");
      setTimeout(() => el.classList.remove("g-flash"), 2400);
    }
  }, 150);
}

// ---------- 💬 도우미 채팅 ----------
S.chat = {};
function chatKey() { return S.cur ? S.cur.id : 0; }
function renderChat() {
  let w = $("#chatWin");
  const msgs = S.chat[chatKey()] || [];
  if (!w) return;
  const body = $("#chatBody", w);
  body.innerHTML = (msgs.length ? "" : `<div class="cm bot">안녕하세요! 논문 쓰다 막히는 것, 앱 사용법, 무엇이든 물어보세요.${S.cur ? `<br><span class="muted small">지금 '${esc(S.cur.title)}' 진행 상황을 알고 답해요.</span>` : ""}
      <div class="chips">${["다음에 뭘 하면 돼요?", "인용 점검 해 줘", "모의 심사 돌려 줘", "예비심사 준비는 어떻게 해요?"].map((q) => `<button class="small" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div></div>`)
    + msgs.map((m, i) => `<div class="cm ${m.role === "user" ? (m.auto ? "me auto" : "me") : "bot"}">${esc(m.content).replace(/\n/g, "<br>")}
      ${(m.actions || []).length ? `<div class="chips">${m.actions.map((a) => `<button class="small ${m.run ? "" : "primary"}" data-tab="${a.tab}" data-anchor="${esc(a.anchor)}">${esc(a.label)} →</button>`).join("")}</div>` : ""}
      ${m.run ? runBox(m.run, i) : ""}</div>`).join("")
    + (S.chatBusy ? `<div class="cm bot"><span class="spin"></span> 생각하는 중…</div>` : "");
  body.scrollTop = body.scrollHeight;
  $$("[data-q]", body).forEach((b) => (b.onclick = () => sendChat(b.dataset.q)));
  $$("[data-anchor]", body).forEach((b) => (b.onclick = () => goTo(b.dataset.tab, b.dataset.anchor)));
  $$("[data-run-go]", body).forEach((b) => (b.onclick = () => chatRun(+b.dataset.runGo)));
  $$("[data-run-no]", body).forEach((b) => (b.onclick = () => { msgs[+b.dataset.runNo].run.state = "cancel"; renderChat(); }));
}

// 도우미가 제안한 일: 예상 금액을 보여 주고, 누르면 실행 → 끝나면 결과 요약
const RUN_EST = { matrix: ["matrix_per_paper", 10] };
function runBox(r, i) {
  const pid = S.cur?.id;
  const [key, mult] = RUN_EST[r.tool] || [r.tool, 1];
  if (r.state === "cancel") return `<div class="run-box muted small">취소했어요.</div>`;
  if (r.state === "running") return `<div class="run-box small"><span class="spin"></span> <b>${esc(r.label)}</b> 실행 중… <span class="run-pg">${esc(r.pg || "")}</span></div>`;
  if (r.state === "done") return `<div class="run-box small">✅ <b>${esc(r.label)}</b> 끝났어요. <button class="small ghost" data-tab="${r.tab}" data-anchor="${r.tab === "review" ? "#tool-" + r.tool : r.tool === "gaps" ? "#mxGap" : r.tool === "intro" ? "#mxIntro" : r.tool === "write" ? "#aiWrite" : "#mxPick"}">결과 보러 가기 →</button></div>`;
  if (r.state === "error") return `<div class="run-box small" style="color:var(--warn)">⚠ ${esc(r.err || "실행하지 못했어요")}</div>`;
  if (r.blocked) return `<div class="run-box small">🛠 <b>${esc(r.label)}</b> — 지금은 못 해요: ${esc(r.blocked)}</div>`;
  return `<div class="run-box">🛠 <b>${esc(r.label)}</b>${r.why ? `<div class="small muted">${esc(r.why)}</div>` : ""}${r.tool === "write" ? `<div class="small" style="color:var(--warn);margin-top:4px">⚠ AI가 쓴 초안이 원고 끝에 덧붙어요. 그대로 내지 말고 근거를 확인하며 내 말로 고쳐 주세요. 실행 중에는 원고를 고치지 마세요.</div>` : ""}
    <div class="row" style="margin-top:6px;gap:6px">${pid ? costTag(pid, key, mult) : ""}<span class="grow"></span><button class="small ghost" data-run-no="${i}">취소</button><button class="small primary" data-run-go="${i}">실행</button></div></div>`;
}
async function chatRun(i) {
  const k = chatKey();
  const m = S.chat[k][i];
  const r = m.run;
  const pid = S.cur?.id;
  if (!pid || r.state) return;
  r.state = "running";
  renderChat();
  let job;
  try { ({ job } = await api(`/api/projects/${pid}/assist_run`, { json: { tool: r.tool } })); }
  catch (e) { r.state = "error"; r.err = e.message; renderChat(); return; }
  runJob(job, r.label + " (도우미)", {
    onLog: (_, j) => { if (j.progress) { r.pg = `${j.progress.pct}%`; const el = $("#chatBody .run-pg"); if (el) el.textContent = r.pg; } },
    onDone: async (_, j) => {
      if (j.status !== "done") { r.state = "error"; r.err = (j.log || []).slice(-1)[0] || "멈췄어요"; renderChat(); return; }
      r.state = "done";
      if (S.cur?.id === pid) { S.cur = await api(`/api/projects/${pid}`); S.est[pid] = null; if (r.tool === "write" && S.tab === "write") renderWrite(); }
      renderChat();
      followUp(k, r.tool, `(실행 완료: ${r.label}) 결과를 쉽게 알려 줘`);
    },
  });
}
async function followUp(k, tool, text) {
  (S.chat[k] = S.chat[k] || []).push({ role: "user", content: text, auto: true });
  S.chatBusy = true;
  renderChat();
  try {
    const { job } = await api("/api/assist", { json: { project_id: S.cur?.id || null, ran: tool, messages: S.chat[k].map(({ role, content }) => ({ role, content })) } });
    runJob(job, "도우미", { onDone: (r, j) => { S.chatBusy = false; S.chat[k].push(j.status === "done" && r ? { role: "bot", content: r.answer, actions: r.actions, run: r.run } : { role: "bot", content: "결과를 요약하지 못했어요. 결과 화면에서 직접 확인해 주세요." }); renderChat(); } });
  } catch (e) { S.chatBusy = false; renderChat(); }
}
async function sendChat(text) {
  text = (text || "").trim();
  if (!text || S.chatBusy) return;
  const k = chatKey();
  (S.chat[k] = S.chat[k] || []).push({ role: "user", content: text });
  S.chatBusy = true;
  renderChat();
  try {
    const { job } = await api("/api/assist", { json: { project_id: S.cur?.id || null, messages: S.chat[k].map(({ role, content }) => ({ role, content })) } });
    runJob(job, "도우미", {
      onDone: (r, j) => {
        S.chatBusy = false;
        S.chat[k].push(j.status === "done" && r ? { role: "bot", content: r.answer, actions: r.actions, run: r.run } : { role: "bot", content: "답을 만들지 못했어요. " + ((j.log || []).slice(-1)[0] || "") });
        if (r?.run && S.cur && !S.est[S.cur.id]) loadEst(S.cur.id).then(renderChat);
        renderChat();
      },
    });
  } catch (e) {
    S.chatBusy = false;
    S.chat[k].push({ role: "bot", content: "연결에 실패했어요: " + e.message });
    renderChat();
  }
}
(function mountChat() {
  const fab = document.createElement("button");
  fab.id = "chatFab";
  fab.textContent = "💬 도우미";
  document.body.appendChild(fab);
  const w = document.createElement("div");
  w.id = "chatWin";
  w.className = "hidden";
  w.innerHTML = `<div class="ch-head"><b>💬 논문 도우미</b><span class="grow"></span><button class="ghost small" id="chatClear" title="대화 지우기">새 대화</button><button class="ghost small" id="chatClose">✕</button></div>
    <div id="chatBody"></div>
    <div class="ch-in"><textarea id="chatText" rows="2" placeholder="질문을 적고 Enter (줄바꿈은 Shift+Enter)"></textarea><button class="primary" id="chatSend">보내기</button></div>`;
  document.body.appendChild(w);
  fab.onclick = () => { w.classList.toggle("hidden"); renderChat(); if (!w.classList.contains("hidden")) $("#chatText").focus(); };
  $("#chatClose").onclick = () => w.classList.add("hidden");
  $("#chatClear").onclick = () => { S.chat[chatKey()] = []; renderChat(); };
  $("#chatSend").onclick = () => { sendChat($("#chatText").value); $("#chatText").value = ""; };
  $("#chatText").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#chatSend").click(); } };
})();

// ---------- ① 연구계획 ----------
let planJob = {};
function startPlan(idea = "") {
  const id = S.cur.id;
  api(`/api/projects/${id}/plan`, { json: { idea } }).then(({ job }) => {
    planJob[id] = job;
    if (S.cur && S.cur.id === id) renderPlan();
    runJob(job, "연구계획 세우는 중", {
      logEl: "planLog",
      onDone: async (r, j) => {
        delete planJob[id];
        if (S.cur && S.cur.id === id) {
          S.cur = await api(`/api/projects/${id}`);
          S.routeSel = S.cur.route || 0;
          if (S.tab === "plan") renderPlan();
        }
        if (j.status === "done") toast("연구계획이 완성됐어요. 경로를 골라 승인해 주세요.");
      },
    });
  });
}

function litCite(w) {
  const sur = (a) => (/[가-힣]/.test(a) ? a.replace(/\s/g, "") : a.split(/\s+/).pop());
  const a = w.authors || [];
  const name = !a.length ? "저자미상" : a.length === 1 ? sur(a[0]) : a.length === 2 ? `${sur(a[0])} & ${sur(a[1])}` : `${sur(a[0])} 외`;
  return `${name}, ${w.year || "연도미상"}`;
}

// AI 요약 속 [번호]를 (저자, 연도)로 바꾸고, 누르면 아래 선행연구 목록의 해당 논문으로 이동
function refLinks(text, lit) {
  return esc(text)
    .replace(/\[(\d{1,3})\](?:\s*(?:와|과|,|및)\s*(?=\[))?/g, (m, n) => {
      const w = lit[+n - 1];
      if (!w) return `<span class="ref bad" title="AI가 목록에 없는 번호를 썼어요 — 믿지 마세요">[${n}?]</span>`;
      return `<span class="ref" data-lit="${+n - 1}" title="${esc(w.title)}">(${esc(litCite(w))})</span>` + (m.length > `[${n}]`.length ? " " : "");
    });
}

function renderPlan() {
  const p = S.cur;
  const body = $("#tabBody");
  if (planJob[p.id] || !p.plan) {
    body.innerHTML = `<div class="pad">
      <div class="card"><h3>논문 주제</h3>
        <textarea id="topic" rows="3">${esc(p.topic)}</textarea>
        <h3 style="margin-top:12px">생각해 둔 연구 방향 <span class="muted small">(선택 — 적으면 AI 추천과 함께 정리해 드려요)</span></h3>
        <textarea id="idea" rows="3" placeholder="예) 상담 대학원생 200명에게 설문해서 애착 불안 → 자기자비 → 우울 매개모형을 검증하고 싶다"></textarea>
        <div class="row" style="margin-top:8px"><button class="primary" id="go" ${planJob[p.id] ? "disabled" : ""}>연구계획 세우기</button>
        <span class="muted small">인터넷 학술 색인 조사 + 로컬 AI 분석 (보통 2~6분)</span></div></div>
      ${planJob[p.id] ? `<div class="card"><h3><span class="spin"></span> 진행 상황</h3><div class="log" id="planLog">시작…</div></div>` : ""}
    </div>`;
    const go = $("#go");
    go.onclick = async () => {
      const topic = $("#topic").value.trim();
      await api(`/api/projects/${p.id}`, { method: "PUT", json: { topic } });
      p.topic = topic;
      startPlan($("#idea").value);
    };
    return;
  }
  const pl = p.plan, lit = pl.literature || [];
  const rec = (pl.recommended || 1) - 1;
  body.innerHTML = `<div class="pad">
    <div class="card"><div class="row"><h3 class="grow">주제: ${esc(p.topic)}</h3><button class="small" id="replan">다시 세우기</button></div>
      <div class="small muted">검색어: ${esc((pl.keywords?.ko_keywords || []).join(", "))} · ${esc((pl.keywords?.en_keywords || []).join(", "))}</div></div>
    <div class="card"><p class="small muted" style="margin-top:0">※ 아래 요약은 AI가 논문 제목·초록을 보고 쓴 것이라 집필 단계처럼 원문 대조 검증은 하지 않았어요. 파란 (저자, 연도)를 누르면 해당 논문으로 이동합니다.</p><h3>배경</h3><p>${refLinks(pl.background, lit)}</p>
      <h3>선행연구의 흐름</h3><p>${refLinks(pl.literature_flow, lit)}</p>
      <h3>연구 공백</h3><ul>${(pl.gaps || []).map((g) => `<li>${refLinks(g, lit)}</li>`).join("")}</ul></div>
    <h3>추천 연구 경로 — 하나를 골라 승인하세요</h3>
    <div class="routes">${(pl.routes || []).map((r, i) => `
      <div class="route ${i === S.routeSel ? "sel" : ""}" data-i="${i}">
        ${r.user ? `<span class="badge warn">내 제안</span>` : i === rec ? `<span class="badge">AI 추천</span>` : ""}
        <h4>${i + 1}. ${esc(r.name)}</h4>
        ${r.user ? `<div class="small muted">처음 쓰신 내용: “${esc(r.idea)}”</div>` : ""}
        <dl>
          ${r.thesis ? `<dt>한 줄기</dt><dd><b>${esc(r.thesis)}</b></dd>` : ""}
          <dt>연구문제</dt><dd><ul>${(r.questions || []).map((q) => `<li>${esc(q)}</li>`).join("")}</ul></dd>
          <dt>방법</dt><dd>${esc(r.method)}</dd>
          <dt>대상·자료</dt><dd>${esc(r.data)}</dd>
          <dt>분석</dt><dd>${esc(r.analysis)}</dd>
          <dt>장점</dt><dd>${esc(r.pros)}</dd>
          <dt>어려움</dt><dd>${esc(r.cons)}</dd>
          <dt>목차</dt><dd><ul>${(r.outline || []).map((o) => `<li>${esc(o)}</li>`).join("")}</ul></dd>
          ${r.advice ? `<dt>AI의 보완 제안</dt><dd>${esc(r.advice)}</dd>` : ""}
        </dl>
        ${r.user ? `<div class="row" style="margin-top:8px"><button class="small" data-edit="${i}">고쳐서 다시 제안</button><button class="small ghost" data-del="${i}">빼기</button></div>` : ""}
      </div>`).join("")}</div>
    <div class="card" style="margin-top:12px"><h3>✏️ 내가 생각한 경로 제안하기</h3>
      <p class="small muted">하고 싶은 연구를 편하게 적어주세요. AI가 연구문제·방법·목차로 정리하고, 선행연구에 비추어 보완할 점을 알려드려요.</p>
      <textarea id="myIdea" rows="3" placeholder="예) 상담 대학원생 200명에게 설문해서 애착 불안 → 자기자비 → 우울 매개모형을 검증하고 싶다"></textarea>
      <div class="row" style="margin-top:8px"><button class="primary" id="addRoute">내 경로로 정리하기</button><span class="muted small">약 1~2분</span></div></div>
    <div class="card" style="margin-top:12px"><b>추천 이유</b> ${refLinks(pl.recommend_reason, lit)}</div>
    <div class="card row"><div class="grow"><b>승인하면</b> 고른 경로에 맞는 논문을 인터넷에서 직접 찾아 내려받습니다
      ${S.status?.settings?.lib_proxy_prefix || S.status?.settings?.lib_login_url ? "(무료가 아닌 논문은 대학 도서관 로그인 경유)" : "(무료 공개 논문만 — 도서관 연결은 설정에서)"}.</div>
      <div class="row" style="flex-wrap:nowrap"><button id="approveNight" title="오늘 밤 로컬 AI로 무료 처리">🌙 새벽 2시에 수집</button>
      <button class="primary" id="approve">이 경로로 승인 → 자료 수집</button></div></div>
    <div class="card"><h3>조사된 선행연구 ${lit.length}편 <span class="muted small">(실제 학술 색인 OpenAlex 검색 결과)</span></h3>
      <table><tr><th>#</th><th>제목</th><th>저자 · 연도</th><th>피인용</th><th>원문</th></tr>
      ${lit.map((w, i) => `<tr id="lit-${i}"><td>${i + 1}</td><td>${w.doi ? `<a href="https://doi.org/${esc(w.doi)}" target="_blank">${esc(w.title)}</a>` : esc(w.title)}</td>
        <td>${esc(w.authors.slice(0, 3).join(", "))}${w.authors.length > 3 ? " 외" : ""} · ${esc(w.year)}</td><td>${w.cited}</td>
        <td>${w.is_oa ? `<span class="badge">무료</span>` : `<span class="badge gray">구독</span>`}</td></tr>`).join("")}</table></div>
    ${pl.local?.length ? `<div class="card"><h3>내 컴퓨터에 이미 있는 관련 자료</h3>${pl.local.map((c) => `<div class="result" data-chunk="${c.id}"><span class="c">${esc(c.cite)}</span> <span class="t">${esc(c.title)} · p.${c.page + 1}</span></div>`).join("")}</div>` : ""}
  </div>`;
  $$(".ref[data-lit]", body).forEach((r) => (r.onclick = () => {
    const row = $("#lit-" + r.dataset.lit);
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    row.classList.add("flash");
    setTimeout(() => row.classList.remove("flash"), 1800);
  }));
  $("#approveNight").onclick = async () => {
    await api(`/api/projects/${p.id}`, { method: "PUT", json: { route: S.routeSel } });
    p.route = S.routeSel;
    scheduleNight("collect", { route: S.routeSel });
  };
  $$(".route").forEach((d) => (d.onclick = () => { S.routeSel = +d.dataset.i; $$(".route").forEach((x) => x.classList.toggle("sel", x === d)); }));
  $("#replan").onclick = () => { if (confirm("연구계획을 새로 세울까요? (내 제안 경로도 지워집니다)")) startPlan(); };
  $$(".route button", body).forEach((b) => b.addEventListener("click", (e) => e.stopPropagation()));
  $$("[data-del]", body).forEach((b) => (b.onclick = async (e) => {
    e.stopPropagation();
    await api(`/api/projects/${p.id}/route_del`, { json: { index: +b.dataset.del } });
    S.cur = await api(`/api/projects/${p.id}`); S.routeSel = S.cur.route || 0; renderPlan();
  }));
  $$("[data-edit]", body).forEach((b) => (b.onclick = (e) => {
    e.stopPropagation();
    $("#myIdea").value = pl.routes[+b.dataset.edit].idea || "";
    $("#myIdea").focus();
    $("#myIdea").scrollIntoView({ block: "center", behavior: "smooth" });
  }));
  $("#addRoute").onclick = async () => {
    const idea = $("#myIdea").value.trim();
    if (!idea) return toast("하고 싶은 연구를 적어주세요");
    $("#addRoute").disabled = true;
    const { job } = await api(`/api/projects/${p.id}/route_add`, { json: { idea } });
    runJob(job, "내 경로 정리 중", {
      onDone: async (r, j) => {
        if (S.cur?.id !== p.id) return;
        S.cur = await api(`/api/projects/${p.id}`);
        S.routeSel = S.cur.route || 0;
        if (S.tab === "plan") renderPlan();
        if (j.status === "done") { toast("내 경로를 정리했어요 — 선택된 상태입니다"); requestAnimationFrame(() => $(".route.sel")?.scrollIntoView({ block: "center" })); }
      },
    });
  };
  $$(".result[data-chunk]", body).forEach((d) => (d.onclick = () => showEvidence({ chunk: d.dataset.chunk, quote: "" })));
  $("#approve").onclick = async () => {
    const { job } = await api(`/api/projects/${p.id}/collect`, { json: { route: S.routeSel } });
    p.route = S.routeSel;
    collectJob[p.id] = job;
    S.tab = "collect";
    renderProject();
    runJob(job, "자료 수집 중", { logEl: "collectLog", onDone: () => { delete collectJob[p.id]; if (S.cur?.id === p.id && S.tab === "collect") renderCollect(); toast("자료 수집 완료 — ④ 집필로 넘어가세요"); } });
  };
}

// ---------- ② 자료수집 ----------
let collectJob = {};
async function renderCollect() {
  const p = S.cur;
  const running = collectJob[p.id];
  $("#tabBody").innerHTML = `<div class="pad">
    ${running ? `<div class="card"><h3><span class="spin"></span> 자료 수집 진행 상황</h3><div class="log" id="collectLog">…</div></div>` : ""}
    <div class="card"><h3>자료 더하기</h3>
      <div class="row">
        <button id="scanBtn">🔍 내 컴퓨터 PDF 찾아 읽기</button>
        <button id="upBtn">＋ PDF 파일 추가</button><input type="file" id="upFile" accept=".pdf" multiple hidden>
        ${p.plan ? `<button id="moreBtn">🌐 인터넷에서 더 수집</button>` : ""}
        <span class="grow"></span><button id="toMatrix">③ 문헌분석으로 →</button><button class="primary" id="toWrite">④ 집필로 →</button>
      </div>
      <p class="small muted owner-only">내 자료 폴더: ${esc((S.status?.settings?.scan_folders || []).join(" · "))} (설정에서 변경)</p></div>
    <div class="card owner-only" id="refFolders"><div class="row"><h3 class="grow">📁 이 논문이 참조할 폴더</h3><button class="primary" id="refPick">폴더 고르기</button></div>
      <p class="small muted" style="margin:2px 0 8px">정해 두면 이 논문의 근거 찾기·문헌분석·집필이 <b>고른 폴더 안의 PDF에서만</b> 이뤄져요. 다른 과목 자료가 섞이지 않아 더 빠르고 정확해요.</p>
      <p class="small fp-tip" style="margin:0 0 8px">💡 가능하시다면 <b>이 논문의 자료(논문 PDF)를 한 폴더에 모아 넣어 주시는 것을 추천드립니다.</b> 여러 곳에 흩어져 있으면 같은 논문이 여러 번 읽히거나, 있는 논문도 못 찾는 경우가 생겨요. 한 폴더 안에 주제별 하위 폴더를 만드는 건 괜찮아요.</p>
      ${(p.ref_folders || []).length ? `<div class="fp-sel">${p.ref_folders.map((f) => `<span class="fp-chip" title="${esc(f)}">📁 ${esc(f)}</span>`).join("")}</div>
        <div class="small muted" style="margin-top:6px">이 폴더들에서 읽힌 논문 <b>${p.ref_papers ?? 0}편</b></div>` : `<div class="small muted">지정 안 함 — 내 자료 전체(${S.status?.papers || 0}편)에서 찾아요</div>`}</div>
    ${S.status?.gdrive ? `<div class="card gd-card"><div class="row"><div class="grow"><b>☁ 구글 드라이브에도 논문이 있나요?</b>
      <div class="small muted">이 컴퓨터에 연결된 구글 드라이브(${esc(S.status.gdrive)})를 찾았어요. 추가하면 드라이브 속 PDF에서도 근거를 찾아요. ('다른 컴퓨터' 백업과 원고·IRB 폴더는 빼고 읽어요)</div></div>
      <button class="primary" id="gdAdd">추가하고 읽기</button></div></div>` : ""}
    <div class="card"><h3>내 자료 (최근 추가순)</h3><div id="plist" class="muted">불러오는 중…</div></div>
  </div>`;
  bindAddButtons(() => renderCollect());
  $("#refPick").onclick = () => chooseRefFolders(() => { if (S.tab === "collect") renderCollect(); });
  if ($("#gdAdd")) $("#gdAdd").onclick = async () => {
    const f = S.status.settings.scan_folders || [];
    await api("/api/settings", { json: { scan_folders: [f[0], S.status.gdrive, ...f.slice(1)].filter(Boolean) } });
    await refreshStatus();
    const { job } = await api("/api/scan", { method: "POST" });
    renderCollect();
    runJob(job, "구글 드라이브 PDF 읽는 중", { onDone: () => renderCollect() });
  };
  if ($("#moreBtn")) $("#moreBtn").onclick = async () => {
    const { job } = await api(`/api/projects/${p.id}/collect`, { json: { route: p.route || 0 } });
    collectJob[p.id] = job;
    renderCollect();
    runJob(job, "자료 수집 중", { logEl: "collectLog", onDone: () => { delete collectJob[p.id]; if (S.tab === "collect") renderCollect(); } });
  };
  $("#toWrite").onclick = () => { S.tab = "write"; renderProject(); };
  $("#toMatrix").onclick = () => { S.tab = "matrix"; renderProject(); };
  const papers = await api("/api/papers");
  $("#plist").innerHTML = paperTable(papers.slice(0, 40));
  bindPaperTable($("#plist"), renderCollect);
}

// ---------- ③ 문헌분석: 문헌분석표 → 연구 공백 → 서론 초안 ----------
let matrixJob = {};
const NONE_TXT = "원문에서 확인 안 됨";

async function renderMatrix() {
  const p = S.cur;
  const body = $("#tabBody");
  if (!body.innerHTML.trim()) body.innerHTML = `<div class="pad"><div class="card muted">불러오는 중…</div></div>`;
  let d;
  try { d = await api(`/api/projects/${p.id}/matrix`); } catch (e) { body.innerHTML = `<div class="pad"><div class="card">${esc(e.message)}</div></div>`; return; }
  if (S.cur?.id !== p.id || S.tab !== "matrix") return;
  await loadEst(p.id);
  const rep = p.reports || {};
  const mj = matrixJob[p.id];
  const num = Object.fromEntries(d.rows.map((r, i) => [r.paper_id, i + 1]));
  const cite = Object.fromEntries(d.rows.map((r) => [r.paper_id, r.cite]));
  const sug = d.candidates.filter((c) => c.suggested && !c.done);
  const refs = (ids) => (ids || []).filter((i) => num[i]).map((i) => `<span class="mx-ref" data-row="${i}">[${num[i]}] ${esc(cite[i])}</span>`).join(" ");
  const logBox = (kind) => (mj && mj.kind === kind ? `<div class="log" id="mxLog">시작…</div>` : "");
  const eng = (x) => (x.engine === "claude" ? "Claude" : "로컬 AI");

  const table = d.rows.length ? `<div class="mx-wrap"><table class="mx"><thead><tr><th>#</th><th>논문</th>${d.fields.map(([, t]) => `<th>${esc(t)}</th>`).join("")}<th></th></tr></thead><tbody>
    ${d.rows.map((r, i) => `<tr id="mxrow-${r.paper_id}"><td>${i + 1}</td><td class="mx-paper"><b>${esc(r.cite)}</b><div class="small muted">${esc(r.title)}</div></td>
      ${d.fields.map(([k]) => {
        const f = r.fields[k] || { v: "" };
        if (f.v === NONE_TXT) return `<td class="mx-none">—</td>`;
        return f.chunk ? `<td class="mx-ev" data-chunk="${f.chunk}" data-quote="${esc(f.quote)}" title="눌러서 원문 위치 보기">${esc(f.v)}</td>`
          : `<td class="mx-nochk" title="원문 위치를 확인하지 못한 칸 — 직접 확인해 주세요">${esc(f.v)}</td>`;
      }).join("")}
      <td><button class="small ghost" data-del="${r.paper_id}" title="표에서 빼기">✕</button></td></tr>`).join("")}
    </tbody></table></div>
    <p class="small muted" style="margin:6px 0 0">칸을 누르면 오른쪽에 원문 위치가 열려요 · 회색 기울임 칸은 원문 위치를 확인하지 못한 내용이라 직접 확인이 필요해요 · — 는 원문에서 찾지 못한 칸</p>`
    : `<p class="muted small">아직 정리한 논문이 없어요.</p>`;

  const g = rep.gaps;
  const sev = { 확실: "ok", 보통: "warn", 약함: "need" };
  const gapsHtml = g ? `<div class="rv-sum">문헌 ${g.n}편 기준 · ${esc(g.ran_at)} · ${eng(g)}</div>
    ${g.trends.length ? `<div class="small muted" style="margin-top:8px">선행연구의 흐름</div>${g.trends.map((t) => `<div class="rv-item">${esc(t.point)}<div class="small">${refs(t.rows)}</div></div>`).join("")}` : ""}
    ${g.gaps.length ? `<div class="small muted" style="margin-top:8px">연구 공백</div>${g.gaps.map((x) => `<div class="rv-item ${sev[x.strength] || ""}">
      <span class="badge rq-${sev[x.strength] || "warn"}">${esc(x.strength)}</span> <b>${esc(x.gap)}</b>
      <div class="small">📊 근거: ${esc(x.evidence)}</div><div class="small">${refs(x.rows)}</div>
      ${x.check ? `<div class="small muted">🔍 주장하기 전에 확인: ${esc(x.check)}</div>` : ""}</div>`).join("")}` : ""}
    ${g.assumptions.length ? `<div class="small muted" style="margin-top:8px">선행연구가 깔고 있는 이론적 가정과 비판</div>${g.assumptions.map((x) => `<div class="rv-item warn"><b>가정:</b> ${esc(x.assumption)}<div class="small"><b>비판:</b> ${esc(x.critique)}</div><div class="small">${refs(x.rows)}</div></div>`).join("")}` : ""}
    ${g.questions.length ? `<div class="small muted" style="margin-top:8px">연구문제 후보</div>${g.questions.map((q) => `<div class="rv-item">❓ ${esc(q)}</div>`).join("")}` : ""}
    ${g.n < 10 ? `<p class="small" style="color:var(--warn)">표의 논문이 ${g.n}편이라 공백 판단이 약할 수 있어요. 관련 논문을 더 정리할수록 정확해져요.</p>` : ""}` : "";

  const it = rep.intro;
  body.innerHTML = `<div class="pad">
    <div class="card"><h3>📚 문헌분석표</h3>
      <p class="small muted">선행연구마다 목적·대상·변인·측정도구·분석 방법·결과·한계를 정리해요. 모든 칸에 원문 근거가 달려 있어요. (한 편에 Claude 20~30초, 로컬 AI 1~2분)</p>
      <div class="row"><button class="primary" id="mxPick" ${mj ? "disabled" : ""}>＋ 논문 골라 정리하기</button>${costTag(p.id, "matrix_per_paper").replace("약 ", "한 편 약 ")}
        ${sug.length ? `<button id="mxSug" ${mj ? "disabled" : ""}>원고에 인용된 논문 ${sug.length}편 한꺼번에 정리</button>${costTag(p.id, "matrix_per_paper", sug.length)}` : ""}
        <span class="grow"></span>${d.rows.length ? `<a class="btnlink ghostlink" href="/api/projects/${p.id}/matrix.csv">⬇ 엑셀로 내려받기 (CSV)</a><a class="btnlink ghostlink" href="/api/projects/${p.id}/refs.ris" title="Zotero·EndNote·Mendeley에서 '가져오기'로 여세요">⬇ Zotero·EndNote로 (RIS)</a>` : ""}</div>
      ${logBox("matrix")}
      <div id="mxPicker" class="mx-picker" style="display:none"></div>
      ${table}</div>
    <div class="card"><div class="row"><h3 class="grow">🔎 연구 동향 · 공백 검증</h3>
        ${costTag(p.id, "gaps")}${g ? `<button class="small ghost" id="mxGapF" ${!mj ? "" : "disabled"}>새로 실행</button>` : ""}
        <button class="primary" id="mxGap" ${d.rows.length >= 3 && !mj ? "" : "disabled"}>연구 공백 찾기</button></div>
      <p class="small muted" style="margin:2px 0 8px">표에 정리한 논문만 근거로 선행연구의 흐름, 연구 공백(몇 편 중 몇 편인지 숫자로), 선행연구가 깔고 있는 이론적 가정과 그 비판, 연구문제 후보를 정리해요. (3편 이상 필요)</p>
      ${logBox("gaps")}${gapsHtml}</div>
    <div class="card"><div class="row"><h3 class="grow">✍ 서론 초안</h3>
        ${costTag(p.id, "intro")}${it ? `<button class="small ghost" id="mxIntroF" ${!mj ? "" : "disabled"}>새로 실행</button>` : ""}
        <button class="primary" id="mxIntro" ${g && !mj ? "" : "disabled"}>서론 초안 쓰기</button></div>
      <p class="small muted" style="margin:2px 0 8px">연구의 필요성 → 선행연구의 흐름 → 연구 공백 → 연구 목적·연구문제 순서로 써요. 문장마다 표의 논문 원문에서 근거를 찾아 연결하고, 못 찾으면 '?'를 달아요. AI 초안이라 원고에서 보라색으로 표시돼요.</p>
      ${logBox("intro")}
      ${it ? `<div class="rv-sum">근거 확인 ${it.ok}문장 · 확인 필요(?) ${it.need}문장 · ${esc(it.ran_at)} · ${eng(it)}</div>
        <div class="paper intro-prev">${it.html}</div>
        <div class="row" style="margin-top:8px"><button class="primary" id="mxIns">④ 집필 원고 맨 앞에 넣기</button><button id="mxInsEnd">맨 뒤에 넣기</button></div>` : ""}</div>
    <div class="card"><h3>📖 내 자료에 묻기</h3>
      <p class="small muted" style="margin:2px 0 8px">내 PDF들에 무엇이든 물어보세요. 답의 문장마다 원문 근거를 찾아 달고, 근거가 없으면 '?'를 달아요. (예: "대인관계 유능성을 측정한 척도는 무엇이 있나?")</p>
      <div class="row"><input id="akQ" class="grow" placeholder="질문을 적으세요"><select id="akScope"><option value="library">내 자료 전체</option><option value="matrix" ${d.rows.length ? "selected" : ""}>문헌분석표 논문만</option></select>
        <button class="primary" id="akGo" ${!mj ? "" : "disabled"}>묻기</button></div>
      ${logBox("ask")}${askHtml(rep.ask)}</div>
    <div class="card"><h3>⚖ 근거 저울</h3>
      <p class="small muted" style="margin:2px 0 8px">"진지한 여가는 삶의 질을 높이는가?"처럼 질문하면 표의 논문들이 각각 예·일부·아니오 중 무엇으로 답하는지 원문 근거와 함께 보여줘요. 결과가 엇갈리는 지점이 논의·연구 공백의 재료가 돼요.</p>
      <div class="row"><input id="mtQ" class="grow" placeholder="예) 진지한 여가 수준이 높을수록 삶의 질이 높은가?"><button class="primary" id="mtGo" ${d.rows.length && !mj ? "" : "disabled"}>물어보기</button></div>
      ${logBox("meter")}${meterHtml(rep.meter, num)}</div>
    <div class="card"><div class="row"><h3 class="grow">🕸 연구 지도</h3><button class="primary" id="mapGo" ${d.rows.length >= 2 && !mj ? "" : "disabled"}>관련 논문 찾기</button></div>
      <p class="small muted" style="margin:2px 0 8px">표의 논문들이 <b>공통으로 인용한 핵심 문헌</b>(꼭 읽어야 할 고전)과, 표의 논문들을 <b>인용한 최근 연구</b>(최근 6년)를 인용 관계로 찾아요. 여러 편과 연결될수록 내 주제와 가까워요. (국내 학술지는 인용 정보가 빠진 경우가 많아요)</p>
      ${logBox("map")}${mapHtml(rep.map)}</div>
    <div class="card"><div class="row"><h3 class="grow">🎯 투고 학술지 추천</h3><button class="primary" id="jnGo" ${!mj ? "" : "disabled"}>학술지 찾기</button></div>
      <p class="small muted" style="margin:2px 0 8px">표의 논문이 실린 학술지와, 내 주제와 비슷한 최근 6년 논문이 많이 실린 학술지를 모아 순위를 매겨요. KCI 등재 여부와 투고 규정은 각 학술지 누리집에서 꼭 확인하세요.</p>
      ${logBox("journals")}${journalHtml(rep.journals)}</div>
    <div class="card"><div class="row"><h3 class="grow">🗺 연구모형 그림</h3><button id="mdAi" ${!mj ? "" : "disabled"}>AI로 변인 채우기</button></div>
      <p class="small muted" style="margin:2px 0 8px">변인 이름을 적으면 연구모형 그림을 바로 그려요. 쉼표(,)로 여러 개를 적을 수 있어요. 그림 파일(PNG·SVG)로 내려받아 한글·워드·PPT에 넣으세요.</p>
      ${logBox("model")}
      <div class="md-form">
        <label>독립변인<input data-md="iv" value="${esc((rep.model?.iv || []).join(", "))}"></label>
        <label>매개변인<input data-md="mediators" value="${esc((rep.model?.mediators || []).join(", "))}"></label>
        <label>종속변인<input data-md="dvs" value="${esc((rep.model?.dvs || []).join(", "))}"></label>
        <label>조절변인<input data-md="moderators" value="${esc((rep.model?.moderators || []).join(", "))}"></label>
        <label>통제변인<input data-md="covariates" value="${esc((rep.model?.covariates || []).join(", "))}"></label>
        <label>매개 방식<select data-md="mediation">${["병렬", "직렬", "단순", "없음"].map((x) => `<option ${(rep.model?.mediation || "병렬") === x ? "selected" : ""}>${x}</option>`).join("")}</select></label>
        <label>그림 제목<input data-md="title" value="${esc(rep.model?.title || "연구모형")}"></label>
      </div>
      <div id="mdSvg" class="md-svg"></div>
      <div class="row"><button id="mdPng">⬇ PNG 그림</button><button id="mdSvgDl">⬇ SVG 그림</button><span class="small muted" id="mdSaved"></span></div></div>
  </div>`;

  const go = (kind, url, json, title) => async () => {
    const { job } = await api(url, json ? { json } : { method: "POST" });
    matrixJob[p.id] = { job, kind };
    renderMatrix();
    runJob(job, title, {
      logEl: "mxLog",
      onDone: async (r, j) => {
        delete matrixJob[p.id];
        if (S.cur?.id === p.id) { S.cur = await api(`/api/projects/${p.id}`); if (S.tab === "matrix") renderMatrix(); }
        if (j.status === "done") toast(`${title} 완료`);
      },
    });
  };
  $("#mxPick").onclick = () => {
    const box = $("#mxPicker");
    if (box.style.display === "block") { box.style.display = "none"; return; }
    box.style.display = "block";
    box.innerHTML = `<input id="mxFilter" placeholder="제목·저자로 찾기"><div class="mx-list">${d.candidates.map((c) => `<label class="chk mx-cand" data-q="${esc((c.cite + " " + c.title).toLowerCase())}">
        <input type="checkbox" value="${c.id}" ${c.suggested && !c.done ? "checked" : ""}> <b>${esc(c.cite)}</b> ${esc(c.title)}
        ${c.done ? `<span class="badge gray">정리됨</span>` : ""}${c.suggested ? `<span class="badge">원고에 인용</span>` : ""}</label>`).join("")}</div>
      <div class="row"><button class="primary" id="mxGo">고른 논문 정리 시작</button><span class="small muted" id="mxCnt"></span></div>`;
    const cnt = () => ($("#mxCnt").textContent = `${$$(".mx-list input:checked", box).length}편 선택`);
    cnt();
    $$(".mx-list input", box).forEach((x) => (x.onchange = cnt));
    $("#mxFilter").oninput = (e) => { const q = e.target.value.toLowerCase(); $$(".mx-cand", box).forEach((l) => (l.style.display = l.dataset.q.includes(q) ? "" : "none")); };
    $("#mxGo").onclick = () => {
      const ids = $$(".mx-list input:checked", box).map((x) => +x.value);
      if (!ids.length) return toast("정리할 논문을 골라주세요");
      go("matrix", `/api/projects/${p.id}/matrix`, { paper_ids: ids }, "문헌분석표 만들기")();
    };
  };
  if ($("#mxSug")) $("#mxSug").onclick = go("matrix", `/api/projects/${p.id}/matrix`, { paper_ids: sug.map((c) => c.id) }, "문헌분석표 만들기");
  $("#mxGap").onclick = go("gaps", `/api/projects/${p.id}/gaps`, null, "연구 공백 찾기");
  if ($("#mxGapF")) $("#mxGapF").onclick = go("gaps", `/api/projects/${p.id}/gaps?force=1`, null, "연구 공백 찾기");
  if ($("#mxIntroF")) $("#mxIntroF").onclick = go("intro", `/api/projects/${p.id}/intro?force=1`, null, "서론 초안 쓰기");
  $("#mxIntro").onclick = go("intro", `/api/projects/${p.id}/intro`, null, "서론 초안 쓰기");
  $$(".mx-ev", body).forEach((td) => (td.onclick = () => showEvidence({ chunk: td.dataset.chunk, quote: td.dataset.quote })));
  $$("[data-del]", body).forEach((b) => (b.onclick = async () => { await api(`/api/projects/${p.id}/matrix/${b.dataset.del}`, { method: "DELETE" }); renderMatrix(); }));
  $$(".mx-ref", body).forEach((x) => (x.onclick = () => {
    const tr = $(`#mxrow-${x.dataset.row}`);
    if (tr) { tr.scrollIntoView({ block: "center", behavior: "smooth" }); tr.classList.add("flash"); setTimeout(() => tr.classList.remove("flash"), 1600); }
  }));
  $$(".intro-prev .cite[data-chunk]", body).forEach((c) => (c.onclick = () => showEvidence(c)));
  $("#mtGo").onclick = () => {
    const q = $("#mtQ").value.trim();
    if (q.length < 5) return toast("질문을 적어주세요");
    go("meter", `/api/projects/${p.id}/meter`, { question: q }, "근거 저울")();
  };
  $("#mapGo").onclick = go("map", `/api/projects/${p.id}/map`, null, "연구 지도");
  $("#jnGo").onclick = go("journals", `/api/projects/${p.id}/journals`, null, "투고 학술지 추천");
  $("#akGo").onclick = () => {
    const q = $("#akQ").value.trim();
    if (q.length < 4) return toast("질문을 적어주세요");
    go("ask", `/api/projects/${p.id}/ask`, { question: q, scope: $("#akScope").value }, "내 자료에 묻기")();
  };
  $("#akQ").onkeydown = (e) => { if (e.key === "Enter") $("#akGo").click(); };
  $$(".ask-ans .cite[data-chunk]", body).forEach((c) => (c.onclick = () => showEvidence(c)));
  $("#mdAi").onclick = go("model", `/api/projects/${p.id}/model_suggest`, null, "연구모형 변인 정리");
  $$(".mt-ev, .mx-ev2", body).forEach((x) => (x.onclick = (ev) => { ev.stopPropagation(); showEvidence({ chunk: x.dataset.chunk, quote: x.dataset.quote }); }));
  $$("[data-mapget]", body).forEach((b) => (b.onclick = async () => {
    const [list, i] = b.dataset.mapget.split("-");
    const w = rep.map[list][+i];
    b.disabled = true;
    const { job } = await api("/api/fetch_work", { json: w });
    runJob(job, "논문 내려받는 중", { onDone: (r) => { b.textContent = r?.paper ? "내 자료에 추가됨 ✓" : "받지 못했어요"; } });
  }));
  const readModel = () => {
    const m = {};
    $$("[data-md]", body).forEach((x) => {
      const k = x.dataset.md;
      m[k] = ["mediation", "title"].includes(k) ? x.value.trim() : x.value.split(/[,，、]/).map((t) => t.trim()).filter(Boolean);
    });
    return m;
  };
  let mdT = null;
  const redraw = () => { $("#mdSvg").innerHTML = modelSvg(readModel()); };
  redraw();
  $$("[data-md]", body).forEach((x) => (x.oninput = x.onchange = () => {
    redraw();
    clearTimeout(mdT);
    mdT = setTimeout(async () => { await api(`/api/projects/${p.id}/model`, { method: "PUT", json: readModel() }); const sv = $("#mdSaved"); if (sv) sv.textContent = "저장됨 ✓"; }, 900);
  }));
  const svgText = () => $("#mdSvg svg").outerHTML;
  const dl = (blob, name) => { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); };
  $("#mdSvgDl").onclick = () => dl(new Blob([svgText()], { type: "image/svg+xml" }), "연구모형.svg");
  $("#mdPng").onclick = () => {
    const svg = $("#mdSvg svg");
    const w = +svg.getAttribute("width"), h = +svg.getAttribute("height");
    const img = new Image();
    img.onload = () => {
      const c = document.createElement("canvas");
      c.width = w * 3; c.height = h * 3;
      const g = c.getContext("2d");
      g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
      g.drawImage(img, 0, 0, c.width, c.height);
      c.toBlob((b) => dl(b, "연구모형.png"), "image/png");
    };
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svgText());
  };
  const insert = (front) => async () => {
    const cur = await api(`/api/projects/${p.id}`);
    const content = front ? it.html + (cur.content || "") : (cur.content || "") + it.html;
    await api(`/api/projects/${p.id}`, { method: "PUT", json: { content } });
    S.cur = await api(`/api/projects/${p.id}`);
    toast("서론 초안을 원고에 넣었어요 — ④ 집필에서 다듬어 주세요");
  };
  if (it) { $("#mxIns").onclick = insert(true); $("#mxInsEnd").onclick = insert(false); }
}

function askHtml(hist) {
  if (!hist || !hist.length) return "";
  const one = (h) => `<div class="mt-q"><b>Q.</b> ${esc(h.question)} <span class="small muted">· ${h.scope === "matrix" ? "문헌분석표" : "내 자료 전체"} · 근거 확인 ${h.ok} · 확인 필요 ${h.need} · ${esc(h.ran_at)}</span></div>
    <div class="paper ask-ans">${h.html}</div>`;
  return one(hist[0]) + (hist.length > 1 ? `<details class="small" style="margin-top:8px"><summary>이전 질문 ${hist.length - 1}개</summary>${hist.slice(1).map(one).join("")}</details>` : "");
}

function journalHtml(j) {
  if (!j || !j.items) return "";
  const max = Math.max(...j.items.map((x) => x.score), 1);
  return `<div class="rv-sum">${esc(j.ran_at)} · 검색어: ${esc(j.query)}</div>` + j.items.map((x, i) => `<div class="rv-item"><b>${i + 1}. ${esc(x.venue)}</b>
    <div class="jn-bar"><span style="width:${(x.score / max) * 100}%"></span></div>
    <div class="small muted">${x.examples.map((t) => esc(t)).join(" · ")}</div></div>`).join("");
}

function meterHtml(hist, num) {
  if (!hist || !hist.length) return "";
  const col = { 예: "ok", 일부: "warn", 아니오: "need", 관련없음: "" };
  const one = (h) => {
    const rel = h.items.filter((x) => x.verdict !== "관련없음");
    const tot = rel.length || 1;
    const bar = ["예", "일부", "아니오"].map((k) => h.counts[k] ? `<span class="mt-seg mt-${col[k]}" style="width:${(h.counts[k] / tot) * 100}%">${k} ${h.counts[k]}</span>` : "").join("");
    return `<div class="mt-q"><b>Q.</b> ${esc(h.question)} <span class="small muted">· ${esc(h.ran_at)}</span></div>
      <div class="mt-bar">${bar || `<span class="small muted">이 질문을 다룬 논문이 없어요</span>`}</div>
      <div class="small" style="margin:4px 0 6px">${esc(h.summary)}</div>
      ${h.items.filter((x) => x.verdict !== "관련없음").map((x) => `<div class="rv-item ${col[x.verdict]}"><span class="badge rq-${col[x.verdict]}">${x.verdict}</span> <b>${num[x.paper_id] ? `[${num[x.paper_id]}] ` : ""}${esc(x.cite)}</b> ${esc(x.reason)}
        ${x.chunk ? `<span class="mt-ev small" data-chunk="${x.chunk}" data-quote="${esc(x.quote)}">원문 ↗</span>` : ""}</div>`).join("")}
      ${h.counts["관련없음"] ? `<div class="small muted">관련없음 ${h.counts["관련없음"]}편: ${esc(h.items.filter((x) => x.verdict === "관련없음").map((x) => x.cite).join(", "))}</div>` : ""}`;
  };
  return one(hist[0]) + (hist.length > 1 ? `<details class="small" style="margin-top:8px"><summary>이전 질문 ${hist.length - 1}개</summary>${hist.slice(1).map((h) => `<div style="margin-top:8px">${one(h)}</div>`).join("")}</details>` : "");
}

function mapHtml(m) {
  if (!m) return "";
  const row = (list) => (w, i) => `<div class="rv-item"><b>${w.doi ? `<a href="https://doi.org/${esc(w.doi)}" target="_blank">${esc(w.title)}</a>` : esc(w.title)}</b>
    <div class="small muted">${esc((w.authors || []).slice(0, 3).join(", "))} · ${esc(w.year)} · 피인용 ${w.cited} · <b>표의 논문 ${w.count}편과 연결</b></div>
    <div class="row" style="margin-top:4px">${w.have ? `<span class="badge gray">내 자료에 있음</span>` : `<button class="small" data-mapget="${list}-${i}">${w.is_oa ? "내 자료로 내려받기" : "내려받기 시도(도서관 경유)"}</button>`}</div></div>`;
  return `<div class="rv-sum">인용 정보를 찾은 논문 ${m.seeds}/${m.total}편 기준 · ${esc(m.ran_at)}</div>
    <div class="small muted" style="margin-top:8px">📜 공통으로 인용한 핵심 문헌 (${m.prior.length})</div>${m.prior.map(row("prior")).join("") || `<div class="small muted">없음</div>`}
    <div class="small muted" style="margin-top:8px">🌱 표의 논문들을 인용한 최근 연구 (${m.later.length})</div>${m.later.map(row("later")).join("") || `<div class="small muted">없음</div>`}`;
}

// 연구모형 그림 (SVG) — 병렬·직렬·단순 매개, 조절, 다중 종속
function modelSvg(m) {
  const X = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const iv = m.iv?.length ? m.iv : ["독립변인"], dv = m.dvs?.length ? m.dvs : ["종속변인"];
  const med = m.mediation === "없음" ? [] : m.mediators || [];
  const serial = m.mediation === "직렬" && med.length > 1;
  const BW = 150, BH = 46, GAP = 30;
  const boxes = [], lines = [];
  const colH = (n) => n * BH + (n - 1) * GAP;
  let W, H, top = m.moderators?.length ? 110 : 50;
  const stackY = (n, cy) => Array.from({ length: n }, (_, i) => cy - colH(n) / 2 + i * (BH + GAP));
  const pos = {};
  if (serial) {
    W = 60 + (med.length + 2) * (BW + 50);
    const yTop = top, yBot = top + 140;
    med.forEach((t, i) => (pos["m" + i] = [60 + (i + 1) * (BW + 50), yTop, t]));
    const ivY = stackY(iv.length, yBot + BH / 2), dvY = stackY(dv.length, yBot + BH / 2);
    iv.forEach((t, i) => (pos["i" + i] = [30, ivY[i], t]));
    dv.forEach((t, i) => (pos["d" + i] = [60 + (med.length + 1) * (BW + 50), dvY[i], t]));
    H = Math.max(...ivY, ...dvY) + BH + 60;
    iv.forEach((_, i) => lines.push(["i" + i, "m0", false]));
    med.forEach((_, i) => i && lines.push(["m" + (i - 1), "m" + i, false]));
    dv.forEach((_, j) => lines.push(["m" + (med.length - 1), "d" + j, false]));
  } else {
    W = med.length ? 3 * BW + 2 * 140 + 60 : 2 * BW + 200 + 60;
    const n = Math.max(iv.length, med.length + (med.length % 2 ? 1 : 0), dv.length, 1);
    const cy = top + colH(Math.max(n, 2)) / 2 + 10;
    const xs = med.length ? [30, 30 + BW + 140, 30 + 2 * (BW + 140)] : [30, null, 30 + BW + 200];
    stackY(iv.length, cy).forEach((y, i) => (pos["i" + i] = [xs[0], y, iv[i]]));
    stackY(dv.length, cy).forEach((y, i) => (pos["d" + i] = [xs[2], y, dv[i]]));
    // 매개변인은 가운데 줄(직접경로)을 비워 위아래로
    const half = Math.ceil(med.length / 2);
    med.forEach((t, i) => {
      const up = i < half, k = up ? half - 1 - i : i - half;
      const y = up ? cy - BH / 2 - 50 - k * (BH + GAP) - BH : cy + BH / 2 + 50 + k * (BH + GAP);
      pos["m" + i] = [xs[1], y, t];
    });
    H = Math.max(...Object.values(pos).map((p) => p[1])) + BH + 60;
    iv.forEach((_, i) => med.forEach((__, k) => lines.push(["i" + i, "m" + k, false])));
    med.forEach((_, k) => dv.forEach((__, j) => lines.push(["m" + k, "d" + j, false])));
  }
  iv.forEach((_, i) => dv.forEach((__, j) => lines.push(["i" + i, "d" + j, med.length > 0])));
  const c = (k) => { const [x, y] = pos[k]; return [x + BW / 2, y + BH / 2]; };
  const edge = (from, to) => {  // 상자 테두리에서 시작·끝
    const [x1, y1] = c(from), [x2, y2] = c(to);
    const cut = (x, y, dx, dy) => { const t = Math.min(Math.abs((BW / 2) / (dx || 1e-6)), Math.abs((BH / 2) / (dy || 1e-6))); return [x + dx * t, y + dy * t]; };
    const [ax, ay] = cut(x1, y1, x2 - x1, y2 - y1), [bx, by] = cut(x2, y2, x1 - x2, y1 - y2);
    return [ax, ay, bx, by];
  };
  let svgLines = lines.map(([a, b, dash]) => { const [x1, y1, x2, y2] = edge(a, b); return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#333" stroke-width="1.6" ${dash ? 'stroke-dasharray="6 4"' : ""} marker-end="url(#ah)"/>`; }).join("");
  if (m.moderators?.length) {
    const tgt = med.length ? edge("i0", serial ? "m0" : "m0") : edge("i0", "d0");
    const mx = (tgt[0] + tgt[2]) / 2, my = (tgt[1] + tgt[3]) / 2;
    m.moderators.forEach((t, i) => {
      const bx = mx - BW / 2 + i * (BW + 20), by = 20;
      boxes.push(`<rect x="${bx}" y="${by}" width="${BW}" height="${BH}" rx="6" fill="#fff" stroke="#333" stroke-width="1.4"/><text x="${bx + BW / 2}" y="${by + BH / 2 + 5}" text-anchor="middle" font-size="14">${X(t)}</text>`);
      svgLines += `<line x1="${bx + BW / 2}" y1="${by + BH}" x2="${mx}" y2="${my - 3}" stroke="#333" stroke-width="1.6" marker-end="url(#ah)"/>`;
    });
  }
  Object.values(pos).forEach(([x, y, t]) => {
    const fs = String(t).length > 10 ? 12 : 14;
    boxes.push(`<rect x="${x}" y="${y}" width="${BW}" height="${BH}" rx="6" fill="#fff" stroke="#333" stroke-width="1.4"/><text x="${x + BW / 2}" y="${y + BH / 2 + fs / 3}" text-anchor="middle" font-size="${fs}">${X(t)}</text>`);
  });
  const W2 = Math.max(W, ...Object.values(pos).map((p) => p[0] + BW + 30));
  const foot = [m.covariates?.length ? `통제변인: ${m.covariates.join(", ")}` : "", med.length ? "점선: 직접효과" : ""].filter(Boolean).join("    ");
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W2}" height="${H + 20}" viewBox="0 0 ${W2} ${H + 20}" font-family="'Malgun Gothic','맑은 고딕',sans-serif">
    <defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#333"/></marker></defs>
    <rect width="100%" height="100%" fill="#fff"/>
    <text x="16" y="${m.moderators?.length ? H + 8 : 26}" font-size="14" font-weight="700">${X(m.title || "연구모형")}</text>
    ${svgLines}${boxes.join("")}
    <text x="${W2 - 16}" y="${H + 8}" text-anchor="end" font-size="11" fill="#555">${X(foot)}</text></svg>`;
}

// ---------- 📁 폴더 고르기 창 (탐색기처럼 펼쳐 보며 체크) ----------
function folderPicker({ title, hint, selected = [], onSave, cover = null }) {
  const sel = [...selected];
  const d = document.createElement("dialog");
  d.className = "wizard fpick";
  document.body.appendChild(d);
  let cur = "";
  const chips = () => sel.length ? sel.map((f, i) => `<span class="fp-chip" title="${esc(f)}">📁 ${esc(f.split(/[\\/]/).filter(Boolean).slice(-2).join(" › "))}<b data-rm="${i}">✕</b></span>`).join("") : `<span class="small muted">아직 고른 폴더가 없어요</span>`;
  const draw = async () => {
    d.innerHTML = `<h3 style="margin:0 0 4px">${esc(title)}</h3><p class="small muted" style="margin:0 0 8px;white-space:pre-line">${esc(hint || "")}</p>
      <div class="fp-sel">${chips()}</div><div class="fp-cover"></div>
      <div class="fp-nav"><button class="small" id="fpUp" ${cur ? "" : "disabled"}>⬆ 위로</button><span class="small muted fp-path">${esc(cur || "내 컴퓨터")}</span></div>
      <div class="fp-list"><div class="small muted"><span class="spin"></span> 폴더를 살펴보는 중…</div></div>
      <div class="row" style="justify-content:flex-end;margin-top:10px"><button class="ghost" id="fpCancel">취소</button><button class="primary" id="fpSave">이 폴더들로 정하기 (${sel.length})</button></div>`;
    $$("[data-rm]", d).forEach((b) => (b.onclick = () => { sel.splice(+b.dataset.rm, 1); draw(); }));
    drawCover();
    $("#fpCancel", d).onclick = () => { d.close(); d.remove(); };
    $("#fpSave", d).onclick = () => { d.close(); d.remove(); onSave(sel); };
    let r;
    try { r = await api(`/api/fs?path=${encodeURIComponent(cur)}`); } catch (e) { $(".fp-list", d).innerHTML = `<div class="small" style="color:var(--warn)">${esc(e.message)}</div>`; return; }
    $("#fpUp", d).onclick = () => { cur = r.parent || ""; draw(); };
    const icon = (x) => (x.kind === "drive" ? "💽" : x.kind === "shortcut" ? (x.name === "구글 드라이브" ? "☁" : "⭐") : "📁");
    $(".fp-list", d).innerHTML = (r.items.length ? r.items.map((x, i) => `<div class="fp-item">
        ${x.kind === "dir" ? `<input type="checkbox" data-pick="${i}" ${sel.includes(x.path) ? "checked" : ""}>` : `<span style="width:16px"></span>`}
        <span class="fp-open" data-open="${i}">${icon(x)} ${esc(x.name)}</span>
        ${x.kind === "dir" ? `<span class="small muted">${x.pdfs ? `PDF ${x.pdfs}${x.more ? "+" : ""}개` : "PDF 없음"}</span>` : ""}
        <span class="grow"></span><button class="small ghost" data-open="${i}">열기 ›</button></div>`).join("") : `<div class="small muted">하위 폴더가 없어요</div>`)
      + (r.direct_pdfs ? `<div class="small muted" style="margin-top:6px">이 폴더 바로 안에 PDF ${r.direct_pdfs}개 — <a href="#" id="fpHere">이 폴더 고르기</a></div>` : "");
    $$("[data-open]", d).forEach((b) => (b.onclick = () => { cur = r.items[+b.dataset.open].path; draw(); }));
    $$("[data-pick]", d).forEach((b) => (b.onchange = () => {
      const pth = r.items[+b.dataset.pick].path;
      const i = sel.indexOf(pth);
      if (b.checked && i < 0) sel.push(pth);
      if (!b.checked && i >= 0) sel.splice(i, 1);
      $(".fp-sel", d).innerHTML = chips();
      drawCover();
      $("#fpSave", d).textContent = `이 폴더들로 정하기 (${sel.length})`;
      $$("[data-rm]", d).forEach((x) => (x.onclick = () => { sel.splice(+x.dataset.rm, 1); draw(); }));
    }));
    if ($("#fpHere", d)) $("#fpHere", d).onclick = (e) => { e.preventDefault(); if (!sel.includes(cur)) sel.push(cur); draw(); };
  };
  // 원고에 인용된 논문이 고른 폴더에서 몇 개나 찾아지는지 + 추천 폴더
  const norm = (x) => x.toLowerCase().replace(/[\\/]+$/, "");
  const inSel = (dir) => sel.some((f) => { const a = norm(dir), b = norm(f); return a === b || a.startsWith(b + "\\") || a.startsWith(b + "/"); });
  function drawCover() {
    const box = $(".fp-cover", d);
    if (!box || !cover) return;
    if (!cover.total) { box.innerHTML = ""; return; }
    const ok = cover.cites.filter((c) => c.dirs.some(inSel)).length;
    const sug = cover.suggest.filter((s) => !inSel(s.path));
    const warn = sel.length && ok < cover.found;
    box.innerHTML = `<div class="fp-cov ${warn ? "warn" : ""}">
      <b>원고에 인용된 논문 ${cover.total}편 중 고른 폴더에서 찾을 수 있는 것: ${ok}편</b>
      <span class="small muted">(내 자료 전체에는 ${cover.found}편이 있어요${cover.found < cover.total ? ` · 나머지 ${cover.total - cover.found}편은 아직 PDF가 없거나 읽지 않았어요` : ""})</span>
      ${sug.length ? `<div class="small" style="margin-top:6px">인용된 논문이 들어 있는 폴더:</div>` + sug.map((s, i) => `<div class="fp-sug"><span title="${esc(s.path)}">📁 ${esc(s.path.split(/[\\/]/).filter(Boolean).slice(-3).join(" › "))}</span><span class="small muted">인용 ${s.n}편</span><button class="small" data-sug="${i}">＋ 추가</button></div>`).join("") : ""}
    </div>`;
    $$("[data-sug]", box).forEach((b) => (b.onclick = () => { const pth = sug[+b.dataset.sug].path; if (!sel.includes(pth)) sel.push(pth); draw(); }));
  }
  d.showModal();
  draw();
}

// 이 논문의 참조 폴더 정하기 → 내 자료 폴더에도 넣고 그 폴더만 읽기
async function chooseRefFolders(after) {
  const p = S.cur;
  folderPicker({
    title: "📁 이 논문이 참조할 폴더",
    hint: "⚠ 이 논문과 관련된 논문만 모아 둔 폴더를 골라 주세요. 드라이브 전체나 큰 폴더를 고르면 상관없는 PDF까지 다 읽느라 비용(Claude 사용료)과 시간이 많이 들어요. 고른 폴더 안의 PDF에서만 근거를 찾고, 하위 폴더까지 함께 읽어요. 💡 가능하시다면 이 논문의 자료를 한 폴더에 모아 넣어 주시는 것을 추천드립니다. 흩어져 있으면 있는 논문도 못 찾을 수 있어요.",
    selected: p.ref_folders || [],
    cover: await api(`/api/projects/${p.id}/cite_folders`).catch(() => null),
    onSave: async (sel) => {
      await api(`/api/projects/${p.id}`, { method: "PUT", json: { ref_folders: sel } });
      const all = S.status?.settings?.scan_folders || [];
      const add = sel.filter((f) => !all.includes(f));
      if (add.length) { await api("/api/settings", { json: { scan_folders: [...all, ...add] } }); await refreshStatus(); }
      S.cur = await api(`/api/projects/${p.id}`);
      if (sel.length) {
        const { job } = await api("/api/scan", { json: { folders: sel } });
        toast("고른 폴더의 PDF를 읽기 시작했어요");
        runJob(job, "참조 폴더 PDF 읽는 중", { onDone: async () => { S.cur = await api(`/api/projects/${p.id}`); after && after(); } });
      }
      after && after();
    },
  });
}

function bindAddButtons(after) {
  $("#scanBtn").onclick = async () => {
    const { job } = await api("/api/scan", { method: "POST" });
    runJob(job, "내 컴퓨터 PDF 읽는 중", { onDone: after });
  };
  $("#upBtn").onclick = () => $("#upFile").click();
  $("#upFile").onchange = async (e) => {
    const fd = new FormData();
    [...e.target.files].forEach((f) => fd.append("files", f));
    const { job } = await api("/api/papers/upload", { method: "POST", body: fd });
    runJob(job, "PDF 읽는 중", { onDone: after });
  };
}

function paperTable(papers) {
  if (!papers.length) return `<p class="muted">아직 자료가 없어요. 위 버튼으로 PDF를 추가하거나 내 컴퓨터를 검색하세요.</p>`;
  return `<table><tr><th>인용 표기</th><th>제목</th><th>쪽</th><th>출처</th><th></th></tr>
    ${papers.map((p) => `<tr data-id="${p.id}"><td><b>${esc(p.cite)}</b></td><td>${esc(p.title)}
      ${p.status !== "ok" ? `<span class="badge warn">글자 없음(스캔본)</span>` : ""}</td><td>${p.pages || ""}</td>
      <td class="small muted">${p.source === "web" ? "인터넷" : "내 PC"}</td>
      <td class="row" style="flex-wrap:nowrap"><button class="small" data-a="edit">수정</button><a class="small" href="/api/pdf/${p.id}" target="_blank">PDF</a>
      <button class="small ghost" data-a="del">빼기</button></td></tr>`).join("")}</table>`;
}

function bindPaperTable(root, after) {
  $$("tr[data-id]", root).forEach((tr) => {
    const id = tr.dataset.id;
    $$("button", tr).forEach((b) => (b.onclick = async () => {
      if (b.dataset.a === "del") {
        if (!confirm("이 자료를 목록에서 뺄까요? (PDF 파일은 지우지 않습니다)")) return;
        await api(`/api/papers/${id}`, { method: "DELETE" });
        return after();
      }
      const papers = await api("/api/papers");
      const p = papers.find((x) => x.id == id);
      const r = await formDialog("인용 정보 수정", [
        ["title", "제목", p.title],
        ["authors", "저자 (쉼표로 구분, 예: 이기태, 김민수)", p.authors.join(", ")],
        ["year", "연도", p.year],
      ]);
      if (!r) return;
      await api(`/api/papers/${id}`, { method: "PUT", json: { title: r.title, year: r.year, authors: r.authors.split(",").map((s) => s.trim()).filter(Boolean) } });
      after();
    }));
  });
}

function formDialog(title, fields, okText = "저장") {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.innerHTML = `<h3 style="margin-top:0">${esc(title)}</h3>
      ${fields.map(([k, label, v, type]) => type === "checks"
        ? `<div class="field"><span>${esc(label)}</span>${v.map((o, i) => `<label class="chk"><input type="checkbox" name="${k}" value="${esc(o)}" checked> ${esc(o)}</label>`).join("")}</div>`
        : `<label class="field"><span>${esc(label)}</span><input name="${k}" value="${esc(v)}"></label>`).join("")}
      <div class="row" style="justify-content:flex-end"><button data-x="0">취소</button><button class="primary" data-x="1">${esc(okText)}</button></div>`;
    document.body.appendChild(d);
    d.showModal();
    d.onclick = (e) => {
      const x = e.target.dataset?.x;
      if (x === undefined) return;
      let out = null;
      if (x === "1") {
        out = {};
        fields.forEach(([k, , , type]) => {
          out[k] = type === "checks" ? $$(`input[name="${k}"]:checked`, d).map((i) => i.value) : $(`input[name="${k}"]`, d).value;
        });
      }
      d.close(); d.remove(); resolve(out);
    };
  });
}

// 이 브라우저에만 기억하는 작은 설정 (막혀 있어도 앱은 정상 동작)
function lsGet(k) { try { return localStorage.getItem("pa_" + k) || ""; } catch { return ""; } }
function lsSet(k, v) { try { localStorage.setItem("pa_" + k, v); } catch {} }

// ---------- ④ 집필 ----------
let editor = null, margin = null, saveT = null, detectT = null, composing = false, openCite = null;
const citeMsg = new WeakMap();

function renderWrite() {
  const p = S.cur;
  $("#tabBody").innerHTML = `
    <div class="toolbar">
      <button class="primary" id="aiWrite">✍ AI로 집필</button>
      <button id="aiCont" title="커서 앞까지 쓴 글에 이어질 다음 문단을 근거를 붙여 써요">✍ 이어 쓰기</button>
      <button id="aiExpand" title="드래그해서 고른 메모·개요를 근거 있는 문단으로 풀어 써요">📝 메모 → 문단</button>
      <button id="aiWriteNight" title="오늘 밤 로컬 AI로 무료 처리">🌙 새벽 집필 예약</button>
      <button id="autoAll">🤖 출처 필요(?) 모두 자동 찾기</button>
      <button id="refsBtn">📑 참고문헌 만들기</button>
      <button id="aiDraftBtn" class="aidraft-btn" style="display:none" title="AI가 쓴 문장을 아직 고치지 않은 문단 — 누르면 차례로 이동해요"></button>
      <span class="hint" id="saveState">(저자, 연도)로 쓰면 자동 인식 · 인용을 누르면 원문 위치</span>
    </div>
    <details class="ai-caution" ${lsGet("cautionClosed") ? "" : "open"}><summary>⚠ AI 글쓰기 주의점</summary>
      <ul>
        <li><b>✍ AI로 집필</b>(장 전체): 연구계획 목차대로 쓴 <b>초안</b>이에요. 그대로 내면 AI가 쓴 글로 보일 수 있으니 내 말로 고쳐 쓰세요.</li>
        <li><b>✍ 이어 쓰기</b>: 커서 앞까지의 흐름을 따라가요. 앞 내용이 틀리면 같이 틀리니, 앞 문단부터 확인하고 쓰세요. 커서가 있는 문단 바로 뒤에 들어가요.</li>
        <li><b>📝 메모 → 문단</b>: 드래그한 메모의 주장만 살려 문장과 근거를 채워요. 메모에 없던 주장은 넣지 않지만 문장은 AI가 만든 거예요. 원래 메모는 지우지 않고 남겨 둬요.</li>
        <li>공통: AI가 쓴 문단은 <span class="ai-draft-sample">보라색</span>으로 표시되고, 절반 넘게 고쳐 쓰면 사라져요. 인용을 눌러 원문이 정말 그 말을 하는지 꼭 확인하세요. <b>?</b>는 근거를 못 찾은 문장이에요. 사용 기록은 ⑤ 'AI 사용 명시'에 자동으로 남아요.</li>
      </ul></details>
    <div class="desk"><div class="paper" id="editor" contenteditable="true" spellcheck="false"></div><div class="margin" id="margin"></div></div>`;
  editor = $("#editor");
  $(".ai-caution").ontoggle = (e) => lsSet("cautionClosed", e.target.open ? "" : "1");
  margin = $("#margin");
  editor.innerHTML = p.content || "";
  if (reviewJob[p.id]) {
    editor.contentEditable = "false";
    $(".toolbar").insertAdjacentHTML("afterend", `<div class="lockbar"><span class="spin"></span> 원고 점검 중이라 편집을 잠가 두었어요. 끝나면 결과가 원고에 표시됩니다.</div>`);
  }
  normalizeCites();
  editor.addEventListener("compositionstart", () => (composing = true));
  editor.addEventListener("compositionend", () => { composing = false; scheduleDetect(); });
  editor.addEventListener("input", () => { scheduleDetect(); scheduleSave(); layoutMemos(); clearTimeout(aiT); aiT = setTimeout(checkAiDrafts, 800); });
  editor.addEventListener("blur", () => detectCites(true));
  editor.addEventListener("click", (e) => {
    const c = e.target.closest(".cite");
    if (!c) return;
    e.preventDefault();
    if (c.dataset.chunk) showEvidence(c);
    if (!c.dataset.chunk || c.classList.contains("warn")) toggleMemo(c, true);
  });
  editor.addEventListener("paste", (e) => {
    e.preventDefault();
    document.execCommand("insertText", false, e.clipboardData.getData("text/plain"));
  });
  $("#aiWrite").onclick = () => aiWriteDialog(false);
  ["#aiCont", "#aiExpand"].forEach((id) => ($(id).onmousedown = (e) => e.preventDefault()));  // 누를 때 커서·선택이 풀리지 않게
  $("#aiCont").onclick = () => aiDraft("continue");
  $("#aiExpand").onclick = () => aiDraft("expand");
  $("#aiWriteNight").onclick = () => aiWriteDialog(true);
  $("#autoAll").onclick = autoAll;
  $("#refsBtn").onclick = makeRefs;
  $("#aiDraftBtn").onclick = () => {
    const list = $$("p.ai-draft", editor);
    if (!list.length) return;
    aiIdx = (aiIdx + 1) % list.length;
    const el = list[aiIdx];
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1600);
  };
  checkAiDrafts();
  requestAnimationFrame(layoutMemos);
}

// ---------- AI 초안 표시: 내 말로 고쳐 쓰면 색이 사라짐 ----------
let aiT = null, aiIdx = -1;
function bigrams(t) {
  const x = t.replace(/[\s.,;:!?'"“”‘’()\[\]·…-]/g, "");
  const out = new Set();
  for (let i = 0; i < x.length - 1; i++) out.add(x.slice(i, i + 2));
  return out;
}
function plainText(el) {
  const c = el.cloneNode(true);
  c.querySelectorAll(".cite").forEach((x) => x.remove());
  return c.textContent;
}
function checkAiDrafts() {
  if (!editor) return;
  let changed = false;
  $$("p.ai-draft", editor).forEach((el) => {
    const cur = bigrams(plainText(el));
    if (!el.dataset.ai || cur.size < 4) { el.classList.remove("ai-draft"); delete el.dataset.ai; changed = true; return; }
    const orig = bigrams(el.dataset.ai);
    let same = 0;
    cur.forEach((b) => { if (orig.has(b)) same++; });
    if (same / cur.size < 0.5) {  // 절반 넘게 내 글로 바뀌면 '고쳐 씀'
      el.classList.remove("ai-draft");
      delete el.dataset.ai;
      el.removeAttribute("title");
      changed = true;
    } else el.title = "AI가 쓴 초안이에요 — 내 말로 고쳐 쓰면 색이 사라져요";
  });
  const n = $$("p.ai-draft", editor).length;
  const b = $("#aiDraftBtn");
  if (b) { b.style.display = n ? "" : "none"; b.textContent = `🤖 AI 초안 그대로 ${n}문단`; }
  if (changed) scheduleSave();
}

function normalizeCites() {
  $$(".cite", editor).forEach((c) => {
    c.contentEditable = "false";
    c.classList.toggle("need", !c.dataset.chunk);
  });
  if (S.focusCid) {  // ⑤ 원고 점검 결과에서 누른 인용으로 이동
    const c = $(`.cite[data-cid="${S.focusCid}"]`, editor);
    S.focusCid = null;
    if (c) requestAnimationFrame(() => { c.scrollIntoView({ block: "center", behavior: "smooth" }); toggleMemo(c, true); if (c.dataset.chunk) showEvidence(c); });
  }
  if (S.focusPara) {  // 예비심사 리뷰에서 누른 문단으로 이동
    const el = $(`[data-rv="${S.focusPara}"]`, editor);
    S.focusPara = null;
    if (el) requestAnimationFrame(() => { el.scrollIntoView({ block: "center", behavior: "smooth" }); if (el.dataset.review) toggleMemo(el, true); else { el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1600); } });
  }
}

function scheduleSave() {
  $("#saveState").textContent = "저장 대기…";
  clearTimeout(saveT);
  saveT = setTimeout(saveNow, 1200);
}
async function saveNow() {
  if (!editor || !S.cur || !document.body.contains(editor)) return;
  clearTimeout(saveT);
  const content = editor.innerHTML;
  if (content === S.cur.content) return;
  S.cur.content = content;
  await api(`/api/projects/${S.cur.id}`, { method: "PUT", json: { content } });
  const st = $("#saveState");
  if (st) st.textContent = "저장됨 ✓";
}

function scheduleDetect() {
  clearTimeout(detectT);
  detectT = setTimeout(() => detectCites(false), 700);
}

// 사용자가 직접 쓴 "(이기태, 2026)"을 인용으로 바꾸고 '?' 메모를 단다
function detectCites(force) {
  if (!editor || composing) return;
  const sel = getSelection();
  let caretNode = sel.rangeCount ? sel.anchorNode : null;
  const caretOff = sel.anchorOffset;
  // 참고문헌 목록은 인용이 아니므로 표시하지 않음 (잘못 붙은 것은 글자로 되돌림)
  const refHead = $$("h1, h2, h3, h4", editor).find((h) => /^\s*(참\s*고\s*문\s*헌|references?|bibliography|인용\s*문헌)\s*$/i.test(h.textContent));
  const inRefs = (n) => refHead && (refHead.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING);
  const stray = refHead ? $$(".cite:not([data-chunk])", editor).filter(inRefs) : [];
  stray.forEach((c) => c.replaceWith(c.textContent));
  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest(".cite") || inRefs(n) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  let changed = stray.length > 0;
  for (const node of nodes) {
    const ms = findCites(node.data);
    for (let i = ms.length - 1; i >= 0; i--) {
      const m = ms[i];
      const end = m.index + m.length;
      const isCaret = node === caretNode;
      if (!force && isCaret && caretOff === end) continue; // 아직 타이핑 중
      const mid = node.splitText(m.index);
      const rest = mid.splitText(m.length);
      const frag = document.createDocumentFragment();
      if (m.group) frag.append("(");
      let span;
      m.parts.forEach((label, k) => {
        if (k) frag.append("; ");
        span = document.createElement("span");
        span.className = "cite need";
        span.contentEditable = "false";
        span.textContent = label;
        frag.append(span);
      });
      if (m.group) frag.append(")");
      mid.replaceWith(frag);
      changed = true;
      if (isCaret && caretOff >= m.index && !force) {
        const r = document.createRange();
        if (caretOff >= end) r.setStart(rest, caretOff - end); else r.setStartAfter(span);
        r.collapse(true);
        sel.removeAllRanges();
        sel.addRange(r);
        caretNode = null;
      }
    }
  }
  if (changed) { layoutMemos(); scheduleSave(); }
}

// 재인용 꼬리 '…: 김명소 외, 2001에서 재인용' / ', as cited in Lee, 2001' 는 빼고 원문 저자·연도만
const SECONDARY_RE = /\s*(?:[:;]\s*|,\s*(?=as cited in))(?:as cited in\s+[^()]+?|[^():;]+?\s*에서\s*재인용)\s*(?=\)|$)/i;
function parseLabel(label) {
  const t = label.trim().replace(SECONDARY_RE, "");
  const m = t.match(/^\(?\s*(.+?)\s*,\s*((?:19|20)\d{2}[a-z]?(?:\/(?:19|20)\d{2}[a-z]?)?|연도미상)(?:\s*,\s*pp?\.\s*[\d\-–]+)?\s*\)?$/) || t.match(/^(.+?)\s?\(((?:19|20)\d{2}[a-z]?(?:\/(?:19|20)\d{2}[a-z]?)?)\)$/);
  return m ? { author: m[1].trim(), year: m[2] === "연도미상" ? "" : m[2] } : { author: "", year: "" };
}

function sentenceOf(c) {
  let block = c.parentElement;
  while (block && block !== editor && getComputedStyle(block).display === "inline") block = block.parentElement;
  const r = document.createRange();
  r.setStart(block, 0);
  r.setEndBefore(c);
  const clean = (x) => x.replace(CITE_RE, "").replace(/\((출처 필요|저자미상[^)]*)\)/g, "").replace(/\(\s*[;,\s]*\)/g, "").replace(/\s+([.,;:!?])/g, "$1");
  const parts = clean(r.toString()).split(/(?<=[.!?。])\s+/);
  let s = (parts.pop() || "").trim();
  if (/\((?:19|20)\d{2}[a-z]?\)$/.test(c.textContent.trim())) {
    // 저자(연도) 형태는 인용이 들어 있는 문장 전체
    const r2 = document.createRange();
    r2.setStartAfter(c);
    r2.setEnd(block, block.childNodes.length);
    const tail = clean(r2.toString()).split(/(?<=[.!?。])\s+/)[0];
    return `${s} ${c.textContent.trim()}${tail}`.trim();
  }
  if (s.length < 10 && parts.length) s = parts.pop().trim() + " " + s;
  return s.replace(/^[\s.;,)]+/, "");
}

function layoutMemos() {
  if (!margin || !editor) return;
  margin.innerHTML = "";
  const base = margin.getBoundingClientRect().top;
  let bottom = -Infinity;
  $$(".cite.need, .cite.warn, [data-review]", editor).forEach((c) => {
    const top = c.getBoundingClientRect().top - base - 2;
    const y = Math.max(top, bottom + 6);
    const m = document.createElement("div");
    if (c.dataset.review !== undefined) {
      let notes = [];
      try { notes = JSON.parse(c.dataset.review || "[]"); } catch {}
      if (!notes.length) { delete c.dataset.review; return; }
      m.className = "memo rv" + (openCite === c ? " open" : "");
      m.style.top = y + "px";
      m.innerHTML = `<span class="dot"><b>💬</b>심사 의견${notes.length > 1 ? " " + notes.length : ""}</span>
        <div class="box">${notes.map((n, i) => `<div class="rvn"><div class="lbl"><span class="badge sev-${esc(n.sev)}">${esc(n.sev)}</span> ${esc(n.cat)}</div>
          <div>${esc(n.issue)}</div>${n.fix ? `<div class="prog">✏ ${esc(n.fix)}</div>` : ""}${n.cat === "AI 문체" && n.fix ? `<div class="row"><button data-cpfix="${i}">수정안 복사</button></div>` : ""}${n.q ? `<div class="prog">🎤 “${esc(n.q)}”</div>` : ""}
          <div class="row"><button data-done="${i}">반영했어요</button></div></div>`).join("")}</div>`;
      margin.appendChild(m);
      $(".dot", m).onclick = () => toggleMemo(c);
      $$("[data-cpfix]", m).forEach((b) => (b.onclick = async () => {
        try { await navigator.clipboard.writeText(notes[+b.dataset.cpfix].fix); toast("수정안을 복사했어요"); } catch { toast("복사하지 못했어요"); }
      }));
      $$("[data-done]", m).forEach((b) => (b.onclick = () => {
        notes.splice(+b.dataset.done, 1);
        if (notes.length) c.dataset.review = JSON.stringify(notes);
        else { delete c.dataset.review; c.classList.remove("active"); openCite = null; }
        layoutMemos();
        scheduleSave();
      }));
      bottom = y + m.offsetHeight;
      return;
    }
    const warn = c.classList.contains("warn") && !c.classList.contains("need");
    m.className = "memo" + (warn ? " warn" : "") + (openCite === c ? " open" : "");
    m.style.top = y + "px";
    const msg = citeMsg.get(c) || "";
    const note = c.dataset.note || "";
    const sec = c.dataset.sugSecondary === "1";
    const sug = c.dataset.sugChunk ? `<div class="sug">${sec ? `${esc(note)}<br>바꿀 표기: ` : "추천 근거: "}<b>${esc(c.dataset.sugLabel)}</b>
        <div class="row"><button data-a="sugview">미리보기</button><button class="primary" data-a="sugapply">${sec ? "재인용으로 바꾸기" : "이 근거 적용"}</button></div></div>` : "";
    m.innerHTML = warn
      ? `<span class="dot"><b>⚠</b>내용 확인</span>
      <div class="box"><div class="lbl">${esc(c.textContent)}</div>
        <div class="sent">“${esc(sentenceOf(c).slice(0, 120))}”</div>
        <div class="prog">${esc(note || "원문과 내용이 다를 수 있어요")}</div>
        <div class="row"><button data-a="view">원문 보기</button><button data-a="auto">다른 근거 찾기</button><button data-a="okay">괜찮아요</button></div>
        ${msg ? `<div class="prog">${esc(msg)}</div>` : ""}</div>`
      : `<span class="dot"><b>?</b>${c.dataset.sugChunk ? "추천 근거 있음" : "출처 필요"}</span>
      <div class="box"><div class="lbl">${esc(c.textContent)}</div>
        <div class="sent">“${esc(sentenceOf(c).slice(0, 120))}”</div>
        ${note && !c.dataset.sugChunk ? `<div class="prog">${esc(note)}</div>` : ""}${sug}
        <div class="row"><button data-a="manual">직접 찾기</button><button class="primary" data-a="auto">자동</button></div>
        ${msg ? `<div class="prog">${esc(msg)}</div>` : ""}</div>`;
    margin.appendChild(m);
    const on = (a, f) => { const b = $(`[data-a="${a}"]`, m); if (b) b.onclick = f; };
    $(".dot", m).onclick = () => toggleMemo(c);
    on("manual", () => showFinder(c));
    on("auto", () => autoFind(c));
    on("view", () => showEvidence(c));
    on("okay", () => { c.classList.remove("warn"); delete c.dataset.note; openCite = null; layoutMemos(); scheduleSave(); });
    on("sugview", () => showEvidence({ chunk: c.dataset.sugChunk, quote: c.dataset.sugQuote }));
    on("sugapply", () => {
      const hit = { chunk_id: c.dataset.sugChunk, quote: c.dataset.sugQuote };
      // 재인용 제안은 내가 쓴 저자는 그대로 두고 '…에서 재인용'만 붙인 표기라 바꿔도 됨
      const label = c.dataset.sugSecondary === "1" || !parseLabel(c.textContent).author ? c.dataset.sugLabel : null;
      ["sugChunk", "sugQuote", "sugLabel", "sugSecondary", "note", "auto"].forEach((k) => delete c.dataset[k]);
      linkCite(c, hit, label);
    });
    bottom = y + m.offsetHeight;
  });
}

function toggleMemo(c, forceOpen) {
  openCite = forceOpen || openCite !== c ? c : null;
  $$(".cite.active, [data-review].active", editor).forEach((x) => x.classList.remove("active"));
  if (openCite) c.classList.add("active");
  layoutMemos();
}

function linkCite(c, hit, newLabel) {
  c.dataset.chunk = hit.chunk_id;
  c.dataset.quote = hit.quote || "";
  if (newLabel) c.textContent = newLabel;
  c.classList.remove("need", "warn");
  delete c.dataset.note;
  citeMsg.delete(c);
  if (openCite === c) openCite = null;
  layoutMemos();
  scheduleSave();
  showEvidence(c);
}

async function autoFind(c) {
  const { author, year } = parseLabel(c.textContent);
  const sentence = sentenceOf(c);
  if (!sentence) return toast("인용 앞에 문장이 없어요");
  citeMsg.set(c, "로컬 AI가 찾는 중…");
  layoutMemos();
  const { job } = await api("/api/autofind", { json: { sentence, author, year, project_id: S.cur?.id } });
  return runJob(job, `근거 찾기 ${c.textContent}`, {
    onLog: (last) => { citeMsg.set(c, last); if (openCite === c) layoutMemos(); },
    onDone: (r) => {
      if (!r) { citeMsg.set(c, "오류가 났어요. 다시 시도해 주세요."); return layoutMemos(); }
      if (r.status === "linked") {
        if (!author) {
          // 저자가 없는 자리('출처 필요' 등): AI가 고른 자료라서 확인 표시를 남긴다. 원래 글자는 메모에 남김
          const before = c.textContent;
          linkCite(c, r, /^\W*$|출처\s*필요|저자\s*미상|연도\s*미상/.test(before) ? r.cite : null);  // 빈자리일 때만 표기를 채움
          c.classList.add("warn");
          citeMsg.set(c, `AI가 고른 자료예요 (원래 표기: ${before}). 맞는 근거인지 확인해 주세요.`);
          layoutMemos();
          toast("근거 후보를 연결했어요 — 확인이 필요해요");
        } else {
          linkCite(c, r, null);  // 내가 쓴 인용 표기는 절대 바꾸지 않음
          toast("근거를 찾아 연결했어요");
        }
      } else if (r.status === "alternative") {
        if (confirm(`인용하신 ${c.textContent} 논문에서는 근거를 찾지 못했습니다.\n대신 ${r.cite} 「${r.title}」에서 근거를 찾았어요.\n\n인용을 ${r.cite}(으)로 바꿀까요?`)) linkCite(c, r, r.cite);
        else { citeMsg.set(c, `다른 자료 ${r.cite}에 근거가 있음 (바꾸지 않음)`); layoutMemos(); }
      } else {
        citeMsg.set(c, "근거를 찾지 못했어요. '직접 찾기'로 후보를 보세요.");
        layoutMemos();
      }
    },
  });
}

async function autoAll() {
  const list = $$(".cite.need", editor);
  if (!list.length) return toast("출처 필요(?) 표시가 없어요 👍");
  const e = S.est[S.cur.id] || (await loadEst(S.cur.id));
  const money = e && e.sub ? " 내 Claude 구독으로 해요(추가 요금 없음)." : e && e.engine === "claude" ? ` Claude 사용 예상 약 ${(e.won.autoall || 0).toLocaleString()}원.` : " 내 컴퓨터 로컬 AI로 해요(무료).";
  if (!confirm(`출처 필요 ${list.length}곳을 차례로 자동으로 찾을까요?${money}`)) return;
  for (const c of list) if (document.body.contains(c) && c.classList.contains("need")) await autoFind(c);
}

// 이어 쓰기 / 메모를 문단으로: 커서(또는 선택) 위치의 문단 바로 뒤에 근거 있는 AI 초안을 넣음
async function aiDraft(mode) {
  const p = S.cur;
  const sel = window.getSelection();
  if (!sel.rangeCount || !editor.contains(sel.anchorNode)) return toast(mode === "expand" ? "풀어 쓸 메모를 원고에서 드래그해서 골라 주세요" : "이어 쓸 곳(문단 끝)에 커서를 먼저 두세요");
  const range = sel.getRangeAt(0);
  const text = mode === "expand" ? sel.toString().trim() : "";
  if (mode === "expand" && text.length < 5) return toast("풀어 쓸 메모를 원고에서 드래그해서 골라 주세요");
  const pre = document.createRange();
  pre.setStart(editor, 0);
  pre.setEnd(range.endContainer, range.endOffset);
  const before = pre.toString().slice(-2000);
  let node = range.endContainer.nodeType === 1 ? range.endContainer : range.endContainer.parentElement;
  let block = node.closest("p,h1,h2,h3,h4,li,div");
  if (!block || block === editor || !editor.contains(block)) block = null;
  while (block && block.parentElement !== editor) block = block.parentElement;
  let h = block, heading = "";
  while (h) { if (/^H[1-4]$/.test(h.tagName)) { heading = h.textContent.trim(); break; } h = h.previousElementSibling; }
  const wait = document.createElement("p");
  wait.className = "ai-wait";
  wait.contentEditable = "false";
  wait.textContent = mode === "expand" ? "⏳ 메모를 문단으로 풀어 쓰는 중… (근거 확인까지 1~2분)" : "⏳ 다음 문단을 쓰는 중… (근거 확인까지 1~2분)";
  if (block) block.after(wait); else editor.appendChild(wait);
  const pid = p.id;
  let job;
  try { ({ job } = await api(`/api/projects/${pid}/draft`, { json: { mode, text, before, heading } })); }
  catch (e) { wait.remove(); return toast("⚠ " + e.message, 6000); }
  runJob(job, mode === "expand" ? "메모 → 문단" : "이어 쓰기", {
    onDone: (r, j) => {
      if (j.status !== "done" || !r?.html) { wait.remove(); toast("⚠ " + ((j.log || []).slice(-1)[0] || "쓰지 못했어요"), 7000); return; }
      if (S.cur?.id !== pid || !document.body.contains(wait)) {
        api(`/api/projects/${pid}`).then((pp) => api(`/api/projects/${pid}`, { method: "PUT", json: { content: (pp.content || "") + r.html } }));
        return;
      }
      wait.insertAdjacentHTML("afterend", r.html);
      wait.remove();
      normalizeCites();
      checkAiDrafts();
      layoutMemos();
      scheduleSave();
      toast("보라색 문단이 AI가 쓴 부분이에요. 근거를 눌러 확인하고 내 말로 고쳐 주세요", 5000);
    },
  });
}

async function aiWriteDialog(night) {
  const p = S.cur;
  if (!p.plan) return toast("먼저 ① 연구계획을 세워주세요");
  const route = p.plan.routes[p.route || 0];
  const r = await formDialog("AI로 집필 — 쓸 장을 고르세요", [
    ["secs", `경로: ${route.name}`, route.outline || [], "checks"],
    ["extra", "추가로 쓸 장 (선택, 쉼표로 구분)", ""],
  ], night ? "새벽 2시에 집필하도록 예약" : "집필 시작");
  if (!r) return;
  const sections = [...r.secs, ...r.extra.split(",").map((s) => s.trim()).filter(Boolean)];
  if (!sections.length) return;
  if (night) return scheduleNight("write", { sections });
  const { job } = await api(`/api/projects/${p.id}/write`, { json: { sections } });
  const pid = p.id;
  runJob(job, "AI 집필 중 (장마다 1~4분)", {
    onEmit: (html) => {
      if (S.cur?.id !== pid || !document.body.contains(editor)) {
        // 다른 화면을 보고 있으면 저장본에 직접 덧붙임
        api(`/api/projects/${pid}`).then((pp) => api(`/api/projects/${pid}`, { method: "PUT", json: { content: (pp.content || "") + html } }));
        return;
      }
      editor.insertAdjacentHTML("beforeend", html);
      normalizeCites();
      checkAiDrafts();
      layoutMemos();
      scheduleSave();
    },
    onDone: () => toast("집필 완료 — ? 표시는 근거 확인이 필요한 문장이에요"),
  });
}

async function makeRefs() {
  const ids = $$(".cite[data-chunk]", editor).map((c) => c.dataset.chunk);
  if (!ids.length) return toast("연결된 인용이 아직 없어요");
  const refs = await api("/api/references", { json: { chunks: ids } });
  $$("[data-refs]", editor).forEach((e) => e.remove());
  editor.insertAdjacentHTML("beforeend", `<h2 data-refs="1">참고문헌</h2><div data-refs="1">${refs.map((r) => `<p style="padding-left:2em;text-indent:-2em">${typeof r === "string" ? esc(r) : r.html}</p>`).join("")}</div>`);
  if (refs.some((r) => (r.text || r).includes("[확인 필요"))) toast("‘[확인 필요]’ 항목은 학술지·권(호)·쪽을 원문에서 확인해 채워 주세요", 5000);
  scheduleSave();
  layoutMemos();
}

// ---------- 새벽 예약 ----------
async function scheduleNight(kind, args) {
  const r = await api("/api/schedule", { json: { project_id: S.cur.id, kind, args } });
  toast(`🌙 ${r.next}에 로컬 AI로 처리하도록 예약했어요`, 4000);
  if (S.tab === "review") renderReview();
}

const TOOL_INFO = [
  ["panel", "⓪ 심사위원 3인 모의 심사", "방법론 전문가·이론 전문가·지도교수형 심사위원 3명이 각자 심사한 뒤 서로 토론하고 최종 판정(통과/조건부 통과/수정 후 재심/불통과)을 내려요. 수정 요구마다 답변서를 쓰면 재심사를 받을 수 있어요. (Claude 3~5분 권장)"],
  ["structure", "⓪ 예비심사 리뷰", "지도교수·심사위원의 눈으로 읽어요 — 논문이 일목요연한지(연구문제 → 선행연구 → 방법 → 결과 → 결론이 한 줄기인지), 연구문제에 결론이 답하는지 보고, 심사에서 나올 비판과 고칠 점을 제안해요. 원고 옆에 💬 메모로도 달려요."],
  ["cites", "② 인용 전체 점검", "원고 속 (저자, 연도)를 모두 찾아 근거를 확인해요 — ✔ 확인 · ⚠ 원문과 다를 수 있음 · ? 근거 없음. 인용이 많으면 오래 걸려요."],
  ["uncited", "③ 근거 없는 문장 찾기", "인용 없이 주장하는 문장에 '출처 필요'를 달고, 내 자료에서 근거를 추천해요. (연구방법·결과 장은 제외)"],
  ["discuss", "④ 논의 보강", "결과 장에서 핵심 결과를 뽑아 일치하는 연구·상반되는 연구를 찾고, 논의에 쓸 문장을 제안해요."],
  ["recent", "⑤ 최신 연구 찾기", "최근 3년 사이 나온 관련 논문 중 원고에 아직 없는 것을 찾아, 원고 어디에 쓰면 좋을지 알려줘요."],
  ["aitone", "⑧ AI 문체 점검", "제출 전에 AI가 쓴 티가 나는 문장을 찾아요 — 문장마다 AI 확률(0~100%)과 AI/의심/사람 분류, 13가지 언어 패턴(문장 길이 균일, 헤지어 과다, 기계적 전환어 등)으로 이유를 보여주고 자연스러운 수정안을 제시해요. 수정안도 AI 도움이라 'AI 사용 기록'에 남아 공개 문구에 반영돼요."],
  ["style", "⑦ 문장 다듬기", "교정자처럼 문장을 점검해요 — 비문·호응, 시제·어미(계획서는 '~할 것이다'), '완벽하게·입증한다' 같은 과장 표현, 중복, 구어체, 용어 일관성. 고친 예시와 함께 원고 옆 💬 메모로 달려요."],
  ["refs", "⑥ 참고문헌 대조", "본문 인용과 참고문헌 목록을 맞춰봐요 — 목록에 없는 인용 · 연도가 다른 것 · 본문에서 안 쓰인 문헌. (몇 초)"],
];
const TOOL_NAME = Object.fromEntries(TOOL_INFO.map(([k, t]) => [k, t.slice(2)]));
let reviewJob = {};

function kindName(r) {
  const a = JSON.parse(r.args || "{}");
  if (r.kind === "collect") return "자료 수집";
  if (r.kind === "write") return `집필 (${(a.sections || []).length}개 장)`;
  return a.tool === "full" ? "원고 전체 검토" : TOOL_NAME[a.tool] || "원고 점검";
}

function reportHtml(k, rep) {
  if (!rep) return "";
  const d = rep.data;
  let h = `<div class="rv-sum">${esc(rep.summary)} <span class="muted small">· ${esc(rep.ran_at)} · ${rep.engine === "claude" ? "Claude" : "로컬 AI"}</span></div>`;
  const st = { ok: "✔", warn: "⚠", need: "?" };
  if (k === "aitone" && d && d.items) {
    const st = d.stats || {};
    h += `<div class="at-meter"><div class="at-bar"><span class="at-ai" style="width:${d.ai_pct}%"></span><span class="at-sus" style="width:${d.sus_pct}%"></span></div>
      <div class="small"><b style="color:var(--warn)">AI ${d.ai_pct}%</b> · <b style="color:#c98a00">의심 ${d.sus_pct}%</b> · 사람 ${Math.max(0, 100 - d.ai_pct - d.sus_pct)}% <span class="muted">(글자 수 기준)</span></div>
      ${st.sentences ? `<div class="small muted">전체 ${st.sentences}문장 · 평균 ${st.len_mean}자 · 문장 길이 변화 ${st.len_cv}${st.len_cv < 0.35 ? " (고른 편 — 기계적으로 보일 수 있음)" : ""} · 헤지어 ${st.hedge}% · 기계적 전환어로 시작 ${st.trans}%</div>` : ""}</div>`;
    h += d.items.map((it) => `<div class="rv-item ${it.label === "AI" ? "need" : "warn"}" data-para="${esc(it.id)}" style="cursor:pointer">
      <span class="badge rq-${it.label === "AI" ? "need" : "warn"}">${it.label} ${it.score}%</span> <span class="small muted">${esc(it.id)} · ${esc((it.patterns || []).join(", "))}</span>
      <div class="small">“${esc(it.sentence)}”</div><div class="small muted">${esc(it.reason)}</div>
      ${it.rewrite ? `<div class="small at-fix">✏ ${esc(it.rewrite)} <button class="small ghost" data-copy-fix="${esc(it.rewrite)}">복사</button></div>` : ""}</div>`).join("") || `<div class="rv-item ok">✔ AI처럼 보이는 문장이 없어요</div>`;
  }
  if (k === "style" && Array.isArray(d) && d.length) {
    h += d.map((it) => `<div class="rv-item warn" data-para="${esc(it.id)}" style="cursor:pointer"><span class="badge gray">${esc(it.category)}</span> <span class="small muted">${esc(it.id)}</span> “${esc(it.excerpt)}”
      <div class="small">${esc(it.problem)}</div>${it.fix ? `<div class="small">✏ ${esc(it.fix)}</div>` : ""}</div>`).join("");
  }
  if (k === "panel" && d && d.reviews) {
    const vc = { 통과: "ok", "조건부 통과": "ok", "수정 후 재심": "warn", 불통과: "need" };
    const sc = { 해결: "ok", "부분 해결": "warn", 미해결: "need" };
    h += `<div class="pn-verdict ${vc[d.verdict] || ""}"><div class="small muted">${esc(d.target)} · ${d.round}차 판정</div><b>${esc(d.verdict)}</b><div class="small">${esc(d.verdict_reason)}</div></div>`;
    h += `<details class="small" style="margin-top:8px"><summary>세 심사위원의 독립 심사 보기</summary>${d.reviews.map((rv) => `<div class="rv-item"><b>${esc(rv.name)}</b> <span class="muted">${esc(rv.role)}</span> <span class="badge rq-${vc[rv.decision] || "warn"}">${esc(rv.decision)}</span>
      <div>${esc(rv.summary)}</div>${(rv.strengths || []).length ? `<div class="muted">강점: ${esc(rv.strengths.join(" · "))}</div>` : ""}
      ${(rv.concerns || []).map((c) => `<div>• <span class="badge sev-${esc(c.severity)}">${esc(c.severity)}</span> <span class="muted">${esc(c.where)}</span> ${esc(c.issue)} <span class="muted">→ ${esc(c.request)}</span></div>`).join("")}</div>`).join("")}</details>`;
    if (d.agreements.length) h += `<div class="small muted" style="margin-top:8px">세 사람이 공통으로 지적한 점</div>${d.agreements.map((x) => `<div class="rv-item warn">${esc(x)}</div>`).join("")}`;
    if (d.disagreements.length) h += `<div class="small muted" style="margin-top:8px">의견이 갈린 쟁점</div>${d.disagreements.map((x) => `<div class="rv-item"><b>${esc(x.issue)}</b>
      <div class="small">① ${esc(x.r1)}<br>② ${esc(x.r2)}<br>③ ${esc(x.r3)}</div><div class="small"><b>위원회 정리:</b> ${esc(x.resolution)}</div></div>`).join("")}`;
    h += `<div class="small muted" style="margin-top:8px">수정 요구와 답변서 (답을 적고 '재심사 받기'를 누르세요)</div>` + d.required.map((it) => `<div class="rv-item ${sc[it.status] || ""}">
      <b>${it.id}.</b> ${esc(it.issue)} <span class="small muted">(심사위원 ${esc((it.by || []).join(", "))})</span>${it.status ? ` <span class="badge rq-${sc[it.status]}">${esc(it.status)}</span>` : ""}
      <div class="small muted">${esc(it.why)}</div>
      ${it.comment ? `<div class="small"><b>재심 의견:</b> ${esc(it.comment)}</div>` : ""}
      ${it.status === "해결" ? `<div class="small muted">내 답변: ${esc(it.response)}</div>` : `<textarea class="pn-resp" data-resp="${it.id}" rows="2" placeholder="어떻게 고쳤는지, 또는 왜 고치지 않는지 적어주세요">${esc(it.response || "")}</textarea>`}</div>`).join("");
    if (d.required.some((it) => it.status !== "해결")) h += `<div class="row" style="margin-top:6px"><button class="primary" id="pnRespond">답변 제출하고 재심사 받기</button><span class="small muted">원고를 고친 뒤 받으면 수정 내용까지 확인해요</span></div>`;
  }
  if (k === "structure" && d && d.skeleton) {
    const go = (pp) => pp ? `<button class="small ghost" data-para="${esc(pp)}">원고에서 보기</button>` : "";
    h += `<div class="sv-thread"><div class="small muted">이 논문의 한 줄기</div><b>${esc(d.thread)}</b><div class="small" style="margin-top:6px">${esc(d.verdict)}</div></div>`;
    if (d.strengths.length) h += `<div class="small muted" style="margin-top:8px">잘된 점</div>${d.strengths.map((x) => `<div class="rv-item ok"><b class="st">✔</b> ${esc(x)}</div>`).join("")}`;
    if (d.rq.length) {
      const mark = { 예: ["ok", "✔ 답함"], 부분: ["warn", "△ 일부"], 아니오: ["need", "✕ 안 답함"] };
      h += `<div class="small muted" style="margin-top:8px">연구문제 ↔ 결론</div>` + d.rq.map((x) => {
        const [cl, lb] = mark[x.answered] || ["", x.answered];
        return `<div class="rv-item ${cl}"><span class="badge rq-${cl}">${lb}</span> ${esc(x.question)}<div class="small muted">${esc(x.comment)}</div>${go(x.where)}</div>`;
      }).join("");
    }
    if (d.critiques.length) {
      const sevc = { 중요: "need", 보통: "warn", 사소: "" };
      h += `<div class="small muted" style="margin-top:8px">심사 의견 · 고칠 점</div>` + d.critiques.map((c) => `<div class="rv-item ${sevc[c.severity] || ""}">
        <span class="badge sev-${esc(c.severity)}">${esc(c.severity)}</span> <span class="small muted">${esc(c.category)}${c.para ? " · " + esc(c.para) : " · 논문 전체"}</span>
        <div><b>${esc(c.issue)}</b></div>
        ${c.fix ? `<div class="small">✏ 이렇게 고쳐보세요: ${esc(c.fix)}</div>` : ""}
        ${c.question ? `<div class="small muted">🎤 예상 질문: “${esc(c.question)}”</div>` : ""}${go(c.para)}</div>`).join("");
    }
    h += `<details class="sv-map" open><summary class="small muted">논문 뼈대 — 장마다 하는 말 (빨간 점은 흐름이 걸리는 문단)</summary>
      ${d.skeleton.map((sec) => `<div class="sv-sec"><div class="sv-h">${esc(sec.title || "(제목 없음)")}</div><div class="sv-claim">${esc(sec.claim)}</div>
        <div class="sv-paras">${sec.paras.map((x) => `<span class="sv-p${x.problem ? " bad" : ""}" data-para="${esc(x.id)}" title="${esc(x.problem || x.topic)}">${esc(x.id)} ${esc(x.topic)}</span>`).join("")}</div></div>`).join("")}</details>`;
  }
  if (k === "cites" && d.length) {
    const order = { warn: 0, need: 1, ok: 2 };
    h += [...d].sort((a, b) => order[a.status] - order[b.status]).map((it) => `<div class="rv-item ${it.status}" data-cid="${it.cid}">
      <b class="st">${st[it.status] || ""}</b> <b>${esc(it.label)}</b> <span class="muted">${esc(it.note || "")}</span>
      <div class="small">“${esc(it.sentence)}”</div></div>`).join("");
  }
  if (k === "uncited" && d.length) {
    h += d.map((it) => `<div class="rv-item need"><b class="st">?</b> <span class="muted small">${esc(it.section || "본문")}</span>
      ${it.suggest ? `<span class="badge">추천 ${esc(it.suggest)}</span>` : ""}<div class="small">“${esc(it.sentence)}”</div></div>`).join("");
  }
  if (k === "discuss" && d.length) {
    const rec = (r, rel, fi, ri) => `<div class="rv-item ${rel === "support" ? "ok" : "warn"}">
      <b>${esc(r.cite)}</b> <span class="muted small">${esc(r.title)}</span>
      <div class="small">${esc(r.reason)}</div>
      <div class="small"><b>제안 문장:</b> ${esc(r.sentence)}</div>
      <div class="row" style="margin-top:4px">${r.chunk_id ? `<button class="small" data-ev="${r.chunk_id}" data-q="${esc(r.quote || "")}">원문 보기</button>` : `<span class="small muted">${r.doi ? `PDF를 받지 못했어요 — <a href="https://doi.org/${esc(r.doi)}" target="_blank">논문 페이지</a>에서 받아 추가하면 근거가 연결돼요` : ""}</span>`}
      <button class="small primary" data-ins="${fi}-${rel}-${ri}">논의에 넣기</button></div></div>`;
    h += d.map((f, fi) => `<div class="rv-find"><div class="rv-ftitle">결과 ${fi + 1}. ${esc(f.finding)}</div>
      ${f.support.length ? `<div class="small muted">일치하는 연구</div>${f.support.map((r, ri) => rec(r, "support", fi, ri)).join("")}` : ""}
      ${f.contrast.length ? `<div class="small muted">상반되는 연구</div>${f.contrast.map((r, ri) => rec(r, "contrast", fi, ri)).join("")}` : ""}
      ${!f.support.length && !f.contrast.length ? `<div class="small muted">관련 연구를 찾지 못했어요</div>` : ""}</div>`).join("");
  }
  if (k === "recent" && d.length) {
    h += d.map((w, i) => `<div class="rv-item"><b>${w.doi ? `<a href="https://doi.org/${esc(w.doi)}" target="_blank">${esc(w.title)}</a>` : esc(w.title)}</b>
      <div class="small muted">${esc((w.authors || []).slice(0, 3).join(", "))} · ${esc(w.year)} · ${esc(w.venue || "")} · 피인용 ${w.cited}</div>
      ${w.where ? `<div class="small">📍 쓰면 좋을 곳: ${esc(w.where)}</div>` : ""}
      <details class="small"><summary>초록</summary>${esc(w.abstract)}</details>
      <div class="row" style="margin-top:4px">${w.is_oa ? `<button class="small" data-get="${i}">내 자료로 내려받기</button>` : `<span class="small muted">구독 논문 — 내려받기는 도서관 경유로 시도</span><button class="small" data-get="${i}">시도</button>`}</div></div>`).join("");
  }
  if (k === "refs" && d && d.n_refs !== undefined) {
    const list = (t, arr, f) => arr.length ? `<div class="small muted" style="margin-top:6px">${t} (${arr.length})</div>${arr.map(f).join("")}` : "";
    h += list("참고문헌 목록에 없는 본문 인용", d.missing, (x) => `<div class="rv-item need"><b class="st">?</b> ${esc(x)}</div>`);
    h += list("연도가 다른 것", d.wrong_year, (x) => `<div class="rv-item warn"><b class="st">⚠</b> 본문 ${esc(x.label)}<div class="small">목록: ${esc(x.refs.join(" / "))}</div></div>`);
    h += list("본문에서 인용하지 않은 문헌", d.unused, (x) => `<div class="rv-item"><b class="st">–</b> <span class="small">${esc(x)}</span></div>`);
    if (!d.missing.length && !d.wrong_year.length && !d.unused.length) h += `<div class="rv-item ok">✔ 본문 인용과 참고문헌이 모두 맞아요</div>`;
  }
  return h;
}

async function renderReview() {
  const p = S.cur;
  if (!S.est[p.id]) loadEst(p.id).then(() => { if (S.cur?.id === p.id && S.tab === "review") renderReview(); });
  const body = $("#tabBody");
  const has = (p.content || "").replace(/<[^>]+>/g, "").trim().length > 30;
  let sched = { rows: [], next: "", task: "none" };
  if (!S.guest) try { sched = await api("/api/schedule"); } catch {}
  const mine = sched.rows.filter((r) => r.project_id === p.id && r.status !== "done").slice(0, 10);
  const rj = reviewJob[p.id];
  const card = ([k, t, d]) => `<div class="card" id="tool-${k}"><div class="row"><h3 class="grow">${t}</h3>
      ${k === "panel" ? `<select id="pnTarget" class="small">${["학위논문 예비심사", "학위논문 본심사", "KCI 학술지 투고", "SSCI 학술지 투고"].map((t) => `<option ${(p.reports?.panel?.data?.target || "학위논문 예비심사") === t ? "selected" : ""}>${t}</option>`).join("")}</select>` : ""}
      <button class="small" data-night="${k}" ${has ? "" : "disabled"}>🌙 새벽 예약</button>
      ${costTag(p.id, k)}
      ${p.reports?.[k] ? `<button class="small ghost" data-force="${k}" ${has && !rj ? "" : "disabled"} title="원고가 그대로여도 다시 돌려요">새로 실행</button>` : ""}
      <button class="small primary" data-run="${k}" ${has && !rj ? "" : "disabled"}>${p.reports?.[k] ? "다시 실행" : "지금 실행"}</button></div>
      <p class="small muted" style="margin:2px 0 8px">${d}</p>
      ${k === "structure" && p.engine !== "claude" ? `<p class="small" style="color:var(--warn);margin:-4px 0 8px">로컬 AI는 논문 뼈대 정리까지는 쓸 만하지만, 심사 의견은 얕거나 빗나갈 수 있어요. 이 기능은 Claude로 돌리기를 권해요. (민감한 원고라 로컬만 써야 한다면 참고용으로만 보세요)</p>` : ""}
      ${rj && (rj.tool === k || rj.tool === "full") ? `<div class="log" id="rvlog-${k}">시작…</div>` : ""}
      <div class="rv-body">${reportHtml(k, p.reports?.[k])}</div></div>`;
  body.innerHTML = `<div class="pad">
    <div class="card"><h3>① 원고 불러오기</h3>
      <p class="small muted">${has ? "원고가 들어 있어요. 다른 파일로 바꾸려면 다시 불러오세요 (지금 원고는 덮어써져요)." : "이미 쓴 원고(.docx, .hwpx, .txt)를 올리거나 글을 붙여넣으세요. 원고 속 (저자, 연도)·저자(연도)는 자동으로 인용으로 인식돼요."}</p>
      <div class="row"><button id="impFile">📄 파일 선택</button><input type="file" id="impInput" accept=".docx,.hwpx,.txt,.md,.hwp" hidden>
        <button id="impPasteBtn">📋 글 붙여넣기</button>${has ? `<button id="goWrite">④ 집필 화면에서 원고 보기 →</button>` : ""}</div>
      <div id="impPaste" style="display:none;margin-top:8px"><textarea id="impText" rows="7" placeholder="여기에 원고를 붙여넣으세요. 장 제목(I. 서론, 1. 연구의 필요성 등)은 자동으로 제목이 돼요."></textarea>
        <button class="primary" id="impPasteGo" style="margin-top:6px">불러오기</button></div>
    </div>
    <div class="card"><div class="row"><div class="grow"><b>AI 엔진:</b> ${p.engine === "claude" ? "Claude — 빠르고 정확 (사용량만큼 요금)" : "내 컴퓨터 로컬 AI — 무료, 느림"}
        <label class="chk small"><input type="checkbox" id="localOnly" ${p.local_only ? "checked" : ""}> 이 원고는 로컬 AI만 사용 (상담 사례 등 민감한 내용이 있을 때)</label></div>
      <button id="fullNight" ${has ? "" : "disabled"}>🌙 새벽 2시에 전체 검토</button>
      <button class="primary" id="fullNow" ${has && !rj ? "" : "disabled"}>전체 검토 지금</button></div>
      <p class="small muted" style="margin:4px 0 0">전체 검토 = ⓪ 예비심사 리뷰 → ⑥ 참고문헌 → ② 인용 → ③ 근거 없는 문장 → ⑤ 최신 연구 → ④ 논의 보강 순서로 모두 실행</p>
      ${rj && rj.tool === "full" ? `<div class="log" id="rvlog-full" style="margin-top:8px">시작…</div>` : ""}</div>
    ${mine.length ? `<div class="card"><h3>🌙 예약된 작업 — ${esc(sched.next)} 실행 (로컬 AI)</h3>
      ${mine.map((r) => `<div class="row small sched"><span class="grow">${esc(kindName(r))} — ${{ waiting: "대기", running: "실행 중", error: "오류" }[r.status] || r.status}</span>
        ${r.status === "waiting" ? `<button class="small ghost" data-unsched="${r.id}">취소</button>` : ""}</div>`).join("")}
      ${sched.task !== "registered" ? `<p class="small" style="color:var(--warn);margin-bottom:0">새벽에 컴퓨터가 앱을 자동으로 켜게 하려면 ⚙ 설정 → '새벽 자동 실행 등록'을 눌러주세요. 등록하지 않으면 앱이 켜져 있을 때만 실행돼요.</p>` : ""}</div>` : ""}
    ${TOOL_INFO.map(card).join("")}
    <div class="card" id="aiUse"><h3>🧾 AI 사용 기록 · 투고용 공개 문구</h3>
      <p class="small muted" style="margin:2px 0 8px">이 원고에 어떤 AI 기능을 언제 썼는지 자동으로 기록해 두었어요. 학술지에 낼 때 아래 'AI 사용 명시' 문구를 투고 규정에 맞게 고쳐 쓰세요. (학술지·학교마다 규정이 달라요)</p>
      <div id="aiUseBody" class="small muted">불러오는 중…</div></div>
  </div>`;
  loadAiUse(p);

  // 불러오기
  $("#impFile").onclick = () => $("#impInput").click();
  $("#impInput").onchange = async (e) => {
    if (has && !confirm("지금 원고를 이 파일로 바꿀까요?")) return;
    const fd = new FormData();
    fd.append("file", e.target.files[0]);
    await api(`/api/projects/${p.id}/import`, { method: "POST", body: fd });
    S.cur = await api(`/api/projects/${p.id}`);
    toast("원고를 불러왔어요");
    renderReview();
  };
  $("#impPasteBtn").onclick = () => { $("#impPaste").style.display = "block"; $("#impText").focus(); };
  $("#impPasteGo").onclick = async () => {
    const text = $("#impText").value.trim();
    if (!text) return toast("원고를 붙여넣어 주세요");
    if (has && !confirm("지금 원고를 붙여넣은 글로 바꿀까요?")) return;
    const fd = new FormData();
    fd.append("text", text);
    await api(`/api/projects/${p.id}/import`, { method: "POST", body: fd });
    S.cur = await api(`/api/projects/${p.id}`);
    toast("원고를 불러왔어요");
    renderReview();
  };
  if ($("#goWrite")) $("#goWrite").onclick = () => { S.tab = "write"; renderProject(); };
  $("#localOnly").onchange = async (e) => {
    await api(`/api/projects/${p.id}`, { method: "PUT", json: { local_only: e.target.checked ? 1 : 0 } });
    S.cur = await api(`/api/projects/${p.id}`);
    renderProject();
  };
  $("#fullNow").onclick = () => runReview("full");
  $("#fullNight").onclick = () => scheduleNight("review", { tool: "full" });
  $$("[data-run]", body).forEach((b) => (b.onclick = () => runReview(b.dataset.run)));
  $$("[data-force]", body).forEach((b) => (b.onclick = () => runReview(b.dataset.force, true)));
  $$("[data-night]", body).forEach((b) => (b.onclick = () => scheduleNight("review", { tool: b.dataset.night })));
  $$("[data-unsched]", body).forEach((b) => (b.onclick = async () => { await api(`/api/schedule/${b.dataset.unsched}`, { method: "DELETE" }); renderReview(); }));
  // 결과에서 원고로 이동
  $$(".rv-item[data-cid]", body).forEach((d) => (d.onclick = () => { S.focusCid = d.dataset.cid; S.tab = "write"; renderProject(); }));
  $$("[data-para]", body).forEach((d) => (d.onclick = () => { S.focusPara = d.dataset.para; S.tab = "write"; renderProject(); }));
  $$("[data-copy-fix]", body).forEach((b) => (b.onclick = async (e) => {
    e.stopPropagation();
    try { await navigator.clipboard.writeText(b.dataset.copyFix); toast("수정안을 복사했어요 — ④ 집필에서 붙여넣으세요"); } catch { toast("복사하지 못했어요"); }
  }));
  if ($("#pnRespond")) $("#pnRespond").onclick = async () => {
    const responses = Object.fromEntries($$(".pn-resp", body).map((t) => [t.dataset.resp, t.value]));
    if (!Object.values(responses).some((v) => v.trim())) return toast("답변을 하나 이상 적어주세요");
    await saveNow();
    const { job } = await api(`/api/projects/${p.id}/panel/respond`, { json: { responses } });
    reviewJob[p.id] = { job, tool: "panel" };
    renderReview();
    runJob(job, "재심사", {
      logEl: "rvlog-panel",
      onDone: async (r, j) => {
        delete reviewJob[p.id];
        if (S.cur?.id === p.id) { S.cur = await api(`/api/projects/${p.id}`); if (S.tab === "review") renderReview(); }
        if (j.status === "done") toast("재심사 완료");
      },
    });
  };
  $$("[data-ev]", body).forEach((b) => (b.onclick = () => showEvidence({ chunk: b.dataset.ev, quote: b.dataset.q })));
  $$("[data-ins]", body).forEach((b) => (b.onclick = async () => {
    const [fi, rel, ri] = b.dataset.ins.split("-");
    const r = p.reports.discuss.data[+fi][rel][+ri];
    await api(`/api/projects/${p.id}/insert`, { json: { sentence: r.sentence, chunk_id: r.chunk_id || null, quote: r.quote || "", label: r.cite } });
    S.cur = await api(`/api/projects/${p.id}`);
    b.textContent = "넣었어요 ✓";
    b.disabled = true;
    toast("논의 장 끝에 문장을 넣었어요 — ④ 집필에서 다듬어 주세요");
  }));
  $$("[data-get]", body).forEach((b) => (b.onclick = async () => {
    const w = p.reports.recent.data[+b.dataset.get];
    b.disabled = true;
    const { job } = await api("/api/fetch_work", { json: w });
    runJob(job, "논문 내려받는 중", { onDone: (r) => { b.textContent = r?.paper ? "내 자료에 추가됨 ✓" : "받지 못했어요"; } });
  }));
}

async function loadAiUse(p) {
  const el = $("#aiUseBody");
  let u;
  try { u = await api(`/api/projects/${p.id}/ai_usage`); } catch { el.textContent = "기록을 불러오지 못했어요"; return; }
  const eng = (r) => r.engine === "claude" ? "Claude" : r.engine === "local" ? "로컬 AI" : "—";
  const warn = u.drafts ? `<div class="rv-item warn"><b class="st">⚠</b> 아직 <b>AI 초안 그대로인 문단이 ${u.drafts}개</b> 있어요. ④ 집필에서 보라색 문단을 내 말로 고쳐 쓰거나, 공개 문구에 초안 작성 사실을 밝혀 주세요.</div>` : "";
  if (!u.rows.length) { el.innerHTML = warn + "아직 이 원고에서 AI 기능을 쓴 기록이 없어요."; return; }
  const box = (id, t, v) => `<div class="small muted" style="margin-top:10px">${t}</div>
    <textarea id="${id}" rows="4" style="width:100%">${esc(v)}</textarea><button class="small" data-copy="${id}">📋 복사</button>`;
  el.classList.remove("muted");
  el.innerHTML = warn + `<table class="aiuse"><tr><th>기능</th><th>엔진</th><th>횟수</th><th>마지막 사용</th></tr>
    ${u.rows.map((r) => `<tr><td>${esc(r.label)}</td><td>${eng(r)}${r.model ? ` <span class="muted">${esc(r.model)}</span>` : ""}</td><td>${r.n}</td><td>${esc(r.last.slice(0, 16))}</td></tr>`).join("")}</table>
    ${box("aiKo", "공개 문구 (한국어)", u.ko)}${box("aiEn", "Disclosure statement (English)", u.en)}`;
  $$("[data-copy]", el).forEach((b) => (b.onclick = async () => {
    const t = $("#" + b.dataset.copy);
    try { await navigator.clipboard.writeText(t.value); } catch { t.select(); document.execCommand("copy"); }
    toast("복사했어요");
  }));
}

async function runReview(tool, force = false) {
  const p = S.cur;
  await saveNow();
  const qs = [];
  if (tool === "panel" && $("#pnTarget")) qs.push(`target=${encodeURIComponent($("#pnTarget").value)}`);
  if (force) qs.push("force=1");
  const q = qs.length ? "?" + qs.join("&") : "";
  const { job } = await api(`/api/projects/${p.id}/review/${tool}${q}`, { method: "POST" });
  reviewJob[p.id] = { job, tool };
  renderReview();
  const title = tool === "full" ? "원고 전체 검토" : TOOL_NAME[tool];
  runJob(job, title, {
    logEl: `rvlog-${tool}`,
    onDone: async (r, j) => {
      delete reviewJob[p.id];
      if (S.cur?.id === p.id) {
        S.cur = await api(`/api/projects/${p.id}`);
        if (S.tab === "review" || S.tab === "write") renderProject();
      }
      if (j.status === "done") toast(`${title} 완료`);
    },
  });
}

// ---------- 오른쪽 근거 패널 ----------
function openPanel(titleHtml) {
  $("#panel").classList.remove("hidden");
  document.body.classList.add("panel-open");
  $("#panelTitle").innerHTML = titleHtml;
  requestAnimationFrame(layoutMemos);
  return $("#panelBody");
}
$("#panelClose").onclick = () => {
  $("#panel").classList.add("hidden");
  document.body.classList.remove("panel-open");
  if (editor) $$(".cite.active", editor).forEach((x) => x.classList.remove("active"));
  requestAnimationFrame(layoutMemos);
};

async function showEvidence(src) {
  const chunk = src.dataset ? src.dataset.chunk : src.chunk;
  const quote = src.dataset ? src.dataset.quote || "" : src.quote || "";
  if (src.classList && editor) { $$(".cite.active", editor).forEach((x) => x.classList.remove("active")); src.classList.add("active"); }
  let e;
  try { e = await api(`/api/evidence?chunk=${chunk}&quote=${encodeURIComponent(quote)}`); }
  catch (err) { toast(err.message); return; }
  const body = openPanel(`<div class="ev-cite">${esc(e.cite)}</div><div class="ev-title">${esc(e.title)}</div>
    <div class="ev-meta">${esc(e.authors.join(", "))} · ${esc(e.year)} · ${e.pages}쪽 중 <b>${e.page + 1}쪽</b></div>`);
  renderEvidence(body, e);
}

function markedText(e) {
  if (!e.quote_range) return esc(e.text);
  const [a, b] = e.quote_range;
  return esc(e.text.slice(0, a)) + "<mark>" + esc(e.text.slice(a, b)) + "</mark>" + esc(e.text.slice(b));
}

function renderEvidence(box, e, extraHtml = "") {
  let page = e.page;
  const matchTxt = e.match >= 0.99 ? `<span class="match ok">✔ 원문과 글자 그대로 일치</span>`
    : e.match > 0 ? `<span class="match part">부분 일치 (${Math.round(e.match * 100)}%)</span>`
    : `<span class="match part">문단 단위로 표시</span>`;
  box.innerHTML = `${extraHtml}${matchTxt}
    <div class="quote" id="evQuote">${markedText(e)}</div>
    <div class="row" style="margin:6px 0"><button class="small" id="evExplain">🇰🇷 쉽게 설명·번역</button></div><div id="evExpl"></div>
    <div class="pagenav"><button data-d="-1">◀</button><span id="pgLbl"></span><button data-d="1">▶</button>
      <button class="small" id="backPg">인용 쪽으로</button><span class="grow"></span>
      <a href="/api/pdf/${e.paper_id}#page=${e.page + 1}" target="_blank">원본 PDF 열기 ↗</a></div>
    <div class="pageview" id="pv"></div>`;
  $("#evExplain", box).onclick = async () => {
    const b = $("#evExplain", box), out = $("#evExpl", box);
    b.disabled = true;
    out.innerHTML = `<div class="small muted"><span class="spin"></span> 풀어 쓰는 중…</div>`;
    const quote = e.quote_range ? e.text.slice(e.quote_range[0], e.quote_range[1]) : "";
    const { job } = await api("/api/explain", { json: { chunk: e.id, quote, project_id: S.cur?.id || null } });
    runJob(job, "쉽게 설명", {
      onDone: (r, j) => {
        b.disabled = false;
        if (j.status !== "done" || !r) { out.innerHTML = `<div class="small" style="color:var(--warn)">${esc((j.log || []).slice(-1)[0] || "실패했어요")}</div>`; return; }
        out.innerHTML = `<div class="explain">${r.translation ? `<div><b>번역</b> ${esc(r.translation)}</div>` : ""}
          <div><b>쉽게 말하면</b> ${esc(r.plain)}</div>
          ${(r.terms || []).length ? `<div class="small">${r.terms.map((t) => `<b>${esc(t.term)}</b>: ${esc(t.meaning)}`).join(" · ")}</div>` : ""}</div>`;
      },
    });
  };
  const draw = () => {
    $("#pgLbl", box).textContent = `${page + 1} / ${e.pages}쪽`;
    const rects = page === e.page ? e.hl.map((h) => {
      const [x0, y0, x1, y1] = h.r;
      return `<div class="hlr ${h.strong || !e.quote_range ? "strong" : ""}" style="left:${(x0 / e.pw) * 100}%;top:${(y0 / e.ph) * 100}%;width:${((x1 - x0) / e.pw) * 100}%;height:${((y1 - y0) / e.ph) * 100}%"></div>`;
    }).join("") : "";
    $("#pv", box).innerHTML = `<img src="/api/page/${e.paper_id}/${page}.png" alt="${page + 1}쪽">${rects}`;
    if (page === e.page) {
      const img = $("#pv img", box);
      img.onload = () => {
        const first = $("#pv .hlr.strong", box);
        if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
      };
    }
  };
  $$(".pagenav button[data-d]", box).forEach((b) => (b.onclick = () => { page = Math.max(0, Math.min(e.pages - 1, page + +b.dataset.d)); draw(); }));
  $("#backPg", box).onclick = () => { page = e.page; draw(); };
  draw();
}

// '직접 찾기': 사용자가 내 자료에서 근거를 골라 연결
async function showFinder(c) {
  const { author, year } = parseLabel(c.textContent);
  const sentence = sentenceOf(c);
  const body = openPanel(`<div class="ev-cite">근거 직접 찾기</div><div class="ev-meta">${esc(c.textContent)} — “${esc(sentence.slice(0, 80))}”</div>`);
  const found = author ? await api(`/api/lookup?author=${encodeURIComponent(author)}&year=${year}`) : [];
  body.innerHTML = `
    <div class="row"><input id="fq" class="grow" value="${esc(sentence)}"><button class="primary" id="fgo">검색</button></div>
    ${author ? (found.length
      ? `<label class="chk"><input type="checkbox" id="fonly" checked> ${esc(author)}(${esc(year)})의 논문에서만 찾기 — ${esc(found[0].title.slice(0, 50))}</label>`
      : `<p class="small" style="color:var(--warn)">내 자료에 ${esc(c.textContent)} 논문이 없습니다. PDF를 추가하거나 [자동]을 눌러 인터넷에서 찾게 하세요.</p>`) : ""}
    <div class="row" style="margin:6px 0 12px"><button id="fauto">🤖 자동으로 찾기</button>
      <button id="fup">＋ 내 컴퓨터 PDF 추가</button><input type="file" id="fupFile" accept=".pdf" multiple hidden></div>
    <div id="fres" class="muted">검색 중…</div><div id="fprev"></div>`;
  const search = async () => {
    const only = $("#fonly", body)?.checked;
    const q = encodeURIComponent($("#fq", body).value);
    const r = await api(`/api/search?q=${q}${only ? `&author=${encodeURIComponent(author)}&year=${year}` : ""}&project_id=${S.cur?.id || 0}`);
    $("#fres", body).innerHTML = r.results.length
      ? r.results.map((x, i) => `<div class="result" data-i="${i}"><span class="c">${esc(x.cite)}</span> <span class="t">${esc(x.title)} · p.${x.page + 1}</span><div class="s">${esc(x.text)}</div></div>`).join("")
      : `<p>결과가 없어요. 다른 말로 검색하거나 PDF를 추가해 보세요.</p>`;
    $$(".result", body).forEach((d) => (d.onclick = async () => {
      $$(".result", body).forEach((x) => x.classList.toggle("sel", x === d));
      const x = r.results[+d.dataset.i];
      const e = await api(`/api/evidence?chunk=${x.id}`);
      const prev = $("#fprev", body);
      renderEvidence(prev, e, `<div class="card" style="margin-top:12px;padding:10px 12px"><div class="row"><b class="grow">${esc(e.cite)} p.${e.page + 1}</b>
        <button class="primary" id="linkIt">✔ 이 근거로 연결</button></div>
        <div class="small muted">아래 인용 상자에서 정확한 부분을 드래그해 선택하면 그 줄만 형광펜으로 표시돼요.</div></div>`);
      $("#linkIt", prev).onclick = () => {
        const s = getSelection();
        const q = s.rangeCount && $("#evQuote", prev).contains(s.anchorNode) ? s.toString().trim() : "";
        const mine = parseLabel(c.textContent);
        const theirs = parseLabel(e.cite);
        let label = null;
        if (!mine.author) label = e.cite;
        else if (theirs.author && !theirs.author.includes(mine.author.split(/[ ,&]/)[0]) && confirm(`인용 표기를 ${e.cite}(으)로 바꿀까요?`)) label = e.cite;
        linkCite(c, { chunk_id: e.id, quote: q }, label);
        toast("근거를 연결했어요");
      };
      prev.scrollIntoView({ behavior: "smooth" });
    }));
  };
  $("#fgo", body).onclick = search;
  $("#fq", body).onkeydown = (ev) => ev.key === "Enter" && search();
  $("#fonly", body) && ($("#fonly", body).onchange = search);
  $("#fauto", body).onclick = () => autoFind(c);
  $("#fup", body).onclick = () => $("#fupFile", body).click();
  $("#fupFile", body).onchange = async (ev) => {
    const fd = new FormData();
    [...ev.target.files].forEach((f) => fd.append("files", f));
    const { job } = await api("/api/papers/upload", { method: "POST", body: fd });
    runJob(job, "PDF 읽는 중", { onDone: () => showFinder(c) });
  };
  search();
}

// ---------- Claude 연결 도우미 (각자 자기 키 넣기) ----------
const CONSOLE = "https://platform.claude.com";
const WIZ = [
  {
    t: "① Anthropic 계정 만들기",
    b: `<p>Claude를 만든 회사(Anthropic)의 개발자 콘솔에 가입해요. <b>구글 계정으로 가입</b>하면 가장 쉬워요.</p>
      <p class="small muted">이미 계정이 있으면 로그인만 하고 다음으로 넘어가세요. Claude 채팅 앱(Pro 구독)과는 별개라서, 구독 중이어도 여기 가입이 필요해요.</p>`,
    link: [CONSOLE, "콘솔 열기 ↗"],
  },
  {
    t: "② 크레딧 충전",
    b: `<p>Claude는 <b>쓴 만큼만</b> 요금이 나가는 선불 충전 방식이에요. 콘솔의 <b>Billing(결제)</b>에서 카드를 등록하고 충전하세요.</p>
      <ul class="small">
        <li>처음엔 <b>5~10달러(약 7천~1만 4천 원)</b>면 충분해요. 연구계획 한 번이 100~200원, 한 장 집필이 100원 안팎이에요(어림값).</li>
        <li><b>자동 충전은 꺼두세요.</b> 크레딧이 다 떨어지면 멈출 뿐 추가 요금은 나가지 않아요.</li>
        <li>더 안심하려면 <b>Limits(한도)</b>에서 월 사용 한도를 정해두세요.</li>
      </ul>`,
    link: [CONSOLE + "/settings/billing", "결제 페이지 열기 ↗"],
    link2: [CONSOLE + "/settings/limits", "월 한도 설정 ↗"],
  },
  {
    t: "③ 키 만들어서 붙여넣기",
    b: `<ol class="small">
        <li>아래 <b>'키 페이지 열기'</b>를 누르세요. (안 열리면 콘솔 왼쪽 메뉴의 <b>API Keys</b>)</li>
        <li><b>Create Key</b>를 누르고 이름을 <b>논문AI</b>로 적어 만드세요.</li>
        <li>나타난 키(<code>sk-ant-</code>로 시작)의 <b>Copy</b>를 누르세요. <span style="color:var(--warn)">키는 이때 한 번만 보여요.</span></li>
        <li>이 창으로 돌아와 <b>'📋 복사한 키 붙여넣기'</b>를 누르면 끝!</li>
      </ol>
      <div class="row" style="margin-top:8px"><button class="primary" id="wzPaste">📋 복사한 키 붙여넣기</button></div>
      <input id="wzKey" type="password" placeholder="또는 여기에 직접 붙여넣기 (Ctrl+V)" style="margin-top:8px">
      <div id="wzMsg" class="small" style="margin-top:8px"></div>`,
    link: [CONSOLE + "/settings/keys", "키 페이지 열기 ↗"],
  },
];

function claudeWizard(onDone) {
  let step = 0;
  const d = document.createElement("dialog");
  d.className = "wizard";
  document.body.appendChild(d);
  const draw = () => {
    const w = WIZ[step];
    d.innerHTML = `<div class="wz-steps">${WIZ.map((x, i) => `<span class="${i === step ? "on" : i < step ? "done" : ""}">${i < step ? "✓" : i + 1}</span>`).join("")}</div>
      <h3 style="margin:6px 0 10px">${w.t}</h3>${w.b}
      <div class="row" style="margin:10px 0"><a class="btnlink" href="${w.link[0]}" target="_blank">${w.link[1]}</a>${w.link2 ? `<a class="btnlink ghostlink" href="${w.link2[0]}" target="_blank">${w.link2[1]}</a>` : ""}</div>
      <div class="row" style="justify-content:space-between;margin-top:14px"><button data-x="close" class="ghost">나중에 하기</button>
        <span>${step ? `<button data-x="prev">← 이전</button>` : ""} ${step < WIZ.length - 1 ? `<button class="primary" data-x="next">다음 →</button>` : ""}</span></div>`;
    d.querySelectorAll("[data-x]").forEach((b) => (b.onclick = () => {
      const x = b.dataset.x;
      if (x === "close") { d.close(); d.remove(); return; }
      step += x === "next" ? 1 : -1;
      draw();
    }));
    if (step === WIZ.length - 1) {
      const msg = $("#wzMsg", d);
      const tryKey = async (raw) => {
        const key = (raw || "").trim().replace(/^["']|["']$/g, "");
        if (!/^sk-ant-[\w-]{20,}$/.test(key)) {
          msg.style.color = "var(--warn)";
          msg.textContent = key ? "Claude 키 모양이 아니에요. 'sk-ant-'로 시작하는 키 전체를 복사했는지 확인해 주세요." : "복사된 내용이 없어요. 콘솔에서 Copy를 먼저 눌러주세요.";
          return;
        }
        msg.style.color = "";
        msg.innerHTML = `<span class="spin"></span> 저장하고 연결을 확인하는 중…`;
        if (S.guest) await api("/api/me/key", { json: { key } });
        else await api("/api/settings", { json: { claude_key: key, engine: "claude" } });
        const r = await api("/api/claude/test", { method: "POST" });
        if (r.ok) {
          msg.innerHTML = `✅ <b>연결 완료!</b> 이제 연구계획·집필·원고 점검을 Claude가 해요.<div class="muted">${esc(r.msg)}</div>`;
          d.querySelector(".row:last-child span").innerHTML = `<button class="primary" id="wzFin">완료</button>`;
          $("#wzFin", d).onclick = () => { d.close(); d.remove(); refreshStatus(); onDone && onDone(); };
        } else {
          if (!S.guest) await api("/api/settings", { json: { engine: "local" } });
          msg.style.color = "var(--warn)";
          msg.textContent = /크레딧|credit|balance/i.test(r.msg)
            ? "키는 맞는데 크레딧이 없어요. ② 단계로 돌아가 충전한 뒤 다시 시도해 주세요."
            : "연결에 실패했어요: " + r.msg;
        }
      };
      $("#wzPaste", d).onclick = async () => {
        try { tryKey(await navigator.clipboard.readText()); }
        catch { msg.textContent = "클립보드를 읽지 못했어요. 아래 칸을 누르고 Ctrl+V로 붙여넣어 주세요."; $("#wzKey", d).focus(); }
      };
      $("#wzKey", d).oninput = (e) => { if (e.target.value.trim().length > 30) tryKey(e.target.value); };
    }
  };
  draw();
  d.showModal();
}

// ---------- 내 자료 ----------
async function renderLibrary() {
  S.cur = null;
  loadProjects();
  $("#main").innerHTML = `<div class="pad">
    <h2>📚 내 자료</h2>
    <div class="card"><div class="row">
      <button id="scanBtn">🔍 내 컴퓨터 PDF 찾아 읽기</button>
      <button id="upBtn">＋ PDF 파일 추가</button><input type="file" id="upFile" accept=".pdf" multiple hidden>
    </div>
    <div class="row" style="margin-top:10px"><input id="lq" class="grow" placeholder="내 자료 안에서 내용 검색 (예: 자기자비와 우울)"><button id="lgo">검색</button></div>
    <div id="lres"></div></div>
    <div class="card"><div id="plist" class="muted">불러오는 중…</div></div></div>`;
  bindAddButtons(renderLibrary);
  const doSearch = async () => {
    const r = await api(`/api/search?q=${encodeURIComponent($("#lq").value)}`);
    $("#lres").innerHTML = r.results.map((x) => `<div class="result" data-chunk="${x.id}"><span class="c">${esc(x.cite)}</span> <span class="t">${esc(x.title)} · p.${x.page + 1}</span><div class="s">${esc(x.text)}</div></div>`).join("") || "<p class='muted'>결과 없음</p>";
    $$("#lres .result").forEach((d) => (d.onclick = () => showEvidence({ chunk: d.dataset.chunk, quote: "" })));
  };
  $("#lgo").onclick = doSearch;
  $("#lq").onkeydown = (e) => e.key === "Enter" && doSearch();
  const papers = await api("/api/papers");
  $("#plist").innerHTML = `<h3>전체 ${papers.length}편</h3>` + paperTable(papers);
  bindPaperTable($("#plist"), renderLibrary);
}

// ---------- 설정 ----------
async function renderSettings() {
  if (S.guest) return renderMySettings();
  S.cur = null;
  loadProjects();
  await refreshStatus();
  const s = S.status.settings, models = S.status.models || [];
  const opt = (v, list) => [...new Set([v, ...list])].map((m) => `<option ${m === v ? "selected" : ""}>${esc(m)}</option>`).join("");
  $("#main").innerHTML = `<div class="pad" style="max-width:720px">
    <h2>⚙ 설정</h2>
    <div class="card"><h3>로컬 AI</h3>
      <label class="field"><span>글쓰기 모델 (클수록 똑똑하지만 느림)</span><select id="model">${opt(s.model, models.filter((m) => !m.includes("bge")))}</select></label>
      <label class="field"><span>의미 검색 방식</span><select id="embed_backend">
        <option value="builtin" ${s.embed_backend !== "ollama" ? "selected" : ""}>앱 내장 모델 — 빠름, 설치 필요 없음 (추천)</option>
        <option value="ollama" ${s.embed_backend === "ollama" ? "selected" : ""}>Ollama 검색 모델 — 조금 더 정확, 아주 느림</option></select></label>
      <label class="field"><span>Ollama 검색 모델 (위에서 Ollama를 고를 때만)</span><select id="embed_model">${opt(s.embed_model, models)}</select></label>
      <label class="chk"><input type="checkbox" id="strict_verify" ${s.strict_verify ? "checked" : ""}> 엄격 검증 — AI가 쓴 문장의 인용구가 원문에서 확인되지 않으면 '?'로 표시</label>
      ${models.length ? "" : `<p style="color:var(--warn)">Ollama가 꺼져 있어요. 시작 메뉴에서 Ollama를 실행해 주세요.</p>`}
    </div>
    <div class="card"><h3>AI 엔진</h3>
      <label class="chk"><input type="radio" name="engine" value="local" ${s.engine !== "claude" ? "checked" : ""}> 내 컴퓨터 로컬 AI — 무료, 느림, 원고가 밖으로 나가지 않음</label>
      <label class="chk"><input type="radio" name="engine" value="claude" ${s.engine === "claude" ? "checked" : ""}> Claude (인터넷) — 연구계획·집필·원고 점검이 빠르고 정확함, 사용량만큼 요금</label>
      <div style="margin:6px 0 2px 22px">
        <label class="chk"><input type="radio" name="claude_via" value="api" ${s.claude_via !== "subscription" ? "checked" : ""}> API 키로 — 쓴 만큼 요금</label>
        <label class="chk"><input type="radio" name="claude_via" value="subscription" ${s.claude_via === "subscription" ? "checked" : ""}> 내 Claude 구독으로 (이 컴퓨터의 Claude Code) — 추가 요금 없음, 한 번에 몇 초 더 걸림, 구독 사용량 한도 안에서</label>
        <p class="small muted" style="margin:2px 0 0">구독은 이 컴퓨터 주인만 써요. 웹으로 들어온 친구는 항상 자기 API 키를 써요.</p>
      </div>
      <label class="field" style="margin-top:6px"><span>Claude API 키 (Anthropic 콘솔에서 발급 · 이 컴퓨터에 암호화 저장)</span><input id="claude_key" type="password" value="${esc(s.claude_key)}" placeholder="sk-ant-..."></label>
      <div class="row"><button class="primary" id="claudeWiz">🧭 Claude 연결 도우미 (처음이라면)</button><button id="claudeTest">저장하고 연결 테스트</button><span id="claudeMsg" class="small"></span></div>
      <p class="small" id="claudeUsage" style="margin-bottom:0"></p>
      <label class="field" style="margin-top:8px"><span>이번 달 Claude 사용 한도 (원) — 넘으면 Claude 작업이 멈춰요. 0이면 한도 없음</span>
        <input id="monthly_cap_won" type="number" min="0" step="1000" value="${s.monthly_cap_won || 0}" style="max-width:160px"></label>
      <p class="small muted">Claude를 골라도 근거 검색·문장 위치 찾기는 항상 내 컴퓨터에서 해요. 새벽 예약 작업과 '로컬 AI만 사용'을 켠 원고는 항상 로컬 AI로 처리돼요.</p>
    </div>
    <div class="card" id="inviteCard"><h3>👥 친구 초대 (웹 주소로 함께 쓰기)</h3><div class="small muted">불러오는 중…</div></div>
    <div class="card"><h3>🌙 새벽 자동 실행</h3>
      <p class="small muted">낮에 '🌙 새벽 예약'을 눌러둔 일(자료 수집·집필·원고 검토)을 새벽 2시에 로컬 AI로 처리해요(무료).
      아래를 등록하면 매일 01:55에 컴퓨터를 절전에서 깨워 앱을 조용히 켜고, 일을 마치면 스스로 꺼져요. 컴퓨터를 완전히 끄면 실행되지 않아요.</p>
      <div class="row"><span id="nightState" class="small">확인 중…</span><span class="grow"></span>
        <button id="nightReg">새벽 자동 실행 등록</button><button class="ghost" id="nightUnreg">해제</button><button class="small" id="nightNow">예약 작업 지금 실행</button></div>
    </div>
    <div class="card"><h3>내 컴퓨터에서 PDF를 찾을 폴더</h3>
      <textarea id="scan_folders" rows="4">${esc(s.scan_folders.join("\n"))}</textarea>
      <div class="row" style="margin-top:6px"><button id="scanPick">📁 폴더 고르기</button></div>
      <label class="chk small" style="margin-top:6px"><input type="checkbox" id="scan_with_claude" ${s.scan_with_claude ? "checked" : ""}> PDF의 저자·연도 읽기를 Claude(가장 싼 Haiku)로 빠르게 (한 편에 1원 안팎 · 끄면 무료인 로컬 AI로 천천히)</label>
      <p class="small muted">한 줄에 폴더 하나. '내 컴퓨터 PDF 찾아 읽기'와 근거 자동찾기가 이 폴더들을 봅니다.</p>
      <label class="field"><span>한 번에 내려받을 최대 논문 수</span><input id="max_downloads" type="number" min="1" max="60" value="${s.max_downloads}"></label>
    </div>
    <div class="card"><h3>대학 도서관 연결 (선택)</h3>
      <p class="small muted">무료가 아닌 논문을 학교 도서관 권한으로 내려받을 때 씁니다. 실제 크롬/엣지 창이 열려 자동으로 로그인하고,
      2단계 인증이 있으면 창에서 직접 마무리하면 됩니다. 로그인 상태는 이 컴퓨터에만 저장되고, 비밀번호는 Windows 계정으로 암호화됩니다.</p>
      <label class="field"><span>도서관 로그인 페이지 주소</span><input id="lib_login_url" value="${esc(s.lib_login_url)}" placeholder="예) https://lib.○○.ac.kr/login"></label>
      <label class="field"><span>학외접속(프록시) 주소 앞부분 — 도서관 홈페이지 '학외접속' 안내 참고</span><input id="lib_proxy_prefix" value="${esc(s.lib_proxy_prefix)}" placeholder="예) https://libproxy.○○.ac.kr/_Lib_Proxy_Url/  또는  https://ezproxy.○○.ac.kr/login?url="></label>
      <div class="row"><label class="field grow"><span>도서관 아이디</span><input id="lib_id" value="${esc(s.lib_id)}"></label>
      <label class="field grow"><span>비밀번호</span><input id="lib_pw" type="password" value="${esc(s.lib_pw)}"></label></div>
      <button id="libLogin">저장하고 도서관 로그인 해보기</button>
    </div>
    <button class="primary" id="saveSet">저장</button>
  </div>`;
  const collect = () => ({
    model: $("#model").value, embed_model: $("#embed_model").value, embed_backend: $("#embed_backend").value, strict_verify: $("#strict_verify").checked,
    scan_folders: $("#scan_folders").value.split("\n").map((x) => x.trim()).filter(Boolean),
    max_downloads: +$("#max_downloads").value || 15,
    scan_with_claude: $("#scan_with_claude").checked,
    monthly_cap_won: Math.max(0, +$("#monthly_cap_won").value || 0),
    lib_login_url: $("#lib_login_url").value.trim(), lib_proxy_prefix: $("#lib_proxy_prefix").value.trim(),
    lib_id: $("#lib_id").value.trim(), lib_pw: $("#lib_pw").value,
    engine: $('input[name="engine"]:checked').value, claude_key: $("#claude_key").value,
    claude_via: ($('input[name="claude_via"]:checked') || {}).value || "api",
  });
  $("#claudeWiz").onclick = () => claudeWizard(renderSettings);
  $("#scanPick").onclick = () => folderPicker({
    title: "📁 내 자료 폴더", hint: "PDF를 찾아 읽을 폴더를 고르세요. 하위 폴더까지 함께 읽어요.",
    selected: $("#scan_folders").value.split("\n").map((x) => x.trim()).filter(Boolean),
    onSave: (sel) => { $("#scan_folders").value = sel.join("\n"); toast("아래 '저장'을 눌러 반영해 주세요"); },
  });
  $$('input[name="engine"]').forEach((r) => (r.onchange = () => {
    if (r.value === "claude" && r.checked && !$("#claude_key").value && $('input[name="claude_via"]:checked')?.value !== "subscription") claudeWizard(renderSettings);
  }));
  api("/api/claude/usage").then((u) => {
    $("#claudeUsage").innerHTML = u.calls
      ? `이번 달 Claude 사용: <b>약 ${u.won.toLocaleString()}원</b> (US$${u.usd}, ${u.calls}회) <span class="muted">— 이 앱 기준 어림값, 정확한 금액은 <a href="${CONSOLE}/usage" target="_blank">콘솔 사용량</a>에서</span>`
      : `<span class="muted">이번 달 이 앱에서 Claude를 쓴 기록이 아직 없어요.</span>`;
  });
  $("#claudeTest").onclick = async () => {
    const c = collect();
    if (!c.claude_key) return toast("API 키를 넣어주세요");
    await api("/api/settings", { json: c });
    $("#claudeMsg").textContent = "확인 중…";
    const r = await api("/api/claude/test", { method: "POST" });
    if (r.ok && c.engine !== "claude") {  // 연결에 성공했으면 Claude를 쓰도록 바로 전환
      $('input[name="engine"][value="claude"]').checked = true;
      await api("/api/settings", { json: { engine: "claude" } });
      r.msg += " — AI 엔진을 Claude로 바꿨어요";
    }
    $("#claudeMsg").textContent = (r.ok ? "✅ " : "⚠ ") + r.msg;
    $("#claudeMsg").style.color = r.ok ? "" : "var(--warn)";
    refreshStatus();
  };
  const drawInvite = async () => {
    const r = await api("/api/users");
    $("#inviteCard").innerHTML = `<h3>👥 친구 초대 (웹 주소로 함께 쓰기)</h3>
      <p class="small">친구가 웹 주소로 들어와 가입할 때 넣을 <b>초대 코드: <code style="font-size:16px">${esc(r.code)}</code></b> <button class="small ghost" id="newCode">코드 바꾸기</button></p>
      <p class="small muted">친구는 자기 Claude 키로 쓰고, 자기 원고·PDF만 봐요. 이 컴퓨터의 폴더·설정·도서관 계정은 친구에게 안 보여요.</p>
      ${r.users.length ? `<table class="tbl small"><tr><th>이름</th><th>아이디</th><th>가입</th><th>원고</th><th>PDF</th><th></th></tr>${r.users.map((u) => `<tr><td>${esc(u.name)}</td><td>${esc(u.login)}</td><td>${esc((u.created || "").slice(0, 10))}</td><td>${u.projects}</td><td>${u.papers}</td><td><button class="small ghost" data-deluser="${u.id}">내보내기</button></td></tr>`).join("")}</table>` : `<p class="small muted">아직 가입한 친구가 없어요.</p>`}`;
    $("#newCode").onclick = async () => { if (!confirm("코드를 바꾸면 예전 코드로는 가입할 수 없어요. 바꿀까요?")) return; await api("/api/users/code", { method: "POST" }); drawInvite(); };
    $$("[data-deluser]").forEach((b) => (b.onclick = async () => { if (!confirm("이 친구의 계정과 원고·PDF를 모두 지울까요? 되돌릴 수 없어요.")) return; await api(`/api/users/${b.dataset.deluser}`, { method: "DELETE" }); drawInvite(); }));
  };
  drawInvite();
  const nightState = async () => {
    const r = await api("/api/schedule");
    const waiting = r.rows.filter((x) => x.status === "waiting").length;
    $("#nightState").textContent = `${r.task === "registered" ? "✔ 등록됨" : "등록 안 됨"} · 대기 중인 예약 ${waiting}개`;
  };
  nightState();
  $("#nightReg").onclick = async () => { const r = await api("/api/night/register", { method: "POST" }); toast(r.msg, 7000); nightState(); };
  $("#nightUnreg").onclick = async () => { const r = await api("/api/night/unregister", { method: "POST" }); toast(r.msg); nightState(); };
  $("#nightNow").onclick = async () => {
    if (!confirm("대기 중인 새벽 예약을 지금 바로 로컬 AI로 실행할까요? (컴퓨터가 느려질 수 있어요)")) return;
    await api("/api/night/run_now", { method: "POST" });
    toast("예약 작업을 시작했어요. 각 프로젝트의 ⑤ 원고 점검 탭에서 상태를 볼 수 있어요.", 5000);
  };
  $("#saveSet").onclick = async () => {
    const c = collect();
    if (c.engine === "claude" && !c.claude_key && c.claude_via !== "subscription") return toast("Claude를 쓰려면 API 키를 넣거나 '내 Claude 구독으로'를 골라주세요");
    await api("/api/settings", { json: c });
    toast("저장했어요");
    refreshStatus();
  };
  $("#libLogin").onclick = async () => {
    await api("/api/settings", { json: collect() });
    const { job } = await api("/api/library/login", { method: "POST" });
    runJob(job, "도서관 로그인", { onDone: (r, j) => toast(j.log[j.log.length - 1] || "끝", 5000) });
  };
}

// ---------- 시작 ----------
$("#newProject").onclick = showWelcome;
$$(".top nav button").forEach((b) => (b.onclick = () => { saveNow(); (b.dataset.view === "library" ? renderLibrary : renderSettings)(); }));
window.addEventListener("resize", () => layoutMemos());
// 버튼을 눌렀는데 조용히 실패하는 일이 없도록 모든 오류를 화면에 알림
window.addEventListener("unhandledrejection", (e) => {
  toast("⚠ " + (e.reason?.message || "문제가 생겼어요"), 7000);
  $$("button:disabled").forEach((b) => (b.disabled = false));
});
window.addEventListener("beforeunload", () => saveNow());
refreshStatus();
setInterval(refreshStatus, 8000);

// 새로고침해도 진행 중인 작업 상자를 다시 띄움 (오류로 끝난 작업도 30분간)
const JOB_TITLES = {
  scan: "참조 폴더 PDF 읽는 중", index: "PDF 읽는 중", plan: "연구 계획 세우기", route: "연구 경로 다시 짜기",
  collect: "자료 모으기", write: "집필", autofind: "출처 자동 찾기", login: "도서관 로그인", matrix: "문헌분석표 만들기",
  gaps: "연구 공백 찾기", intro: "서론 초안 쓰기", map: "연구 지도", meter: "근거 저울", journals: "투고 학술지 찾기",
  ask: "내 자료에 묻기", explain: "쉽게 설명", model: "연구모형 그림", fetch: "논문 내려받기",
};
const REVIEW_TITLES = { structure: "뼈대 점검", cites: "인용 점검", uncited: "근거 없는 문장", discuss: "논의 보강", recent: "최신 연구",
  refs: "참고문헌 대조", panel: "3인 모의 심사", panel_respond: "답변서 재심사", style: "문장 다듬기", aitone: "AI 문체 점검", full: "전체 검토" };
async function restoreJobs() {
  let list = [];
  try { list = await api("/api/jobs"); } catch { return; }
  const names = Object.fromEntries((S.projects || []).map((p) => [p.id, p.title]));
  for (const j of list) {
    if (document.querySelector(`.job[data-id="${j.id}"]`)) continue;
    let t = j.kind === "review" ? REVIEW_TITLES[j.feature] || "원고 점검" : JOB_TITLES[j.kind] || "작업";
    if (j.pid && names[j.pid]) t += ` — ${names[j.pid].slice(0, 14)}`;
    runJob(j.id, t, { onDone: () => S.cur && j.pid === S.cur.id && openProject(S.cur.id) });
  }
}
boot();
async function boot() {
  let st;
  try { st = await api("/api/status"); } catch { return; }  // 401이면 로그인 화면이 떠 있음
  S.guest = !!st.guest;
  document.body.classList.toggle("guest", S.guest);
  if (S.guest) {
    $$(".top nav button").forEach((b) => { if (b.dataset.view === "settings") b.textContent = `👤 내 설정`; });
    $(".brand .sub").textContent = "모든 문장에 근거";
  }
  await loadProjects();
  restoreJobs();
  if (S.guest && !st.me?.has_key) claudeWizard(() => refreshStatus());
  // 처음 설치한 사람: Claude Code가 있으면 구독으로, 없으면 API 키 안내
  const ready = st.settings.engine === "claude" && (st.settings.claude_key || st.settings.claude_via === "subscription");
  if (!S.guest && st.cli && !st.settings.claude_key && !ready) subWizard();
  else if (!S.guest && !st.models && !ready) claudeWizard(() => refreshStatus());
  return S.projects.length ? openProject(S.projects[0].id) : showWelcome();
}

// ---------- 내 Claude 구독으로 쓰기 (Claude Code) ----------
function subWizard() {
  const d = document.createElement("dialog");
  d.className = "wizard";
  document.body.appendChild(d);
  d.innerHTML = `<h3 style="margin:0 0 8px">Claude 구독으로 연결하기</h3>
    <p>이 컴퓨터에 <b>Claude Code</b>가 설치되어 있어요. 쓰고 계신 <b>Claude 구독(Pro·Max)</b>으로 앱을 돌리면 <b>따로 요금이 나가지 않아요.</b> (구독 사용량 안에서)</p>
    <ol class="small"><li>아래 <b>로그인 창 열기</b>를 누르면 까만 창이 떠요.</li><li>창에서 안내대로 로그인 방식을 고르면 브라우저가 열려요. Claude 계정으로 로그인하고 '허용'을 눌러요.</li><li>창에 입력 칸이 보이면 로그인 끝이에요. 그 창은 닫고 아래 <b>연결 확인</b>을 눌러요.</li></ol>
    <p class="small muted" id="swMsg"></p>
    <div class="row" style="justify-content:space-between;margin-top:10px"><button class="ghost" id="swApi">API 키로 쓸래요</button>
      <span><button id="swLogin">로그인 창 열기</button> <button class="primary" id="swTest">연결 확인</button></span></div>`;
  const msg = $("#swMsg", d);
  $("#swApi", d).onclick = () => { d.close(); d.remove(); claudeWizard(() => refreshStatus()); };
  $("#swLogin", d).onclick = async () => { await api("/api/claude/login", { method: "POST" }); msg.textContent = "로그인 창을 열었어요. 로그인을 마친 뒤 '연결 확인'을 눌러 주세요."; };
  $("#swTest", d).onclick = async () => {
    msg.innerHTML = `<span class="spin"></span> 확인하는 중… (10~20초)`;
    await api("/api/settings", { json: { engine: "claude", claude_via: "subscription" } });
    const r = await api("/api/claude/test", { method: "POST" });
    if (r.ok) {
      msg.innerHTML = `✅ <b>연결 완료!</b> 이제 Claude 구독으로 돌아가요.`;
      $("#swTest", d).textContent = "완료";
      $("#swTest", d).onclick = () => { d.close(); d.remove(); refreshStatus(); };
    } else {
      msg.style.color = "var(--warn)";
      msg.textContent = /로그인/.test(r.msg) ? "아직 로그인이 안 됐어요. '로그인 창 열기'로 로그인을 마친 뒤 다시 눌러 주세요." : "연결에 실패했어요: " + r.msg;
    }
  };
  d.showModal();
}

// ---------- 로그인 (웹 주소로 들어온 사람) ----------
function showLogin() {
  if ($("#loginBox")) return;
  const d = document.createElement("div");
  d.id = "loginBox";
  d.className = "login-wrap";
  let mode = "login";
  const draw = () => {
    d.innerHTML = `<div class="login card">
      <h2 style="margin:0 0 4px">📝 논문 AI</h2>
      <p class="small muted" style="margin:0 0 14px">모든 문장에 근거를 붙여 주는 논문 도우미</p>
      <div class="row" style="margin-bottom:10px"><button class="${mode === "login" ? "primary" : "ghost"}" data-m="login">로그인</button><button class="${mode === "signup" ? "primary" : "ghost"}" data-m="signup">처음이에요 (가입)</button></div>
      <label class="field"><span>아이디</span><input id="lgId" autocomplete="username"></label>
      ${mode === "signup" ? `<label class="field"><span>이름 (화면에 보일 이름)</span><input id="lgName"></label>` : ""}
      <label class="field"><span>비밀번호${mode === "signup" ? " (6자 이상)" : ""}</span><input id="lgPw" type="password" autocomplete="${mode === "signup" ? "new-password" : "current-password"}"></label>
      ${mode === "signup" ? `<label class="field"><span>초대 코드 (앱 주인에게 받은 6자리)</span><input id="lgCode" style="text-transform:uppercase"></label>` : ""}
      <p id="lgMsg" class="small" style="color:var(--warn);min-height:1.2em"></p>
      <button class="primary" id="lgGo" style="width:100%">${mode === "login" ? "로그인" : "가입하고 시작하기"}</button>
      <p class="small muted" style="margin-top:12px">각자 자기 Claude API 키로 사용해요. 내 원고와 PDF는 나만 볼 수 있어요.</p>
      <p class="small server-note" hidden style="margin-top:8px;color:var(--warn)">⚠ 무료 체험판이에요. 서버가 쉬었다가 다시 켜지면 가입 정보와 올린 자료가 지워질 수 있어요. 원고는 꼭 내 컴퓨터에도 보관해 주세요.</p></div>`;
    api("/api/auth/info").then((i) => { if (i.server) $(".server-note", d).hidden = false; }).catch(() => {});
    $$("[data-m]", d).forEach((b) => (b.onclick = () => { mode = b.dataset.m; draw(); }));
    const go = async () => {
      const body = { login: $("#lgId", d).value, password: $("#lgPw", d).value };
      if (mode === "signup") Object.assign(body, { name: $("#lgName", d).value, code: $("#lgCode", d).value });
      try {
        await api(`/api/auth/${mode}`, { json: body });
        location.reload();
      } catch (e) { $("#lgMsg", d).textContent = e.message; }
    };
    $("#lgGo", d).onclick = go;
    $$("input", d).forEach((i) => (i.onkeydown = (e) => { if (e.key === "Enter") go(); }));
  };
  draw();
  document.body.appendChild(d);
}

async function renderMySettings() {
  S.cur = null;
  loadProjects();
  const me = await api("/api/me");
  $("#main").innerHTML = `<div class="pad" style="max-width:640px">
    <h2>👤 내 설정</h2>
    <div class="card"><h3>${esc(me.name)} <span class="small muted">(${esc(me.login)})</span></h3>
      <p>Claude API 키: ${me.has_key ? "✅ 넣어 둠" : "❌ 아직 없음"}</p>
      <p class="small muted" id="myUsage"></p>
      <div class="row"><button class="primary" id="myKey">${me.has_key ? "키 바꾸기" : "Claude 키 넣기 (안내 따라 하기)"}</button>
        ${me.has_key ? `<button class="ghost" id="myKeyDel">키 지우기</button>` : ""}<span class="grow"></span><button id="myOut">로그아웃</button></div>
      <p class="small muted">내 키는 이 앱에서 내 작업에만 쓰이고, 요금도 내 Anthropic 계정에 나와요. 실행 전에 예상 금액이 표시돼요.</p></div>
    <div class="card"><h3>안내</h3><p class="small">웹으로 쓸 때는 PDF를 <b>'＋ PDF 파일 추가'</b>로 올려 주세요. 올린 PDF와 원고는 나만 볼 수 있어요. 학교 도서관 자동 다운로드와 새벽 예약은 앱 주인 컴퓨터에서만 돼요.</p></div></div>`;
  api("/api/claude/usage").then((u) => { $("#myUsage").textContent = `이번 달 이 앱에서 쓴 Claude 요금: 약 ${(u.won || 0).toLocaleString()}원`; }).catch(() => {});
  $("#myKey").onclick = () => claudeWizard(renderMySettings);
  if ($("#myKeyDel")) $("#myKeyDel").onclick = async () => { if (!confirm("저장된 키를 지울까요?")) return; await api("/api/me/key", { json: { key: "" } }); renderMySettings(); refreshStatus(); };
  $("#myOut").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); location.reload(); };
}
