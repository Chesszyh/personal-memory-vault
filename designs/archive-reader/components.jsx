const { ArchiveIcon } = window;

function Sidebar({
  conversations,
  activeId,
  view,
  query,
  activeFilters,
  filterOptions,
  filterOpen,
  searchInputRef,
  onQueryChange,
  onClearQuery,
  onToggleFilterOpen,
  onToggleFilter,
  onSelectConversation,
  onViewChange,
  onOpenSnapshots
}) {
  const groups = conversations.reduce((result, conversation) => {
    const existing = result.find((entry) => entry.label === conversation.dateGroup);
    if (existing) {
      existing.items.push(conversation);
    } else {
      result.push({ label: conversation.dateGroup, items: [conversation] });
    }
    return result;
  }, []);

  return (
    <aside className={`sidebar ${view === "insights" ? "insights-mode" : ""}`} aria-label="归档导航">
      <header className="sidebar-header">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">AR</div>
          <div className="brand-copy">
            <span className="brand-name">Archive Reader</span>
            <span className="brand-subtitle">Local evidence view</span>
          </div>
        </div>
        <button className="icon-button" type="button" aria-label="归档设置（演示）">
          <ArchiveIcon name="more" />
        </button>
      </header>

      <div className="workspace-switch" role="tablist" aria-label="归档工作区">
        <button
          className={`workspace-tab ${view === "reader" ? "is-active" : ""}`}
          type="button"
          role="tab"
          aria-selected={view === "reader"}
          onClick={() => onViewChange("reader")}
        >
          <ArchiveIcon name="archive" size={15} />
          阅读
        </button>
        <button
          className={`workspace-tab ${view === "insights" ? "is-active" : ""}`}
          type="button"
          role="tab"
          aria-selected={view === "insights"}
          onClick={() => onViewChange("insights")}
        >
          <ArchiveIcon name="chart" size={15} />
          洞察
        </button>
      </div>

      <div className="sidebar-search-wrap">
        <label className="search-box">
          <ArchiveIcon name="search" />
          <input
            ref={searchInputRef}
            type="search"
            value={query}
            placeholder="搜索标题、正文或标签"
            aria-label="搜索会话"
            onChange={(event) => onQueryChange(event.target.value)}
          />
          {query ? (
            <button className="clear-search" type="button" aria-label="清空搜索" onClick={onClearQuery}>
              <ArchiveIcon name="close" size={14} />
            </button>
          ) : (
            <span className="shortcut-key" aria-hidden="true">/</span>
          )}
        </label>
      </div>

      <div className="filter-row">
        <span className="filter-summary">
          {conversations.length} 条模拟会话
        </span>
        <button
          className={`filter-button ${filterOpen ? "is-open" : ""}`}
          type="button"
          aria-haspopup="true"
          aria-expanded={filterOpen}
          onClick={onToggleFilterOpen}
        >
          <ArchiveIcon name="filter" size={15} />
          筛选
          {activeFilters.length > 0 ? <span className="filter-count">{activeFilters.length}</span> : null}
        </button>

        {filterOpen ? (
          <div className="filter-popover" role="menu" aria-label="会话筛选">
            {filterOptions.map((option) => {
              const selected = activeFilters.includes(option.id);
              return (
                <button
                  key={option.id}
                  className={`filter-option ${selected ? "is-selected" : ""}`}
                  type="button"
                  role="menuitemcheckbox"
                  aria-checked={selected}
                  onClick={() => onToggleFilter(option.id)}
                >
                  <span className="filter-option-label">
                    <span className="check-indicator"><ArchiveIcon name="check" size={12} /></span>
                    {option.label}
                  </span>
                  <span>{option.id === "attachments" ? "文件" : option.id === "branches" ? "树" : "状态"}</span>
                </button>
              );
            })}
          </div>
        ) : null}
      </div>

      <nav className="conversation-list" aria-label="模拟会话列表">
        {groups.length > 0 ? groups.map((group) => (
          <React.Fragment key={group.label}>
            <div className="date-heading">{group.label}</div>
            {group.items.map((conversation) => (
              <button
                key={conversation.id}
                className={`conversation-item ${conversation.id === activeId ? "is-active" : ""}`}
                type="button"
                aria-current={conversation.id === activeId ? "page" : undefined}
                onClick={() => onSelectConversation(conversation.id)}
              >
                <span className="conversation-copy">
                  <span className="conversation-title">{conversation.title}</span>
                  <span className="conversation-preview">{conversation.preview}</span>
                  <span className="conversation-flags" aria-label="会话特征">
                    {conversation.hasAttachment ? (
                      <span className="mini-flag" title="含附件"><ArchiveIcon name="paperclip" /></span>
                    ) : null}
                    {conversation.hasBranches ? (
                      <span className="mini-flag" title="含替代分支"><ArchiveIcon name="branch" /></span>
                    ) : null}
                    {conversation.hasAbsence ? (
                      <span className="mini-flag" title="最新快照源缺席"><ArchiveIcon name="clock" /></span>
                    ) : null}
                  </span>
                </span>
                <span className="conversation-time">{conversation.time}</span>
              </button>
            ))}
          </React.Fragment>
        )) : (
          <div className="empty-list">
            <div>
              <strong>没有匹配的模拟会话</strong>
              调整关键词或取消一个筛选条件。
            </div>
          </div>
        )}
      </nav>

      <footer className="sidebar-footer">
        <button className="snapshot-compact" type="button" onClick={onOpenSnapshots}>
          <span className="snapshot-compact-copy">
            <span className="snapshot-compact-title">
              <span className="status-dot" aria-hidden="true"></span>
              示例快照 S-004 已校验
            </span>
            <span className="snapshot-compact-meta">观察时间 2026-08-01 · 模拟数据</span>
          </span>
          <ArchiveIcon name="arrow" size={15} />
        </button>
        <div className="privacy-note">原型未连接真实导出、账户字段或本地数据库。</div>
      </footer>
    </aside>
  );
}

