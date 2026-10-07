import {addMessageActions, addBookmarksButton, trackPosition, savedPosition} from "./annotations.js";
import { renderMarkdown } from "./markdown.js";

const API = Object.freeze({
  library: "/api/library",
  snapshots: "/api/snapshots",
  conversations: "/api/conversations",
  groupThreads: "/api/group-threads",
  search: "/api/search",
  analytics: "/api/analytics"
});

const PAGE_SIZE = 50;
const state = {
  view: "reader",
  libraryMode: true,
  listKind: "conversations",
  drawerTab: "provenance",
  snapshots: [],
  snapshotId: null,
  snapshot: null,
  activeIdentityKey: null,
  conversation: null,
  selectedNode: null,
  listItems: [],
  listTotal: 0,
  listNextOffset: null,
  listLoading: false,
  listError: null,
  searchQuery: "",
  searchMode: null,
  searchGuidance: null,
  analytics: null,
  analyticsSnapshotId: null,
  listController: null,
  detailController: null,
  toastTimer: null
};

const dom = {};

document.addEventListener("DOMContentLoaded", boot);

async function boot() {
  collectDom();
  initializeTheme();
  bindEvents();
  addBookmarksButton(dom.openSnapshots.parentElement);

  try {
    const payload = await fetchJson(withQuery(API.snapshots, { limit: 100, offset: 0 }));
    state.snapshots = Array.isArray(payload.items) ? payload.items : [];
    state.snapshotId = payload.default_snapshot_id ?? state.snapshots[0]?.id ?? null;
    state.snapshot = findSnapshot(state.snapshotId);
    for (const snapshot of state.snapshots) {
      const option = element("option", null, `${snapshot.captured_at.slice(0, 10)} · ${snapshot.source.identity_scope === "new-chatgpt-account" ? "新账号" : snapshot.source.identity_scope === "legacy-chatgpt-account" ? "旧账号" : snapshot.source.identity_scope}`);
      option.value = String(snapshot.id);
      dom.archiveScope.append(option);
    }
    renderSnapshotSummary();

    if (!state.snapshotId) {
      renderListEmpty("尚无可读取的快照", "请先导入一份已校验的导出快照。");
      renderReaderEmpty("尚无归档内容", "导入完成后，会话会出现在左侧列表中。");
      renderDrawer();
      return;
    }

    const citation = new URLSearchParams(window.location.search);
    if (citation.has("source")) {
      const resolved = await fetchJson(withQuery("/api/citation", { source: citation.get("source") }));
      if (resolved.source_kind === "pi_session") {
        state.snapshots.push({id:resolved.snapshot_id,snapshot_key:resolved.snapshot_key,
          captured_at:resolved.captured_at,source:{kind:"pi_session",identity_scope:"local-pi"},counts:{}});
        const option=element("option",null,"Pi 会话");option.value=resolved.snapshot_id;dom.archiveScope.append(option);
      }
      citation.set("snapshot", resolved.snapshot_key);
      citation.set("conversation", resolved.conversation_id);
      citation.set("message", resolved.message_id);
    }
    const conversationId = citation.get("conversation");
    if (conversationId?.startsWith("pi/") && !state.snapshots.some(s=>s.snapshot_key===conversationId)) {
      const detail=await fetchJson(`/api/pi/sessions/${encodeURIComponent(conversationId.slice(3))}`);
      state.snapshots.push({...detail.snapshot,counts:{}});
      const option=element("option",null,"Pi 会话");option.value=detail.snapshot.id;dom.archiveScope.append(option);
    }
    if (conversationId) {
      const snapshot = state.snapshots.find(item => item.snapshot_key === citation.get("snapshot"));
      if (!snapshot) throw new Error("引用对应的快照不存在，请核对来源链接。");
      state.libraryMode = false;
      state.snapshotId = snapshot.id;
      state.snapshot = snapshot;
      dom.archiveScope.value = String(snapshot.id);
      renderSnapshotSummary();
      if (snapshot.source.kind !== "pi_session") await loadList({ reset: true, autoSelect: false });
      await selectConversation(conversationId, { messageId: citation.get("message") });
    } else {
      await loadList({ reset: true, autoSelect: true });
    }
  } catch (error) {
    renderSnapshotSummary(error);
    renderListError(error);
    renderReaderError(error, () => window.location.reload());
  }
}

function collectDom() {
  const ids = [
    "app", "sidebar", "close-sidebar", "show-reader", "show-groups", "show-analytics", "archive-scope",
    "search-form", "search-input", "search-guidance", "search-shortcut", "clear-search",
    "list-summary", "retry-list", "conversation-list", "load-more", "list-loading",
    "open-snapshots", "snapshot-summary-title", "snapshot-summary-meta",
    "open-sidebar", "topbar-conversation-title", "topbar-meta", "theme-toggle", "drawer-toggle",
    "reader-view", "snapshot-banner", "snapshot-banner-copy", "snapshot-banner-status",
    "reader-scroll", "reader-state", "conversation", "conversation-eyebrow", "conversation-title",
    "conversation-summary", "conversation-badges", "message-list", "analytics-view",
    "analytics-state", "analytics-content", "drawer", "close-drawer", "show-provenance",
    "show-snapshots", "drawer-state", "sidebar-backdrop", "drawer-backdrop", "toast"
  ];
  for (const id of ids) {
    dom[toCamel(id)] = document.getElementById(id);
  }
}

function bindEvents() {
  dom.archiveScope.addEventListener("change", () => switchSnapshot(dom.archiveScope.value === "all" || dom.archiveScope.value.startsWith("pi/") ? dom.archiveScope.value : Number(dom.archiveScope.value)));
  dom.showReader.addEventListener("click", () => activateListKind("conversations"));
  dom.showGroups.addEventListener("click", () => activateListKind("groups"));
  dom.showAnalytics.addEventListener("click", () => showView("analytics"));
  dom.openSidebar.addEventListener("click", () => toggleSidebar(true));
  dom.closeSidebar.addEventListener("click", () => toggleSidebar(false));
  dom.sidebarBackdrop.addEventListener("click", () => toggleSidebar(false));
  dom.drawerToggle.addEventListener("click", () => toggleDrawer(!dom.app.classList.contains("drawer-open")));
  dom.closeDrawer.addEventListener("click", () => toggleDrawer(false));
  dom.drawerBackdrop.addEventListener("click", () => toggleDrawer(false));
  dom.showProvenance.addEventListener("click", () => setDrawerTab("provenance"));
  dom.showSnapshots.addEventListener("click", () => setDrawerTab("snapshots"));
  dom.openSnapshots.addEventListener("click", () => {
    showView("reader");
    setDrawerTab("snapshots");
    toggleDrawer(true);
  });
  dom.themeToggle.addEventListener("click", toggleTheme);
  dom.retryList.addEventListener("click", () => loadList({ reset: true, autoSelect: false }));
  dom.loadMore.addEventListener("click", () => loadList({ reset: false, autoSelect: false }));
  dom.clearSearch.addEventListener("click", clearSearch);
  dom.searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    applySearch(dom.searchInput.value);
  });

  let searchTimer = null;
  dom.searchInput.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    const query = dom.searchInput.value;
    updateSearchGuidance(query);
    searchTimer = window.setTimeout(() => applySearch(query), 280);
  });

  window.addEventListener("keydown", (event) => {
    const typing = event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement;
    if (event.key === "/" && !typing) {
      event.preventDefault();
      dom.searchInput.focus();
    }
    if (event.key === "Escape") {
      toggleSidebar(false);
      toggleDrawer(false);
    }
  });
}

