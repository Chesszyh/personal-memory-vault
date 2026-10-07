const { ArchiveIcon: InsightsIcon } = window;

function InsightSectionHeader({ eyebrow, title, description, badge }) {
  return (
    <header className="insight-section-header">
      <div className="insight-heading-copy">
        <span className="insight-eyebrow">{eyebrow}</span>
        <h3>{title}</h3>
        {description ? <p>{description}</p> : null}
      </div>
      {badge ? <span className="calculation-badge"><InsightsIcon name="shield" size={13} />{badge}</span> : null}
    </header>
  );
}

function HeadlineMetric({ metric }) {
  return (
    <article className="headline-metric">
      <div className="headline-metric-icon"><InsightsIcon name={metric.icon} size={17} /></div>
      <div className="headline-metric-copy">
        <span>{metric.label}</span>
        <strong>{metric.value}</strong>
        <small>{metric.detail}</small>
      </div>
    </article>
  );
}

function buildMockHeatmap(seed) {
  return Array.from({ length: 371 }, (_, index) => {
    const week = Math.floor(index / 7);
    const day = index % 7;
    const signal = (week * 17 + day * 11 + seed * 7) % 23;
    if (week > 34 && seed === 23) {
      return 0;
    }
    if (signal < 6) return 0;
    if (signal < 11) return 1;
    if (signal < 16) return 2;
    if (signal < 20) return 3;
    return 4;
  });
}

