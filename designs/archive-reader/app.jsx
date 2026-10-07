const { useEffect, useMemo, useRef, useState } = React;
const {
  archiveMockConversations,
  archiveMockFilterOptions,
  archiveMockInsights,
  Sidebar,
  Reader,
  MetadataDrawer,
  InsightsView,
  ArchiveIcon
} = window;

function ArchiveReaderApp() {
  const initialTheme = (() => {
    try {
      const saved = window.localStorage.getItem("archive-reader-demo-theme");
      if (saved === "light" || saved === "dark") {
        return saved;
      }
    } catch (error) {
      // Storage can be unavailable in hardened browser contexts; the prototype still works.
    }
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  })();

  const [activeId, setActiveId] = useState(archiveMockConversations[0].id);
  const [view, setView] = useState("reader");
  const [insightYear, setInsightYear] = useState("2026");
  const [query, setQuery] = useState("");
  const [activeFilters, setActiveFilters] = useState([]);
  const [filterOpen, setFilterOpen] = useState(false);
  const [theme, setTheme] = useState(initialTheme);
  const [drawerOpen, setDrawerOpen] = useState(() => window.innerWidth > 1190);
  const [drawerTab, setDrawerTab] = useState("provenance");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [branchState, setBranchState] = useState({});
  const [selectedMessageId, setSelectedMessageId] = useState(null);
  const [toast, setToast] = useState("");
  const searchInputRef = useRef(null);

  const activeConversation = useMemo(
    () => archiveMockConversations.find((conversation) => conversation.id === activeId) || archiveMockConversations[0],
    [activeId]
  );

  const visibleConversations = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
    return archiveMockConversations.filter((conversation) => {
      const searchText = [
        conversation.title,
        conversation.preview,
        conversation.summary,
        conversation.tags.join(" "),
        ...conversation.messages.flatMap((message) => [
          ...message.paragraphs,
          ...(message.bullets || []),
          ...(message.branches || []).map((branch) => branch.copy),
          message.attachment ? message.attachment.name : ""
        ])
      ].join(" ").toLocaleLowerCase("zh-CN");

      const matchesQuery = !normalizedQuery || searchText.includes(normalizedQuery);
      const matchesFilters = activeFilters.every((filterId) => {
        const option = archiveMockFilterOptions.find((entry) => entry.id === filterId);
        return option ? Boolean(conversation[option.property]) : true;
      });
      return matchesQuery && matchesFilters;
    });
  }, [query, activeFilters]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem("archive-reader-demo-theme", theme);
    } catch (error) {
      // Theme persistence is a convenience, not a prerequisite.
    }
  }, [theme]);

  useEffect(() => {
    const onKeyDown = (event) => {
      const typing = event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement;
      if (event.key === "/" && !typing) {
        event.preventDefault();
        searchInputRef.current?.focus();
      }
      if (event.key === "Escape") {
        setFilterOpen(false);
        setSidebarOpen(false);
        if (window.innerWidth <= 1190) {
          setDrawerOpen(false);
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (!toast) {
      return undefined;
    }
    const timeout = window.setTimeout(() => setToast(""), 2400);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  const announce = (message) => {
    setToast("");
    window.requestAnimationFrame(() => setToast(message));
  };

  const copyText = async (text, successMessage) => {
    try {
      await navigator.clipboard.writeText(text);
      announce(successMessage);
    } catch (error) {
      announce("当前预览环境未授权剪贴板，内容未复制");
    }
  };

  const handleToggleFilter = (filterId) => {
    setActiveFilters((current) => current.includes(filterId)
      ? current.filter((entry) => entry !== filterId)
      : [...current, filterId]
    );
  };

  const handleSelectConversation = (conversationId) => {
    setActiveId(conversationId);
    setView("reader");
    setSidebarOpen(false);
    setFilterOpen(false);
    setSelectedMessageId(null);
    window.requestAnimationFrame(() => {
      document.getElementById("reader-scroll")?.scrollTo({ top: 0, behavior: "smooth" });
    });
  };

  const handleInspectMessage = (messageId) => {
    setSelectedMessageId(messageId);
    setDrawerTab("provenance");
    setDrawerOpen(true);
  };

  const handleOpenSnapshots = () => {
    setView("reader");
    setDrawerTab("snapshots");
    setDrawerOpen(true);
    setSidebarOpen(false);
  };

  const handleViewChange = (nextView) => {
    setView(nextView);
    setSidebarOpen(false);
    setFilterOpen(false);
    if (nextView === "insights") {
      setDrawerOpen(false);
    }
  };

  return (
    <div
      className={`app-shell ${view === "reader" && drawerOpen ? "drawer-open" : ""} ${sidebarOpen ? "sidebar-open" : ""} ${view === "insights" ? "insights-view-active" : ""}`}
      data-screen-label="Archive Reader desktop prototype"
    >
      <Sidebar
        conversations={visibleConversations}
        activeId={activeId}
        view={view}
        query={query}
        activeFilters={activeFilters}
        filterOptions={archiveMockFilterOptions}
        filterOpen={filterOpen}
        searchInputRef={searchInputRef}
        onQueryChange={setQuery}
        onClearQuery={() => setQuery("")}
        onToggleFilterOpen={() => setFilterOpen((open) => !open)}
        onToggleFilter={handleToggleFilter}
        onSelectConversation={handleSelectConversation}
        onViewChange={handleViewChange}
        onOpenSnapshots={handleOpenSnapshots}
      />

      {view === "reader" ? (
        <Reader
          conversation={activeConversation}
          theme={theme}
          drawerOpen={drawerOpen}
          branchState={branchState}
          onToggleTheme={() => setTheme((current) => current === "light" ? "dark" : "light")}
          onToggleDrawer={() => setDrawerOpen((open) => !open)}
          onOpenSidebar={() => setSidebarOpen(true)}
          onToggleBranch={(messageId) => setBranchState((current) => ({
            ...current,
            [messageId]: !current[messageId]
          }))}
          onCopyMessage={(message) => copyText(message.paragraphs.join("\n\n"), "模拟消息已复制")}
          onInspectMessage={handleInspectMessage}
          onOpenAttachment={(attachment) => announce(`${attachment.name}：原型仅演示附件入口`)}
        />
      ) : (
        <InsightsView
          data={archiveMockInsights}
          theme={theme}
          year={insightYear}
          onYearChange={setInsightYear}
          onToggleTheme={() => setTheme((current) => current === "light" ? "dark" : "light")}
          onOpenSidebar={() => setSidebarOpen(true)}
          onReviewExperiments={() => announce("演示：3 个 LLM 候选等待人工审核")}
        />
      )}

      {view === "reader" ? (
        <MetadataDrawer
          conversation={activeConversation}
          tab={drawerTab}
          selectedMessageId={selectedMessageId}
          onTabChange={setDrawerTab}
          onClose={() => setDrawerOpen(false)}
          onCopyReference={() => copyText(
            `[${activeConversation.provenance.sourceSnapshot}/${activeConversation.provenance.conversationId}/${selectedMessageId || activeConversation.provenance.selectedNodeId}]`,
            "模拟来源引用已复制"
          )}
        />
      ) : null}

      <button
        className="mobile-backdrop sidebar-backdrop"
        type="button"
        aria-label="关闭会话列表"
        onClick={() => setSidebarOpen(false)}
      ></button>
      {view === "reader" ? <button
        className="mobile-backdrop drawer-backdrop"
        type="button"
        aria-label="关闭来源抽屉"
        onClick={() => setDrawerOpen(false)}
      ></button> : null}

      {toast ? (
        <div className="toast" role="status" aria-live="polite">
          <ArchiveIcon name="check" />
          {toast}
        </div>
      ) : null}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<ArchiveReaderApp />);