function initializeTheme() {
  let theme = null;
  try {
    theme = window.localStorage.getItem("archive-reader-theme");
  } catch (_error) {
    // Theme persistence is optional in hardened browser profiles.
  }
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  setTheme(theme);
}

function toggleTheme() {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  dom.themeToggle?.setAttribute("aria-label", theme === "dark" ? "切换到浅色主题" : "切换到深色主题");
  dom.themeToggle?.setAttribute("title", theme === "dark" ? "切换到浅色主题" : "切换到深色主题");
  try {
    window.localStorage.setItem("archive-reader-theme", theme);
  } catch (_error) {
    // Reading remains functional when local storage is unavailable.
  }
}

async function activateListKind(kind) {
  const normalized = kind === "groups" ? "groups" : "conversations";
  if (normalized === "groups") {
    state.libraryMode = false;
    dom.archiveScope.value = String(state.snapshotId);
    renderSnapshotSummary();
  }
  if (state.listKind === normalized) {
    showView("reader");
    return;
  }
  state.listKind = normalized;
  state.activeIdentityKey = null;
  state.conversation = null;
  state.selectedNode = null;
  state.searchQuery = "";
  dom.searchInput.value = "";
  dom.searchInput.placeholder = normalized === "groups"
    ? "搜索群聊名称或消息正文"
    : "搜索标题或消息正文";
  dom.clearSearch.hidden = true;
  dom.searchShortcut.hidden = false;
  updateSearchGuidance("");
  showView("reader");
  await loadList({ reset: true, autoSelect: true });
}

function showView(view) {
  state.view = view;
  const analytics = view === "analytics";
  dom.app.dataset.view = view;
  dom.readerView.hidden = analytics;
  dom.analyticsView.hidden = !analytics;
  const conversations = !analytics && state.listKind === "conversations";
  const groups = !analytics && state.listKind === "groups";
  dom.showReader.classList.toggle("is-active", conversations);
  dom.showReader.setAttribute("aria-selected", String(conversations));
  dom.showGroups.classList.toggle("is-active", groups);
  dom.showGroups.setAttribute("aria-selected", String(groups));
  dom.showAnalytics.classList.toggle("is-active", analytics);
  dom.showAnalytics.setAttribute("aria-selected", String(analytics));
  dom.drawerToggle.hidden = analytics;
  toggleSidebar(false);

  if (analytics) {
    toggleDrawer(false);
    dom.topbarConversationTitle.textContent = "归档洞察";
    dom.topbarMeta.textContent = snapshotLabel(findSnapshot(state.snapshotId));
    loadAnalytics();
  } else {
    updateTopbar();
  }
}

function toggleSidebar(open) {
  dom.app.classList.toggle("sidebar-open", Boolean(open));
}

function toggleDrawer(open) {
  const shouldOpen = Boolean(open) && state.view === "reader";
  dom.app.classList.toggle("drawer-open", shouldOpen);
  dom.drawerToggle.classList.toggle("is-active", shouldOpen);
  dom.drawerToggle.setAttribute("aria-expanded", String(shouldOpen));
  dom.drawer.setAttribute("aria-hidden", String(!shouldOpen));
  if (shouldOpen) {
    renderDrawer();
    toggleSidebar(false);
  }
}

function setDrawerTab(tab) {
  state.drawerTab = tab;
  const provenance = tab === "provenance";
  dom.showProvenance.classList.toggle("is-active", provenance);
  dom.showProvenance.setAttribute("aria-selected", String(provenance));
  dom.showSnapshots.classList.toggle("is-active", !provenance);
  dom.showSnapshots.setAttribute("aria-selected", String(!provenance));
  renderDrawer();
}

async function applySearch(rawQuery) {
  const query = rawQuery.trim();
  if (query === state.searchQuery) {
    return;
  }
  state.searchQuery = query;
  dom.clearSearch.hidden = query.length === 0;
  dom.searchShortcut.hidden = query.length > 0;
  updateSearchGuidance(query);
  await loadList({ reset: true, autoSelect: false });
}

function clearSearch() {
  dom.searchInput.value = "";
  state.searchQuery = "__force_reset__";
  applySearch("");
  dom.searchInput.focus();
}

function updateSearchGuidance(rawQuery, serverGuidance = null, mode = null) {
  const query = rawQuery.trim();
  dom.searchGuidance.classList.remove("is-warning", "is-active");
  if (serverGuidance) {
    dom.searchGuidance.textContent = serverGuidance;
    dom.searchGuidance.classList.add(mode === "literal_like" ? "is-warning" : "is-active");
  } else if (state.listKind === "groups") {
    dom.searchGuidance.textContent = query.length
      ? "群聊名称和消息正文将使用逐字匹配"
      : "搜索范围仅限当前快照中的 ChatGPT group chat";
    dom.searchGuidance.classList.toggle("is-active", query.length > 0);
  } else if (query.length === 0) {
    dom.searchGuidance.textContent = "1–2 个字符将使用逐字匹配；3 个以上使用全文索引";
  } else if (query.length <= 2) {
    dom.searchGuidance.textContent = "短关键词将逐字匹配，范围较大时可能稍慢";
    dom.searchGuidance.classList.add("is-warning");
  } else {
    dom.searchGuidance.textContent = "使用本地全文索引搜索消息正文";
    dom.searchGuidance.classList.add("is-active");
  }
}

async function loadList({ reset, autoSelect }) {
  if (!state.snapshotId) {
    return;
  }
  if (String(state.snapshotId).startsWith("pi/")) {
    const payload=await fetchJson(`/api/pi/sessions/${encodeURIComponent(String(state.snapshotId).slice(3))}`);
    const conversation=payload.conversation;
    const text=[conversation.title,...conversation.current_branch.map(n=>n.message?.text||"")].join("\n");
    state.listItems=text.toLowerCase().includes(state.searchQuery.toLowerCase()) ? normalizeConversationItems([conversation]) : [];
    state.listTotal=state.listItems.length;state.listNextOffset=null;state.listLoading=false;
    renderList();
    if(autoSelect && state.listItems.length)await selectConversation(conversation.identity_key);
    return;
  }

  state.listController?.abort();
  const controller = new AbortController();
  state.listController = controller;
  state.listLoading = true;
  state.listError = null;
  dom.retryList.hidden = true;
  dom.listLoading.hidden = false;
  dom.loadMore.hidden = true;
  dom.conversationList.setAttribute("aria-busy", "true");
  if (reset) {
    state.listItems = [];
    state.listTotal = 0;
    state.listNextOffset = null;
    renderListLoading();
  }

  const offset = reset ? 0 : state.listNextOffset;
  if (offset === null && !reset) {
    state.listLoading = false;
    return;
  }

  const searching = state.searchQuery.length > 0;
  const url = state.libraryMode && state.listKind === "conversations"
    ? withQuery(API.library, { q: state.searchQuery, limit: PAGE_SIZE, offset })
    : state.listKind === "groups"
    ? withQuery(API.groupThreads, {
        q: state.searchQuery,
        snapshot_id: state.snapshotId,
        limit: PAGE_SIZE,
        offset
      })
    : searching
      ? withQuery(API.search, { q: state.searchQuery, snapshot_id: state.snapshotId, limit: PAGE_SIZE, offset })
      : withQuery(API.conversations, { snapshot_id: state.snapshotId, limit: PAGE_SIZE, offset });

  try {
    const payload = await fetchJson(url, { signal: controller.signal });
    if (controller !== state.listController) {
      return;
    }
    const incoming = state.libraryMode || state.listKind === "groups"
      ? normalizeConversationItems(payload.items)
      : searching
        ? normalizeSearchItems(payload.items)
        : normalizeConversationItems(payload.items);
    state.listItems = reset ? incoming : state.listItems.concat(incoming);
    state.listTotal = Number(payload.pagination?.total ?? state.listItems.length);
    state.listNextOffset = payload.pagination?.next_offset ?? null;
    state.searchMode = payload.mode ?? null;
    state.searchGuidance = payload.guidance ?? null;
    if (payload.snapshot) {
      state.snapshot = { ...state.snapshot, ...payload.snapshot };
    }
    renderList();
    if (searching) {
      updateSearchGuidance(state.searchQuery, state.searchGuidance, state.searchMode);
    }

    if (autoSelect && !state.activeIdentityKey && state.listItems[0]?.identity_key) {
      await selectConversation(state.listItems[0].identity_key);
    } else if (autoSelect && state.listItems.length === 0) {
      renderReaderEmpty(
        state.listKind === "groups" ? "这个快照没有群聊" : "这个快照没有会话",
        "可以从“来源与快照”切换到其他已导入快照。"
      );
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      state.listError = error;
      renderListError(error);
    }
  } finally {
    if (controller === state.listController) {
      state.listLoading = false;
      dom.listLoading.hidden = true;
      dom.conversationList.setAttribute("aria-busy", "false");
      dom.loadMore.hidden = state.listNextOffset === null || Boolean(state.listError);
    }
  }
}

