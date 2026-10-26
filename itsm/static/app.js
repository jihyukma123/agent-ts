// ITSM 웹 프론트엔드. 빌드 도구 없이 FastAPI 정적 파일로 제공됩니다.
// 현재 사용자는 서버 측 세션(쿠키)으로만 결정되며, 이 스크립트는 사용자 ID·역할을 다른 API에 보내지 않습니다.
// 티켓 데이터는 ITSM 웹 백엔드가 ITSM API(v2)에서 받아 그대로 전달한 형식입니다.

const $ = (id) => document.getElementById(id);

const OPEN_STATUSES = ["received", "assigned", "investigating", "pending", "monitoring"];

const state = {
  user: null,
  selectedTicketId: null,
  agentCreatedTicketId: null,
  nextCursor: null,
  catalog: null,
  assistant: {
    variant: "baseline",
    connecting: false,
    reachable: false,
    configured: false,
    ready: false,
    sending: false,
  },
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
    let message = detail && detail.message ? detail.message : `요청 실패 (HTTP ${res.status})`;
    if (detail && Array.isArray(detail.errors)) message += "\n" + formatFieldErrors(detail.errors);
    const err = new Error(message);
    err.status = res.status;
    err.code = detail && detail.code;
    throw err;
  }
  return data;
}

const FIELD_LABELS = {
  title: "제목",
  description: "상세 설명",
  service_id: "대상 서비스",
  component_id: "컴포넌트",
  model: "관련 모델",
  impact_level: "영향도",
  urgency: "긴급도",
  impact_note: "영향 범위",
  occurred_at: "발생 시각",
};

// ITSM problem+json 의 errors 배열을 사람이 읽을 수 있는 문장으로 바꿉니다.
function formatFieldErrors(errors) {
  return errors.map((e) => `${FIELD_LABELS[e.field] || e.field}: ${e.message}`).join("\n");
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

function formatTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : `${d.toLocaleString("ko-KR", { hour12: false })}`;
}

function statusBadge(ticket) {
  return el("span", { class: `badge status-${ticket.status}` }, ticket.status_label);
}

function priorityBadge(ticket) {
  return el("span", { class: `badge priority-${ticket.priority}` }, `${ticket.priority} ${ticket.priority_label}`);
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
  box.append(
    "현재 사용자: ",
    el("strong", {}, state.user.display_name),
    " ",
    el("span", { class: `badge role-${state.user.role}` }, state.user.role_label),
  );
  $("user-select").value = String(state.user.id);
}

function canCreateTickets() {
  return Boolean(state.user) && (state.user.permissions || []).includes("ticket:create");
}

async function loadSession() {
  try {
    const data = await api("/api/session");
    state.user = data.user;
  } catch (err) {
    if (err.status !== 401) throw err;
    state.user = null;
  }
  renderCurrentUser();
}

async function switchUser(userId) {
  state.assistant.connecting = true;
  renderAssistantStatus();
  try {
    const data = await api("/api/session", { method: "POST", body: JSON.stringify({ user_id: Number(userId) }) });
    state.user = data.user;
    state.selectedTicketId = null;
    state.catalog = null;
    state.assistant.ready = false;
    dismissAgentTicketNotice();
    renderCurrentUser();
    closeTicketForm();
    renderChatLog([]);
    await Promise.all([refreshAll(), connectAssistant()]);
  } finally {
    state.assistant.connecting = false;
    renderAssistantStatus();
  }
}

// ---------- 티켓 목록 ----------

function ticketQuery(cursor) {
  const params = new URLSearchParams();
  const q = $("search-input").value.trim();
  const status = $("status-filter").value;
  if (q) params.set("q", q);
  if (status === "open") OPEN_STATUSES.forEach((s) => params.append("status", s));
  else if (status) params.append("status", status);
  if (cursor) params.set("cursor", cursor);
  return params.toString();
}

