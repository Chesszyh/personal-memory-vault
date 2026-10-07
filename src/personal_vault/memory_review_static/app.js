"use strict";

const state = {
  items: [],
  batches: [],
  counts: { total: 0, by_status: {}, by_kind: {}, decision_events: 0 },
  selected: new Set(),
  activeId: null,
  detail: null,
  filters: { snapshotKey: "", statuses: new Set(["pending"]), kinds: new Set(), query: "", sort: "newest", minConfidence: "", dateFrom: "", dateTo: "" },
  searchTimer: null,
};

const labels = {
  status: { pending: "待审核", confirmed: "已确认", rejected: "已拒绝" },
  kind: { identity: "身份", preference: "偏好", boundary: "边界", plan: "计划" },
  role: { user: "你", assistant: "助手", system: "系统", tool: "工具" },
  clue: { duplicate: "可能重复", possible_conflict: "可能冲突", related: "相关候选" },
};

const elements = {};

function byId(id) { return document.getElementById(id); }
function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}
function clear(target) { while (target.firstChild) target.firstChild.remove(); }
function formatDate(value) {
  if (value === null || value === undefined) return "时间未知";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}
function truncate(value, size) { return value.length <= size ? value : `${value.slice(0, size)}…`; }
function localDayStart(value) {
  if (!value) return null;
  const timestamp = new Date(`${value}T00:00:00`).getTime();
  return Number.isNaN(timestamp) ? null : timestamp / 1000;
}
function localDayAfter(value) {
  if (!value) return null;
  const day = new Date(`${value}T00:00:00`);
  if (Number.isNaN(day.getTime())) return null;
  day.setDate(day.getDate() + 1);
  return day.getTime() / 1000;
}
function batchLabel(batch) {
  const date = batch.snapshot_key.match(/\d{4}-\d{2}-\d{2}$/)?.[0] || "";
  const account = batch.snapshot_key.includes("new-account") ? "新账号" : batch.snapshot_key.includes("chatgpt-official") ? "旧账号" : batch.snapshot_key;
  return `${account}${date ? ` · ${date}` : ""}`;
}

async function api(path, options = {}) {
  setSaveState(options.method === "POST" ? "saving" : "idle");
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
  });
  const payload = await response.json().catch(() => ({ error: { message: "本地服务返回了不可解析内容" } }));
  if (!response.ok) {
    setSaveState("error");
    throw new Error(payload.error?.message || `请求失败 (${response.status})`);
  }
  if (options.method === "POST") setSaveState("saved");
  return payload;
}

function setSaveState(mode) {
  elements.saveState.classList.toggle("is-saving", mode === "saving");
  elements.saveState.classList.toggle("is-error", mode === "error");
  elements.saveState.lastChild.textContent = mode === "saving" ? "正在保存…" : mode === "error" ? "保存失败" : "所有更改自动保存";
}

function toast(message, error = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("is-error", error);
  elements.toast.hidden = false;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => { elements.toast.hidden = true; }, 3600);
}

function renderFilterGroups() {
  clear(elements.statusFilters);
  for (const status of ["pending", "confirmed", "rejected"]) {
    const label = node("label", "filter-option");
    const wrap = node("span");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.filters.statuses.has(status);
    checkbox.addEventListener("change", () => {
      checkbox.checked ? state.filters.statuses.add(status) : state.filters.statuses.delete(status);
      loadCandidates();
    });
    wrap.append(checkbox, node("span", "", labels.status[status]));
    label.append(wrap, node("b", "", state.counts.by_status[status] || 0));
    elements.statusFilters.append(label);
  }
  clear(elements.kindFilters);
  for (const kind of ["identity", "preference", "boundary", "plan"]) {
    const label = node("label", "filter-option");
    const wrap = node("span");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.filters.kinds.has(kind);
    checkbox.addEventListener("change", () => {
      checkbox.checked ? state.filters.kinds.add(kind) : state.filters.kinds.delete(kind);
      loadCandidates();
    });
    wrap.append(checkbox, node("span", "", labels.kind[kind]));
    label.append(wrap, node("b", "", state.counts.by_kind[kind] || 0));
    elements.kindFilters.append(label);
  }
}