async function selectConversation(identityKey, options = {}) {
  if (!identityKey || (!options.force && identityKey === state.activeIdentityKey && state.conversation)) {
    showView("reader");
    toggleSidebar(false);
    return;
  }

  state.detailController?.abort();
  const controller = new AbortController();
  state.detailController = controller;
  state.activeIdentityKey = identityKey;
  state.conversation = null;
  state.selectedNode = null;
  renderList();
  showView("reader");
  toggleSidebar(false);
  renderReaderLoading();
  updateTopbar();

  const collection = state.listKind === "groups" ? API.groupThreads : API.conversations;
  const path = identityKey.startsWith("pi/") ? `/api/pi/sessions/${encodeURIComponent(identityKey.slice(3))}` : `${collection}/${encodeURIComponent(identityKey)}`;
  const itemSnapshot = state.libraryMode ? state.listItems.find(item => item.identity_key === identityKey)?.snapshot_id : null;
  const url = withQuery(path, { snapshot_id: itemSnapshot ?? state.snapshotId });
  try {
    const payload = await fetchJson(url, { signal: controller.signal });
    if (controller !== state.detailController) {
      return;
    }
    state.snapshot = { ...state.snapshot, ...(payload.snapshot || {}) };
    state.conversation = normalizeConversationDetail(payload.conversation || payload);
    state.selectedNode = [...state.conversation.current_branch].reverse()
      .find((node) => node.message)?.node_identity_key ?? null;
    renderConversation();
    renderDrawer();
    updateTopbar();
    dom.readerScroll.scrollTo({ top: 0, behavior: "auto" });
    const requestedMessage = options.messageId || await savedPosition(state.snapshot.snapshot_key, identityKey);
    if (requestedMessage) {
      const target = [...dom.messageList.querySelectorAll("[data-message-identity-key]")]
        .find(item => item.dataset.messageIdentityKey === requestedMessage);
      if (!target && options.messageId) throw new Error("引用消息不在该快照的当前主线中，请核对来源链接。");
      if (target) {
      target.classList.add("is-citation-target");
      target.tabIndex = -1;
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: "center", behavior: "auto" });
      }
    }
    trackPosition(dom.readerScroll, state.conversation.current_branch);
  } catch (error) {
    if (error.name !== "AbortError") {
      renderReaderError(error, () => selectConversation(identityKey, { ...options, force: true }));
    }
  }
}

async function switchSnapshot(snapshotId) {
  if (snapshotId === state.snapshotId && !state.libraryMode) {
    return;
  }
  state.libraryMode = snapshotId === "all";
  state.snapshotId = state.libraryMode ? state.snapshots[0].id : snapshotId;
  state.snapshot = findSnapshot(state.snapshotId);
  state.listKind = "conversations";
  dom.archiveScope.value = state.libraryMode ? "all" : String(snapshotId);
  state.activeIdentityKey = null;
  state.conversation = null;
  state.selectedNode = null;
  state.analytics = null;
  state.analyticsSnapshotId = null;
  state.searchQuery = "";
  dom.searchInput.value = "";
  dom.clearSearch.hidden = true;
  dom.searchShortcut.hidden = false;
  updateSearchGuidance("");
  renderSnapshotSummary();
  renderDrawer();
  toggleDrawer(false);
  showView("reader");
  await loadList({ reset: true, autoSelect: true });
}

async function loadAnalytics() {
  if (!state.snapshotId) {
    renderAnalyticsEmpty("尚无可分析的快照");
    return;
  }
  if (state.analytics && state.analyticsSnapshotId === state.snapshotId) {
    renderAnalytics();
    return;
  }
  dom.analyticsState.hidden = false;
  dom.analyticsContent.hidden = true;
  setCenterState(dom.analyticsState, "loading", "正在计算归档指标", "所有计算均在规范化记录上完成。", null);
  try {
    state.analytics = await fetchJson(withQuery(API.analytics, { snapshot_id: state.snapshotId }));
    state.analyticsSnapshotId = state.snapshotId;
    renderAnalytics();
  } catch (error) {
    setCenterState(dom.analyticsState, "error", "无法读取归档指标", error.message, () => loadAnalytics());
  }
}

function renderListLoading() {
  dom.listSummary.textContent = state.searchQuery
    ? "正在搜索消息…"
    : state.listKind === "groups" ? "正在读取群聊…" : "正在读取会话…";
  dom.conversationList.replaceChildren(
    createListSkeleton(),
    createListSkeleton(),
    createListSkeleton(),
    createListSkeleton()
  );
}

function createListSkeleton() {
  const item = element("div", "conversation-item list-skeleton");
  const copy = element("span", "conversation-item-copy");
  copy.append(element("span", "skeleton-line wide"), element("span", "skeleton-line"));
  item.append(copy);
  return item;
}

function renderList() {
  const items = state.listItems;
  dom.retryList.hidden = true;
  dom.conversationList.replaceChildren();

  if (items.length === 0) {
    renderListEmpty(
      state.searchQuery
        ? "没有匹配的消息"
        : state.listKind === "groups" ? "这个快照没有群聊" : "这个快照没有会话",
      state.searchQuery ? "换一个关键词，或减少关键词长度后再试。" : "尝试切换到其他快照。"
    );
    return;
  }

  let lastGroup = null;
  for (const item of items) {
    const group = dateGroup(item.update_time ?? item.create_time);
    if (group !== lastGroup) {
      dom.conversationList.append(element("div", "date-heading", group));
      lastGroup = group;
    }
    dom.conversationList.append(renderConversationItem(item));
  }

  const noun = state.searchQuery
    ? (state.libraryMode ? "个匹配会话" : "个匹配片段")
    : state.listKind === "groups" ? "个群聊" : "条会话";
  dom.listSummary.textContent = `${formatNumber(items.length)} / ${formatNumber(state.listTotal)} ${noun}`;
  dom.loadMore.hidden = state.listNextOffset === null || state.listLoading;
}