function ActivityPanel({ activity, year, onYearChange }) {
  const selected = activity.years[year];
  const heatmap = buildMockHeatmap(selected.seed);
  const maxMonth = Math.max(...selected.monthly, 1);
  const monthPositions = [1, 5, 9, 14, 18, 22, 27, 31, 36, 40, 44, 49];

  return (
    <div className="activity-panel">
      <div className="activity-toolbar">
        <div className="activity-summary">
          <strong>{selected.activeDays}</strong>
          <span>个活跃日 · {selected.conversations} 个会话</span>
        </div>
        <div className="year-switch" role="group" aria-label="活动年份">
          {Object.keys(activity.years).map((item) => (
            <button
              key={item}
              className={item === year ? "is-active" : ""}
              type="button"
              aria-label={`选择 ${item} 年`}
              aria-pressed={item === year}
              onClick={() => onYearChange(item)}
            >{item}</button>
          ))}
        </div>
      </div>

      <div className="heatmap-scroll" aria-label={`${year} 年模拟活动热图`}>
        <div className="heatmap-canvas">
          <div className="heatmap-month-labels" aria-hidden="true">
            {activity.monthLabels.map((label, index) => (
              <span key={label} style={{ gridColumn: monthPositions[index] }}>{label}</span>
            ))}
          </div>
          <div className="heatmap-body">
            <div className="heatmap-weekdays" aria-hidden="true"><span>一</span><span>三</span><span>五</span></div>
            <div className="heatmap-grid">
              {heatmap.map((level, index) => (
                <span key={`${year}-${index}`} className="heatmap-cell" data-level={level} title={`演示日 ${index + 1} · 活跃等级 ${level}`}></span>
              ))}
            </div>
          </div>
        </div>
      </div>
      <div className="heatmap-legend"><span>较少</span><i data-level="0"></i><i data-level="1"></i><i data-level="2"></i><i data-level="3"></i><i data-level="4"></i><span>较多</span></div>

      <div className="monthly-chart" aria-label={`${year} 年每月模拟会话量`}>
        {selected.monthly.map((value, index) => (
          <div className="month-column" key={`${year}-${activity.monthLabels[index]}`}>
            <div className="month-value">{value || "–"}</div>
            <div className="month-track"><span style={{ height: `${Math.max(4, (value / maxMonth) * 100)}%` }}></span></div>
            <div className="month-label">{index + 1}月</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function DistributionCard({ title, subtitle, items, unit = "%", icon }) {
  const max = Math.max(...items.map((item) => item.share ?? item.value), 1);
  return (
    <article className="insight-card distribution-card">
      <header className="insight-card-header">
        <div className="card-icon"><InsightsIcon name={icon} size={16} /></div>
        <div><h4>{title}</h4><p>{subtitle}</p></div>
      </header>
      <div className="distribution-list">
        {items.map((item) => {
          const numeric = item.share ?? item.value;
          return (
            <div className="distribution-row" key={item.label}>
              <div className="distribution-label"><span>{item.label}</span><strong>{numeric}{unit}</strong></div>
              <div className="distribution-track"><span style={{ width: `${(numeric / max) * 100}%` }}></span></div>
              {item.share !== undefined ? <small>{item.value.toLocaleString("zh-CN")} 条</small> : null}
            </div>
          );
        })}
      </div>
    </article>
  );
}

function StructureRatesCard({ rates }) {
  return (
    <article className="insight-card structure-card">
      <header className="insight-card-header">
        <div className="card-icon"><InsightsIcon name="branch" size={16} /></div>
        <div><h4>分支与编辑结构</h4><p>由节点父子关系确定计算</p></div>
      </header>
      <div className="structure-list">
        {rates.map((rate) => (
          <div className="structure-row" key={rate.label}>
            <div className="structure-value"><strong>{rate.value}%</strong><span>{rate.label}</span></div>
            <div className="structure-track"><span style={{ width: `${rate.value}%` }}></span></div>
            <div className="structure-note"><span>{rate.count}</span><small>{rate.note}</small></div>
          </div>
        ))}
      </div>
    </article>
  );
}

function AttachmentHealth({ attachments }) {
  return (
    <article className="insight-card attachment-health">
      <header className="insight-card-header">
        <div className="card-icon"><InsightsIcon name="paperclip" size={16} /></div>
        <div><h4>附件引用健康度</h4><p>{attachments.total.toLocaleString("zh-CN")} 个模拟引用</p></div>
      </header>
      <div className="attachment-health-body">
        <div
          className="health-donut"
          style={{
            "--resolved": `${attachments.resolved.share * 3.6}deg`,
            "--missing": `${(attachments.resolved.share + attachments.missing.share) * 3.6}deg`
          }}
          aria-label={`确定匹配 ${attachments.resolved.share}%`}
        >
          <div><strong>{attachments.resolved.share}%</strong><span>确定匹配</span></div>
        </div>
        <div className="health-legend">
          {[attachments.resolved, attachments.missing, attachments.ambiguous].map((item, index) => (
            <div className="health-item" key={item.label}>
              <i data-health={index}></i>
              <span>{item.label}</span>
              <strong>{item.count}</strong>
              <small>{item.share}%</small>
            </div>
          ))}
        </div>
      </div>
    </article>
  );
}

function SnapshotDeltaCard({ snapshots }) {
  const maxAdded = Math.max(...snapshots.map((snapshot) => snapshot.added));
  return (
    <article className="insight-card snapshot-delta-card">
      <header className="insight-card-header">
        <div className="card-icon"><InsightsIcon name="layers" size={16} /></div>
        <div><h4>快照增量</h4><p>新增、内容变化与源缺席分开计数</p></div>
      </header>
      <div className="snapshot-delta-list">
        {snapshots.map((snapshot) => (
          <div className="snapshot-delta-row" key={snapshot.id}>
            <div className="delta-id"><strong>{snapshot.id}</strong><span>{snapshot.label}</span></div>
            <div className="delta-bars">
              <span className="delta-added" style={{ width: `${(snapshot.added / maxAdded) * 100}%` }}></span>
              <span className="delta-changed" style={{ width: `${Math.max(2, (snapshot.changed / maxAdded) * 100)}%` }}></span>
              <span className="delta-absent" style={{ width: `${Math.max(2, (snapshot.absent / maxAdded) * 100)}%` }}></span>
            </div>
            <div className="delta-values"><span>+{snapshot.added}</span><span>~{snapshot.changed}</span><span>–{snapshot.absent}</span></div>
          </div>
        ))}
      </div>
      <div className="delta-legend"><span><i className="added"></i>新增</span><span><i className="changed"></i>变化</span><span><i className="absent"></i>源缺席</span></div>
    </article>
  );
}

function CoverageCard({ sources, quality }) {
  return (
    <article className="insight-card coverage-card">
      <header className="insight-card-header">
        <div className="card-icon"><InsightsIcon name="shield" size={16} /></div>
        <div><h4>来源覆盖与数据质量</h4><p>覆盖声明不跨来源外推</p></div>
      </header>
      <div className="coverage-list">
        {sources.map((source) => (
          <div className="coverage-row" key={source.label}>
            <div className="coverage-copy"><strong>{source.label}</strong><span>{source.detail}</span></div>
            <div className="coverage-measure"><span>{source.coverage}%</span><small>{source.state}</small></div>
            <div className="coverage-track"><span style={{ width: `${source.coverage}%` }}></span></div>
          </div>
        ))}
      </div>
      <div className="quality-grid">
        {quality.map((item) => (
          <div className="quality-item" key={item.label} data-state={item.state}>
            <span>{item.label}</span><strong>{item.value}</strong>
          </div>
        ))}
      </div>
    </article>
  );
}

function ExperimentArea({ experiments, onReview }) {
  return (
    <section className="experiment-zone" data-zone="experimental" aria-labelledby="experiment-title">
      <header className="experiment-header">
        <div className="experiment-heading">
          <span className="experiment-icon"><InsightsIcon name="flask" size={18} /></span>
          <div>
            <span className="experiment-kicker">实验区 · LLM 推断 · 待人工审核</span>
            <h3 id="experiment-title">候选洞察，不是个人事实</h3>
            <p>这些结果没有进入 Confirmed Memory，也不参与上方确定性统计。</p>
          </div>
        </div>
        <button className="experiment-review-button" type="button" onClick={onReview}>查看待审核队列</button>
      </header>
      <div className="experiment-grid">
        {experiments.map((experiment) => (
          <article className="experiment-card" key={experiment.title}>
            <div className="experiment-card-meta"><span>MOCK · LLM</span><strong>{experiment.confidence}</strong></div>
            <h4>{experiment.title}</h4>
            <div className="experiment-value">{experiment.value}</div>
            <p>{experiment.detail}</p>
            <div className="unreviewed-stamp"><InsightsIcon name="clock" size={12} />未确认，不写入画像</div>
          </article>
        ))}
      </div>
    </section>
  );
}

function InsightsView({ data, theme, year, onYearChange, onToggleTheme, onOpenSidebar, onReviewExperiments }) {
  return (
    <main className="insights-shell" data-screen-label="Archive deterministic insights dashboard">
      <header className="reader-header insights-header">
        <div className="reader-heading">
          <button className="icon-button mobile-menu-button" type="button" aria-label="打开会话列表" onClick={onOpenSidebar}>
            <InsightsIcon name="menu" />
          </button>
          <div className="reader-title-block">
            <h1 className="reader-title">洞察总览</h1>
            <div className="reader-subtitle">
              <span className="source-label">确定性统计</span><span aria-hidden="true">·</span><span>{data.generatedLabel}</span>
            </div>
          </div>
        </div>
        <div className="reader-actions">
          <span className="mock-data-badge">MOCK DATA</span>
          <button
            className="icon-button"
            type="button"
            aria-label={theme === "light" ? "切换到深色主题" : "切换到浅色主题"}
            onClick={onToggleTheme}
          ><InsightsIcon name={theme === "light" ? "moon" : "sun"} /></button>
        </div>
      </header>

      <div className="insights-scroll">
        <div className="insights-stage">
          <section className="insights-intro" data-zone="deterministic">
            <div className="insights-intro-copy">
              <span className="conversation-kicker">可复算 · 有来源 · 无模型推断</span>
              <h2>档案发生了什么，证据有多完整</h2>
              <p>上半部分只展示可由规范化记录、关系和清单确定计算的指标。所有数值均为演示数据。</p>
            </div>
            <div className="insights-scope"><InsightsIcon name="shield" size={15} /><span>{data.scopeLabel}</span></div>
          </section>

          <section className="headline-metrics" aria-label="归档摘要指标" data-zone="deterministic">
            {data.headlineMetrics.map((metric) => <HeadlineMetric metric={metric} key={metric.label} />)}
          </section>

          <section className="insight-section" data-zone="deterministic">
            <InsightSectionHeader
              eyebrow="Activity"
              title="年度与月度活跃"
              description="按消息发生日期聚合；不推断活跃原因或情绪。"
              badge="确定性聚合"
            />
            <ActivityPanel activity={data.activity} year={year} onYearChange={onYearChange} />
          </section>

          <section className="insight-section" data-zone="deterministic">
            <InsightSectionHeader
              eyebrow="Composition"
              title="内容构成与会话结构"
              description="角色、模型标签和分支关系均来自可追溯字段。"
              badge="来源字段"
            />
            <div className="composition-grid">
              <DistributionCard title="角色分布" subtitle="23,946 个消息节点" items={data.roles} unit="%" icon="activity" />
              <DistributionCard title="模型分布" subtitle="按导出中的模型标签归并" items={data.models} unit="%" icon="source" />
              <StructureRatesCard rates={data.structureRates} />
            </div>
          </section>

          <section className="insight-section" data-zone="deterministic">
            <InsightSectionHeader
              eyebrow="Integrity"
              title="附件、快照与覆盖质量"
              description="失败与歧义不会被成功率吞掉；源缺席也不会标成删除。"
              badge="证据健康度"
            />
            <div className="integrity-grid">
              <AttachmentHealth attachments={data.attachments} />
              <SnapshotDeltaCard snapshots={data.snapshotDeltas} />
              <CoverageCard sources={data.sourceCoverage} quality={data.quality} />
            </div>
          </section>

          <ExperimentArea experiments={data.experiments} onReview={onReviewExperiments} />
          <footer className="insights-footnote">原型说明：正式离线阅读器需本地打包前端依赖；此页尚未连接真实数据契约。</footer>
        </div>
      </div>
    </main>
  );
}

Object.assign(window, {
  InsightsView
});