function renderTicketItem(t) {
  const target = [t.service.name, t.component && t.component.name, t.model].filter(Boolean).join(" / ");
  const owner = t.assignee ? t.assignee.display_name : t.assignment_group ? t.assignment_group.name : "미배정";
  const agentCreated = t.id === state.agentCreatedTicketId;
  const flags = [t.is_major ? "주요 장애" : null, t.classification === "restricted" ? "보안" : null, t.sla_breached ? "SLA 초과" : null]
    .filter(Boolean)
    .map((f) => el("span", { class: "flag" }, f));
  const li = el(
    "li",
    { "data-id": t.id },
    el(
      "div",
      { class: "row" },
      el("span", {}, t.id),
      el(
        "span",
        { class: "badges" },
        agentCreated ? el("span", { class: "badge agent-created-badge" }, "Agent 등록") : null,
        priorityBadge(t),
        statusBadge(t),
      ),
    ),
    el("div", { class: "title" }, t.title),
    el("div", { class: "meta" }, `${target} · ${owner}`, ...flags),
  );
  if (t.id === state.selectedTicketId) li.classList.add("selected");
  if (agentCreated) li.classList.add("agent-created");
  li.addEventListener("click", () => selectTicket(t.id));
  return li;
}

function dismissAgentTicketNotice() {
  state.agentCreatedTicketId = null;
  $("agent-ticket-notice").hidden = true;
  $("agent-ticket-notice-id").textContent = "";
  for (const item of $("ticket-list").children) {
    item.classList.remove("agent-created");
    const badge = item.querySelector(".agent-created-badge");
    if (badge) badge.remove();
  }
}

function showAgentTicketNotice(ticketId) {
  state.agentCreatedTicketId = ticketId;
  $("agent-ticket-notice-id").textContent = `${ticketId} · 현재 대화의 도구 실행으로 생성됨`;
  $("agent-ticket-notice").hidden = false;
}

async function viewAgentCreatedTicket() {
  const ticketId = state.agentCreatedTicketId;
  if (!ticketId) return;
  $("search-input").value = "";
  $("status-filter").value = "";
  state.selectedTicketId = ticketId;
  closeTicketForm();
  await Promise.all([loadTickets(), loadDetail()]);
}

async function loadTickets({ append = false } = {}) {
  const list = $("ticket-list");
  const count = $("ticket-count");
  if (!append) list.replaceChildren();
  $("more-tickets").hidden = true;
  if (!state.user) {
    count.textContent = "사용자를 선택하면 티켓을 볼 수 있습니다.";
    return;
  }
  try {
    const data = await api(`/api/tickets?${ticketQuery(append ? state.nextCursor : null)}`);
    for (const t of data.items) list.append(renderTicketItem(t));
    state.nextCursor = data.page.next_cursor;
    $("more-tickets").hidden = !data.page.has_more;
    count.textContent = `${list.children.length}건 표시${data.page.has_more ? " (더 있음)" : ""}`;
  } catch (err) {
    count.textContent = err.message;
  }
}

async function selectTicket(id) {
  state.selectedTicketId = id;
  closeTicketForm();
  for (const li of $("ticket-list").children) li.classList.toggle("selected", li.dataset.id === id);
  await loadDetail();
}

// ---------- 티켓 상세 ----------

function slaText(sla) {
  const parts = [sla.policy];
  if (sla.paused) parts.push("보류로 일시정지");
  else if (sla.resolution_due_at) parts.push(`해결 기한 ${formatTime(sla.resolution_due_at)}`);
  if (sla.response_breached) parts.push("응답 SLA 초과");
  if (sla.resolution_breached) parts.push("해결 SLA 초과");
  return parts.join(" · ");
}