function renderConversationItem(item) {
  const button = element("button", "conversation-item");
  button.type = "button";
  button.dataset.identityKey = item.identity_key;
  button.classList.toggle("is-active", item.identity_key === state.activeIdentityKey);
  if (item.identity_key === state.activeIdentityKey) {
    button.setAttribute("aria-current", "page");
  }
  button.addEventListener("click", () => selectConversation(item.identity_key));

  const copy = element("span", "conversation-item-copy");
  copy.append(
    element("span", "conversation-item-title", item.title || "无标题会话"),
    element("span", "conversation-item-preview", item.preview || (item.snapshot_id ? findSnapshot(item.snapshot_id)?.captured_at?.slice(0, 10) || "" : ""))
  );

  const flags = element("span", "conversation-flags");
  const stats = item.stats || {};
  if (item.document_kind === "group_thread") {
    flags.append(flag("◎", "ChatGPT 群聊"));
  }
  if (Number(stats.attachments) > 0) {
    flags.append(flag("⌕", `${formatNumber(stats.attachments)} 个附件`));
  }
  if (Number(stats.branch_points) > 0) {
    flags.append(flag("⌘", `${formatNumber(stats.branch_points)} 个分支点`));
  }
  if (Number(stats.unresolved_assets) > 0) {
    flags.append(flag("!", `${formatNumber(stats.unresolved_assets)} 个附件未解析`));
  }
  if (item.search_role) {
    flags.append(flag("·", roleLabel(item.search_role)));
  }
  copy.append(flags);
  button.append(copy, element("span", "conversation-item-time", formatCompactDate(item.update_time ?? item.create_time)));
  return button;
}

function flag(icon, label) {
  const node = element("span", "mini-flag");
  node.title = label;
  node.append(element("span", null, icon), document.createTextNode(label));
  return node;
}

function renderListEmpty(title, detail) {
  const empty = element("div", "empty-inline");
  empty.append(element("strong", null, title), element("br"), document.createTextNode(detail));
  dom.conversationList.replaceChildren(empty);
  dom.listSummary.textContent = "0 条结果";
  dom.listLoading.hidden = true;
  dom.loadMore.hidden = true;
  dom.conversationList.setAttribute("aria-busy", "false");
}

function renderListError(error) {
  state.listError = error;
  renderListEmpty(
    state.listKind === "groups" ? "无法读取群聊列表" : "无法读取会话列表",
    error.message
  );
  dom.listSummary.textContent = "列表读取失败";
  dom.retryList.hidden = false;
}

function renderReaderLoading() {
  dom.conversation.hidden = true;
  dom.readerState.hidden = false;
  setCenterState(
    dom.readerState,
    "loading",
    state.listKind === "groups" ? "正在读取群聊" : "正在读取会话",
    state.listKind === "groups" ? "正在按来源顺序还原群聊消息。" : "正在还原当前主线与替代分支。",
    null
  );
}

function renderReaderEmpty(title, detail) {
  dom.conversation.hidden = true;
  dom.readerState.hidden = false;
  dom.snapshotBanner.hidden = true;
  setCenterState(dom.readerState, "empty", title, detail, null);
}

function renderReaderError(error, retry) {
  dom.conversation.hidden = true;
  dom.readerState.hidden = false;
  dom.snapshotBanner.hidden = true;
  setCenterState(dom.readerState, "error", "无法读取这条会话", error.message, retry);
}

function renderConversation() {
  const conversation = state.conversation;
  if (!conversation) {
    renderReaderEmpty(
      state.listKind === "groups" ? "请选择一个群聊" : "请选择一条会话",
      "左侧列表保留原快照中观察到的记录。"
    );
    return;
  }
  dom.readerState.hidden = true;
  dom.conversation.hidden = false;
  dom.snapshotBanner.hidden = false;
  dom.snapshotBannerCopy.textContent = conversation.kind === "group_thread"
    ? `${snapshotLabel(state.snapshot)} · ${formatNumber(conversation.current_branch.length)} 条群聊消息`
    : `${snapshotLabel(state.snapshot)} · 当前分支 ${formatNumber(conversation.current_branch.length)} 个节点`;
  dom.snapshotBannerStatus.textContent = "证据可回溯";
  dom.conversationEyebrow.textContent = conversation.kind === "group_thread"
    ? `${sourceKindLabel(state.snapshot?.source?.kind)} · GROUP CHAT`
    : `${sourceKindLabel(state.snapshot?.source?.kind)} · 当前主线`;
  dom.conversationTitle.textContent = conversation.title || "无标题会话";
  dom.conversationSummary.textContent = "";
  renderConversationBadges(conversation);
  renderMessages(conversation);
}

function renderConversationBadges(conversation) {
  const stats = conversation.stats || {};
  const badges = [
    badge(`${formatNumber(stats.messages ?? countMessages(conversation.current_branch))} 条消息`, "accent")
  ];
  if (conversation.kind === "group_thread") {
    badges.push(badge("群聊线性记录"));
  } else {
    badges.push(
      badge(`${formatNumber(stats.branch_points ?? countBranchPoints(conversation.current_branch))} 个分支点`)
    );
  }
  if (Number(stats.attachments) > 0) {
    badges.push(badge(`${formatNumber(stats.attachments)} 个附件`));
  }
  if (Number(stats.unresolved_assets) > 0) {
    badges.push(badge(`${formatNumber(stats.unresolved_assets)} 个附件待解析`, "amber"));
  }
  badges.push(badge(`更新于 ${formatDateTime(conversation.update_time ?? conversation.create_time)}`));
  dom.conversationBadges.replaceChildren(...badges);
}

function badge(text, tone = null) {
  const node = element("span", "badge", text);
  if (tone) {
    node.classList.add(`is-${tone}`);
  }
  return node;
}

function renderMessages(conversation) {
  dom.messageList.replaceChildren();
  const current = conversation.current_branch;
  if (current.length === 0) {
    dom.messageList.append(element("div", "empty-inline", "当前快照没有可显示的主线消息。"));
    return;
  }

  const alternatives = conversation.alternative_nodes;
  const altByParent = groupAlternatives(alternatives);
  const rootAlternatives = altByParent.get(null) || [];
  if (rootAlternatives.length > 0) {
    dom.messageList.append(renderAlternativeModule("会话根节点", rootAlternatives, alternatives));
  }

  for (const node of current) {
    if (node.message) {
      dom.messageList.append(renderMessage(node));
    }
    const directAlternatives = altByParent.get(node.node_identity_key) || [];
    if (directAlternatives.length > 0) {
      dom.messageList.append(renderAlternativeModule(node.node_identity_key, directAlternatives, alternatives));
    }
  }
}