function renderProgress() {
  const counts = state.counts.by_status;
  const reviewed = (counts.confirmed || 0) + (counts.rejected || 0);
  const total = state.counts.total || 0;
  const percent = total ? Math.round((reviewed / total) * 100) : 0;
  elements.progressLabel.textContent = `已审核 ${reviewed} / ${total}`;
  const batch = state.batches.find((item) => item.snapshot_key === state.filters.snapshotKey);
  elements.progressDetail.textContent = `${percent}% 完成 · ${state.counts.decision_events || 0} 次审核操作 · ${batch ? batchLabel(batch) : state.filters.snapshotKey}`;
  elements.progressFill.style.width = `${percent}%`;
  clear(elements.statusCounts);
  for (const status of ["confirmed", "pending", "rejected"]) {
    const item = node("span", `status-count ${status}`);
    item.append(node("i"), node("span", "", `${labels.status[status]} ${counts[status] || 0}`));
    elements.statusCounts.append(item);
  }
}

function renderBatches() {
  clear(elements.batchSelect);
  for (const batch of state.batches) {
    const option = document.createElement("option");
    option.value = batch.snapshot_key;
    option.textContent = `${batchLabel(batch).split(" · ")[0]} · ${batch.total} 条`;
    option.selected = batch.snapshot_key === state.filters.snapshotKey;
    elements.batchSelect.append(option);
  }
}

async function loadBatches() {
  const payload = await api("/api/batches");
  state.batches = payload.items;
  let remembered = "";
  try { remembered = localStorage.getItem("pmv-memory-review-batch") || ""; } catch (_error) { /* optional */ }
  const available = new Set(state.batches.map((batch) => batch.snapshot_key));
  const preferred = state.filters.snapshotKey || remembered;
  state.filters.snapshotKey = available.has(preferred) ? preferred : (state.batches[0]?.snapshot_key || "");
  renderBatches();
}

function candidateCard(item) {
  const card = node("article", "candidate-card");
  card.dataset.candidateId = item.candidate_id;
  card.classList.toggle("is-active", state.activeId === item.candidate_id);
  card.classList.toggle("is-selected", state.selected.has(item.candidate_id));
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = state.selected.has(item.candidate_id);
  checkbox.setAttribute("aria-label", "选择候选");
  checkbox.addEventListener("click", (event) => event.stopPropagation());
  checkbox.addEventListener("change", () => toggleSelection(item.candidate_id, checkbox.checked));

  const main = node("div", "candidate-main");
  const meta = node("div", "candidate-meta");
  meta.append(
    node("span", "kind-pill", labels.kind[item.kind]),
    node("span", `status-pill ${item.status}`, labels.status[item.status]),
  );
  if (item.edited) meta.append(node("span", "kind-pill", "已编辑"));
  meta.append(node("time", "candidate-time", formatDate(item.message_create_time)));
  const statement = node("p", "candidate-statement", item.statement);
  const source = node("div", "candidate-source");
  source.append(
    node("span", "", item.conversation_title || item.source_snapshot_key),
    node("em", "", "查看上下文 →"),
  );
  main.append(meta, statement, source);
  card.append(checkbox, main);
  card.addEventListener("click", () => openDetail(item.candidate_id));
  return card;
}

function renderCandidates() {
  clear(elements.candidateList);
  elements.candidateState.hidden = state.items.length > 0;
  elements.candidateState.textContent = state.items.length ? "" : "当前筛选下没有候选";
  for (const item of state.items) elements.candidateList.append(candidateCard(item));
  elements.resultCount.textContent = `显示 ${state.items.length} / ${state.currentTotal || 0}`;
  elements.selectAll.checked = state.items.length > 0 && state.items.every((item) => state.selected.has(item.candidate_id));
  elements.selectAll.indeterminate = state.items.some((item) => state.selected.has(item.candidate_id)) && !elements.selectAll.checked;
  renderSelectionBar();
}

async function loadCandidates({ preserveDetail = true } = {}) {
  elements.candidateState.hidden = false;
  elements.candidateState.textContent = "正在读取候选…";
  const params = new URLSearchParams({ limit: "1000", offset: "0", sort: state.filters.sort });
  if (state.filters.snapshotKey) params.set("snapshot", state.filters.snapshotKey);
  params.set("status", [...state.filters.statuses].join(","));
  params.set("kind", [...state.filters.kinds].join(","));
  if (state.filters.query) params.set("q", state.filters.query);
  if (state.filters.minConfidence) params.set("min_confidence", state.filters.minConfidence);
  const afterTime = localDayStart(state.filters.dateFrom);
  const beforeTime = localDayAfter(state.filters.dateTo);
  if (afterTime !== null) params.set("after_time", String(afterTime));
  if (beforeTime !== null) params.set("before_time", String(beforeTime));
  try {
    const payload = await api(`/api/candidates?${params}`);
    state.items = payload.items;
    state.currentTotal = payload.pagination.total;
    state.counts = payload.counts;
    renderFilterGroups();
    renderProgress();
    renderCandidates();
    if (preserveDetail && state.activeId) {
      const stillVisible = state.items.some((item) => item.candidate_id === state.activeId);
      if (!stillVisible) closeDetail();
    }
  } catch (error) {
    elements.candidateState.hidden = false;
    elements.candidateState.textContent = error.message;
    toast(error.message, true);
  }
}