function renderWorklogs(items) {
  if (!items.length) return el("p", { class: "muted" }, "작업 기록이 없습니다.");
  const ul = el("ul", { class: "worklogs" });
  for (const w of items) {
    ul.append(
      el(
        "li",
        { class: `worklog ${w.visibility}` },
        el(
          "div",
          { class: "meta" },
          `${w.author.display_name} · ${formatTime(w.created_at)}`,
          w.visibility === "internal" ? el("span", { class: "flag" }, "내부 메모") : null,
        ),
        el("div", { class: "body" }, w.body),
      ),
    );
  }
  return ul;
}

async function loadDetail() {
  const box = $("ticket-detail");
  box.classList.add("muted");
  if (!state.user || !state.selectedTicketId) {
    box.replaceChildren("목록에서 티켓을 선택하세요.");
    return;
  }
  const id = encodeURIComponent(state.selectedTicketId);
  try {
    const [t, worklogs] = await Promise.all([api(`/api/tickets/${id}`), api(`/api/tickets/${id}/worklogs`)]);
    box.classList.remove("muted");
    const rows = [
      ["티켓 ID", t.id],
      ["제목", t.title],
      ["상태", t.pending_reason_label ? el("span", {}, statusBadge(t), ` ${t.pending_reason_label}`) : statusBadge(t)],
      ["우선순위", el("span", {}, priorityBadge(t), ` (영향도 ${t.impact_label} · 긴급도 ${t.urgency_label})`)],
      ["SLA", slaText(t.sla)],
      ["대상 서비스", t.service.name],
      ["컴포넌트", t.component ? t.component.name : "-"],
      ["관련 모델", t.model || "해당 없음"],
      ["담당", [t.assignment_group && t.assignment_group.name, t.assignee && t.assignee.display_name].filter(Boolean).join(" / ") || "미배정"],
      ["상세 설명", t.description],
      ["영향 범위", t.impact_note],
      ["요청자", `${t.requester.display_name} (${t.requester.username})`],
      ["발생 시각", formatTime(t.occurred_at)],
      ["생성 시각", formatTime(t.created_at)],
      ["최근 갱신", formatTime(t.updated_at)],
    ];
    if (t.problem) rows.push(["관련 문제", `${t.problem.id} ${t.problem.title}`]);
    if (t.merged_into) rows.push(["병합됨", t.merged_into]);
    if (t.duplicates.length) rows.push(["중복 티켓", t.duplicates.join(", ")]);
    if (t.resolution) {
      rows.push(["해결", `${t.resolution.code_label} · ${formatTime(t.resolution.resolved_at)}`], ["조치 내용", t.resolution.note]);
    }
    const dl = el("dl");
    for (const [k, v] of rows) dl.append(el("dt", {}, k), el("dd", {}, v));
    box.replaceChildren(dl, el("h3", {}, `작업 기록 (${t.worklog_count})`), renderWorklogs(worklogs.items));
  } catch (err) {
    box.replaceChildren(err.message);
  }
}

// ---------- 티켓 등록 ----------
// 버튼은 ticket:create 권한이 있을 때만 보이지만, 실제 권한 검사는 ITSM API 가 합니다.

function renderNewTicketButton() {
  $("new-ticket").hidden = !canCreateTickets();
}

function toLocalInputValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function option(value, label) {
  return el("option", { value }, label);
}

async function loadCatalog() {
  if (state.catalog) return state.catalog;
  const [services, models] = await Promise.all([api("/api/catalog/services"), api("/api/catalog/models")]);
  state.catalog = { services, models };
  return state.catalog;
}

function fillComponents() {
  const form = $("ticket-form");
  const service = state.catalog.services.find((s) => s.id === form.elements.service_id.value);
  form.elements.component_id.replaceChildren(
    option("", "선택 안 함"),
    ...(service ? service.components.map((c) => option(String(c.id), c.name)) : []),
  );
}

function showFormError(message) {
  const box = $("ticket-form-error");
  box.textContent = message || "";
  box.hidden = !message;
}