function renderMessage(node) {
  const message = node.message;
  const role = normalizeRole(message.role);
  const article = element("article", `message role-${role}`);
  article.dataset.nodeIdentityKey = node.node_identity_key || "";
  article.dataset.messageIdentityKey = message.identity_key || "";
  article.append(element("div", "message-avatar", roleAvatar(role)));

  const body = element("div", "message-body");
  const meta = element("div", "message-meta");
  meta.append(
    element("strong", null, message.name || roleLabel(role)),
    element("time", null, formatDateTime(message.create_time)),
    element("span", "message-current", "当前主线")
  );
  if (message.model) {
    meta.append(element("span", null, String(message.model)));
  }
  body.append(meta);

  const text = messageText(message);
  const surface = element("div", "message-surface markdown-body");
  surface.append(renderMarkdown(text || contentFallback(message)));
  body.append(surface);

  const attachments = Array.isArray(message.attachments) ? message.attachments : [];
  if (attachments.length > 0) {
    body.append(renderAttachments(attachments));
  }

  const actions = element("div", "message-actions");
  const copy = element("button", "message-action", "复制");
  copy.type = "button";
  copy.addEventListener("click", () => copyText(text, "消息内容已复制"));
  const source = element("button", "message-action", "来源");
  source.type = "button";
  source.addEventListener("click", () => {
    state.selectedNode = node.node_identity_key;
    setDrawerTab("provenance");
    toggleDrawer(true);
  });
  actions.append(copy, source);
  addMessageActions(actions, message, state.conversation?.title || "");
  body.append(actions);
  article.append(body);
  return article;
}

function renderAttachments(attachments) {
  const list = element("div", "attachment-list");
  for (const attachment of attachments) {
    const safeDownload = sameOriginUrl(attachment.download_url);
    const card = element(safeDownload ? "a" : "div", "attachment-card");
    if (safeDownload) {
      card.href = safeDownload;
      card.setAttribute("download", "");
    }
    const type = mimeLabel(attachment.mime, attachment.name);
    card.append(element("span", "attachment-icon", type));
    const copy = element("span", "attachment-copy");
    copy.append(
      element("strong", null, attachment.name || "未命名附件"),
      element("small", null, [formatBytes(attachment.size_bytes), shortHash(attachment.sha256)].filter(Boolean).join(" · "))
    );
    const status = attachment.status === "resolved" ? "原件已关联" : attachment.status || "未解析";
    card.append(copy, element("span", "attachment-status", status));
    list.append(card);
    if (safeDownload && /^(image\/(png|jpeg|gif|webp|avif))$/.test(attachment.mime || "")) {
      const image = element("img", "attachment-preview");
      image.src = safeDownload; image.alt = attachment.name || "图片附件";
      image.loading = "lazy";
      const open = element("a"); open.href = safeDownload; open.target = "_blank";
      open.append(image); list.append(open);
    }
  }
  return list;
}

function renderAlternativeModule(parentKey, roots, allAlternatives) {
  const details = element("details", "alternatives");
  const summary = element("summary");
  const count = countAlternativeSubtree(roots, allAlternatives);
  const main = element("span", null, `${formatNumber(count)} 个替代节点`);
  main.append(element("span", "alternatives-copy", ` · ${shortIdentity(parentKey)} 后的未选中分支`));
  summary.append(main);
  details.append(summary);

  const list = element("div", "alternative-list");
  const map = new Map(allAlternatives.map((node) => [node.node_identity_key, node]));
  for (const root of roots) {
    appendAlternativeTree(list, root, map, new Set(), 0);
  }
  details.append(list);
  return details;
}

function appendAlternativeTree(container, node, map, visited, depth) {
  if (!node || visited.has(node.node_identity_key)) {
    return;
  }
  const nextVisited = new Set(visited);
  nextVisited.add(node.node_identity_key);
  const card = element("article", "alternative-card");
  const header = element("header");
  header.append(
    element("strong", null, node.message ? roleLabel(node.message.role) : "空节点"),
    element("span", "mono", `${depth > 0 ? `↳ ${depth} · ` : ""}${shortIdentity(node.node_identity_key)}`)
  );
  card.append(header, element("p", null, node.message ? messageText(node.message) || contentFallback(node.message) : "此节点没有消息正文。"));
  card.addEventListener("click", () => {
    state.selectedNode = node.node_identity_key;
    setDrawerTab("provenance");
    toggleDrawer(true);
  });
  container.append(card);

  for (const childKey of Array.isArray(node.child_options) ? node.child_options : []) {
    appendAlternativeTree(container, map.get(childKey), map, nextVisited, depth + 1);
  }
}

function renderDrawer() {
  dom.drawerState.replaceChildren();
  if (state.drawerTab === "snapshots") {
    renderSnapshotsDrawer();
  } else {
    renderProvenanceDrawer();
  }
}

function renderProvenanceDrawer() {
  const conversation = state.conversation;
  if (!conversation) {
    dom.drawerState.append(element(
      "div",
      "empty-inline",
      state.listKind === "groups"
        ? "选择一个群聊后，这里会显示快照、源文件与消息定位信息。"
        : "选择一条会话后，这里会显示快照、源文件与节点定位信息。"
    ));
    return;
  }
  const selected = findNode(conversation, state.selectedNode) || conversation.current_branch.find((node) => node.message) || conversation.current_branch[0];
  const message = selected?.message || null;
  const section = element("section", "drawer-section");
  section.append(element("div", "provenance-status", "●  来源链可回溯"));
  section.append(keyValueList([
    [conversation.kind === "group_thread" ? "群聊标识" : "会话标识", conversation.identity_key],
    [conversation.kind === "group_thread" ? "原生群聊 ID" : "原生会话 ID", conversation.native_id],
    [conversation.kind === "group_thread" ? "所选消息节点" : "所选节点", selected?.node_identity_key],
    [conversation.kind === "group_thread" ? "原生消息 ID" : "原生节点 ID", selected?.native_id],
    ["消息标识", message?.identity_key],
    ["内容类型", message?.content_type],
    ["模型标签", message?.model]
  ], true));
  dom.drawerState.append(section);

  const sourceSection = element("section", "drawer-section");
  sourceSection.append(element("h2", null, "来源定位"));
  sourceSection.append(keyValueList([
    ["快照", state.snapshot?.snapshot_key],
    ["捕获时间", formatDateTime(state.snapshot?.captured_at)],
    ["源文件", conversation.source?.source_file_path],
    ["数组位置", conversation.source?.array_index],
    ["JSON Pointer", conversation.source?.json_pointer]
  ], true));
  const copy = element("button", "copy-reference", "复制来源引用");
  copy.type = "button";
  copy.addEventListener("click", () => copyText(sourceReference(conversation, selected), "来源引用已复制"));
  sourceSection.append(copy);
  dom.drawerState.append(sourceSection);

  if (message?.attachments?.length) {
    const attachmentSection = element("section", "drawer-section");
    attachmentSection.append(element("h2", null, "附件引用"));
    for (const attachment of message.attachments) {
      attachmentSection.append(keyValueList([
        ["名称", attachment.name],
        ["解析状态", attachment.status],
        ["MIME", attachment.mime],
        ["大小", formatBytes(attachment.size_bytes)],
        ["SHA-256", attachment.sha256]
      ], true));
    }
    dom.drawerState.append(attachmentSection);
  }

  if (conversation.diagnostics?.length) {
    const diagnosticsSection = element("section", "drawer-section");
    diagnosticsSection.append(element("h2", null, "导入诊断"));
    for (const diagnostic of conversation.diagnostics) {
      diagnosticsSection.append(keyValueList([
        ["级别", diagnostic.severity],
        ["代码", diagnostic.code],
        ["详情", diagnostic.details]
      ], true));
    }
    dom.drawerState.append(diagnosticsSection);
  }
}