function AttachmentCard({ attachment, onOpen }) {
  return (
    <button className="attachment-card" type="button" onClick={() => onOpen(attachment)}>
      <span className="attachment-preview" aria-hidden="true"><span>{attachment.kind}</span></span>
      <span className="attachment-copy">
        <span className="attachment-title">{attachment.name}</span>
        <span className="attachment-meta">{attachment.size} · {attachment.hash}</span>
      </span>
      <span className="attachment-status"><ArchiveIcon name="check" size={14} />{attachment.status}</span>
    </button>
  );
}

function BranchModule({ message, expanded, onToggle }) {
  const branches = message.branches || [];
  if (branches.length === 0) {
    return null;
  }

  return (
    <div className="branch-module">
      <button
        className="branch-toggle"
        type="button"
        aria-expanded={expanded}
        onClick={() => onToggle(message.id)}
      >
        <span className="branch-toggle-main">
          <ArchiveIcon name="branch" size={16} />
          {branches.length} 个替代回答
          <span className="branch-toggle-meta">未选中的会话分支</span>
        </span>
        <ArchiveIcon name="chevron" size={15} className="chevron" />
      </button>
      {expanded ? (
        <div className="branch-list">
          {branches.map((branch) => (
            <div className="branch-option" key={`${message.id}-${branch.label}`}>
              <div className="branch-option-label">
                <span>{branch.label}</span>
                <span className="branch-option-time">{branch.time}</span>
              </div>
              <div className="branch-option-copy">{branch.copy}</div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Message({ message, branchExpanded, onToggleBranch, onCopy, onInspect, onOpenAttachment }) {
  const isAssistant = message.role === "assistant";
  return (
    <article className="message" data-message-id={message.id}>
      <div className={`avatar ${isAssistant ? "assistant" : ""}`} aria-hidden="true">
        {isAssistant ? "A" : "你"}
      </div>
      <div className="message-body">
        <div className="message-meta">
          <span className="message-author">{message.author}</span>
          <span className="message-time">{message.time}</span>
          {message.current ? <span className="message-current-label">当前主线</span> : null}
        </div>
        <div className="message-content" lang="zh-CN">
          {message.paragraphs.map((paragraph, index) => <p key={`${message.id}-p-${index}`}>{paragraph}</p>)}
          {message.bullets ? (
            <ul>
              {message.bullets.map((bullet, index) => <li key={`${message.id}-b-${index}`}>{bullet}</li>)}
            </ul>
          ) : null}
        </div>
        {message.attachment ? <AttachmentCard attachment={message.attachment} onOpen={onOpenAttachment} /> : null}
        <BranchModule message={message} expanded={branchExpanded} onToggle={onToggleBranch} />
        <div className="message-footer">
          <button className="message-action" type="button" onClick={() => onCopy(message)}>
            <ArchiveIcon name="copy" />复制
          </button>
          <button className="message-action" type="button" onClick={() => onInspect(message.id)}>
            <ArchiveIcon name="source" />来源
          </button>
        </div>
      </div>
    </article>
  );
}

function Reader({
  conversation,
  theme,
  drawerOpen,
  branchState,
  onToggleTheme,
  onToggleDrawer,
  onOpenSidebar,
  onToggleBranch,
  onCopyMessage,
  onInspectMessage,
  onOpenAttachment
}) {
  const latestSnapshot = conversation.snapshots[0];
  const absent = latestSnapshot.state === "absent";

  return (
    <main className="reader-shell" data-screen-label="Archive conversation reader">
      <header className="reader-header">
        <div className="reader-heading">
          <button className="icon-button mobile-menu-button" type="button" aria-label="打开会话列表" onClick={onOpenSidebar}>
            <ArchiveIcon name="menu" />
          </button>
          <div className="reader-title-block">
            <h1 className="reader-title">{conversation.title}</h1>
            <div className="reader-subtitle">
              <span className="source-label">{conversation.source}</span>
              <span aria-hidden="true">·</span>
              <span>{conversation.messages.length} 条主线消息</span>
              {conversation.hasBranches ? <><span aria-hidden="true">·</span><span>含替代分支</span></> : null}
            </div>
          </div>
        </div>
        <div className="reader-actions">
          <button
            className="icon-button"
            type="button"
            aria-label={theme === "light" ? "切换到深色主题" : "切换到浅色主题"}
            title={theme === "light" ? "深色主题" : "浅色主题"}
            onClick={onToggleTheme}
          >
            <ArchiveIcon name={theme === "light" ? "moon" : "sun"} />
          </button>
          <button
            className={`icon-button ${drawerOpen ? "is-active" : ""}`}
            type="button"
            aria-label={drawerOpen ? "关闭来源与快照抽屉" : "打开来源与快照抽屉"}
            aria-expanded={drawerOpen}
            onClick={onToggleDrawer}
          >
            <ArchiveIcon name="panelRight" />
          </button>
        </div>
      </header>

      <div className="snapshot-ribbon">
        <div className="snapshot-ribbon-main">
          <ArchiveIcon name="layers" size={15} />
          <strong>{latestSnapshot.id}</strong>
          <span>{latestSnapshot.label} 的来源观察</span>
        </div>
        <div className="snapshot-ribbon-status">
          <ArchiveIcon name={absent ? "clock" : "check"} size={14} />
          <span>{absent ? "源缺席" : "证据校验通过"}</span>
          {absent ? <span>不等同于删除</span> : null}
        </div>
      </div>

      <div className="reader-scroll" id="reader-scroll">
        <div className="conversation-stage">
          <section className="conversation-intro" aria-labelledby="conversation-heading">
            <span className="conversation-kicker">模拟档案 · 当前主线</span>
            <h2 className="conversation-heading" id="conversation-heading">{conversation.title}</h2>
            <p className="conversation-description">{conversation.summary}</p>
          </section>
          <section className="message-list" aria-label="当前主线消息">
            {conversation.messages.map((message) => (
              <Message
                key={message.id}
                message={message}
                branchExpanded={Boolean(branchState[message.id])}
                onToggleBranch={onToggleBranch}
                onCopy={onCopyMessage}
                onInspect={onInspectMessage}
                onOpenAttachment={onOpenAttachment}
              />
            ))}
          </section>
          <div className="end-marker">当前主线结束</div>
        </div>
      </div>
    </main>
  );
}

function ProvenancePanel({ conversation, selectedMessageId }) {
  const provenance = conversation.provenance;
  return (
    <>
      <section className="drawer-section">
        <span className="section-label">证据状态</span>
        <div className="provenance-card">
          <div className="provenance-status">来源链可回溯</div>
          <dl className="metadata-list">
            <div className="metadata-row">
              <dt>会话标识</dt>
              <dd>{provenance.conversationId}</dd>
            </div>
            <div className="metadata-row">
              <dt>所选节点</dt>
              <dd>{selectedMessageId || provenance.selectedNodeId}</dd>
            </div>
            <div className="metadata-row">
              <dt>源文件</dt>
              <dd>{provenance.sourceFile}</dd>
            </div>
            <div className="metadata-row">
              <dt>载荷摘要</dt>
              <dd>{provenance.payloadHash}</dd>
            </div>
          </dl>
        </div>
      </section>

      <section className="drawer-section">
        <span className="section-label">来源路径</span>
        <div className="evidence-path">
          <div className="evidence-step">
            <span className="evidence-index">1</span>
            <span className="evidence-copy">
              <strong>Master Evidence Copy</strong>
              <span>{provenance.sourceSnapshot} · 只读快照</span>
            </span>
          </div>
          <div className="evidence-step">
            <span className="evidence-index">2</span>
            <span className="evidence-copy">
              <strong>Canonical Record</strong>
              <span>{provenance.mappingPath}</span>
            </span>
          </div>
          <div className="evidence-step is-current">
            <span className="evidence-index">3</span>
            <span className="evidence-copy">
              <strong>Reading Archive</strong>
              <span>当前显示的派生阅读视图</span>
            </span>
          </div>
        </div>
      </section>

      <section className="drawer-section">
        <span className="section-label">观察与分支</span>
        <dl className="metadata-list">
          <div className="metadata-row">
            <dt>来源观察</dt>
            <dd className="plain">{provenance.observedAt}</dd>
          </div>
          <div className="metadata-row">
            <dt>导入时间</dt>
            <dd className="plain">{provenance.importedAt}</dd>
          </div>
          <div className="metadata-row">
            <dt>当前分支</dt>
            <dd>{provenance.currentBranch}</dd>
          </div>
        </dl>
      </section>
    </>
  );
}

function SnapshotsPanel({ conversation }) {
  return (
    <>
      <section className="drawer-section">
        <span className="section-label">快照观察</span>
        <div className="snapshot-timeline">
          {conversation.snapshots.map((snapshot, index) => (
            <article
              key={snapshot.id}
              className={`snapshot-card ${index === 0 ? "is-latest" : ""} ${snapshot.state === "absent" ? "is-absent" : ""}`}
            >
              <header className="snapshot-card-header">
                <span className="snapshot-card-title">{snapshot.id} · {snapshot.label}</span>
                <span className={`snapshot-state ${snapshot.state}`}>
                  {snapshot.state === "present" ? snapshot.detail : "源缺席"}
                </span>
              </header>
              <div className="snapshot-details">
                <span className="snapshot-stat"><strong>{snapshot.messages}</strong><span>消息观察</span></span>
                <span className="snapshot-stat"><strong>{snapshot.assets}</strong><span>附件观察</span></span>
              </div>
            </article>
          ))}
        </div>
      </section>
      <section className="drawer-section">
        <span className="section-label">状态解释</span>
        <div className="notice">
          “源缺席”只表示这次快照没有观察到该记录。没有明确删除事件时，档案保留旧证据，也不会推断用户删除了它。
        </div>
      </section>
    </>
  );
}

function MetadataDrawer({ conversation, tab, selectedMessageId, onTabChange, onClose, onCopyReference }) {
  return (
    <aside className="metadata-drawer" aria-label="来源与快照">
      <div className="drawer-inner">
        <header className="drawer-header">
          <div className="drawer-title">
            <strong>来源与快照</strong>
            <span>模拟证据视图</span>
          </div>
          <button className="icon-button" type="button" aria-label="关闭来源抽屉" onClick={onClose}>
            <ArchiveIcon name="close" />
          </button>
        </header>
        <div className="drawer-tabs" role="tablist" aria-label="来源抽屉视图">
          <button
            className={`drawer-tab ${tab === "provenance" ? "is-active" : ""}`}
            type="button"
            role="tab"
            aria-selected={tab === "provenance"}
            onClick={() => onTabChange("provenance")}
          >溯源</button>
          <button
            className={`drawer-tab ${tab === "snapshots" ? "is-active" : ""}`}
            type="button"
            role="tab"
            aria-selected={tab === "snapshots"}
            onClick={() => onTabChange("snapshots")}
          >快照</button>
        </div>
        <div className="drawer-content">
          {tab === "provenance" ? (
            <ProvenancePanel conversation={conversation} selectedMessageId={selectedMessageId} />
          ) : (
            <SnapshotsPanel conversation={conversation} />
          )}
        </div>
        <footer className="drawer-footer">
          <span className="drawer-footer-copy">所有标识与数值均为演示数据</span>
          <button className="text-button" type="button" onClick={onCopyReference}>复制来源引用</button>
        </footer>
      </div>
    </aside>
  );
}

Object.assign(window, {
  Sidebar,
  Reader,
  MetadataDrawer
});