async function openTicketForm() {
  const form = $("ticket-form");
  form.reset();
  showFormError("");
  try {
    const catalog = await loadCatalog();
    form.elements.service_id.replaceChildren(option("", "서비스 선택…"), ...catalog.services.map((s) => option(s.id, s.name)));
    form.elements.model.replaceChildren(option("", "해당 없음"), ...catalog.models.map((m) => option(m.id, m.name)));
    fillComponents();
  } catch (err) {
    showFormError(`카탈로그를 불러오지 못했습니다: ${err.message}`);
  }
  form.elements.occurred_at.value = toLocalInputValue(new Date());
  form.dataset.idempotencyKey = crypto.randomUUID(); // 같은 폼의 재전송은 한 건으로 처리됩니다.
  $("detail-title").textContent = "티켓 등록";
  $("ticket-detail").hidden = true;
  form.hidden = false;
  form.elements.title.focus();
}

function closeTicketForm() {
  $("ticket-form").hidden = true;
  $("ticket-detail").hidden = false;
  $("detail-title").textContent = "상세";
}

async function submitTicket(event) {
  event.preventDefault();
  const form = event.target;
  const value = (name) => form.elements[name].value.trim();

  const missing = ["title", "description", "service_id", "impact_note", "occurred_at"].filter((name) => !value(name));
  if (missing.length) {
    showFormError(`필수 항목을 입력하세요: ${missing.map((name) => FIELD_LABELS[name]).join(", ")}`);
    return;
  }
  const occurred = new Date(value("occurred_at"));
  if (Number.isNaN(occurred.getTime())) {
    showFormError("발생 시각 형식이 올바르지 않습니다.");
    return;
  }

  const body = {
    title: value("title"),
    description: value("description"),
    service_id: value("service_id"),
    component_id: value("component_id") ? Number(value("component_id")) : null,
    model: value("model") || null,
    impact_level: value("impact_level"),
    urgency: value("urgency"),
    impact_note: value("impact_note"),
    occurred_at: occurred.toISOString(), // 브라우저 현지 시각 -> UTC
    classification: form.elements.restricted.checked ? "restricted" : "standard",
  };

  const submit = $("ticket-form-submit");
  submit.disabled = true;
  showFormError("");
  try {
    const created = await api("/api/tickets", {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": form.dataset.idempotencyKey },
    });
    dismissAgentTicketNotice();
    closeTicketForm();
    $("search-input").value = "";
    $("status-filter").value = "";
    state.selectedTicketId = created.id;
    await loadTickets();
    await loadDetail();
  } catch (err) {
    showFormError(err.message);
  } finally {
    submit.disabled = false;
  }
}

// ---------- AI Assistant ----------

const PRESENTATION_KINDS = new Set([
  "ticket_progress", "ticket_list", "ticket_detail", "ticket_input", "ticket_review", "ticket_success",
  "ticket_unavailable", "ticket_auth_error", "ticket_conflict", "ticket_rate_limited", "ticket_ambiguous",
]);

function validTextValue(value) {
  return value && typeof value.text === "string" && ["server_allowlist", "itsm_untrusted"].includes(value.origin);
}

function validPresentationField(field) {
  return field && typeof field.key === "string" && typeof field.label === "string" && validTextValue(field.value);
}

function validPresentation(value) {
  if (!value || value.version !== 1 || !PRESENTATION_KINDS.has(value.kind) || !Array.isArray(value.blocks)) return false;
  return value.blocks.every((block) => {
    if (!block || typeof block.type !== "string") return false;
    if (block.type === "paragraph") return validTextValue(block.text);
    if (block.type === "fields") return Array.isArray(block.fields) && block.fields.every(validPresentationField);
    if (block.type === "items") {
      return Array.isArray(block.items) && block.items.every((row) => Array.isArray(row) && row.every(validPresentationField));
    }
    if (block.type === "actions") {
      return Array.isArray(block.actions) && block.actions.every((action) =>
        action && typeof action.label === "string" &&
        ["provide_input", "confirm", "revise", "cancel", "retry_later", "start_over"].includes(action.action));
    }
    return false;
  });
}

