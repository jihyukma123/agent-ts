// ITSM Assistant 프론트엔드. 빌드 도구 없이 FastAPI 정적 파일로 제공됩니다.
// 현재 사용자는 서버 측 세션(쿠키)으로만 결정되며, 이 스크립트는 사용자 ID·역할을 채팅 API에 보내지 않습니다.

const $ = (id) => document.getElementById(id);

const state = {
  user: null,
  agentConfigured: false,
  sending: false,
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data && data.detail;
    const err = new Error(detail && detail.message ? detail.message : `요청 실패 (HTTP ${res.status})`);
    err.status = res.status;
    err.code = detail && detail.code;
    throw err;
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// ---------- 사용자 전환 ----------

async function loadUsers() {
  const users = await api("/api/users");
  const select = $("user-select");
  for (const u of users) {
    const team = u.team ? ` · ${u.team}` : "";
    select.append(el("option", { value: String(u.id) }, `${u.display_name} (${u.username}) · ${u.role_label}${team}`));
  }
}

function renderCurrentUser() {
  const box = $("current-user");
  box.replaceChildren();
  if (!state.user) {
    box.append("세션 없음 — 사용자를 선택하세요");
    $("user-select").value = "";
    return;
  }
  box.append("현재 사용자: ", el("strong", {}, state.user.display_name), " ", el("span", { class: "badge" }, state.user.role_label));
  $("user-select").value = String(state.user.id);
}

async function loadSession() {
  try {
    state.user = (await api("/api/session")).user;
  } catch (err) {
    if (err.status !== 401) throw err;
    state.user = null;
  }
  renderCurrentUser();
}

async function switchUser(userId) {
  const data = await api("/api/session", { method: "POST", body: JSON.stringify({ user_id: Number(userId) }) });
  state.user = data.user;
  renderCurrentUser();
  renderChatLog([]); // 사용자 전환 시 서버에서 대화가 초기화됩니다.
  renderAgentStatus();
}

// ---------- 채팅 ----------

function appendMessage(role, text) {
  const log = $("chat-log");
  log.append(el("div", { class: `msg ${role}` }, text));
  log.scrollTop = log.scrollHeight;
}

function renderChatLog(messages) {
  $("chat-log").replaceChildren();
  for (const m of messages) appendMessage(m.role, m.text);
}

function renderAgentStatus() {
  const status = $("agent-status");
  const enabled = Boolean(state.user) && state.agentConfigured;
  $("chat-input").disabled = !enabled;
  $("chat-send").disabled = !enabled || state.sending;
  if (!state.agentConfigured) {
    status.textContent = "Agent 설정 누락: .env.local 에 OPENAI_API_KEY 를 설정하고 서버를 재시작하세요.";
  } else if (!state.user) {
    status.textContent = "사용자를 선택하면 대화할 수 있습니다.";
  } else {
    status.textContent = "";
  }
}

async function loadChatHistory() {
  if (!state.user) {
    renderChatLog([]);
    return;
  }
  try {
    renderChatLog((await api("/api/agent/messages")).messages);
  } catch {
    renderChatLog([]);
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const input = $("chat-input");
  const message = input.value.trim();
  if (!message || state.sending) return;
  state.sending = true;
  renderAgentStatus();
  appendMessage("user", message);
  input.value = "";
  const pending = el("div", { class: "msg assistant muted" }, "응답을 기다리는 중…");
  $("chat-log").append(pending);
  try {
    const data = await api("/api/agent/chat", { method: "POST", body: JSON.stringify({ message }) });
    pending.remove();
    appendMessage("assistant", data.reply);
  } catch (err) {
    pending.remove();
    appendMessage("error", err.message);
  } finally {
    state.sending = false;
    renderAgentStatus();
    input.focus();
  }
}

async function resetChat() {
  if (!state.user) return;
  await api("/api/agent/messages", { method: "DELETE" });
  renderChatLog([]);
}

// ---------- 초기화 ----------

async function init() {
  const health = await api("/api/health");
  state.agentConfigured = health.agent.configured;
  $("itsm-status").textContent = health.itsm.reachable ? "" : `ITSM API 연결 안 됨 (${health.itsm.base_url})`;
  await loadUsers();
  await loadSession();
  renderAgentStatus();
  await loadChatHistory();

  $("user-select").addEventListener("change", (e) => {
    if (e.target.value) switchUser(e.target.value).catch((err) => appendMessage("error", err.message));
  });
  $("chat-form").addEventListener("submit", sendMessage);
  $("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("chat-form").requestSubmit();
    }
  });
  $("chat-reset").addEventListener("click", () => resetChat().catch((err) => appendMessage("error", err.message)));
}

init().catch((err) => {
  document.body.prepend(el("p", { class: "notice" }, `초기화 실패: ${err.message}`));
});