function toggleSelection(candidateId, checked) {
  checked ? state.selected.add(candidateId) : state.selected.delete(candidateId);
  renderCandidates();
}

function renderSelectionBar() {
  const count = state.selected.size;
  elements.selectionBar.hidden = count === 0;
  elements.selectionLabel.textContent = `已选择 ${count} 条`;
}

async function openDetail(candidateId) {
  state.activeId = candidateId;
  renderCandidates();
  elements.detailEmpty.hidden = true;
  elements.detailContent.hidden = false;
  elements.detailPane.classList.add("is-open");
  elements.detailEyebrow.textContent = "正在加载来源上下文";
  elements.detailTitle.textContent = candidateId;
  try {
    state.detail = await api(`/api/candidates/${encodeURIComponent(candidateId)}`);
    renderDetail();
  } catch (error) {
    toast(error.message, true);
  }
}

function closeDetail() {
  state.activeId = null;
  state.detail = null;
  elements.detailPane.classList.remove("is-open");
  elements.detailContent.hidden = true;
  elements.detailEmpty.hidden = false;
  renderCandidates();
}

function renderDetail() {
  const { candidate, context, related, decisions } = state.detail;
  elements.detailEyebrow.textContent = `${labels.kind[candidate.kind]} · ${labels.status[candidate.status]} · ${candidate.source_snapshot_key}`;
  elements.detailTitle.textContent = candidate.conversation_title || "未命名来源会话";
  elements.statementEditor.value = candidate.statement;
  elements.statementEditor.dataset.saved = candidate.statement;
  elements.originalStatement.textContent = candidate.original_statement;
  elements.characterCount.textContent = `${candidate.statement.length} / 4000`;
  elements.editorState.textContent = candidate.edited ? "已保存编辑" : "使用原话";
  elements.editorState.classList.remove("is-dirty");
  elements.sourceReference.textContent = `${candidate.source_snapshot_key} / ${candidate.source_message_identity_key}`;

  clear(elements.contextList);
  if (!context.source_available || !context.messages.length) {
    elements.contextList.append(node("p", "", "来源消息当前无法定位。"));
  } else {
    for (const message of context.messages) {
      const item = node("article", `context-message ${message.role || "unknown"}${message.is_source ? " is-source" : ""}`);
      const header = node("header");
      header.append(node("strong", "", message.is_source ? `${labels.role[message.role] || message.role} · 候选来源` : labels.role[message.role] || message.role), node("time", "", formatDate(message.create_time)));
      item.append(header, node("p", "", message.content));
      elements.contextList.append(item);
    }
  }

  clear(elements.relatedList);
  if (!related.length) {
    elements.relatedList.append(node("p", "", "没有发现明显的重复或冲突线索。"));
  } else {
    for (const candidateItem of related) {
      const pair = node("div", "comparison-row");
      const current = node("article", "comparison-card current");
      current.append(node("strong", "", "当前候选"), node("p", "", candidate.statement));
      const other = node("button", "comparison-card related-item");
      other.type = "button";
      const header = node("header");
      header.append(node("span", `clue-pill ${candidateItem.clue}`, labels.clue[candidateItem.clue]), node("span", `status-pill ${candidateItem.status}`, labels.status[candidateItem.status]));
      other.append(header, node("p", "", candidateItem.statement));
      other.addEventListener("click", () => openDetail(candidateItem.candidate_id));
      pair.append(current, other);
      elements.relatedList.append(pair);
    }
  }

  clear(elements.decisionHistory);
  if (!decisions.length) {
    elements.decisionHistory.append(node("span", "", "尚无审核操作。"));
  } else {
    for (const decision of decisions) {
      const event = node("div", "decision-event");
      event.append(node("span", "", `${labels.status[decision.prior_status]} → ${labels.status[decision.status]}${decision.note ? ` · ${decision.note}` : ""}`), node("time", "", formatDate(decision.decided_at)));
      elements.decisionHistory.append(event);
    }
  }
}