function agentCreatedTicketId(presentation) {
  if (!validPresentation(presentation) || presentation.kind !== "ticket_success") return null;
  const fields = presentation.blocks
    .filter((block) => block.type === "fields")
    .flatMap((block) => block.fields);
  // 현재 Agent 계약에서 등록 성공은 ticket_id만, 변경 성공은 new_version도 함께 반환합니다.
  if (fields.some((field) => field.key === "new_version")) return null;
  const ticketId = fields.find((field) => field.key === "ticket_id");
  return ticketId ? ticketId.value.text : null;
}

function renderPresentationFields(fields) {
  const list = el("dl", { class: "assistant-fields" });
  for (const field of fields) list.append(el("dt", {}, field.label), el("dd", {}, field.value.text));
  return list;
}

function actionPrompt(action) {
  if (action.action === "revise") return "수정: ";
  if (action.action === "retry_later") return "다시 시도해줘";
  if (action.action === "start_over") return action.label;
  return action.label;
}

function usePresentationAction(action) {
  const input = $("chat-input");
  input.value = actionPrompt(action);
  input.focus();
  if (action.action !== "revise") $("chat-form").requestSubmit();
}

function renderAssistantPresentation(presentation) {
  if (!validPresentation(presentation)) return null;
  const box = el("div", { class: `assistant-presentation ${presentation.kind}` });
  for (const block of presentation.blocks) {
    if (block.type === "paragraph") box.append(el("p", {}, block.text.text));
    else if (block.type === "fields") box.append(renderPresentationFields(block.fields));
    else if (block.type === "items") {
      const items = el("div", { class: "assistant-items" });
      for (const row of block.items) items.append(el("section", { class: "assistant-item" }, renderPresentationFields(row)));
      box.append(items);
    } else if (block.type === "actions") {
      const actions = el("div", { class: "assistant-actions" });
      for (const action of block.actions) {
        const button = el("button", { type: "button", "data-action": action.action }, action.label);
        button.addEventListener("click", () => usePresentationAction(action));
        actions.append(button);
      }
      box.append(actions);
    }
  }
  return box;
}

function updateChatEmptyState() {
  const hasMessages = $("chat-history").children.length > 0;
  $("chat-empty").hidden = hasMessages;
}

function appendInlineMarkdown(parent, text) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  for (const part of parts) {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      parent.append(el("strong", {}, part.slice(2, -2)));
    } else if (part) {
      parent.append(document.createTextNode(part));
    }
  }
}

function renderMessageText(role, text) {
  const box = el("div", { class: `message-text${role === "assistant" ? " formatted" : ""}` });
  if (role !== "assistant") {
    box.textContent = text;
    return box;
  }

  let list = null;
  const flushList = () => { list = null; };
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line) {
      flushList();
      continue;
    }
    const bullet = line.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      if (!list) {
        list = el("ul");
        box.append(list);
      }
      const item = el("li");
      appendInlineMarkdown(item, bullet[1]);
      list.append(item);
      continue;
    }
    flushList();
    const paragraph = el("p");
    appendInlineMarkdown(paragraph, line);
    box.append(paragraph);
  }
  return box;
}

function appendChatMessage(role, text, presentation = null, announce = true) {
  const avatar = role === "user" ? "나" : role === "error" ? "!" : "AI";
  const message = el(
    "article",
    { class: `chat-message ${role}` },
    el("span", { class: "message-avatar", "aria-hidden": "true" }, avatar),
    el("div", { class: "message-content" }, renderMessageText(role, text)),
  );
  const structured = renderAssistantPresentation(presentation);
  if (structured) message.querySelector(".message-content").append(structured);
  $("chat-history").append(message);
  if (announce) {
    const speaker = role === "assistant" ? "AI" : role === "error" ? "오류" : "나";
    $("chat-announcer").replaceChildren(`${speaker}: ${text}`);
  }
  updateChatEmptyState();
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
}

