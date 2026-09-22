import { useEffect, useState } from "react";
import { api, ApiError, ApiProblem, post } from "./api";
import "./foundation.css";
import AutomatedIntake from "./AutomatedIntake";
import DirectorySummaryPlanner from "./DirectorySummaryPlanner";

type Data = Record<string, any>;
type FoundationTab = "overview" | "intake" | "classification" | "ledger";

const tabs: Array<{ id: FoundationTab; step: string; label: string; help: string }> = [
  { id: "overview", step: "01", label: "资料地图", help: "电脑里有什么" },
  { id: "intake", step: "02", label: "整理方案", help: "范围和处理深度" },
  { id: "classification", step: "03", label: "AI 整理", help: "分类、摘要和入库" },
  { id: "ledger", step: "检查", label: "检查与明细", help: "状态、失败和来源" },
];

const catalogLabels: Record<string, string> = {
  cataloged: "仅登记位置",
  indexed: "已建立内容记录",
  skipped: "按规则跳过 / 无文字",
  missing: "上次未找到",
  error: "处理失败",
};

const chartColors = ["#42d5c7", "#4f8cff", "#a17dff", "#f0b35a", "#e66b87", "#6fc98c", "#6aa7b8", "#8a9aa7"];

type ReadState = {
  data: Data | null;
  issue: ApiProblem | null;
  loading: boolean;
  retry: () => void;
};

function readProblem(error: unknown): ApiProblem {
  if (error instanceof ApiError) return error.problem;
  if (error instanceof DOMException && error.name === "AbortError") {
    return {
      code: "client_read_timeout",
      title: "这项本机读取超时",
      message: "页面已停止等待，避免界面一直卡住。",
      impact: "没有扫描、同步或改动任何资料。",
      action: "可以重试；如果持续出现，请先查看轻量概览。",
      retryable: true,
      technical_detail: "浏览器读取超时",
    };
  }
  return {
    code: "local_read_unavailable",
    title: "暂时无法读取这项本机资料",
    message: "这次只读检查没有完成。",
    impact: "已有资料和索引没有被改动。",
    action: "可以重试；如果持续出现，请在运行状态中检查本机服务。",
    retryable: true,
    technical_detail: "本机只读接口",
  };
}