function renderSnapshotsDrawer() {
  if (state.snapshots.length === 0) {
    dom.drawerState.append(element("div", "empty-inline", "尚无已导入快照。"));
    return;
  }
  const intro = element("section", "drawer-section");
  intro.append(
    element("h2", null, "快照语义"),
    element("p", "drawer-note", "切换快照会按当时观察到的状态重建会话。某条记录未出现在后续快照中，只表示源缺席。")
  );
  dom.drawerState.append(intro);

  const list = element("div", "snapshot-list");
  const conversationStates = new Map(
    (state.conversation?.snapshot_observations || []).map((item) => [String(item.id), item.state])
  );
  for (const snapshot of state.snapshots) {
    const card = element("article", "snapshot-card");
    const header = element("header");
    const conversationState = conversationStates.get(String(snapshot.id));
    const stateLabel = conversationState === "present"
      ? state.conversation?.kind === "group_thread" ? "群聊可见" : "会话可见"
      : conversationState === "absent_in_snapshot"
        ? "源缺席（非删除）"
        : conversationState === "unknown"
          ? "未观察状态"
          : snapshot.id === state.snapshotId ? "正在阅读" : "可切换";
    header.append(
      element("strong", null, snapshot.snapshot_key || `快照 ${snapshot.id}`),
      element(
        "span",
        `snapshot-state${conversationState === "absent_in_snapshot" ? " is-absent" : ""}`,
        stateLabel
      )
    );
    card.append(header, element("time", null, formatDateTime(snapshot.captured_at)));
    const counts = snapshot.counts || {};
    card.append(element("p", null, `${formatNumber(counts.conversations)} 条会话 · ${formatNumber(counts.messages)} 条消息 · ${formatNumber(counts.warnings)} 个导入警告`));
    const footer = element("footer");
    footer.append(
      element("span", null, `${sourceKindLabel(snapshot.source?.kind)} · ${snapshot.source?.provider || "未知来源"}`),
      element("span", null, `${formatNumber(snapshot.quality?.source_absences)} 个源缺席`)
    );
    card.append(footer);
    if (snapshot.id !== state.snapshotId) {
      const use = element("button", "copy-reference", "使用这个快照");
      use.type = "button";
      use.addEventListener("click", () => switchSnapshot(snapshot.id));
      card.append(use);
    }
    list.append(card);
  }
  dom.drawerState.append(list);
}

function renderSnapshotSummary(error = null) {
  const piSession=String(state.snapshotId).startsWith("pi/");
  dom.showGroups.disabled=piSession;
  dom.showAnalytics.disabled=piSession;
  if (error) {
    dom.snapshotSummaryTitle.textContent = "无法读取快照";
    dom.snapshotSummaryMeta.textContent = error.message;
    return;
  }
  if (state.libraryMode) {
    dom.snapshotSummaryTitle.textContent = "全部会话";
    dom.snapshotSummaryMeta.textContent = `${state.snapshots.length} 份导出 · 按会话保留最新版本`;
    return;
  }
  const snapshot = state.snapshot;
  if (!snapshot) {
    dom.snapshotSummaryTitle.textContent = "没有可用快照";
    dom.snapshotSummaryMeta.textContent = "等待首次导入";
    return;
  }
  dom.snapshotSummaryTitle.textContent = `${snapshot.snapshot_key || `快照 ${snapshot.id}`} 已载入`;
  dom.snapshotSummaryMeta.textContent = `${formatDateTime(snapshot.captured_at)} · ${formatNumber(snapshot.counts?.conversations)} 条会话`;
}

function renderAnalytics() {
  const data = state.analytics;
  if (!data) {
    renderAnalyticsEmpty("没有可显示的归档指标");
    return;
  }
  dom.analyticsState.hidden = true;
  dom.analyticsContent.hidden = false;
  dom.analyticsContent.replaceChildren();
  const snapshot = findSnapshot(state.snapshotId) || state.snapshot;
  const attachments = data.attachments || {};
  const branches = data.branches || {};

  const metrics = element("section", "metric-grid");
  metrics.append(
    metricCard("会话", snapshot?.counts?.conversations, snapshotLabel(snapshot)),
    metricCard("消息", snapshot?.counts?.messages, "规范化消息记录"),
    metricCard("分支点", branches.branch_points, `${formatNumber(branches.alternative_nodes)} 个替代节点`),
    metricCard("唯一附件", attachments.unique_assets, formatBytes(attachments.total_bytes))
  );
  dom.analyticsContent.append(metrics);

  const grid = element("section", "analytics-grid");
  grid.append(
    activityPanel(data.activity || []),
    distributionPanel("年度活跃", data.activity_years || [], "按消息发生年份", true, "year", "messages"),
    distributionPanel("角色分布", data.roles || [], "规范化作者角色"),
    distributionPanel("内容类型", data.content_types || [], "导出中的 content_type"),
    distributionPanel("模型标签", data.models || [], "来源字段，不作模型推断"),
    branchPanel(branches),
    attachmentPanel(attachments),
    snapshotDeltaPanel(data.snapshot_deltas || []),
    qualityPanel(data.snapshot_quality || {})
  );
  dom.analyticsContent.append(grid);
  dom.analyticsContent.append(element("p", "analytics-footnote", `生成方式：${data.generated_from || "deterministic_sql"}。这些结果只描述归档结构与质量，不是用户画像。`));
}

function metricCard(label, value, note) {
  const card = element("article", "metric-card");
  card.append(
    element("span", "metric-label", label),
    element("strong", "metric-value", formatNumber(value)),
    element("span", "metric-note", note || "—")
  );
  return card;
}

function panel(title, subtitle, wide = false) {
  const card = element("article", `analytics-panel${wide ? " wide" : ""}`);
  const heading = element("header", "panel-heading");
  heading.append(element("h2", null, title), element("span", null, subtitle));
  card.append(heading);
  return card;
}

function activityPanel(activity) {
  const card = panel("月度活跃", "按消息发生月份", true);
  if (activity.length === 0) {
    card.append(element("div", "empty-inline", "没有带可用时间戳的活动记录。"));
    return card;
  }
  const rows = activity.map((entry) => ({ key: entry.month, count: Number(entry.messages || 0), note: `${formatNumber(entry.conversations)} 条会话` }));
  card.append(barList(rows));
  return card;
}

function distributionPanel(title, items, subtitle, wide = false, keyField = "key", countField = "count") {
  const card = panel(title, subtitle, wide);
  if (items.length === 0) {
    card.append(element("div", "empty-inline", "没有可聚合的来源字段。"));
    return card;
  }
  card.append(barList(items.map((item) => ({
    key: item[keyField] || "未标记",
    count: Number(item[countField] || 0)
  }))));
  return card;
}

function branchPanel(branches) {
  const card = panel("分支与编辑结构", "由节点父子关系确定计算", true);
  const table = element("table", "analytics-table");
  const head = element("thead");
  const headRow = element("tr");
  headRow.append(element("th", null, "指标"), element("th", null, "数量"), element("th", null, "说明"));
  head.append(headRow);
  const body = element("tbody");
  const rows = [
    ["含分支会话", branches.conversations_with_branches, `${formatPercent(branches.conversation_branch_rate)} 的会话`],
    ["分支点", branches.branch_points, "同一父节点存在多个子节点"],
    ["替代节点", branches.alternative_nodes, "不在当前主线中"],
    ["助手重生成节点", branches.regenerated_assistant_nodes, "同父节点助手替代项"],
    ["用户编辑节点", branches.edited_user_nodes, "同父节点用户替代项"]
  ];
  for (const values of rows) {
    const row = element("tr");
    row.append(
      element("td", null, values[0]),
      element("td", null, formatNumber(values[1])),
      element("td", null, values[2])
    );
    body.append(row);
  }
  table.append(head, body);
  card.append(table);
  return card;
}