function renderChatLog(messages) {
  $("chat-history").replaceChildren();
  $("chat-announcer").replaceChildren();
  for (const message of messages) appendChatMessage(message.role, message.text, message.presentation, false);
  updateChatEmptyState();
}

function renderAssistantStatus(message = "") {
  const assistant = state.assistant;
  const status = $("assistant-status");
  const dot = $("assistant-dot");
  const busy = assistant.sending || assistant.connecting;
  const enabled = Boolean(state.user) && assistant.ready && !busy;
  $("user-select").disabled = busy;
  for (const button of document.querySelectorAll("[data-agent-variant]")) {
    button.disabled = busy;
    button.setAttribute("aria-pressed", String(button.dataset.agentVariant === assistant.variant));
  }
  $("assistant-variant-label").textContent = assistant.variant === "harness"
    ? "적용 후 · harness-1" : "적용 전 · itsm_dlc";
  $("chat-input").disabled = !enabled;
  $("chat-send").disabled = !enabled;
  $("chat-reset").disabled = !assistant.ready || busy;
  for (const button of document.querySelectorAll("[data-prompt]")) button.disabled = !enabled;

  dot.className = "status-dot";
  if (assistant.connecting) {
    status.textContent = "선택한 Agent에 연결하는 중…";
    dot.classList.add("working");
  } else if (assistant.sending) {
    status.textContent = "응답을 생성하는 중…";
    dot.classList.add("working");
  } else if (message) {
    status.textContent = message;
    dot.classList.add(assistant.ready ? "online" : "offline");
  } else if (!assistant.reachable) {
    status.textContent = "Agent 서비스가 실행 중이지 않습니다";
    dot.classList.add("offline");
  } else if (!assistant.configured) {
    status.textContent = "Agent API 키 설정이 필요합니다";
    dot.classList.add("offline");
  } else if (!state.user) {
    status.textContent = "사용자를 선택하면 대화할 수 있습니다";
  } else if (assistant.ready) {
    status.textContent = "현재 사용자 권한으로 연결됨";
    dot.classList.add("online");
  } else {
    status.textContent = "Agent에 연결하는 중…";
    dot.classList.add("working");
  }
}

function assistantApi(path, options = {}) {
  return api(`/api/assistant/${path}?variant=${state.assistant.variant}`, options);
}

async function selectAssistant(variant) {
  if (state.assistant.sending || state.assistant.connecting || variant === state.assistant.variant) return;
  state.assistant.variant = variant;
  state.assistant.ready = false;
  state.assistant.connecting = true;
  renderChatLog([]);
  renderAssistantStatus();
  await loadAssistantHealth();
  await connectAssistant();
}

async function loadAssistantHealth() {
  try {
    const health = await assistantApi("health");
    state.assistant.reachable = Boolean(health.reachable);
    state.assistant.configured = Boolean(health.configured);
  } catch {
    state.assistant.reachable = false;
    state.assistant.configured = false;
  }
  renderAssistantStatus();
}

async function connectAssistant() {
  if (!state.user || !state.assistant.reachable || !state.assistant.configured) {
    state.assistant.ready = false;
    state.assistant.connecting = false;
    renderAssistantStatus();
    return;
  }
  state.assistant.ready = false;
  state.assistant.connecting = true;
  renderAssistantStatus();
  let errorMessage = "";
  try {
    await assistantApi("session", { method: "POST" });
    const history = await assistantApi("messages");
    renderChatLog(history.messages || []);
    state.assistant.ready = true;
  } catch (err) {
    state.assistant.ready = false;
    errorMessage = err.message;
  } finally {
    state.assistant.connecting = false;
    renderAssistantStatus(errorMessage);
  }
}