async function saveEdit() {
  if (!state.activeId || !state.detail) return false;
  const statement = elements.statementEditor.value.trim();
  if (!statement) { toast("确认后的记忆表述不能为空", true); return false; }
  try {
    const payload = await api(`/api/candidates/${encodeURIComponent(state.activeId)}/edit`, { method: "POST", body: JSON.stringify({ statement }) });
    elements.statementEditor.dataset.saved = payload.statement;
    elements.editorState.textContent = payload.edited ? "已保存编辑" : "使用原话";
    elements.editorState.classList.remove("is-dirty");
    state.detail.candidate.statement = payload.statement;
    state.detail.candidate.edited = payload.edited;
    const visible = state.items.find((item) => item.candidate_id === state.activeId);
    if (visible) { visible.statement = payload.statement; visible.edited = payload.edited; }
    renderCandidates();
    toast("候选表述已保存，原始证据未改变");
    return true;
  } catch (error) { toast(error.message, true); return false; }
}

async function decide(ids, status, note = null) {
  if (!ids.length) return;
  const unsaved = state.activeId && ids.includes(state.activeId) && elements.statementEditor.value.trim() !== elements.statementEditor.dataset.saved;
  if (unsaved && status === "confirmed" && !(await saveEdit())) return;
  try {
    const payload = await api("/api/decisions", { method: "POST", body: JSON.stringify({ candidate_ids: ids, status, note: note || null }) });
    for (const id of ids) state.selected.delete(id);
    toast(`${payload.updated_count} 条候选已设为“${labels.status[status]}”`);
    await loadBatches();
    await loadCandidates({ preserveDetail: false });
    if (ids.length === 1 && state.filters.statuses.has(status)) await openDetail(ids[0]); else closeDetail();
  } catch (error) { toast(error.message, true); }
}

function openHelp(open) {
  elements.helpDrawer.classList.toggle("is-open", open);
  elements.drawerBackdrop.classList.toggle("is-open", open);
  elements.helpDrawer.setAttribute("aria-hidden", open ? "false" : "true");
}

async function rebuild() {
  elements.rebuildConfirm.disabled = true;
  elements.rebuildConfirm.textContent = "正在重建…";
  try {
    const payload = await api("/api/rebuild", { method: "POST", body: "{}" });
    clear(elements.rebuildResult);
    elements.rebuildResult.append(
      node("strong", "", `已使用 ${payload.confirmed_count} 条确认记忆`),
      node("p", "", `画像：${payload.profile_path}`),
      node("p", "", `迁移包：${payload.migration_path}`),
    );
    elements.rebuildResult.hidden = false;
    elements.rebuildConfirm.textContent = "再次重建";
    toast("画像和 ChatGPT 迁移包已重建");
  } catch (error) {
    toast(error.message, true);
    elements.rebuildConfirm.textContent = "重试";
  } finally { elements.rebuildConfirm.disabled = false; }
}