function barList(items) {
  const list = element("div", "bar-list");
  const max = Math.max(1, ...items.map((item) => item.count));
  for (const item of items.slice(0, 18)) {
    const row = element("div", "bar-row");
    const progress = document.createElement("progress");
    progress.max = max;
    progress.value = item.count;
    progress.setAttribute("aria-label", `${item.key}: ${item.count}`);
    row.append(
      element("span", null, item.key || "未标记"),
      progress,
      element("strong", null, `${formatNumber(item.count)}${item.note ? ` · ${item.note}` : ""}`)
    );
    list.append(row);
  }
  return list;
}

function attachmentPanel(attachments) {
  const card = panel("附件健康度", "引用解析状态");
  card.append(barList([
    { key: "已解析", count: Number(attachments.resolved || 0) },
    { key: "未解析", count: Number(attachments.unresolved || 0) },
    { key: "存在歧义", count: Number(attachments.ambiguous || 0) }
  ]));
  card.append(element("p", "analytics-footnote", `${formatNumber(attachments.references)} 个引用关联到 ${formatNumber(attachments.unique_assets)} 个唯一资产，共 ${formatBytes(attachments.total_bytes)}。`));
  return card;
}

function snapshotDeltaPanel(snapshots) {
  const card = panel("快照差异", "新增、内容变化与源缺席分开计算", true);
  if (!snapshots.length) {
    card.append(element("div", "empty-inline", "没有可比较的同来源快照。"));
    return card;
  }
  const table = element("table", "analytics-table");
  const head = element("thead");
  const headRow = element("tr");
  headRow.append(
    element("th", null, "快照"),
    element("th", null, "会话 + / ~ / 缺席"),
    element("th", null, "消息 + / ~ / 缺席"),
    element("th", null, "附件 + / 缺席")
  );
  head.append(headRow);
  const body = element("tbody");
  for (const snapshot of snapshots) {
    const row = element("tr");
    row.append(
      element("td", null, snapshot.snapshot_key || String(snapshot.snapshot_id)),
      element("td", null, deltaText(snapshot.conversations)),
      element("td", null, deltaText(snapshot.messages)),
      element("td", null, `+${formatNumber(snapshot.assets?.added)} / ${formatNumber(snapshot.assets?.absent)}`)
    );
    body.append(row);
  }
  table.append(head, body);
  card.append(table, element("p", "analytics-footnote", "缺席只表示该快照未观察到记录，不等同于提供方删除。"));
  return card;
}

function qualityPanel(quality) {
  const card = panel("快照质量", "声明、诊断与源缺席");
  const table = element("table", "analytics-table");
  const head = element("thead");
  const headRow = element("tr");
  headRow.append(element("th", null, "检查"), element("th", null, "状态"), element("th", null, "数量"));
  head.append(headRow);
  const body = element("tbody");
  const diagnostics = quality.diagnostics || {};
  const rows = [
    ["诊断警告", diagnostics.warnings ? "需留意" : "通过", diagnostics.warnings],
    ["诊断错误", diagnostics.errors ? "失败" : "通过", diagnostics.errors],
    ["源缺席", "非删除", quality.source_absences]
  ];
  for (const row of rows) {
    const tr = element("tr");
    for (const value of row) {
      tr.append(element("td", null, typeof value === "number" ? formatNumber(value) : String(value ?? "—")));
    }
    body.append(tr);
  }
  table.append(head, body);
  card.append(table);

  const claims = Array.isArray(quality.claims) ? quality.claims : [];
  if (claims.length) {
    const claimSummary = claims.map((claim) => `${claim.claim || claim.name || "检查"}: ${claim.status || "未知"}`).join(" · ");
    card.append(element("p", "analytics-footnote", claimSummary));
  }
  const coverage = Array.isArray(quality.source_file_coverage) ? quality.source_file_coverage : [];
  if (coverage.length) {
    const coverageSummary = coverage.map((item) => (
      `${item.key || "other"}/${item.handling_status}: ${formatNumber(item.files)} 文件`
    )).join(" · ");
    card.append(element("p", "analytics-footnote", coverageSummary));
  }
  card.append(element("p", "analytics-footnote", "显式来源删除：未知（当前规范化结构没有该事件字段）。"));
  return card;
}

function renderAnalyticsEmpty(message) {
  dom.analyticsContent.hidden = true;
  dom.analyticsState.hidden = false;
  setCenterState(dom.analyticsState, "empty", message, "导入规范化记录后再查看洞察。", null);
}

function setCenterState(container, kind, title, detail, retry) {
  const children = [];
  if (kind === "loading") {
    children.push(element("span", "spinner"));
  } else {
    children.push(element("span", `state-symbol is-${kind}`, kind === "error" ? "!" : "○"));
  }
  children.push(element("strong", null, title));
  if (detail) {
    children.push(element("span", null, detail));
  }
  if (retry) {
    const button = element("button", null, "重试");
    button.type = "button";
    button.addEventListener("click", retry);
    children.push(button);
  }
  container.replaceChildren(...children);
}

function normalizeConversationItems(items) {
  return (Array.isArray(items) ? items : []).map((item) => ({
    ...item,
    identity_key: item.identity_key,
    title: item.title || "无标题会话",
    preview: item.preview || "",
    stats: item.stats || {}
  })).filter((item) => item.identity_key);
}

function normalizeSearchItems(items) {
  return (Array.isArray(items) ? items : []).map((item) => ({
    identity_key: item.conversation_identity_key,
    native_id: null,
    title: item.conversation_title || "无标题会话",
    preview: compactText(item.text),
    create_time: item.create_time,
    update_time: item.create_time,
    search_role: item.role,
    search_document_kind: item.document_kind,
    stats: {}
  })).filter((item) => item.identity_key);
}

function normalizeConversationDetail(conversation) {
  return {
    ...conversation,
    stats: conversation.stats || {},
    source: conversation.source || {},
    current_branch: normalizeNodes(conversation.current_branch),
    alternative_nodes: normalizeNodes(conversation.alternative_nodes)
  };
}

function normalizeNodes(nodes) {
  return (Array.isArray(nodes) ? nodes : []).map((node) => ({
    ...node,
    child_options: Array.isArray(node.child_options) ? node.child_options : [],
    message: node.message ? {
      ...node.message,
      attachments: Array.isArray(node.message.attachments) ? node.message.attachments : []
    } : null
  }));
}

function groupAlternatives(nodes) {
  const map = new Map();
  for (const node of nodes) {
    const key = node.parent_node_identity_key ?? null;
    if (!map.has(key)) {
      map.set(key, []);
    }
    map.get(key).push(node);
  }
  return map;
}

function countAlternativeSubtree(roots, allNodes) {
  const map = new Map(allNodes.map((node) => [node.node_identity_key, node]));
  const visited = new Set();
  const visit = (node) => {
    if (!node || visited.has(node.node_identity_key)) {
      return;
    }
    visited.add(node.node_identity_key);
    for (const child of node.child_options || []) {
      visit(map.get(child));
    }
  };
  roots.forEach(visit);
  return visited.size;
}

