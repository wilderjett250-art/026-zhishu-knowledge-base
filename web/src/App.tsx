import { FormEvent, lazy, ReactNode, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { api, Envelope, post } from "./api";
import { PRODUCT } from "./brand";
import DesktopTitlebar from "./DesktopTitlebar";

const CapabilityCenter = lazy(() => import("./CapabilityCenter"));
const RagLab = lazy(() => import("./RagLab"));
const ReadinessCenter = lazy(() => import("./ReadinessCenter"));
const RuntimeCenter = lazy(() => import("./RuntimeCenter"));
const FoundationCenter = lazy(() => import("./FoundationCenter"));
const DocumentParsingSettings = lazy(() => import("./DocumentParsingSettings"));

type Page = "home" | "foundation" | "runtime" | "readiness" | "capabilities" | "knowledge" | "rag" | "customers" | "timeline" | "import" | "workflows" | "persona" | "distill" | "settings";
type Json = Record<string, any>;

const nav: Array<{ id: Page; label: string; mark: string }> = [
  { id: "home", label: "工作台", mark: "总" },
  { id: "foundation", label: "资料管理", mark: "资" },
  { id: "runtime", label: "运行状态", mark: "运" },
  { id: "readiness", label: "系统检查", mark: "检" },
  { id: "capabilities", label: "能力管理", mark: "能" },
  { id: "knowledge", label: "知识检索", mark: "知" },
  { id: "rag", label: "检索评测", mark: "评" },
  { id: "customers", label: "客户会话", mark: "客" },
  { id: "timeline", label: "工作记录", mark: "记" },
  { id: "import", label: "资料导入", mark: "导" },
  { id: "workflows", label: "自动化任务", mark: "任" },
  { id: "persona", label: "个人画像", mark: "像" },
  { id: "distill", label: "训练数据", mark: "训" },
  { id: "settings", label: "系统设置", mark: "设" },
];
const pageIds = new Set<Page>(nav.map((item) => item.id));

function initialPage(): Page {
  const requested = new URLSearchParams(window.location.search).get("page") as Page | null;
  return requested && pageIds.has(requested) ? requested : "home";
}

const pageMeta: Record<Page, { section: string; title: string; intro: string }> = {
  foundation: { section: "资料管理", title: "资料底座", intro: "查看资料范围、分类结果和入库状态。" },
  home: { section: "工作台", title: "个人知识工作台", intro: "集中查看资料、检索和系统状态。" },
  runtime: { section: "服务状态", title: "运行状态", intro: "查看本机服务、语义检索和后台任务的状态。" },
  readiness: { section: "系统状态", title: "系统检查", intro: "检查资料索引、检索服务和客户端接入情况。" },
  capabilities: { section: "能力管理", title: "能力管理", intro: "统一查看 Skill、MCP 和客户端配置。" },
  knowledge: { section: "知识检索", title: "知识检索", intro: "检索本机资料，并返回可追溯的原始来源。" },
  rag: { section: "检索质量", title: "检索评测", intro: "查看全文、向量和混合检索的覆盖与评测结果。" },
  customers: { section: "客户资料", title: "客户会话", intro: "管理已确认的客户会话、业务事项和回复依据。" },
  timeline: { section: "工作记录", title: "工作记录", intro: "按日期查看已确认事项、推断和待办。" },
  import: { section: "资料导入", title: "资料导入", intro: "从已导出的 WeFlow 文件或指定路径导入资料。" },
  workflows: { section: "自动化任务", title: "自动化任务", intro: "查看可执行任务、执行记录和处理结果。" },
  persona: { section: "个人画像", title: "个人画像", intro: "管理有证据支持的个人偏好与工作习惯。" },
  distill: { section: "训练数据", title: "训练数据", intro: "整理可审核、可导出的个人训练样本。" },
  settings: { section: "系统设置", title: "系统设置", intro: "查看本机运行、文档解析和操作记录。" },
};

function App() {
  const [page, setPage] = useState<Page>(initialPage);
  const [dashboard, setDashboard] = useState<Json | null>(null);
  const [health, setHealth] = useState<Json | null>(null);
  const [sources, setSources] = useState<Json[]>([]);
  const [syncRoots, setSyncRoots] = useState<Json[]>([]);
  const [workflowDefs, setWorkflowDefs] = useState<Json[]>([]);
  const [workflowRuns, setWorkflowRuns] = useState<Json[]>([]);
  const [persona, setPersona] = useState<Json[]>([]);
  const [distillation, setDistillation] = useState<Json[]>([]);
  const [audit, setAudit] = useState<Json[]>([]);
  const [customers, setCustomers] = useState<Json[]>([]);
  const [customerSignals, setCustomerSignals] = useState<Json[]>([]);
  const [notice, setNotice] = useState<{ status: string; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [evidence, setEvidence] = useState<Json | null>(null);
  const [pageRefresh, setPageRefresh] = useState(0);

  const refresh = useCallback(async () => {
    if (["readiness", "runtime", "foundation", "capabilities", "rag", "timeline", "import"].includes(page)) {
      setPageRefresh(value => value + 1);
      return;
    }
    const requests: Array<{ request: Promise<Envelope<any>>; accept: (data: any) => void }> = [];
    if (page === "home") {
      requests.push({ request: api<Json>("/api/dashboard"), accept: setDashboard });
      requests.push({ request: api<Json>("/api/health"), accept: setHealth });
    } else if (page === "knowledge") {
      requests.push({ request: api<Json[]>("/api/sources?limit=100"), accept: setSources });
      requests.push({ request: api<Json[]>("/api/sync/roots"), accept: setSyncRoots });
    } else if (page === "customers") {
      requests.push({ request: api<Json[]>("/api/customers?limit=500"), accept: setCustomers });
      requests.push({ request: api<Json[]>("/api/customer-signals?limit=500"), accept: setCustomerSignals });
    } else if (page === "workflows") {
      requests.push({ request: api<Json[]>("/api/workflows"), accept: setWorkflowDefs });
      requests.push({ request: api<Json[]>("/api/workflows/runs?limit=50"), accept: setWorkflowRuns });
    } else if (page === "persona") {
      requests.push({ request: api<Json[]>("/api/persona?limit=100"), accept: setPersona });
    } else if (page === "distill") {
      requests.push({ request: api<Json[]>("/api/distillation?limit=100"), accept: setDistillation });
    } else if (page === "settings") {
      requests.push({ request: api<Json>("/api/health"), accept: setHealth });
      requests.push({ request: api<Json[]>("/api/audit?limit=100"), accept: setAudit });
    }
    const results = await Promise.allSettled(requests.map(item => item.request));
    results.forEach((result, index) => {
      if (result.status === "fulfilled") requests[index].accept(result.value.data);
    });
    if (results.some((result) => result.status === "rejected")) {
      setNotice({ status: "warning", text: "部分系统数据暂时未能读取，请确认后端已启动。" });
    }
  }, [page]);

  useEffect(() => { void refresh(); }, [refresh]);

  const run = async <T,>(job: () => Promise<Envelope<T>>, after?: (data: T) => void) => {
    setBusy(true);
    try {
      const result = await job();
      const guidance = result.next_actions.filter(Boolean);
      setNotice({
        status: result.status,
        text: guidance.length ? `${result.summary} · ${guidance.join(" · ")}` : result.summary,
      });
      after?.(result.data);
      await refresh();
      return result;
    } catch (error) {
      setNotice({ status: "error", text: error instanceof Error ? error.message : "操作失败" });
      return null;
    } finally {
      setBusy(false);
    }
  };

  const meta = pageMeta[page];
  const navigate = (nextPage: Page, foundationTab?: "intake") => {
    setPage(nextPage);
    const url = new URL(window.location.href);
    if (nextPage === "home") {
      url.searchParams.delete("page");
      url.searchParams.delete("foundationTab");
    } else {
      url.searchParams.set("page", nextPage);
      if (nextPage === "foundation" && foundationTab) url.searchParams.set("foundationTab", foundationTab);
      else url.searchParams.delete("foundationTab");
    }
    window.history.replaceState({}, "", url);
    if (nextPage === page && nextPage === "foundation") setPageRefresh(value => value + 1);
  };
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => navigate("home")} aria-label="返回总览">
          <span className="brand-glyph"><i /><i /><i /></span>
          <span><strong>{PRODUCT.name}</strong><small>{PRODUCT.shortTagline}</small></span>
        </button>
        <div className="domain-pulse">
          <span className="pulse-dot" />
          <span>本地运行 · 数据由你控制</span>
        </div>
        <nav aria-label="主导航">
          {nav.map((item) => (
            <button key={item.id} className={page === item.id ? "active" : ""} onClick={() => navigate(item.id)}>
              <span className="nav-mark">{item.mark}</span><span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span>资料状态</span>
          <strong>{health ? "已连接" : ["runtime", "readiness", "foundation"].includes(page) ? "见当前页面" : "未检查"}</strong>
          <small>{dashboard ? `${dashboard.counts?.knowledge_chunks ?? 0} 个知识片段` : ["runtime", "readiness", "foundation"].includes(page) ? "按页面显示真实状态" : "资料数量未读取"}</small>
        </div>
      </aside>

      <main className="main-stage">
        <DesktopTitlebar>
          <div className="breadcrumb"><span>{PRODUCT.name}</span><b>/</b><strong>{nav.find((item) => item.id === page)?.label}</strong></div>
          <div className="top-actions">
            <button className="quiet-button" onClick={() => void refresh()} disabled={busy}>刷新</button>
            <span className="health-pill"><i /> 本机运行</span>
          </div>
        </DesktopTitlebar>
        <div className="content-wrap">
          <section className="page-heading">
            <div><p>{meta.section}</p><h1>{meta.title}</h1><span>{meta.intro}</span></div>
          </section>
          {notice && <div className={`notice ${notice.status}`}><span>{notice.text}</span><button onClick={() => setNotice(null)}>×</button></div>}

          {page === "home" && <Home dashboard={dashboard} setPage={navigate} setEvidence={setEvidence} run={run} />}
          {page === "readiness" && <DeferredPage><ReadinessCenter refreshKey={pageRefresh} /></DeferredPage>}
          {page === "runtime" && <DeferredPage><RuntimeCenter key={pageRefresh} /></DeferredPage>}
          {page === "foundation" && <DeferredPage><FoundationCenter key={pageRefresh} /></DeferredPage>}
          {page === "capabilities" && <DeferredPage><CapabilityCenter key={pageRefresh} /></DeferredPage>}
          {page === "knowledge" && <Knowledge sources={sources} syncRoots={syncRoots} run={run} setEvidence={setEvidence} busy={busy} />}
          {page === "rag" && <DeferredPage><RagLab key={pageRefresh} busy={busy} setEvidence={setEvidence} /></DeferredPage>}
          {page === "customers" && <CustomersPanel customers={customers} signals={customerSignals} run={run} setEvidence={setEvidence} busy={busy} />}
          {page === "timeline" && <PersonalTimeline key={pageRefresh} run={run} busy={busy} setEvidence={setEvidence} />}
          {page === "import" && <ImportPanel key={pageRefresh} run={run} busy={busy} />}
          {page === "workflows" && <Workflows definitions={workflowDefs} runs={workflowRuns} run={run} busy={busy} />}
          {page === "persona" && <PersonaPanel items={persona} run={run} busy={busy} />}
          {page === "distill" && <DistillPanel items={distillation} run={run} busy={busy} />}
          {page === "settings" && <SettingsPanel health={health} audit={audit} />}
        </div>
      </main>
      <EvidenceSpine evidence={evidence} close={() => setEvidence(null)} />
    </div>
  );
}

function DeferredPage({ children }: { children: ReactNode }) {
  return <Suspense fallback={<section className="panel"><div className="panel-body"><div className="empty-state"><span>…</span><p>正在加载当前功能，不会扫描资料或调用模型。</p></div></div></section>}>{children}</Suspense>;
}

function Home({ dashboard, setPage, setEvidence, run }: { dashboard: Json | null; setPage: (page: Page, foundationTab?: "intake") => void; setEvidence: (data: Json) => void; run: Runner }) {
  const counts = dashboard?.counts ?? {};
  const openSource = (item: Json) => item.document_id
    ? void run(() => api<Json>(`/api/documents/${item.document_id}`), setEvidence)
    : setEvidence(item);
  return <>
    <section className="command-deck">
      <div className="command-copy"><span>现在想查什么？</span><strong>搜索你的资料，并回到原始证据</strong></div>
      <div className="command-actions"><button className="accent" onClick={() => setPage("knowledge")}>搜索资料 <b>⌘ K</b></button></div>
    </section>
    {Number(counts.knowledge_sources ?? 0) === 0 && <FirstUseGuide setPage={setPage} />}
    <section className="metric-grid">
      <Metric label="微信会话" value={counts.customers ?? 0} unit="会话" tone="cyan" />
      <Metric label="客户聊天消息" value={counts.customer_messages ?? 0} unit="消息" tone="violet" />
      <Metric label="业务事项" value={counts.customer_signals ?? 0} unit="事项" tone="amber" />
      <Metric label="知识片段" value={counts.knowledge_chunks ?? 0} unit="片段" tone="green" />
    </section>
    <section className="two-column">
      <Panel title="领域分布" code="DOMAIN MAP">
        <div className="domain-map">
          {[{ key: "work", label: "业务 / 工作", tone: "cyan" }, { key: "self", label: "自我 / 对话", tone: "amber" }, { key: "shared", label: "共享知识", tone: "violet" }, { key: "distill", label: "蒸馏资料", tone: "green" }].map((item) => {
            const value = dashboard?.domains?.[item.key] ?? 0;
            const total = Math.max(1, counts.knowledge_sources ?? 0);
            return <div className="domain-row" key={item.key}><span>{item.label}<b>{value}</b></span><div><i className={item.tone} style={{ width: `${Math.max(value ? 8 : 0, (value / total) * 100)}%` }} /></div></div>;
          })}
        </div>
      </Panel>
      <Panel title="最近进入知识库" code="PROVENANCE">
        <ItemList items={dashboard?.recent_sources ?? []} empty="还没有资料。进入“资料接入”指定一个明确路径。" render={(item) => <button className="line-item" onClick={() => openSource(item)}><span className="file-mark">{String(item.source_type ?? "FILE").slice(0, 4).toUpperCase()}</span><span><strong>{item.title ?? item.original_name}</strong><small>{item.domain} · {item.privacy}</small></span><time>{formatTime(item.ingested_at)}</time></button>} />
      </Panel>
    </section>
    <Panel title="最近运行" code="ACTIVITY TRACE">
      <ItemList items={dashboard?.recent_runs ?? []} empty="尚无工作流运行。" render={(item) => <div className="run-row"><span className={`status-dot ${item.status}`} /><span><strong>{workflowName(item.workflow_name)}</strong><small>{item.id}</small></span><b className={`status-tag ${item.status}`}>{statusLabel(item.status)}</b><time>{formatTime(item.created_at)}</time></div>} />
    </Panel>
  </>;
}

function FirstUseGuide({ setPage }: { setPage: (page: Page, foundationTab?: "intake") => void }) {
  return <section className="first-use-guide" aria-labelledby="first-use-title">
    <header>
      <div><small>首次使用</small><h2 id="first-use-title">建立你的知识库</h2><p>{PRODUCT.name}负责把资料整理成可搜索的底座；是否扫描、哪些内容深入处理，由你确认。</p></div>
      <span className="first-use-local">本地优先 · 可随时暂停</span>
    </header>
    <div className="first-use-steps">
      <article><b>01</b><div><strong>选整理方式</strong><p>选择预设方案，确认自动建议的资料范围。</p></div></article>
      <article><b>02</b><div><strong>开始整理资料</strong><p>先建立文件目录和分类；重要内容再进入全文或向量搜索。</p></div></article>
      <article><b>03</b><div><strong>搜索并连接 Codex</strong><p>先在{PRODUCT.name}验证召回；MCP 接入是可选项，不会擅自修改客户端设置。</p></div></article>
    </div>
    <footer>
      <button className="accent" onClick={() => setPage("foundation", "intake")}>开始第一次资料接入 <span>→</span></button>
      <span>安装不会自动扫盘、导入微信或调用模型。AI 分类、摘要和向量化按你选的方案及已配置服务运行。</span>
    </footer>
  </section>;
}

function Knowledge({ sources, syncRoots, run, setEvidence, busy }: { sources: Json[]; syncRoots: Json[]; run: Runner; setEvidence: (data: Json) => void; busy: boolean }) {
  const [query, setQuery] = useState("");
  const [domain, setDomain] = useState("");
  const [restricted, setRestricted] = useState(false);
  const [results, setResults] = useState<Json[]>([]);
  const [catalogQuery, setCatalogQuery] = useState("");
  const [catalogRoot, setCatalogRoot] = useState("");
  const [catalogResults, setCatalogResults] = useState<Json[]>([]);
  const submit = (event: FormEvent) => { event.preventDefault(); if (!query.trim()) return; void run(() => post<Json[]>("/api/search", { query, domain: domain || null, limit: 20, include_restricted: restricted }), setResults); };
  const openDocument = (item: Json) => void run(() => api<Json>(`/api/documents/${item.document_id}`), setEvidence);
  const searchCatalog = (event: FormEvent) => { event.preventDefault(); if (!catalogQuery.trim()) return; void run(() => post<Json[]>("/api/sync/catalog/search", { query: catalogQuery, root_id: catalogRoot || null, limit: 50 }), setCatalogResults); };
  const scanRoot = (rootId: string) => void run(() => api<Json>(`/api/sync/roots/${rootId}/scan`, { method: "POST" }));
  return <>
    <form className="search-console" onSubmit={submit}>
      <span className="search-glyph">⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入项目、人物、决策、聊天内容或问题…" aria-label="知识检索" />
      <select value={domain} onChange={(event) => setDomain(event.target.value)}><option value="">全部领域</option><option value="work">业务 / 工作</option><option value="self">自我 / 对话</option><option value="shared">共享</option><option value="distill">蒸馏</option></select>
      <button type="submit">检索证据</button>
    </form>
    <label className="check-line"><input type="checkbox" checked={restricted} onChange={(event) => setRestricted(event.target.checked)} /> 本次检索允许返回 restricted 资料</label>
    <section className="knowledge-layout">
      <Panel title={`检索结果 · ${results.length}`} code="EVIDENCE MATCH">
        <ItemList items={results} empty="输入问题后，这里会显示带原文件路径和段落定位的结果。" render={(item, index) => <button className="result-card" onClick={() => openDocument(item)}><span className="result-rank">{String(index + 1).padStart(2, "0")}</span><span><strong>{item.title}</strong><p>{item.snippet}</p><small>{item.domain} / {item.privacy} · {item.locator}</small></span><b>查看原文 →</b></button>} />
      </Panel>
      <Panel title={`资料来源 · ${sources.length}`} code="SOURCE REGISTRY">
        <ItemList items={sources} empty="尚未导入资料。" render={(item) => <button className="source-card" onClick={() => item.document_id ? openDocument(item) : setEvidence(item)}><span className="file-mark">{String(item.source_type).toUpperCase()}</span><span><strong>{item.title ?? item.original_name}</strong><small>{compactPath(item.original_uri)}</small></span><b>{formatBytes(item.byte_size)}</b></button>} />
      </Panel>
    </section>
    <div className="section-divider"><span>持续资料</span><b>资料地图与增量同步</b></div>
    <section className="sync-overview">
      <Panel title={`持续资料源 · ${syncRoots.length}`} code="AUTO REFRESH / 03:30">
        <div className="sync-root-list">
          {syncRoots.length ? syncRoots.map((root) => <article className="sync-root-card" key={root.id}>
            <header><span className={`status-dot ${root.error_count ? "warning" : "completed"}`} /><div><strong>{root.name}</strong><small>{root.connector_type === "codex_sessions" ? "Codex 任务记录" : root.sync_mode === "index" ? "全文检索" : "目录索引"}</small></div><button disabled={busy} onClick={() => scanRoot(root.id)}>增量刷新</button></header>
            <code title={root.root_uri}>{compactPath(root.root_uri)}</code>
            <div className="sync-root-metrics"><span><b>{formatNumber(root.active_count)}</b><small>有效项</small></span><span><b>{formatNumber(root.indexed_count)}</b><small>已索引</small></span><span><b>{formatNumber(root.skipped_count)}</b><small>安全跳过</small></span><span><b>{formatNumber(root.error_count)}</b><small>不可读取</small></span></div>
            <footer><span>{root.domain} / {root.privacy}</span><time>上次同步 {formatTime(root.last_scan_at)}</time></footer>
          </article>) : <Empty text="尚未注册持续资料源。" />}
        </div>
      </Panel>
      <Panel title={`现成文件定位 · ${catalogResults.length}`} code="PATH CATALOG">
        <form className="catalog-search" onSubmit={searchCatalog}>
          <input value={catalogQuery} onChange={(event) => setCatalogQuery(event.target.value)} placeholder="输入文件名或路径片段，例如 project.config.json" />
          <select value={catalogRoot} onChange={(event) => setCatalogRoot(event.target.value)}><option value="">全部资料源</option>{syncRoots.filter((root) => root.connector_type === "local_files").map((root) => <option key={root.id} value={root.id}>{root.name}</option>)}</select>
          <button disabled={busy || !catalogQuery.trim()} type="submit">搜索资料地图</button>
        </form>
        <ItemList items={catalogResults} empty="这里可以定位尚未抽取正文的现成项目文件。" render={(item) => <button className="source-card" onClick={() => setEvidence(item)}><span className="file-mark">目录</span><span><strong>{item.relative_path}</strong><small>{item.root_name} · {item.state}</small></span><b>{formatBytes(item.byte_size)}</b></button>} />
      </Panel>
    </section>
  </>;
}

function CustomersPanel({ customers, signals, run, setEvidence, busy }: { customers: Json[]; signals: Json[]; run: Runner; setEvidence: (data: Json) => void; busy: boolean }) {
  const [selectedId, setSelectedId] = useState("");
  const [timeline, setTimeline] = useState<Json[]>([]);
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<Json[]>([]);
  const [replyTask, setReplyTask] = useState("");
  const [replyContext, setReplyContext] = useState<Json | null>(null);
  const [signalType, setSignalType] = useState("requirement");
  const [signalStatement, setSignalStatement] = useState("");
  const [company, setCompany] = useState("");
  const [stage, setStage] = useState("active");
  const [tags, setTags] = useState("");
  const [customerSummary, setCustomerSummary] = useState("");
  const selected = useMemo(() => customers.find((item) => item.id === selectedId) ?? customers[0] ?? null, [customers, selectedId]);
  const selectedSignals = useMemo(() => signals.filter((item) => item.customer_id === selected?.id), [signals, selected?.id]);

  useEffect(() => {
    if (!selectedId && customers[0]) setSelectedId(customers[0].id);
  }, [customers, selectedId]);

  useEffect(() => {
    if (!selected) return;
    setCompany(selected.company || "");
    setStage(selected.stage || "active");
    setTags((selected.tags || []).join(", "));
    setCustomerSummary(selected.summary || "");
  }, [selected?.id]);

  const loadTimeline = (customer: Json) => {
    setSelectedId(customer.id);
    setReplyContext(null);
    void run(() => api<Json[]>(`/api/customers/${customer.id}/timeline?limit=120&include_restricted=true`), setTimeline);
  };
  const search = (event: FormEvent) => {
    event.preventDefault();
    if (!selected || !query.trim()) return;
    void run(() => post<Json[]>("/api/customer-messages/search", { query, customer_id: selected.id, limit: 50, include_restricted: true }), setSearchResults);
  };
  const prepareReply = (event: FormEvent) => {
    event.preventDefault();
    if (!selected || !replyTask.trim()) return;
    void run(() => post<Json>("/api/customer-reply/context", { customer_id: selected.id, task: replyTask, recent_limit: 50, search_limit: 30, include_restricted: true }), setReplyContext);
  };
  const addSignal = (event: FormEvent) => {
    event.preventDefault();
    if (!selected || !signalStatement.trim()) return;
    void run(() => post<Json>("/api/customer-signals", { customer_id: selected.id, signal_type: signalType, statement: signalStatement, status: "open", due_at: null, evidence_message_ids: [], confidence: "medium" }), () => setSignalStatement(""));
  };
  const reviewSignal = (id: string, decision: string) => void run(() => post(`/api/customer-signals/${id}/review`, { decision, reason: "在微信客户工作台审核" }));
  const saveProfile = (event: FormEvent) => {
    event.preventDefault();
    if (!selected) return;
    void run(() => post<Json>(`/api/customers/${selected.id}`, { company, stage, tags: tags.split(/[,，]/).map((item) => item.trim()).filter(Boolean), summary: customerSummary, review_status: "approved" }));
  };

  if (!customers.length) return <div className="customer-empty"><span>客户会话</span><h2>还没有微信会话</h2><p>进入“资料导入”，读取 WeFlow 已导出的 XLSX。导入后会先进入待归类区，不会自动认定为客户。</p></div>;
  return <section className="customer-workbench">
    <aside className="customer-rail">
      <header><strong>微信会话 / 客户</strong><span>{customers.length}</span></header>
      <div>{customers.map((item) => <button key={item.id} className={selected?.id === item.id ? "active" : ""} onClick={() => loadTimeline(item)}><span className="customer-avatar">{String(item.display_name).slice(0, 1)}</span><span><strong>{item.display_name}</strong><small>{customerReviewLabel(item.review_status)} · {item.company || customerTypeLabel(item.customer_type)} · {item.message_count} 条</small></span>{item.open_signals > 0 && <b>{item.open_signals}</b>}</button>)}</div>
    </aside>
    <div className="customer-main">
      {selected && <>
        <section className="customer-identity"><div><small>微信会话 / {selected.platform_id}</small><h2>{selected.display_name}</h2><p>{selected.summary || (selected.review_status === "approved" ? "尚未填写客户摘要；Codex 可以基于已审核信号辅助整理。" : "当前只是待归类微信会话；核对身份后再确认为业务客户。")}</p></div><div className="customer-facts"><span><small>归类</small><b>{customerReviewLabel(selected.review_status)}</b></span><span><small>消息</small><b>{selected.message_count}</b></span><span><small>待处理</small><b>{selected.open_signals}</b></span></div></section>
        <div className="customer-tabs">
          <form className="customer-search" onSubmit={search}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="检索这个客户的需求、报价、进度或承诺" /><button disabled={busy}>检索聊天证据</button></form>
        </div>
        {searchResults.length > 0 && <Panel title={`聊天检索 · ${searchResults.length}`} code="MATCHED EVIDENCE"><ItemList items={searchResults} empty="没有匹配消息。" render={(item) => <button className="result-card customer-result" onClick={() => setEvidence(item)}><span className="result-rank">{item.is_self ? "我" : "客"}</span><span><strong>{item.sender_name || item.customer_name}</strong><p>{item.snippet}</p><small>{formatUnixTime(item.sent_at)} · {item.platform_message_id || item.message_id}</small></span><b>证据 →</b></button>} /></Panel>}
        <section className="customer-columns">
          <Panel title={`会话时间线 · ${timeline.length}`} code="RESTRICTED / AUTHORIZED"><ItemList items={timeline} empty="点击左侧客户加载已授权的聊天时间线。" render={(item) => <button className={`chat-line ${item.is_self ? "self" : "customer"}`} onClick={() => setEvidence(item)}><span>{item.is_self ? "我" : (item.sender_name || selected.display_name)}</span><p>{item.content}</p><time>{formatUnixTime(item.sent_at)}</time></button>} /></Panel>
          <div className="customer-side-stack">
            <Panel title="会话归类 / 客户档案" code="CURATED PROFILE"><form className="profile-form" onSubmit={saveProfile}><div><input value={company} onChange={(event) => setCompany(event.target.value)} placeholder="公司 / 组织" /><select value={stage} onChange={(event) => setStage(event.target.value)}><option value="lead">线索</option><option value="active">沟通中</option><option value="delivery">交付中</option><option value="after_sales">售后</option><option value="paused">暂停</option><option value="closed">已结束</option></select></div><input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="标签，用逗号分隔" /><textarea value={customerSummary} onChange={(event) => setCustomerSummary(event.target.value)} placeholder="只写经过核对的客户摘要" /><button disabled={busy}>{selected.review_status === "approved" ? "保存已确认客户档案" : "确认为客户并保存档案"}</button></form></Panel>
            <Panel title="Codex 回复上下文" code="DRAFT ONLY">{selected.review_status !== "approved" ? <div className="boundary-note"><strong>等待客户身份确认</strong><p>普通或私人微信会话不会进入客户回复工作流。请先核对身份并保存为已确认客户。</p></div> : <><form className="form-stack" onSubmit={prepareReply}><Field label="客户当前问题或你的任务"><textarea value={replyTask} onChange={(event) => setReplyTask(event.target.value)} placeholder="例如：根据历史沟通，起草一份关于报价时间和三家门店数据看板的回复。" /></Field><button className="primary-button" disabled={busy}>准备完整回复依据</button></form>{replyContext && <div className="context-summary"><strong>{replyContext.summary}</strong><ol>{replyContext.plan.map((step: string) => <li key={step}>{step}</li>)}</ol><small>上下文已记录，可交给 Codex 生成回复草稿；系统不会直接发送微信。</small></div>}</>}</Panel>
            <Panel title={`业务信号 · ${selectedSignals.length}`} code="HUMAN REVIEW">{selected.review_status !== "approved" ? <div className="boundary-note"><strong>尚未启用业务沉淀</strong><p>只有已确认客户才能保存需求、承诺、待办和风险。</p></div> : <><form className="signal-form" onSubmit={addSignal}><select value={signalType} onChange={(event) => setSignalType(event.target.value)}><option value="requirement">需求</option><option value="commitment">承诺</option><option value="todo">待办</option><option value="risk">风险</option><option value="decision">决策</option><option value="follow_up">跟进</option><option value="preference">偏好</option></select><input value={signalStatement} onChange={(event) => setSignalStatement(event.target.value)} placeholder="写入一个有证据的候选事项" /><button disabled={busy}>保存候选</button></form><ItemList items={selectedSignals} empty="尚未沉淀需求、承诺或待办。" render={(item) => <article className="signal-card"><header><span>{signalTypeLabel(item.signal_type)}</span><b className={`approval ${item.approval_status}`}>{approvalLabel(item.approval_status)}</b></header><p>{item.statement}</p>{item.approval_status === "candidate" && <footer><button onClick={() => reviewSignal(item.id, "rejected")}>驳回</button><button onClick={() => reviewSignal(item.id, "approved")}>批准</button></footer>}</article>} /></>}</Panel>
          </div>
        </section>
      </>}
    </div>
  </section>;
}

function ImportPanel({ run, busy }: { run: Runner; busy: boolean }) {
  const [manualSync, setManualSync] = useState<Json | null>(null);
  const [recordsPath, setRecordsPath] = useState("");
  const [exportCatalog, setExportCatalog] = useState<Json | null>(null);
  const [selectedExports, setSelectedExports] = useState<string[]>([]);
  const [exportKeyword, setExportKeyword] = useState("");
  const [exportInspection, setExportInspection] = useState<Json | null>(null);
  const [chatlabPath, setChatlabPath] = useState("");
  const [chatlabSessionId, setChatlabSessionId] = useState("");
  const [chatlabInspection, setChatlabInspection] = useState<Json | null>(null);
  const [path, setPath] = useState("");
  const [domain, setDomain] = useState("work");
  const [privacy, setPrivacy] = useState("private");
  const [recursive, setRecursive] = useState(true);
  const [inspection, setInspection] = useState<Json | null>(null);
  const [inspectedSignature, setInspectedSignature] = useState("");
  const loadManualSync = useCallback(async () => {
    try {
      const result = await api<Json>("/api/weflow/manual-sync/status");
      setManualSync(result.data);
    } catch {
      setManualSync(null);
    }
  }, []);
  const manualRunning = ["queued", "preparing", "exporting", "importing"].includes(manualSync?.status ?? "");
  useEffect(() => {
    void loadManualSync();
    if (!manualRunning) return;
    const timer = window.setInterval(() => { void loadManualSync(); }, 2000);
    return () => window.clearInterval(timer);
  }, [loadManualSync, manualRunning]);
  const signature = `${path}|${recursive}`;
  const inspect = () => void run(() => post<Json>("/api/import/inspect", { path, recursive }), (data) => { setInspection(data); setInspectedSignature(signature); });
  const execute = () => void run(() => post<Json>("/api/import/run", { path, recursive, domain, privacy, inspection_token: inspection?.inspection_token }), () => { setInspection(null); setInspectedSignature(""); });
  const exportItems = useMemo(() => (exportCatalog?.items ?? []).filter((item: Json) => `${item.display_name} ${item.session_id}`.toLowerCase().includes(exportKeyword.trim().toLowerCase())), [exportCatalog, exportKeyword]);
  const discoverExports = () => void run(() => post<Json>("/api/weflow/exports/discover", { records_path: recordsPath.trim() || null, keyword: "", limit: 1000 }), (data) => { setExportCatalog(data); setRecordsPath(data.records_path); setSelectedExports([]); setExportInspection(null); });
  const toggleExport = (id: string) => { setSelectedExports((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]); setExportInspection(null); };
  const selectVisibleExports = () => { setSelectedExports((current) => Array.from(new Set([...current, ...exportItems.map((item: Json) => item.session_id)]))); setExportInspection(null); };
  const clearExportSelection = () => { setSelectedExports([]); setExportInspection(null); };
  const inspectExports = () => void run(() => post<Json>("/api/weflow/exports/inspect", { records_path: recordsPath.trim() || null, session_ids: selectedExports }), setExportInspection);
  const importExports = () => void run(() => post<Json>("/api/weflow/exports/import", { records_path: recordsPath.trim() || null, session_ids: selectedExports, inspection_token: exportInspection?.inspection_token, privacy: "restricted" }), () => { setSelectedExports([]); setExportInspection(null); });
  const startManualSync = () => void run(
    () => post<Json>("/api/weflow/manual-sync/start", {}),
    (data) => { setManualSync(data); window.setTimeout(() => { void loadManualSync(); }, 800); },
  );
  const inspectChatLab = () => void run(() => post<Json>("/api/weflow/chatlab/inspect", { path: chatlabPath, session_id: chatlabSessionId || null }), setChatlabInspection);
  const importChatLab = () => void run(() => post<Json>("/api/weflow/chatlab/import", { path: chatlabPath, session_id: chatlabSessionId || null, inspection_token: chatlabInspection?.inspection_token, privacy: "restricted" }), () => setChatlabInspection(null));
  return <>
    <section className="manual-weflow-sync">
      <div>
        <small>手动增量同步</small>
        <h2>一键同步微信新消息</h2>
        <p>点击后读取最后入库水位，后台调用 WeFlow 导出已入库会话，再只写入新增消息。不会创建计划任务，也不会弹出终端。{manualExportDescription(manualSync?.export_freshness, manualSync?.usable_export_count)}</p>
      </div>
      <div className="manual-sync-metrics">
        <span><small>最新消息</small><strong>{formatTime(manualSync?.latest_message_at)}</strong></span>
        <span><small>最后入库</small><strong>{formatTime(manualSync?.last_import_at)}</strong></span>
        <span><small>同步范围</small><strong>{manualSync?.conversation_count ?? "—"} 个会话</strong></span>
        <span><small>当前状态</small><strong>{manualSyncLabel(manualSync?.status, manualSync?.summary_code)}</strong></span>
      </div>
      <button className="primary-button" disabled={busy || manualRunning || !manualSync?.weflow_configured || !manualSync?.records_available} onClick={startManualSync}>
        {manualRunning ? "正在同步…" : "同步微信新消息"}
      </button>
    </section>
    <section className="weflow-connect">
      <Panel title="WeFlow 导出记录" code="NO API / NO KEY">
        <div className="form-stack">
          <Field label="导出记录绝对路径" hint="留空会自动读取当前 Windows 用户的 WeFlow 配置目录"><input value={recordsPath} onChange={(event) => { setRecordsPath(event.target.value); setExportCatalog(null); setSelectedExports([]); setExportInspection(null); }} placeholder="自动定位 weflow-export-records.json" /></Field>
          <div className="inline-actions"><button className="primary-button" disabled={busy} onClick={discoverExports}>发现现存 XLSX 导出</button>{exportCatalog && <span className="connector-ok"><i /> EXPORT INDEX READY · API FREE</span>}</div>
          {exportCatalog && <div className="inspection-grid"><MetricMini label="会话" value={exportCatalog.total_sessions} /><MetricMini label="现存" value={exportCatalog.existing_sessions} /><MetricMini label="导出记录" value={exportCatalog.record_count} /><MetricMini label="现存文件" value={exportCatalog.existing_record_count} /></div>}
          <div className="privacy-strip"><strong>隐私边界</strong><span>只读 WeFlow 导出索引，不访问 decryptKey 或 WCDB；只导入你勾选的 XLSX，聊天统一标记 restricted。</span></div>
        </div>
      </Panel>
      <Panel title={`选择已导出会话 · ${selectedExports.length}`} code={`${exportItems.length} AVAILABLE`}>
        <div className="session-toolbar"><input value={exportKeyword} onChange={(event) => setExportKeyword(event.target.value)} placeholder="按导出文件名或 wxid 过滤" /><button disabled={!exportItems.length || busy} onClick={selectVisibleExports}>选择当前结果</button><button disabled={!selectedExports.length || busy} onClick={clearExportSelection}>清空选择</button><button disabled={busy} onClick={discoverExports}>重新发现</button></div>
        <div className="session-picker">{exportItems.length ? exportItems.map((item: Json) => <label key={item.session_id} className={selectedExports.includes(item.session_id) ? "selected" : ""}><input type="checkbox" checked={selectedExports.includes(item.session_id)} onChange={() => toggleExport(item.session_id)} /><span><strong>{item.display_name || item.session_id}</strong><small>{item.conversation_type} · {item.message_count ?? 0} 条 · {item.existing_export_count} 份现存导出</small></span><time>{formatUnixTime(item.export_time)}</time></label>) : <Empty text="点击“发现现存 XLSX 导出”。这里只读取导出记录元数据，不会自动导入聊天正文。" />}</div>
        {!exportInspection ? <button className="danger-safe-button sync-button" disabled={!selectedExports.length || busy} onClick={inspectExports}>只读检查所选 {selectedExports.length} 个 XLSX</button> : <div className="export-confirm"><div className="boundary-note"><strong>检查通过</strong><p>{exportInspection.selected_sessions} 个唯一会话，共声明 {exportInspection.total_messages} 条消息、{formatBytes(exportInspection.total_bytes)}；已跳过 {exportInspection.skipped_alias_sessions ?? 0} 条重复别名。确认后复制原始 XLSX 哈希快照并建立 restricted 待归类微信会话索引。</p></div><button className="danger-safe-button sync-button" disabled={busy} onClick={importExports}>确认导入待归类微信会话</button></div>}
      </Panel>
    </section>
    <section className="import-layout offline-import">
      <Panel title="WeFlow ChatLab 离线文件" code="OFFLINE / JSON"><div className="form-stack"><Field label="ChatLab JSON 绝对路径"><input value={chatlabPath} onChange={(event) => { setChatlabPath(event.target.value); setChatlabInspection(null); }} placeholder="例如 E:\\WeFlow导出\\客户张经理.json" /></Field><Field label="私聊 wxid" hint="通常可自动识别；无法识别时填写"><input value={chatlabSessionId} onChange={(event) => { setChatlabSessionId(event.target.value); setChatlabInspection(null); }} placeholder="可选：wxid_xxx" /></Field><button className="primary-button" disabled={!chatlabPath || busy} onClick={inspectChatLab}>只读检查 ChatLab 文件</button></div></Panel>
      <Panel title="会话结构确认" code="CHATLAB / 0.0.2">{!chatlabInspection ? <Empty text="支持 WeFlow 生成的 ChatLab JSON；先核对会话 ID 和消息数量，再导入 restricted 客户区。" /> : <div className="inspection-report"><div className="scope-path"><small>已检查会话</small><strong>{chatlabInspection.name} · {chatlabInspection.session_id}</strong></div><div className="inspection-grid"><MetricMini label="消息" value={chatlabInspection.message_count} /><MetricMini label="文件" value={chatlabInspection.total_files} /><MetricMini label="敏感" value={chatlabInspection.sensitive_files} /><MetricMini label="版本" value={Number(String(chatlabInspection.chatlab_version || "0").replace(/\D/g, ""))} /></div><div className="boundary-note"><strong>本次写入范围</strong><p>保存 WeFlow 原始 JSON 快照，建立客户时间线和聊天全文索引，默认 restricted；检查后文件变化会拒绝导入。</p></div><button className="danger-safe-button" disabled={busy} onClick={importChatLab}>确认导入客户聊天</button></div>}</Panel>
    </section>
    <div className="section-divider"><span>其他资料</span><b>业务与个人资料</b></div>
    <section className="import-layout">
    <Panel title="明确资料边界" code="01 / INSPECT">
      <div className="form-stack">
        <Field label="绝对路径" hint="可以是单个文件或一个明确目录"><input value={path} onChange={(event) => { setPath(event.target.value); setInspection(null); }} placeholder="例如 E:\\我的资料\\业务" /></Field>
        <div className="form-pair"><Field label="归属领域"><select value={domain} onChange={(event) => setDomain(event.target.value)}><option value="work">业务 / 工作</option><option value="self">自我 / 对话</option><option value="shared">共享知识</option><option value="distill">蒸馏资料</option></select></Field><Field label="隐私级别"><select value={privacy} onChange={(event) => setPrivacy(event.target.value)}><option value="private">private · 仅本机</option><option value="restricted">restricted · 显式授权才检索</option><option value="public">public · 可普通检索</option></select></Field></div>
        <label className="check-line"><input type="checkbox" checked={recursive} onChange={(event) => { setRecursive(event.target.checked); setInspection(null); }} /> 包含子目录</label>
        <button className="primary-button" disabled={!path || busy} onClick={inspect}>只读检查范围</button>
      </div>
    </Panel>
    <Panel title="检查结果与确认" code="02 / COMMIT">
      {!inspection ? <Empty text="系统会先统计文件，不会在这一步复制或索引任何内容。" /> : <div className="inspection-report">
        <div className="scope-path"><small>已检查路径</small><strong>{inspection.path}</strong></div>
        <div className="inspection-grid"><MetricMini label="全部文件" value={inspection.total_files} /><MetricMini label="可导入" value={inspection.supported_files} /><MetricMini label="敏感跳过" value={inspection.sensitive_files} /><MetricMini label="不支持" value={inspection.unsupported_files} /></div>
        <div className="boundary-note"><strong>本次写入范围</strong><p>原件复制到本项目私有哈希仓，建立全文索引，标记为 {domain} / {privacy}。敏感密钥名称不会进入系统。</p></div>
        <button className="danger-safe-button" disabled={busy || signature !== inspectedSignature} onClick={execute}>确认导入与建立索引</button>
      </div>}
    </Panel>
    </section>
  </>;
}

function Workflows({ definitions, runs, run, busy }: { definitions: Json[]; runs: Json[]; run: Runner; busy: boolean }) {
  return <>
    <section className="workflow-grid">
      {definitions.map((item, index) => <article className="workflow-card" key={item.name}><span className="card-index">0{index + 1}</span><div><h3>{item.title}</h3><ol>{item.steps.map((step: string) => <li key={step}>{step}</li>)}</ol><p>{item.approval}</p></div>{item.name === "rebuild_search_index" && <button disabled={busy} onClick={() => void run(() => post<Json>("/api/workflows/rebuild-index", {}))}>立即运行</button>}</article>)}
    </section>
    <Panel title="运行记录" code={`${runs.length} RUNS`}>
      <ItemList items={runs} empty="还没有工作流运行记录。" render={(item) => <div className="run-row detailed"><span className={`status-dot ${item.status}`} /><span><strong>{workflowName(item.workflow_name)}</strong><small>{item.id}</small></span><b className={`status-tag ${item.status}`}>{statusLabel(item.status)}</b><time>{formatTime(item.created_at)}</time></div>} />
    </Panel>
  </>;
}

function PersonalTimeline({ run, busy, setEvidence }: { run: Runner; busy: boolean; setEvidence: (data: Json) => void }) {
  const localToday = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const [date, setDate] = useState(localToday);
  const [days, setDays] = useState<Json[]>([]);
  const [day, setDay] = useState<Json | null>(null);
  const [readiness, setReadiness] = useState<Json | null>(null);
  const loadDays = useCallback(async () => {
    const result = await api<Json[]>("/api/personal-timeline?limit=60");
    setDays(result.data);
    return result.data;
  }, []);
  const loadDay = useCallback(async (value: string) => {
    try {
      const result = await api<Json>(`/api/personal-timeline/${value}`);
      setDay(result.data);
    } catch { setDay(null); }
  }, []);
  const loadReadiness = useCallback(async () => {
    try {
      const result = await api<Json>("/api/personal-timeline/status");
      setReadiness(result.data);
    } catch { setReadiness(null); }
  }, []);
  useEffect(() => {
    void loadDays().then((items) => {
      const first = items[0]?.local_date;
      if (first) { setDate(first); void loadDay(first); }
    });
  }, [loadDay, loadDays]);
  useEffect(() => { void loadReadiness(); }, [loadReadiness]);
  const build = () => void run(
    () => post<Json>("/api/personal-timeline/build", { local_date: date, provider: "codex", model: "gpt-5.6-luna" }),
    (result) => { setDay(result); void loadDays(); void loadReadiness(); },
  );
  const choose = (value: string) => { setDate(value); void loadDay(value); };
  const evidenceById = new Map<string, Json>(
    (day?.evidence ?? []).map((item: Json) => [item.message_id, item]),
  );
  const showEvidence = (id: string) => {
    const evidence = evidenceById.get(id);
    if (evidence) setEvidence(evidence);
  };
  const facts = (day?.activities ?? []).filter((item: Json) => item.certainty === "fact");
  return <section className="timeline-layout">
    <aside className="timeline-days">
      <header><small>日期记录</small><strong>{days.length} 天</strong></header>
      <div className="timeline-create">
        <input type="date" value={date} max={localToday} onChange={(event) => choose(event.target.value)} />
        <button disabled={busy || !date} onClick={build}>{busy ? "正在整理…" : "用 Luna 整理这一天"}</button>
        <p>只发送本地筛出的本人工作候选消息；不会把全部聊天整体交给模型。</p>
      </div>
      {readiness && <div className="boundary-note">
        {Number(readiness.pending_day_count || 0) > 0 ? <><strong>发现 {readiness.pending_day_count} 天待整理</strong><p>下方列出最近 {readiness.pending_days?.length || 0} 天；点击日期只切换查看，不会自动调用 Luna。</p><div className="timeline-pending-list">{(readiness.pending_days ?? []).map((item: Json) => <button key={item.local_date} className={date === item.local_date ? "active" : ""} onClick={() => choose(String(item.local_date))}><strong>{item.local_date}</strong><span>{item.pending_reason === "new_self_messages" ? "有新增本人消息" : "尚未整理"} · {item.message_count} 条本人消息</span></button>)}</div>{Number(readiness.pending_day_count || 0) > Number(readiness.pending_days?.length || 0) && <small className="timeline-pending-note">仅显示最近日期；其余待整理日期会在后续检查中继续显示。</small>}</> : <><strong>近 {readiness.window_days} 天没有遗漏或新增消息的日期</strong><p>同步新消息后，这里会提示需要你确认整理的日期。</p></>}
      </div>}
      <nav>{days.map((item) => <button key={item.local_date} className={date === item.local_date ? "active" : ""} onClick={() => choose(item.local_date)}><strong>{item.local_date}</strong><span>{item.evidence_count} 条证据 · {item.review_status === "unreviewed" ? "待查看" : item.review_status}</span></button>)}</nav>
    </aside>
    <div className="timeline-detail">
      {!day ? <div className="timeline-empty"><strong>{date}</strong><p>这一天还没有整理。整理后会把事实、忙碌方向和待办分开。</p></div> : <>
        <header className="timeline-summary"><div><small>当日摘要</small><h2>{day.local_date}</h2><p>{day.factual_summary || "没有形成可证实摘要。"}</p></div><div><strong>{day.evidence_count}</strong><span>条采用证据</span><small>{day.candidate_message_count} / {day.source_message_count} 条候选</small></div></header>
        <section className="timeline-section"><header><h3>有聊天证据的事项</h3><small>已确认事实</small></header><div className="timeline-cards">
          {facts.map((item: Json) => <article key={item.id}><div><span>{activityTypeLabel(item.activity_type)}</span><b>{activityStatusLabel(item.activity_status)}</b></div><p>{item.statement}</p><footer>{item.evidence_message_ids.map((id: string) => <button key={id} onClick={() => showEvidence(id)}>查看消息证据</button>)}</footer></article>)}
          {facts.length === 0 && <p className="timeline-muted">没有足够证据形成事实项。</p>}
        </div></section>
        <section className="timeline-split">
          <div><header><h3>可能正在忙</h3><small>推断，非事实</small></header>{(day.inferred_focus ?? []).map((item: Json, index: number) => <article key={index}><p>{item.statement}</p><span>{confidenceLabel(item.confidence)}置信度 · 仅作推断</span><footer>{(item.evidence_ids ?? []).map((id: string) => <button key={id} onClick={() => showEvidence(id)}>查看推断依据</button>)}</footer></article>)}</div>
          <div><header><h3>明确待办</h3><small>待处理事项</small></header>{(day.open_items ?? []).map((item: Json, index: number) => <article key={index}><p>{item.statement}</p><span>{confidenceLabel(item.confidence)}置信度</span><footer>{(item.evidence_ids ?? []).map((id: string) => <button key={id} onClick={() => showEvidence(id)}>查看消息证据</button>)}</footer></article>)}</div>
        </section>
      </>}
    </div>
  </section>;
}

function PersonaPanel({ items, run, busy }: { items: Json[]; run: Runner; busy: boolean }) {
  const [type, setType] = useState("preference");
  const [statement, setStatement] = useState("");
  const [confidence, setConfidence] = useState("medium");
  const create = (event: FormEvent) => { event.preventDefault(); if (!statement.trim()) return; void run(() => post<Json>("/api/persona", { observation_type: type, statement, evidence_ids: [], confidence }), () => setStatement("")); };
  const review = (id: string, decision: string) => void run(() => post(`/api/persona/${id}/review`, { decision, reason: "在个人管理台审核" }));
  return <section className="review-layout">
    <Panel title="新增观察候选" code="HUMAN IN THE LOOP"><form className="form-stack" onSubmit={create}><div className="form-pair"><Field label="观察类型"><select value={type} onChange={(event) => setType(event.target.value)}><option value="preference">偏好</option><option value="personality">性格</option><option value="habit">习惯</option><option value="communication">表达方式</option><option value="value">价值判断</option></select></Field><Field label="当前置信度"><select value={confidence} onChange={(event) => setConfidence(event.target.value)}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></Field></div><Field label="观察陈述" hint="尽量写成可被证据支持或反驳的陈述"><textarea value={statement} onChange={(event) => setStatement(event.target.value)} placeholder="例如：在做技术决策前，我偏好先看到真实运行证据。" /></Field><button className="primary-button" disabled={busy}>保存为候选</button></form></Panel>
    <Panel title={`观察队列 · ${items.length}`} code="REVIEW QUEUE"><ItemList items={items} empty="还没有个人观察。Codex 也可以通过 MCP 提交有证据的候选。" render={(item) => <article className="review-card"><header><span>{item.observation_type}</span><b className={`approval ${item.approval_status}`}>{approvalLabel(item.approval_status)}</b></header><p>{item.statement}</p><small>置信度 {item.confidence} · 证据 {item.evidence_count} 条</small>{item.approval_status === "candidate" && <footer><button disabled={busy} onClick={() => review(item.id, "rejected")}>驳回</button><button className="approve" disabled={busy} onClick={() => review(item.id, "approved")}>批准为长期画像</button></footer>}</article>} /></Panel>
  </section>;
}

function DistillPanel({ items, run, busy }: { items: Json[]; run: Runner; busy: boolean }) {
  const [type, setType] = useState("decision");
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const [rationale, setRationale] = useState("");
  const create = (event: FormEvent) => { event.preventDefault(); if (!input.trim() || !output.trim()) return; void run(() => post<Json>("/api/distillation", { example_type: type, input_text: input, preferred_output: output, rationale, source_ids: [], privacy: "restricted" }), () => { setInput(""); setOutput(""); setRationale(""); }); };
  const review = (id: string, decision: string) => void run(() => post(`/api/distillation/${id}/review`, { decision, reason: "在蒸馏中心审核" }));
  const exportData = () => void run(() => post<Json>("/api/distillation/export", { approved_only: true, dataset_split: null }));
  return <>
    <div className="distill-banner"><div><small>已审核样本</small><strong>{items.filter((item) => item.approval_status === "approved").length}</strong><span>条已批准样本</span></div><p>这里只整理未来训练或微调需要的数据，不会自动训练模型。导出文件默认只包含你批准过的样本。</p><button onClick={exportData} disabled={busy}>导出已批准 JSONL</button></div>
    <section className="review-layout">
      <Panel title="沉淀一个高质量样本" code="CURATE"><form className="form-stack" onSubmit={create}><Field label="样本类型"><select value={type} onChange={(event) => setType(event.target.value)}><option value="decision">决策</option><option value="preference">偏好</option><option value="instruction">指令遵循</option><option value="conversation">对话</option></select></Field><Field label="输入 / 情境"><textarea value={input} onChange={(event) => setInput(event.target.value)} placeholder="当时的问题、任务或上下文" /></Field><Field label="你认可的输出"><textarea value={output} onChange={(event) => setOutput(event.target.value)} placeholder="你希望未来模型学会的回答或行动" /></Field><Field label="为什么这样更好"><input value={rationale} onChange={(event) => setRationale(event.target.value)} placeholder="可选：判断标准或偏好原因" /></Field><button className="primary-button" disabled={busy}>保存为候选样本</button></form></Panel>
      <Panel title={`样本队列 · ${items.length}`} code="QUALITY GATE"><ItemList items={items} empty="还没有蒸馏样本。可以从一次满意的 Codex 对话开始沉淀。" render={(item) => <article className="review-card distill"><header><span>{item.example_type}</span><b className={`approval ${item.approval_status}`}>{approvalLabel(item.approval_status)}</b></header><small>输入</small><p>{item.input_text}</p><small>偏好输出</small><p>{item.preferred_output}</p>{item.approval_status === "candidate" && <footer><button disabled={busy} onClick={() => review(item.id, "rejected")}>驳回</button><button className="approve" disabled={busy} onClick={() => review(item.id, "approved")}>批准进入数据集</button></footer>}</article>} /></Panel>
    </section>
  </>;
}

function SettingsPanel({ health, audit }: { health: Json | null; audit: Json[] }) {
  const mcpCommand = "uv --directory E:\\codex-kb run pkas-mcp";
  return <>
    <DeferredPage><DocumentParsingSettings /></DeferredPage>
    <section className="settings-grid"><Panel title="本地运行状态" code="RUNTIME"><dl className="detail-list"><Detail label="服务状态" value={health ? "运行正常" : "连接中"} /><Detail label="应用版本" value={health?.app_version ?? "—"} /><Detail label="SQLite" value={health?.sqlite_version ?? "—"} /><Detail label="全文分词" value={health?.fts_tokenizer ?? "—"} /><Detail label="元数据位置" value={health?.database ?? "—"} /><Detail label="恢复包" value={health?.backup?.bundle_count ? `${health.backup.bundle_count} 个（保留 ${health.backup.retention_target} 个）` : "尚未生成"} /><Detail label="最近备份" value={health?.backup?.latest?.created_at ? formatTime(health.backup.latest.created_at) : "—"} /></dl></Panel><Panel title="Codex 接入" code="MCP / TESTING OFF"><p className="panel-note">MCP 当前按你的要求保持关闭，继续独立测试知识库。下面命令只作为之后重新启用时的参考，不会由页面自动执行。</p><code className="command-code">{mcpCommand}</code><div className="boundary-note"><strong>默认权限</strong><p>微信聊天默认 restricted；WeFlow 接入不读取数据库密钥且不依赖 HTTP API；客户事实只能创建候选；系统不直接发送微信消息。</p></div></Panel></section>
    <Panel title="审计轨迹" code={`${audit.length} EVENTS`}><ItemList items={audit} empty="关键操作发生后会记录在这里。" render={(item) => <div className="audit-row"><span>{String(item.id).padStart(4, "0")}</span><strong>{eventLabel(item.event_type)}</strong><small>{item.subject_type ?? "system"} · {item.subject_id ?? "—"}</small><time>{formatTime(item.created_at)}</time></div>} /></Panel>
  </>;
}

function EvidenceSpine({ evidence, close }: { evidence: Json | null; close: () => void }) {
  if (!evidence) return null;
  return <aside className="evidence-spine"><header><div><small>资料来源</small><strong>原文与定位</strong></div><button onClick={close}>×</button></header><div className="spine-body"><span className="spine-index">已核验来源</span><h2>{evidence.title ?? evidence.original_name ?? evidence.customer_name ?? evidence.sender_name ?? "证据详情"}</h2>{evidence.text && <pre>{evidence.text}</pre>}{evidence.content && !evidence.text && <p className="evidence-quote">{evidence.content}</p>}{evidence.snippet && !evidence.text && !evidence.content && <p className="evidence-quote">{evidence.snippet}</p>}<dl className="detail-list"><Detail label="资料 / 消息 ID" value={evidence.source_id ?? evidence.message_id ?? evidence.id ?? "—"} /><Detail label="平台消息 ID" value={evidence.platform_message_id ?? "—"} /><Detail label="发送者" value={evidence.sender_name ?? "—"} /><Detail label="消息时间" value={evidence.sent_at ? formatUnixTime(evidence.sent_at) : "—"} /><Detail label="原始来源" value={evidence.original_uri ?? evidence.source_uri ?? "—"} /><Detail label="仓内原件" value={evidence.vault_path ?? "—"} /><Detail label="定位" value={evidence.locator ?? evidence.platform_message_id ?? `字符 ${evidence.offset ?? 0} 起`} /><Detail label="领域 / 隐私" value={`${evidence.domain ?? "wechat"} / ${evidence.privacy ?? "—"}`} /></dl></div></aside>;
}

type Runner = <T>(job: () => Promise<Envelope<T>>, after?: (data: T) => void) => Promise<Envelope<T> | null>;
function Panel({ title, code: _code, children }: { title: string; code?: string; children: ReactNode }) { return <section className="panel"><header className="panel-head"><h2>{title}</h2></header><div className="panel-body">{children}</div></section>; }
function Metric({ label, value, unit, tone }: { label: string; value: number; unit: string; tone: string }) { return <article className={`metric-card ${tone}`}><span>{unit}</span><strong>{value.toLocaleString()}</strong><p>{label}</p><i /></article>; }
function MetricMini({ label, value }: { label: string; value: number }) { return <div className="metric-mini"><strong>{value}</strong><span>{label}</span></div>; }
function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) { return <label className="field"><span>{label}{hint && <small>{hint}</small>}</span>{children}</label>; }
function Empty({ text }: { text: string }) { return <div className="empty-state"><span>∅</span><p>{text}</p></div>; }
function ItemList({ items, empty, render }: { items: Json[]; empty: string; render: (item: Json, index: number) => ReactNode }) { return items.length ? <div className="item-list">{items.map((item, index) => <div key={item.id ?? `${index}`}>{render(item, index)}</div>)}</div> : <Empty text={empty} />; }
function Detail({ label, value }: { label: string; value: unknown }) { return <><dt>{label}</dt><dd>{String(value ?? "—")}</dd></>; }

function formatTime(value?: string) { if (!value) return "—"; return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function formatUnixTime(value?: number) { if (!value) return "—"; return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value * 1000)); }
function manualSyncLabel(status?: string, code?: string) {
  if (status === "queued") return "等待启动";
  if (status === "preparing") return "核对入库水位";
  if (status === "exporting") return "WeFlow 导出中";
  if (status === "importing") return "新增消息入库中";
  if (status === "stopped" || code === "owner_exited") return `${PRODUCT.name}退出，已停止同步`;
  if (status === "failed") {
    const labels: Record<string, string> = {
      weflow_already_open: "请先退出 WeFlow",
      export_timeout: "导出超时，可稍后重试",
      node_unavailable: "Node.js 运行环境不可用",
      weflow_path_invalid: "WeFlow 程序目录或导出记录不可用",
      no_sync_scope: "暂无已确认的同步会话",
      weflow_export_database: "WeFlow 数据暂时不可用，请退出后重试",
      weflow_export_permission: "WeFlow 文件权限不足，请检查当前用户权限",
      weflow_export_filesystem: "WeFlow 导出文件无法读取或写入",
      weflow_export_ipc: "WeFlow 本地通信未完成，请退出后重试",
      weflow_export_resource: "WeFlow 导出资源不足，请稍后重试",
      weflow_export_timeout: "WeFlow 导出超时，可稍后重试",
      weflow_export_helper_task_missing: "WeFlow 导出组件不完整，请修复安装",
      weflow_export_helper_store_locked: "WeFlow 配置仍被占用，请确认已完全退出",
      weflow_export_helper_runtime: "WeFlow 导出组件运行失败，请退出后重试",
      weflow_export_no_output: "导出完成但未发现新的可用记录",
      weflow_export_failed: "WeFlow 导出失败，请检查后重试",
    };
    return labels[code ?? ""] ?? "同步失败，请检查 WeFlow 状态后重试";
  }
  if (code === "no_new_messages") return "已是最新";
  if (status === "completed" || status === "success") return "同步完成";
  return "等待手动同步";
}
function manualExportDescription(freshness?: string, usableCount?: number) { if (freshness === "new_export_available") return " 当前导出记录中有较新的可用导出，可点击同步处理。"; if (freshness === "current") return " 当前导出记录与上次同步水位一致。"; if (freshness === "not_compared") return ` 已发现 ${usableCount ?? 0} 份可用导出；首次同步后会建立对比水位。`; if (freshness === "no_usable_export") return " 导出记录存在，但当前同步范围没有可用 XLSX。"; if (freshness === "records_invalid") return " 导出记录格式无法读取，请在 WeFlow 中重新导出。"; if (freshness === "records_unavailable") return " 暂未找到导出记录文件。"; return ""; }
function formatBytes(value?: number) { if (!value) return "0 B"; const units = ["B", "KB", "MB", "GB"]; const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1); return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`; }
function formatNumber(value?: number) { return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value ?? 0); }
function compactPath(value?: string) { if (!value) return "—"; return value.length > 48 ? `…${value.slice(-47)}` : value; }
function statusLabel(value: string) { return ({ completed: "完成", warning: "警告", failed: "失败", running: "运行中" } as Record<string, string>)[value] ?? value; }
function workflowName(value: string) { return ({ import_path: "资料导入与索引", rebuild_search_index: "全文索引重建", persona_review: "个人观察审核", weflow_xlsx_import: "WeFlow XLSX 客户导入", weflow_chatlab_import: "WeFlow ChatLab 导入" } as Record<string, string>)[value] ?? value; }
function approvalLabel(value: string) { return ({ candidate: "待审核", approved: "已批准", rejected: "已驳回" } as Record<string, string>)[value] ?? value; }
function customerTypeLabel(value: string) { return ({ private: "私聊会话", group: "群聊会话" } as Record<string, string>)[value] ?? value; }
function customerReviewLabel(value: string) { return ({ candidate: "待确认关系", approved: "已确认客户" } as Record<string, string>)[value] ?? value; }
function customerStageLabel(value: string) { return ({ lead: "线索", active: "沟通中", delivery: "交付中", after_sales: "售后", paused: "暂停", closed: "已结束" } as Record<string, string>)[value] ?? value; }
function activityTypeLabel(value: string) { return ({ work: "工作", customer: "客户", learning: "学习", personal: "个人", communication: "沟通" } as Record<string, string>)[value] ?? value; }
function activityStatusLabel(value: string) { return ({ done: "已完成", in_progress: "进行中", planned: "计划中", unknown: "状态未知" } as Record<string, string>)[value] ?? value; }
function confidenceLabel(value: string) { return ({ high: "高", medium: "中", low: "低" } as Record<string, string>)[value] ?? "低"; }
function signalTypeLabel(value: string) { return ({ requirement: "需求", commitment: "承诺", todo: "待办", risk: "风险", decision: "决策", follow_up: "跟进", preference: "偏好" } as Record<string, string>)[value] ?? value; }
function eventLabel(value: string) { return ({ source_ingested: "资料已接入", persona_candidate_created: "画像候选已创建", persona_reviewed: "画像已审核", distillation_candidate_created: "蒸馏候选已创建", distillation_reviewed: "蒸馏样本已审核", weflow_messages_ingested: "WeFlow 消息已接入", customer_signal_created: "客户信号候选已创建", customer_signal_reviewed: "客户信号已审核", customer_profile_updated: "客户档案已更新" } as Record<string, string>)[value] ?? value; }

export default App;