function useRead(path: string | null): ReadState {
  const [data, setData] = useState<Data | null>(null);
  const [issue, setIssue] = useState<ApiProblem | null>(null);
  const [loading, setLoading] = useState(false);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    setData(null);
    setIssue(null);
    if (!path) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    let active = true;
    setLoading(true);
    api<Data>(path, { signal: controller.signal })
      .then(result => { if (active) setData(result.data); })
      .catch(error => { if (active) setIssue(readProblem(error)); })
      .finally(() => {
        window.clearTimeout(timeout);
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [path, version]);

  return { data, issue, loading, retry: () => setVersion(value => value + 1) };
}

function ReadIssue({ issue, retry, compact = false }: { issue: ApiProblem | null; retry: () => void; compact?: boolean }) {
  if (!issue) return null;
  return <section className={`foundation-read-issue ${compact ? "compact" : ""}`} role="alert">
    <header><div><small>本机读取提示</small><h3>{issue.title ?? "暂时无法读取这项资料"}</h3></div><span>{issue.code ?? "local_read_unavailable"}</span></header>
    <p>{issue.message ?? "这次只读检查没有完成。"}</p>
    {!compact && <div className="foundation-issue-grid">
      <div><strong>这会影响什么</strong><span>{issue.impact ?? "这一区域暂时不显示；已有资料不会被改动。"}</span></div>
      <div><strong>现在可以怎么做</strong><span>{issue.action ?? "可以重试。"}</span></div>
    </div>}
    <footer><button type="button" onClick={retry}>重新读取</button><details><summary>查看技术明细</summary><code>{issue.technical_detail ?? "本机只读接口"}</code></details></footer>
  </section>;
}

function Loading({ text = "正在读取本机状态…" }: { text?: string }) {
  return <p className="foundation-reading" role="status"><i />{text}</p>;
}

export default function FoundationCenter() {
  const requestedTab = new URLSearchParams(window.location.search).get("foundationTab");
  const initialTab = tabs.some(item => item.id === requestedTab) ? requestedTab as FoundationTab : "overview";
  const [tab, setTab] = useState<FoundationTab>(initialTab);
  const ledgerActive = tab === "ledger";
  const [documentsOpen, setDocumentsOpen] = useState(false);
  const [verifyFulltext, setVerifyFulltext] = useState(false);
  const [documentOffset, setDocumentOffset] = useState(0);
  const [selectedRoot, setSelectedRoot] = useState("");
  const [selectedState, setSelectedState] = useState("cataloged");
  const [fileOffset, setFileOffset] = useState(0);
  const ledger = useRead(ledgerActive ? "/api/foundation/ledger-summary" : null);
  const documentPath = ledgerActive && documentsOpen
    ? `/api/foundation/documents?offset=${documentOffset}&limit=25${verifyFulltext ? "&verify_fulltext=true" : ""}`
    : null;
  const documents = useRead(documentPath);
  const files = useRead(ledgerActive && selectedRoot
    ? `/api/foundation/files?root_id=${encodeURIComponent(selectedRoot)}&state=${selectedState}&offset=${fileOffset}&limit=25`
    : null);
  const roots: Data[] = ledger.data?.roots ?? [];

  const changeTab = (next: FoundationTab) => {
    setTab(next);
    const url = new URL(window.location.href);
    url.searchParams.set("foundationTab", next);
    window.history.replaceState({}, "", url);
  };

  return <div className="foundation-center">
    <FoundationWorkflow activeTab={tab} onNavigate={changeTab} />
    <nav className="foundation-tabs" aria-label="资料底座功能">
      {tabs.map(item => <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => changeTab(item.id)}>
        <i>{item.step}</i><span><strong>{item.label}</strong><small>{item.help}</small></span>
      </button>)}
    </nav>

    {tab === "overview" && <OverviewPanel onNavigate={changeTab} />}
    {tab === "intake" && <AutomatedIntake onNext={() => changeTab("classification")} onLedger={() => changeTab("ledger")} />}
    {tab === "classification" && <DirectorySummaryPlanner />}
    {tab === "ledger" && <>
      <section className="foundation-ledger-intro">
        <div><small>资料检查</small><h2>先看状态，再展开具体明细</h2><p>这里默认只做轻量只读检查；不会扫盘、导入聊天、调用模型，也不会把一次慢统计拖成整个页面不可用。</p></div>
        <button type="button" onClick={ledger.retry} disabled={ledger.loading}>刷新检查</button>
      </section>
      {ledger.loading && <Loading text="正在读取资料检查概览…" />}
      <ReadIssue issue={ledger.issue} retry={ledger.retry} />
      {ledger.data && <>
        <section className="ledger-health-grid" aria-label="资料库检查结果">
          {(ledger.data.checks ?? []).map((check: Data) => <article key={check.id} className={check.status}>
            <span className="ledger-check-dot" /><div><small>{check.status === "ready" ? "状态正常" : check.status === "attention" ? "需要留意" : "按需执行"}</small><h3>{check.title}</h3><p>{check.summary}</p></div>
          </article>)}
        </section>
        <section className="foundation-panel ledger-source-panel">
          <header><div><small>已接入范围</small><h2>资料来源与处理台账</h2><p>“已登记”表示上次已记录到资料库；它不等于正在扫描，也不等于所有文件都已经读完。</p></div><span>{roots.length.toLocaleString()} 个资料源</span></header>
          <div className="foundation-metrics ledger-summary-metrics">
            <div><span>可检索资料</span><strong>{Number(ledger.data.summary?.documents ?? 0).toLocaleString()}</strong><small>正文已建立记录</small></div>
            <div><span>全文切片</span><strong>{Number(ledger.data.summary?.chunks ?? 0).toLocaleString()}</strong><small>用于关键词检索</small></div>
            <div><span>向量登记</span><strong>{Number(ledger.data.summary?.vectors ?? 0).toLocaleString()}</strong><small>用于相近含义检索</small></div>
            <div><span>目录读取失败</span><strong>{Number(ledger.data.summary?.failed_catalog_items ?? 0).toLocaleString()}</strong><small>可在下方按来源查看</small></div>
          </div>
          {roots.length ? <details className="ledger-expand"><summary>查看已接入的资料源</summary><div className="foundation-scroll"><table><thead><tr><th>资料源</th><th>登记范围</th><th>处理方式</th><th>最近扫描</th></tr></thead><tbody>{roots.map(root => <tr key={root.id}><td>{root.name}<small>{root.enabled ? "已登记" : "已停用"}</small></td><td>{root.root_uri}</td><td>{root.sync_mode === "catalog" ? "只登记目录" : "读取并建立内容记录"}<small>{root.connector_type}</small></td><td>{root.last_scan_at ? new Date(root.last_scan_at).toLocaleString() : "尚未扫描"}</td></tr>)}</tbody></table></div></details> : <p>还没有已登记的资料源。</p>}
        </section>

        <section className="foundation-panel ledger-content-panel">
          <header><div><small>内容明细</small><h2>已进入知识库的文件</h2><p>默认不做全库全文对账。先打开当前页文件明细；只有需要排查时才执行较慢的深度校验。</p></div>{!documentsOpen && <button type="button" onClick={() => setDocumentsOpen(true)}>打开文件明细</button>}</header>
          {documentsOpen && <>
            {documents.loading && <Loading text="正在读取当前 25 份文件明细…" />}
            <ReadIssue issue={documents.issue} retry={documents.retry} />
            {documents.data && <>
              <div className={`ledger-deep-check ${documents.data.fulltext_check?.status ?? "not_checked"}`}>
                <div><strong>全文深度对账</strong><span>{documents.data.fulltext_check?.summary ?? "尚未执行。"}</span></div>
                {!verifyFulltext && <button type="button" disabled={documents.loading} onClick={() => { setDocumentOffset(0); setVerifyFulltext(true); }}>执行深度对账</button>}
              </div>
              <div className="foundation-scroll"><table><thead><tr><th>文件 / 来源</th><th>全文状态</th><th>向量登记</th><th>解析提示</th></tr></thead><tbody>{documents.data.items.map((document: Data) => <tr key={document.id}><td>{document.name}<small>{document.path}</small></td><td>{fulltextLabel(document.fulltext)}<small>{document.chunks ? `${document.chunks} 个切片` : "尚未产生切片"}</small></td><td>{document.vector_chunks}/{document.chunks}<small>{document.vector === "recorded" ? "登记完整；在线效果另行检查" : "登记不完整或未排队"}</small></td><td>{document.quality === "attention" ? parsingAttentionLabel(document) : "未发现自动解析提示"}<small>{parserLabel(document.parser_version)} · {document.typed_current ?? 0}/{document.chunks} 使用当前切片规则</small></td></tr>)}</tbody></table></div>
              {!documents.data.items.length && <p>当前没有可显示的文件记录。</p>}
              <div className="foundation-pager"><button disabled={!documentOffset || documents.loading} onClick={() => setDocumentOffset(value => Math.max(0, value - 25))}>上一页</button><span>{documents.data.total ? `${documentOffset + 1}–${Math.min(documentOffset + 25, documents.data.total)}` : "0"} / {documents.data.total}</span><button disabled={!documents.data.has_more || documents.loading} onClick={() => setDocumentOffset(value => value + 25)}>下一页</button></div>
              <p className="chart-footnote">{documents.data.note}</p>
            </>}
          </>}
        </section>

        <details className="foundation-panel ledger-troubleshooting">
          <summary><span><small>按来源排查</small><strong>为什么某个文件搜不到？</strong></span><i>展开</i></summary>
          <p>选择一个已接入资料源和状态，只读取这 25 条记录；不会重新扫描硬盘。</p>
          <div className="foundation-filters"><label>资料源<select aria-label="资料源" value={selectedRoot} onChange={event => { setSelectedRoot(event.target.value); setFileOffset(0); }}><option value="">请选择已接入资料源</option>{roots.map(root => <option key={root.id} value={root.id}>{root.name}</option>)}</select></label><label>状态<select aria-label="处理状态" value={selectedState} onChange={event => { setSelectedState(event.target.value); setFileOffset(0); }}>{Object.entries(catalogLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div>
          {files.loading && <Loading text="正在读取这一个资料源的记录…" />}
          <ReadIssue issue={files.issue} retry={files.retry} compact />
          {files.data && <><div className="foundation-scroll"><table><thead><tr><th>文件</th><th>当前状态</th><th>安全原因代码</th></tr></thead><tbody>{files.data.items.map((file: Data) => <tr key={file.id}><td>{file.relative_path}</td><td>{file.explanation}</td><td>{file.reason || "—"}</td></tr>)}</tbody></table></div>{!files.data.items.length && <p>这个资料源没有该状态的记录。</p>}<div className="foundation-pager"><button disabled={!fileOffset} onClick={() => setFileOffset(value => Math.max(0, value - 25))}>上一页</button><span>第 {fileOffset / 25 + 1} 页</span><button disabled={!files.data.has_more} onClick={() => setFileOffset(value => value + 25)}>下一页</button></div></>}
        </details>
      </>}
    </>}
  </div>;
}

function FoundationWorkflow({ activeTab, onNavigate }: { activeTab: FoundationTab; onNavigate: (tab: FoundationTab) => void }) {
  const catalog = useRead("/api/foundation/machine-catalog");
  const coverage = useRead("/api/foundation/summary-jobs/catalog-progress");
  const knowledge = useRead("/api/foundation/ledger-summary");
  const catalogData = catalog.data ?? {};
  const coverageData = coverage.data ?? {};
  const knowledgeData = knowledge.data?.summary ?? {};
  const catalogState = String(catalogData.state ?? "not_started");
  const files = Number(catalogData.catalog_files ?? catalogData.files_seen ?? 0);
  const aiDone = Number(coverageData.ai_done ?? 0);
  const aiEligible = Number(coverageData.ai_eligible ?? 0);
  const documents = Number(knowledgeData.documents ?? 0);
  const steps = [
    { id: "overview", label: "资料地图", metric: files ? `${files.toLocaleString()} 个文件` : "尚未建立", help: "只登记位置和基本属性，不复制原文件。", tab: "overview" as const },
    { id: "intake", label: "内容理解", metric: aiEligible ? `${aiDone.toLocaleString()} / ${aiEligible.toLocaleString()}` : "待选择范围", help: "按你的方案分批理解重要内容，不会一次读完全部文件。", tab: "intake" as const },
    { id: "classification", label: "可检索知识", metric: `${documents.toLocaleString()} 份`, help: "确认后建立全文或语义检索，保留来源。", tab: "classification" as const },
  ];

  return <section className="foundation-workflow" aria-label="资料整理总览">
    <header><div><small>KNOWLEDGE FOUNDATION</small><h2>把电脑资料整理成可检索知识</h2><p>先知道有什么，再决定读多深，最后才加入可搜索的知识库。</p></div><button type="button" onClick={() => { catalog.retry(); coverage.retry(); knowledge.retry(); }}>刷新进度</button></header>
    <div className="foundation-workflow-steps">{steps.map((step, index) => <button type="button" key={step.id} className={`${activeTab === step.tab ? "current" : ""} ${index === 0 && catalogState === "completed" ? "done" : ""}`} onClick={() => onNavigate(step.tab)}><i>0{index + 1}</i><span><small>{step.label}</small><strong>{step.metric}</strong><em>{step.help}</em></span><b>查看 →</b></button>)}</div>
    {(catalog.issue || coverage.issue || knowledge.issue) && <p className="foundation-workflow-warning">部分概览暂时无法读取；不会影响原文件，也不会自动启动任何处理。请进入“检查与明细”查看原因。</p>}
    <details className="foundation-workflow-safety"><summary>查看处理规则和隐私边界</summary><p>第一步仅在本机登记文件位置和属性。发送文字样本给 Luna 或 DeepSeek、创建摘要、加入全文或向量库，都必须在后续页面显示范围并由你确认。原文件始终保留在原位置。</p></details>
  </section>;
}

function OverviewPanel({ onNavigate }: { onNavigate: (tab: FoundationTab) => void }) {
  const catalog = useRead("/api/foundation/machine-catalog");
  const coverage = useRead("/api/foundation/summary-jobs/catalog-progress");
  const [analytics, setAnalytics] = useState<Data | null>(null);
  const [analyticsIssue, setAnalyticsIssue] = useState<ApiProblem | null>(null);
  const [analyticsLoading, setAnalyticsLoading] = useState(false);
  const [working, setWorking] = useState(false);
  const [notice, setNotice] = useState("");
  const data = catalog.data ?? {};
  const state = String(data.state ?? "not_started");
  const scopes: Data[] = data.scopes ?? [];
  const scopeItems = scopes.map((scope, index) => ({ label: scopeLabel(scope.root_path), value: Number(scope.files_seen ?? 0), color: chartColors[index % chartColors.length] }));
  const progressItems = coverage.data ? [
    { label: "AI 已速判", value: Number(coverage.data.ai_done ?? 0), color: "#42d5c7" },
    { label: "等待速判", value: Number(coverage.data.ai_pending ?? 0), color: "#4f8cff" },
    { label: "本地跳过", value: Number(coverage.data.local_skipped ?? 0), color: "#8a9aa7" },
    { label: "需要复查", value: Number(coverage.data.ai_failed ?? 0), color: "#f0b35a" },
  ] : [];

  const loadAnalytics = async () => {
    setAnalyticsLoading(true);
    setAnalyticsIssue(null);
    try { setAnalytics((await api<Data>("/api/foundation/analytics?days=30")).data); }
    catch (error) { setAnalyticsIssue(readProblem(error)); }
    finally { setAnalyticsLoading(false); }
  };
  const changeCatalog = async (action: "start" | "resume") => {
    setWorking(true);
    setNotice("");
    try {
      const result = action === "start"
        ? await post<Data>("/api/foundation/machine-catalog/start", { confirmed: true })
        : await post<Data>(`/api/foundation/machine-catalog/${data.id}/resume`, {});
      setNotice(result.summary);
      catalog.retry();
    } catch (error) { setNotice(readProblem(error).message ?? "无法更新资料地图状态"); }
    finally { setWorking(false); }
  };

  return <>
    <section className="foundation-panel foundation-map-hero">
      <header><div><small>电脑资料地图</small><h2>索引库和知识库是两层</h2><p>索引库回答“电脑里有什么”；知识库只收你确认后需要被正文或语义搜索的内容。两者数量、空间和处理方式不混算。</p></div><span className={`catalog-state ${state}`}>{catalogStateLabel(state)}</span></header>
      {catalog.loading && <Loading text="正在读取资料地图…" />}
      <ReadIssue issue={catalog.issue} retry={catalog.retry} />
      <div className="foundation-map-flow"><article><small>A 库 · 文件地图</small><strong>{Number(data.catalog_files ?? data.files_seen ?? 0).toLocaleString()}</strong><span>已登记文件 · {formatSize(Number(data.represented_bytes ?? 0))} 原文件容量</span></article><i>按需要选择</i><article><small>知识库 · 可搜索资料</small><strong>{analytics ? Number(analytics.searchable_documents ?? 0).toLocaleString() : "按需加载"}</strong><span>{analytics ? `${Number(analytics.searchable_chunks ?? 0).toLocaleString()} 个全文切片` : "点击下方加载详细构成图"}</span></article></div>
      <div className="foundation-quick-actions" aria-label="资料地图操作"><button className="primary" onClick={() => onNavigate("intake")}><strong>选择整理方案</strong><small>查看自动范围、修改自定义规则</small></button><button onClick={() => onNavigate("classification")}><strong>打开 AI 整理工作台</strong><small>真实任务会显示范围、模型和确认</small></button><button onClick={() => onNavigate("ledger")}><strong>检查与明细</strong><small>查看已入库资料和失败原因</small></button></div>
      {(state === "not_started" || state === "paused" || state === "warning") && <div className="catalog-actions"><button disabled={working} onClick={() => void changeCatalog(state === "not_started" ? "start" : "resume")}>{working ? "正在提交…" : state === "not_started" ? "建立资料地图" : "继续未完成索引"}</button><span>只有点击后才会开始或继续本机索引；原文件不会移动。</span></div>}
      {notice && <p className="foundation-operation-note" role="status">{notice}</p>}
    </section>

    <section className="foundation-map-visuals">
      <DonutChart title="资料地图分布" subtitle="按已选择范围显示文件数量；只使用目录索引数据。" items={scopeItems} empty="尚未建立资料地图。" />
      <DonutChart title="内容理解进度" subtitle="AI 只分批处理需要理解的文件；不会一次读取全盘正文。" items={progressItems} empty="正在读取内容理解进度。" />
    </section>
    {coverage.data && <section className="foundation-progress-explainer"><div><strong>已做出处置结论</strong><b>{formatPercent(Number(coverage.data.decision_coverage_percent ?? 0))}</b><span>含 AI 速判与本地规则明确跳过。</span></div><div><strong>AI 已理解</strong><b>{formatPercent(Number(coverage.data.ai_coverage_percent ?? 0))}</b><span>{Number(coverage.data.ai_done ?? 0).toLocaleString()} / {Number(coverage.data.ai_eligible ?? 0).toLocaleString()} 需要理解的文件。</span></div><div><strong>需要复查</strong><b>{Number(coverage.data.ai_failed ?? 0).toLocaleString()}</b><span>不会被悄悄当作已完成；可在检查页看到原因。</span></div></section>}
    <ReadIssue issue={coverage.issue} retry={coverage.retry} compact />

    <section className="foundation-panel foundation-analytics-panel">
      <header><div><small>按需统计</small><h2>资料构成和历史趋势</h2><p>这部分需要读取较多聚合数据，所以不会在每次打开页面时自动运行。</p></div>{!analytics && <button type="button" disabled={analyticsLoading} onClick={() => void loadAnalytics()}>{analyticsLoading ? "正在读取…" : "加载资料构成图"}</button>}</header>
      {analyticsLoading && <Loading text="正在读取只读资料构成统计…" />}
      <ReadIssue issue={analyticsIssue} retry={() => void loadAnalytics()} />
      {analytics && <><div className="foundation-insight-metrics"><div><span>正式可搜索资料</span><strong>{Number(analytics.searchable_documents ?? 0).toLocaleString()}</strong></div><div><span>完整向量化资料</span><strong>{Number(analytics.vectorized_documents ?? 0).toLocaleString()}</strong></div><div><span>已覆盖原始路径</span><strong>{Number(analytics.represented_original_paths ?? 0).toLocaleString()}</strong></div><div><span>内容库占用</span><strong>{formatSize(Number(analytics.database_bytes ?? 0))}</strong></div></div><div className="foundation-chart-grid"><DonutChart title="可搜索文件类型" subtitle="不含聊天和 Codex 任务记录。" items={groupedSourceTypes(analytics.source_types ?? [])} empty="还没有可显示的正式资料。" /><TrendChart items={analytics.ingest_trend ?? []} /></div><p className="chart-footnote">{analytics.note}</p></>}
    </section>
    <IncludedFilesPanel />
  </>;
}

function IncludedFilesPanel() {
  const [opened, setOpened] = useState(false);
  const [drive, setDrive] = useState("ALL");
  const [offset, setOffset] = useState(0);
  const data = useRead(opened ? `/api/foundation/included-files?drive=${encodeURIComponent(drive)}&offset=${offset}&limit=50` : null);
  if (!opened) return <section className="foundation-panel foundation-lazy-list"><div><small>已入库文件</small><h2>按盘查看知识库清单</h2><p>为了不让总览每次都遍历全部来源，这张清单默认按需打开。展开后可按盘查看已纳入文件、处理层级和原文件大小。</p></div><button type="button" onClick={() => setOpened(true)}>打开资料清单</button></section>;
  const disks: Data[] = data.data?.disks ?? [];
  return <section className="foundation-panel included-browser">
    <header><div><small>已入库文件</small><h2>知识库资料清单</h2><p>原件留在原位置；同内容的其他路径会单列为覆盖路径，不重复保存正文或向量。</p></div><button type="button" onClick={() => setOpened(false)}>收起清单</button></header>
    {data.loading && <Loading text="正在读取已纳入资料清单…" />}
    <ReadIssue issue={data.issue} retry={data.retry} />
    {data.data && <><div className="included-level-summary"><div><small>规范正文</small><strong>{Number(data.data.canonical_total ?? 0).toLocaleString()}</strong><span>不含重复路径</span></div><div><small>全文可搜</small><strong>{levelCount(data.data, "fulltext").toLocaleString()}</strong><span>本地正文和切片</span></div><div><small>语义已登记</small><strong>{levelCount(data.data, "semantic").toLocaleString()}</strong><span>在线效果另行检查</span></div><div><small>派生摘要</small><strong>{levelCount(data.data, "summary").toLocaleString()}</strong><span>与原件分开</span></div><div><small>仅登记</small><strong>{levelCount(data.data, "record_only").toLocaleString()}</strong><span>不搜索正文</span></div></div><div className="included-disk-grid">{disks.map(disk => <button key={disk.key} className={drive === disk.key ? "active" : ""} onClick={() => { setDrive(disk.key); setOffset(0); }}><div><strong>{disk.label}</strong><span>{Number(disk.included_files ?? 0).toLocaleString()} 条路径</span></div>{disk.key !== "DERIVED" ? <><div className="disk-usage-track"><i style={{ width: `${Math.min(100, Number(disk.used_bytes ?? 0) / Math.max(1, Number(disk.total_bytes ?? 0)) * 100)}%` }} /></div><p>原文件 {formatSize(Number(disk.included_bytes ?? 0))} · 占本盘 {formatPercent(Number(disk.disk_share_percent ?? 0))}</p><small>{Number(disk.canonical_files ?? 0).toLocaleString()} 份正文 · 文件地图发现 {Number(disk.catalog_files ?? 0).toLocaleString()} 个</small></> : <><p>{Number(disk.summary_files ?? 0).toLocaleString()} 份派生摘要</p><small>不冒充原盘文件</small></>}</button>)}</div><div className="included-toolbar"><div><span>当前：{drive === "ALL" ? "所有已纳入资料" : disks.find(item => item.key === drive)?.label ?? drive}</span></div><span>内容库 {formatSize(Number(data.data.database_bytes ?? 0))} · 微信消息单独管理</span></div><div className="included-file-list" role="table"><div className="included-file-head" role="row"><span>名称</span><span>所在位置</span><span>纳入程度</span><span>原文件大小</span></div>{data.data.items.map((item: Data) => <div className="included-file-row" role="row" key={item.row_id ?? item.id}><div><i>{item.level === "summary" ? "◇" : "▤"}</i><strong>{item.name}</strong><small>{item.is_alias ? "同内容覆盖路径" : item.source_type}</small></div><code title={item.path}>{item.path}</code><div><b className={`included-level ${item.level}`}>{includedLevelLabels[item.level] ?? item.level}</b><small>{Number(item.chunks ?? 0)} 块 · {Number(item.vectors ?? 0)} 向量</small></div><span>{formatSize(Number(item.byte_size ?? 0))}</span></div>)}{!data.data.items.length && <p>这个位置暂无已纳入资料；文件地图可能仍记录了路径。</p>}</div><div className="foundation-pager"><button disabled={!offset || data.loading} onClick={() => setOffset(value => Math.max(0, value - 50))}>上一页</button><span>{data.data.total ? `${offset + 1}–${Math.min(offset + 50, data.data.total)}` : "0"} / {data.data.total}</span><button disabled={!data.data.has_more || data.loading} onClick={() => setOffset(value => value + 50)}>下一页</button></div><p className="chart-footnote">{data.data.note}</p></>}
  </section>;
}

type ChartItem = { label: string; value: number; color: string };

function DonutChart({ title, subtitle, items, empty }: { title: string; subtitle: string; items: ChartItem[]; empty: string }) {
  const visible = items.filter(item => item.value > 0);
  const total = visible.reduce((sum, item) => sum + item.value, 0);
  const radius = 52;
  const circumference = 2 * Math.PI * radius;
  let consumed = 0;
  return <article className="foundation-chart-card"><header><div><h3>{title}</h3><p>{subtitle}</p></div></header>{total ? <div className="donut-layout"><svg className="donut-chart" viewBox="0 0 140 140" role="img" aria-label={`${title}，合计 ${total.toLocaleString()}`}><circle className="donut-track" cx="70" cy="70" r={radius} />{visible.map(item => { const length = item.value / total * circumference; const offset = -consumed; consumed += length; return <circle key={item.label} className="donut-segment" cx="70" cy="70" r={radius} stroke={item.color} strokeDasharray={`${length} ${circumference - length}`} strokeDashoffset={offset}><title>{item.label}：{item.value.toLocaleString()}</title></circle>; })}<text x="70" y="66" textAnchor="middle" className="donut-total">{compactNumber(total)}</text><text x="70" y="84" textAnchor="middle" className="donut-caption">合计</text></svg><div className="chart-legend">{visible.map(item => <div key={item.label}><i style={{ background: item.color }} /><span>{item.label}</span><strong>{item.value.toLocaleString()}</strong></div>)}</div></div> : <p className="foundation-chart-empty">{empty}</p>}</article>;
}

function TrendChart({ items }: { items: Data[] }) {
  const width = 660, height = 190, padX = 28, padTop = 20, padBottom = 34;
  const max = Math.max(1, ...items.map(item => Number(item.count ?? 0)));
  const points = items.map((item, index) => ({ date: String(item.date ?? ""), count: Number(item.count ?? 0), x: padX + (items.length <= 1 ? 0 : index / (items.length - 1) * (width - padX * 2)), y: padTop + (1 - Number(item.count ?? 0) / max) * (height - padTop - padBottom) }));
  const line = points.map(point => `${point.x},${point.y}`).join(" ");
  const area = points.length ? `${padX},${height - padBottom} ${line} ${points.at(-1)?.x},${height - padBottom}` : "";
  return <article className="foundation-chart-card trend-card"><header><div><h3>最近 30 天入库趋势</h3><p>按进入正式资料库的日期统计，不含聊天和 Codex 任务记录。</p></div><b className="trend-peak">峰值 {max.toLocaleString()}</b></header>{points.length ? <svg className="trend-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="最近30天入库趋势"><line x1={padX} y1={height - padBottom} x2={width - padX} y2={height - padBottom} /><polygon className="trend-area" points={area} /><polyline className="trend-line" points={line} />{points.filter((_, index) => index % Math.max(1, Math.floor(points.length / 6)) === 0 || index === points.length - 1).map(point => <g key={point.date}><circle className="trend-point" cx={point.x} cy={point.y} r="3" /><text x={point.x} y={height - 12} textAnchor="middle">{point.date.slice(5)}</text><title>{point.date}：{point.count.toLocaleString()}</title></g>)}</svg> : <p className="foundation-chart-empty">还没有可显示的趋势。</p>}</article>;
}

function parserLabel(version: unknown) { return version === "hybrid-v2" ? "解析 V2" : "解析版本待核查"; }
function fulltextLabel(value: unknown) { return ({ indexed: "全文已完成深度核对", recorded: "正文切片已记录", needs_global_check: "全文对账发现差异", no_chunks: "尚未产生全文切片" } as Record<string, string>)[String(value)] ?? "状态待核查"; }
function parsingAttentionLabel(document: Data) {
  const labels: Record<string, string> = { empty_native_text: "未提取到正文", low_text_or_scanned_pdf_pages: "疑似扫描或低文字 PDF", visual_pixels_not_read: "图片像素文字未读取", embedded_images_not_ocrd: "内嵌图片文字未读取", charts_need_semantic_parsing: "图表语义尚未解析", replacement_characters: "存在异常字符" };
  const reasons = (document.quality_reasons ?? []).map((reason: string) => labels[reason] ?? "存在解析质量提示").filter((value: string, index: number, all: string[]) => all.indexOf(value) === index);
  return reasons.length ? reasons.join("；") : "存在自动解析提示";
}
function groupedSourceTypes(rows: Data[]): ChartItem[] { return rows.map((row, index) => ({ label: String(row.type ?? "其他"), value: Number(row.count ?? 0), color: chartColors[index % chartColors.length] })); }
const includedLevelLabels: Record<string, string> = { record_only: "只登记", fulltext: "全文可搜", semantic: "全文＋向量", summary: "AI 摘要" };
function levelCount(data: Data | null, level: string) { return Number(data?.canonical_level_counts?.[level] ?? 0); }
function catalogStateLabel(state: string) { return ({ not_started: "尚未建立", running: "正在建立", paused: "已暂停", completed: "资料地图已完成", warning: "部分范围需检查" } as Record<string, string>)[state] ?? state; }
function formatSize(value: number) { if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`; if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`; return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`; }
function formatPercent(value: number) { if (!value) return "0%"; if (value < 0.01) return `${value.toFixed(4)}%`; return `${value.toFixed(2)}%`; }
function compactNumber(value: number) { if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`; if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}K`; return String(value); }
function scopeLabel(path: string) { const normalized = String(path ?? ""); const drive = normalized.match(/^([A-Z]):\\/i)?.[1]?.toUpperCase(); return drive ? `${drive} 盘` : normalized || "其他"; }