function findNode(conversation, nodeIdentityKey) {
  if (!nodeIdentityKey) {
    return null;
  }
  return conversation.current_branch.concat(conversation.alternative_nodes)
    .find((node) => node.node_identity_key === nodeIdentityKey) || null;
}

function countMessages(nodes) {
  return nodes.reduce((total, node) => total + (node.message ? 1 : 0), 0);
}

function countBranchPoints(nodes) {
  return nodes.reduce((total, node) => total + (node.child_options?.length > 1 ? 1 : 0), 0);
}

function updateTopbar() {
  if (state.view !== "reader") {
    return;
  }
  const conversation = state.conversation;
  const listItem = state.listItems.find((item) => item.identity_key === state.activeIdentityKey);
  dom.topbarConversationTitle.textContent = conversation?.title || listItem?.title || "Archive Reader";
  dom.topbarMeta.textContent = conversation
    ? `${sourceKindLabel(state.snapshot?.source?.kind)} · ${formatNumber(conversation.stats?.messages ?? countMessages(conversation.current_branch))} 条消息`
    : state.activeIdentityKey
      ? state.listKind === "groups" ? "正在读取群聊…" : "正在读取会话…"
      : state.listKind === "groups" ? "选择一个群聊开始阅读" : "选择一条会话开始阅读";
}

function findSnapshot(id) {
  return state.snapshots.find((snapshot) => String(snapshot.id) === String(id)) || null;
}

function snapshotLabel(snapshot) {
  if (!snapshot) {
    return "未选择快照";
  }
  return `${snapshot.snapshot_key || `快照 ${snapshot.id}`} · ${formatDateTime(snapshot.captured_at)}`;
}

function sourceReference(conversation, node) {
  const snapshot = state.snapshot?.snapshot_key || state.snapshot?.id || "unknown-snapshot";
  const sourceFile = conversation.source?.source_file_path || "unknown-source";
  const pointer = conversation.source?.json_pointer || "";
  const nodeKey = node?.node_identity_key || "unknown-node";
  return `[${snapshot}/${sourceFile}${pointer} | ${nodeKey}]`;
}

function keyValueList(entries, monoValues = false) {
  const list = element("dl", "kv-list");
  for (const [key, rawValue] of entries) {
    if (rawValue === null || rawValue === undefined || rawValue === "") {
      continue;
    }
    const row = element("div", "kv-row");
    const value = typeof rawValue === "object" ? JSON.stringify(rawValue) : String(rawValue);
    row.append(element("dt", null, key), element("dd", monoValues ? "mono" : null, value));
    list.append(row);
  }
  return list;
}

function messageText(message) {
  if (typeof message?.text === "string") {
    return message.text;
  }
  return "";
}

function contentFallback(message) {
  if (!message) {
    return "此节点没有消息内容。";
  }
  return message.content_type
    ? `此 ${message.content_type} 消息没有可显示的纯文本内容。`
    : "此消息没有可显示的纯文本内容。";
}

function normalizeRole(role) {
  const normalized = String(role || "unknown").toLowerCase();
  if (["user", "assistant", "system", "tool"].includes(normalized)) {
    return normalized;
  }
  return "unknown";
}

function roleLabel(role) {
  return ({ user: "你", assistant: "助手", system: "系统", tool: "工具", unknown: "未知角色" })[normalizeRole(role)];
}

function roleAvatar(role) {
  return ({ user: "你", assistant: "A", system: "S", tool: "T", unknown: "?" })[normalizeRole(role)];
}

function sourceKindLabel(kind) {
  const value = String(kind || "").replaceAll("_", " ");
  if (value.includes("official") || value.includes("export")) {
    return "官方导出";
  }
  if (value.includes("extension")) {
    return "扩展观察";
  }
  return value || "归档来源";
}

function mimeLabel(mime, name) {
  const value = String(mime || "").toLowerCase();
  const suffix = String(name || "").split(".").pop()?.toUpperCase();
  if (value.startsWith("image/")) return "IMG";
  if (value.startsWith("audio/")) return "AUD";
  if (value.startsWith("video/")) return "VID";
  if (value === "application/pdf") return "PDF";
  return suffix && suffix.length <= 4 ? suffix : "FILE";
}

function formatDateTime(value) {
  const date = parseDate(value);
  if (!date) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"
  }).format(date);
}

function formatCompactDate(value) {
  const date = parseDate(value);
  if (!date) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  if (date.getFullYear() === now.getFullYear()) {
    return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { year: "2-digit", month: "numeric", day: "numeric" }).format(date);
}

function dateGroup(value) {
  const date = parseDate(value);
  if (!date) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long" }).format(date);
}

function parseDate(value) {
  if (value === null || value === undefined || value === "") return null;
  let date;
  if (typeof value === "number") {
    date = new Date(value < 1e12 ? value * 1000 : value);
  } else if (/^\d+(\.\d+)?$/.test(String(value))) {
    const numeric = Number(value);
    date = new Date(numeric < 1e12 ? numeric * 1000 : numeric);
  } else {
    date = new Date(value);
  }
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatNumber(value) {
  const number = Number(value ?? 0);
  return Number.isFinite(number) ? new Intl.NumberFormat("zh-CN").format(number) : "0";
}

function formatPercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : "0.0%";
}

function deltaText(delta) {
  return `+${formatNumber(delta?.added)} / ~${formatNumber(delta?.changed)} / ${formatNumber(delta?.absent)}`;
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "大小未知";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = bytes / 1024;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount >= 10 ? amount.toFixed(1) : amount.toFixed(2)} ${units[index]}`;
}

function shortHash(hash) {
  if (!hash) return "";
  const value = String(hash).replace(/^sha256:/, "");
  return `sha256:${value.slice(0, 8)}…${value.slice(-4)}`;
}

function shortIdentity(value) {
  if (!value) return "根节点";
  const text = String(value);
  const tail = text.split("/").pop();
  return tail.length > 17 ? `${tail.slice(0, 8)}…${tail.slice(-5)}` : tail;
}

function compactText(text) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length > 130 ? `${value.slice(0, 127)}…` : value;
}

function sameOriginUrl(rawUrl) {
  if (typeof rawUrl !== "string" || rawUrl.length === 0) return null;
  try {
    const url = new URL(rawUrl, window.location.origin);
    if (url.origin !== window.location.origin || !["http:", "https:"].includes(url.protocol)) {
      return null;
    }
    return url.href;
  } catch (_error) {
    return null;
  }
}

async function copyText(text, successMessage) {
  if (!text) {
    showToast("没有可复制的文本");
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    showToast(successMessage);
  } catch (_error) {
    showToast("浏览器未授权剪贴板，内容未复制");
  }
}

function showToast(message) {
  window.clearTimeout(state.toastTimer);
  dom.toast.textContent = message;
  dom.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => {
    dom.toast.hidden = true;
  }, 2400);
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json" },
    credentials: "same-origin",
    cache: "no-store",
    ...options
  });
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new Error(`本地 API 返回了无法解析的响应（HTTP ${response.status}）`);
  }
  if (!response.ok) {
    const message = payload?.error?.message || `本地 API 请求失败（HTTP ${response.status}）`;
    const error = new Error(message);
    error.code = payload?.error?.code || "api_error";
    error.status = response.status;
    throw error;
  }
  return payload;
}

function withQuery(path, params) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  return `${url.pathname}${url.search}`;
}

function element(tag, className = null, text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null && text !== undefined) node.textContent = String(text);
  return node;
}

function toCamel(value) {
  return value.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}