function bind() {
  Object.assign(elements, {
    saveState: byId("save-state"), progressLabel: byId("progress-label"), progressDetail: byId("progress-detail"), progressFill: byId("progress-fill"), statusCounts: byId("status-counts"), batchSelect: byId("batch-select"),
    statusFilters: byId("status-filters"), kindFilters: byId("kind-filters"), searchInput: byId("search-input"), confidenceSelect: byId("confidence-select"), dateFrom: byId("date-from"), dateTo: byId("date-to"), sortSelect: byId("sort-select"), selectAll: byId("select-all"), clearSelection: byId("clear-selection"), resultCount: byId("result-count"), candidateState: byId("candidate-state"), candidateList: byId("candidate-list"),
    detailPane: byId("detail-pane"), detailEmpty: byId("detail-empty"), detailContent: byId("detail-content"), detailEyebrow: byId("detail-eyebrow"), detailTitle: byId("detail-title"), detailClose: byId("detail-close"), statementEditor: byId("statement-editor"), editorState: byId("editor-state"), characterCount: byId("character-count"), restoreOriginal: byId("restore-original"), saveEdit: byId("save-edit"), originalStatement: byId("original-statement"), sourceReference: byId("source-reference"), contextList: byId("context-list"), relatedList: byId("related-list"), decisionHistory: byId("decision-history"),
    selectionBar: byId("selection-bar"), selectionLabel: byId("selection-label"), batchNote: byId("batch-note"), helpToggle: byId("help-toggle"), helpDrawer: byId("help-drawer"), helpClose: byId("help-close"), drawerBackdrop: byId("drawer-backdrop"), toast: byId("toast"), themeToggle: byId("theme-toggle"), rebuildButton: byId("rebuild-button"), rebuildModal: byId("rebuild-modal"), rebuildCancel: byId("rebuild-cancel"), rebuildConfirm: byId("rebuild-confirm"), rebuildResult: byId("rebuild-result"),
  });

  elements.batchSelect.addEventListener("change", () => {
    state.filters.snapshotKey = elements.batchSelect.value;
    state.selected.clear();
    closeDetail();
    try { localStorage.setItem("pmv-memory-review-batch", state.filters.snapshotKey); } catch (_error) { /* optional */ }
    loadCandidates({ preserveDetail: false });
  });
  elements.searchInput.addEventListener("input", () => {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(() => { state.filters.query = elements.searchInput.value.trim(); loadCandidates(); }, 220);
  });
  elements.sortSelect.addEventListener("change", () => { state.filters.sort = elements.sortSelect.value; loadCandidates(); });
  elements.confidenceSelect.addEventListener("change", () => { state.filters.minConfidence = elements.confidenceSelect.value; loadCandidates(); });
  elements.dateFrom.addEventListener("change", () => { state.filters.dateFrom = elements.dateFrom.value; loadCandidates(); });
  elements.dateTo.addEventListener("change", () => { state.filters.dateTo = elements.dateTo.value; loadCandidates(); });
  elements.selectAll.addEventListener("change", () => {
    for (const item of state.items) elements.selectAll.checked ? state.selected.add(item.candidate_id) : state.selected.delete(item.candidate_id);
    renderCandidates();
  });
  elements.clearSelection.addEventListener("click", () => { state.selected.clear(); renderCandidates(); });
  elements.detailClose.addEventListener("click", closeDetail);
  elements.statementEditor.addEventListener("input", () => {
    const value = elements.statementEditor.value;
    elements.characterCount.textContent = `${value.length} / 4000`;
    const dirty = value.trim() !== elements.statementEditor.dataset.saved;
    elements.editorState.textContent = dirty ? "尚未保存" : (state.detail?.candidate.edited ? "已保存编辑" : "使用原话");
    elements.editorState.classList.toggle("is-dirty", dirty);
  });
  elements.restoreOriginal.addEventListener("click", () => { if (state.detail) { elements.statementEditor.value = state.detail.candidate.original_statement; elements.statementEditor.dispatchEvent(new Event("input")); } });
  elements.saveEdit.addEventListener("click", saveEdit);
  document.querySelectorAll("[data-detail-status]").forEach((button) => button.addEventListener("click", () => decide([state.activeId], button.dataset.detailStatus)));
  document.querySelectorAll("[data-batch-status]").forEach((button) => button.addEventListener("click", () => decide([...state.selected], button.dataset.batchStatus, elements.batchNote.value.trim())));
  elements.helpToggle.addEventListener("click", () => openHelp(true));
  elements.helpClose.addEventListener("click", () => openHelp(false));
  elements.drawerBackdrop.addEventListener("click", () => openHelp(false));
  elements.themeToggle.addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("pmv-memory-review-theme", theme); } catch (_error) { /* optional */ }
  });
  elements.rebuildButton.addEventListener("click", () => { elements.rebuildModal.hidden = false; elements.rebuildResult.hidden = true; });
  elements.rebuildCancel.addEventListener("click", () => { elements.rebuildModal.hidden = true; });
  elements.rebuildConfirm.addEventListener("click", rebuild);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { openHelp(false); elements.rebuildModal.hidden = true; if (window.innerWidth < 1180) closeDetail(); }
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && state.activeId) { event.preventDefault(); decide([state.activeId], "confirmed"); }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  try { document.documentElement.dataset.theme = localStorage.getItem("pmv-memory-review-theme") || "light"; } catch (_error) { /* optional */ }
  bind();
  renderFilterGroups();
  try {
    await loadBatches();
    await loadCandidates();
  } catch (error) {
    elements.candidateState.textContent = error.message;
    toast(error.message, true);
  }
});