async function sendAssistantMessage(message) {
  const clean = message.trim();
  if (!clean || state.assistant.sending || !state.assistant.ready) return;
  state.assistant.sending = true;
  appendChatMessage("user", clean, null, false);
  $("chat-input").value = "";
  renderAssistantStatus();
  const pending = el(
    "article",
    { class: "chat-message assistant pending" },
    el("span", { class: "message-avatar", "aria-hidden": "true" }, "AI"),
    el("div", { class: "typing", "aria-label": "응답을 기다리는 중" }, el("i"), el("i"), el("i")),
  );
  $("chat-history").append(pending);
  $("chat-announcer").replaceChildren("AI 응답을 기다리는 중");
  updateChatEmptyState();
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
  try {
    const data = await assistantApi("chat", { method: "POST", body: JSON.stringify({ message: clean }) });
    pending.remove();
    appendChatMessage("assistant", data.reply, data.presentation);
    const createdTicketId = agentCreatedTicketId(data.presentation);
    if (createdTicketId) showAgentTicketNotice(createdTicketId);
    if (data.presentation && data.presentation.kind === "ticket_success") await refreshAll();
  } catch (err) {
    pending.remove();
    appendChatMessage("error", err.message);
  } finally {
    state.assistant.sending = false;
    renderAssistantStatus();
    $("chat-input").focus();
  }
}

async function resetChat() {
  if (!state.assistant.ready || state.assistant.sending) return;
  state.assistant.sending = true;
  renderAssistantStatus();
  try {
    await assistantApi("messages", { method: "DELETE" });
    renderChatLog([]);
  } finally {
    state.assistant.sending = false;
    renderAssistantStatus();
    $("chat-input").focus();
  }
}

function setAssistantOpen(open) {
  $("assistant-panel").hidden = !open;
  $("assistant-toggle").setAttribute("aria-expanded", String(open));
  document.body.classList.toggle("assistant-closed", !open);
}

// ---------- 초기화 ----------

async function refreshAll() {
  renderNewTicketButton();
  await Promise.all([loadTickets(), loadDetail()]);
}

async function init() {
  const health = await api("/api/health");
  $("itsm-status").textContent = health.itsm.reachable ? "" : `ITSM API 연결 안 됨 (${health.itsm.base_url})`;
  await Promise.all([loadUsers(), loadAssistantHealth()]);
  await loadSession();
  await Promise.all([refreshAll(), connectAssistant()]);

  for (const button of document.querySelectorAll("[data-agent-variant]")) {
    button.addEventListener("click", () => selectAssistant(button.dataset.agentVariant));
  }
  $("user-select").addEventListener("change", (e) => {
    if (e.target.value) switchUser(e.target.value).catch((err) => alert(err.message));
  });
  $("search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    loadTickets();
  });
  $("status-filter").addEventListener("change", () => loadTickets());
  $("more-tickets").addEventListener("click", () => loadTickets({ append: true }));
  $("new-ticket").addEventListener("click", () => openTicketForm());
  $("agent-ticket-view").addEventListener("click", () => viewAgentCreatedTicket().catch((err) => alert(err.message)));
  $("agent-ticket-dismiss").addEventListener("click", dismissAgentTicketNotice);
  $("ticket-form").elements.service_id.addEventListener("change", fillComponents);
  $("ticket-form-cancel").addEventListener("click", closeTicketForm);
  $("ticket-form").addEventListener("submit", submitTicket);
  $("assistant-toggle").addEventListener("click", () => setAssistantOpen($("assistant-panel").hidden));
  $("assistant-close").addEventListener("click", () => setAssistantOpen(false));
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendAssistantMessage($("chat-input").value);
  });
  $("chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("chat-form").requestSubmit();
    }
  });
  $("chat-reset").addEventListener("click", () => resetChat().catch((err) => appendChatMessage("error", err.message)));
  for (const button of document.querySelectorAll("[data-prompt]")) {
    button.addEventListener("click", () => sendAssistantMessage(button.dataset.prompt || ""));
  }
}

init().catch((err) => {
  document.body.prepend(el("p", { class: "notice" }, `초기화 실패: ${err.message}`));
});
