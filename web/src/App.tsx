import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { api, Envelope, post } from "./api";

type Page = "home" | "knowledge" | "customers" | "import" | "workflows" | "agent" | "persona" | "distill" | "settings";
type Json = Record<string, any>;

const nav: Array<{ id: Page; label: string; mark: string }> = [
  { id: "home", label: "总览", mark: "OV" },
  { id: "knowledge", label: "知识与来源", mark: "KN" },
  { id: "customers", label: "微信客户", mark: "CX" },
  { id: "import", label: "WeFlow / 资料", mark: "IN" },
  { id: "workflows", label: "工作流", mark: "WF" },
  { id: "agent", label: "智能体", mark: "AG" },
  { id: "persona", label: "自我画像", mark: "ME" },
  { id: "distill", label: "蒸馏中心", mark: "DS" },
  { id: "settings", label: "系统与审计", mark: "SY" },
];

const pageMeta: Record<Page, { eyebrow: string; title: string; intro: string }> = {
  home: { eyebrow: "PERSONAL INTELLIGENCE OS", title: "你的知识，在同一个坐标系里", intro: "资料、证据、工作流和长期自我模型都由你控制。" },
  knowledge: { eyebrow: "KNOWLEDGE ATLAS", title: "从答案返回原始证据", intro: "全文检索业务与自我资料，并沿来源路径回到原文。" },
  customers: { eyebrow: "CUSTOMER INTELLIGENCE", title: "每个微信客户都有完整上下文", intro: "沿聊天时间线核对需求、承诺、待办和历史沟通，再交给 Codex 起草回复。" },
  import: { eyebrow: "SOURCE GATE", title: "读取 WeFlow 导出，或导入明确资料", intro: "不访问数据库密钥、不依赖 HTTP API；只有你确认的导出会话或路径会进入知识库。" },
  workflows: { eyebrow: "REPEATABLE OPERATIONS", title: "把可靠做法固化成工作流", intro: "每次运行都有步骤、状态、结果与错误记录。" },
  agent: { eyebrow: "CODEX CONTEXT ENGINE", title: "让智能体自己找对资料", intro: "系统先选择领域、检索证据，再把可追溯上下文交给 Codex。" },
  persona: { eyebrow: "SELF MODEL", title: "被证据约束的长期自我画像", intro: "性格与偏好先作为候选，只有你批准后才成为长期事实。" },
  distill: { eyebrow: "DISTILLATION LAB", title: "沉淀未来可训练的高质量样本", intro: "把你的判断、表达和偏好整理为可审核、可导出的数据集。" },
  settings: { eyebrow: "LOCAL CONTROL PLANE", title: "系统状态、隐私与审计", intro: "本机监听、数据位置和所有关键动作都清清楚楚。" },
};

function App() {
  const [page, setPage] = useState<Page>("home");
  const [dashboard, setDashboard] = useState<Json | null>(null);
  const [health, setHealth] = useState<Json | null>(null);
  const [sources, setSources] = useState<Json[]>([]);
  const [syncRoots, setSyncRoots] = useState<Json[]>([]);
  const [workflowDefs, setWorkflowDefs] = useState<Json[]>([]);
  const [workflowRuns, setWorkflowRuns] = useState<Json[]>([]);
  const [agentRuns, setAgentRuns] = useState<Json[]>([]);
  const [persona, setPersona] = useState<Json[]>([]);
  const [distillation, setDistillation] = useState<Json[]>([]);
  const [audit, setAudit] = useState<Json[]>([]);
  const [customers, setCustomers] = useState<Json[]>([]);
  const [customerSignals, setCustomerSignals] = useState<Json[]>([]);
  const [notice, setNotice] = useState<{ status: string; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [evidence, setEvidence] = useState<Json | null>(null);

  const refresh = useCallback(async () => {
    const [dashboardResult, healthResult, sourcesResult, syncRootsResult, definitionsResult, runsResult,
      agentResult, personaResult, distillationResult, auditResult, customersResult,
      customerSignalsResult] = await Promise.allSettled([
      api<Json>("/api/dashboard"),
      api<Json>("/api/health"),
      api<Json[]>("/api/sources?limit=100"),
      api<Json[]>("/api/sync/roots"),
      api<Json[]>("/api/workflows"),
      api<Json[]>("/api/workflows/runs?limit=50"),
      api<Json[]>("/api/agent/runs?limit=50"),
      api<Json[]>("/api/persona?limit=100"),
      api<Json[]>("/api/distillation?limit=100"),
      api<Json[]>("/api/audit?limit=100"),
      api<Json[]>("/api/customers?limit=500"),
      api<Json[]>("/api/customer-signals?limit=500"),
    ]);
    if (dashboardResult.status === "fulfilled") setDashboard(dashboardResult.value.data);
    if (healthResult.status === "fulfilled") setHealth(healthResult.value.data);
    if (sourcesResult.status === "fulfilled") setSources(sourcesResult.value.data);
    if (syncRootsResult.status === "fulfilled") setSyncRoots(syncRootsResult.value.data);
    if (definitionsResult.status === "fulfilled") setWorkflowDefs(definitionsResult.value.data);
    if (runsResult.status === "fulfilled") setWorkflowRuns(runsResult.value.data);
    if (agentResult.status === "fulfilled") setAgentRuns(agentResult.value.data);
    if (personaResult.status === "fulfilled") setPersona(personaResult.value.data);
    if (distillationResult.status === "fulfilled") setDistillation(distillationResult.value.data);
    if (auditResult.status === "fulfilled") setAudit(auditResult.value.data);
    if (customersResult.status === "fulfilled") setCustomers(customersResult.value.data);
    if (customerSignalsResult.status === "fulfilled") setCustomerSignals(customerSignalsResult.value.data);
    if ([dashboardResult, healthResult, sourcesResult, syncRootsResult, definitionsResult, runsResult,
      agentResult, personaResult, distillationResult, auditResult, customersResult,
      customerSignalsResult].some((result) => result.status === "rejected")) {
      setNotice({ status: "warning", text: "部分系统数据暂时未能读取，请确认后端已启动。" });
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const run = async <T,>(job: () => Promise<Envelope<T>>, after?: (data: T) => void) => {
    setBusy(true);
    try {
      const result = await job();
      setNotice({ status: result.status, text: result.summary });
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
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => setPage("home")} aria-label="返回总览">
          <span className="brand-glyph"><i /><i /><i /></span>
          <span><strong>知枢</strong><small>PERSONAL OS</small></span>
        </button>
        <div className="domain-pulse">
          <span className="pulse-dot" />
          <span>LOCAL · PRIVATE</span>
        </div>
        <nav aria-label="主导航">
          {nav.map((item) => (
            <button key={item.id} className={page === item.id ? "active" : ""} onClick={() => setPage(item.id)}>
              <span className="nav-mark">{item.mark}</span><span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span>INDEX STATUS</span>
          <strong>{health ? "ONLINE" : "CHECKING"}</strong>
          <small>{dashboard?.counts?.chunks ?? 0} 个可检索片段</small>
        </div>
      </aside>

      <main className="main-stage">
        <header className="topbar">
          <div className="breadcrumb"><span>知枢</span><b>/</b><strong>{nav.find((item) => item.id === page)?.label}</strong></div>
          <div className="top-actions">
            <button className="quiet-button" onClick={() => void refresh()} disabled={busy}>刷新</button>
            <span className="health-pill"><i /> 本机运行</span>
          </div>
        </header>
        <div className="content-wrap">
          <section className="page-heading">
            <div><p>{meta.eyebrow}</p><h1>{meta.title}</h1><span>{meta.intro}</span></div>
            <div className="axis-badge"><small>ACTIVE AXIS</small><b>{page === "persona" || page === "distill" ? "SELF" : page === "home" ? "ALL" : "WORK"}</b></div>
          </section>
          {notice && <div className={`notice ${notice.status}`}><span>{notice.text}</span><button onClick={() => setNotice(null)}>×</button></div>}

          {page === "home" && <Home dashboard={dashboard} setPage={setPage} setEvidence={setEvidence} />}
          {page === "knowledge" && <Knowledge sources={sources} syncRoots={syncRoots} run={run} setEvidence={setEvidence} busy={busy} />}
          {page === "customers" && <CustomersPanel customers={customers} signals={customerSignals} run={run} setEvidence={setEvidence} busy={busy} />}
          {page === "import" && <ImportPanel run={run} busy={busy} />}
          {page === "workflows" && <Workflows definitions={workflowDefs} runs={workflowRuns} run={run} busy={busy} />}
          {page === "agent" && <AgentPanel runs={agentRuns} run={run} setEvidence={setEvidence} busy={busy} />}
          {page === "persona" && <PersonaPanel items={persona} run={run} busy={busy} />}
          {page === "distill" && <DistillPanel items={distillation} run={run} busy={busy} />}
          {page === "settings" && <SettingsPanel health={health} audit={audit} />}
        </div>
      </main>
      <EvidenceSpine evidence={evidence} close={() => setEvidence(null)} />
    </div>
  );
}

function Home({ dashboard, setPage, setEvidence }: { dashboard: Json | null; setPage: (page: Page) => void; setEvidence: (data: Json) => void }) {
  const counts = dashboard?.counts ?? {};
  return <>
    <section className="command-deck">
      <div className="command-copy"><span>现在想完成什么？</span><strong>搜索知识，或把任务交给智能体</strong></div>
      <div className="command-actions"><button onClick={() => setPage("knowledge")}>检索全部知识 <b>⌘ K</b></button><button className="accent" onClick={() => setPage("agent")}>准备智能体上下文 →</button></div>
    </section>
    <section className="metric-grid">
      <Metric label="微信客户会话" value={counts.customers ?? 0} unit="CUSTOMER" tone="cyan" />
      <Metric label="客户聊天消息" value={counts.customer_messages ?? 0} unit="MESSAGE" tone="violet" />
      <Metric label="业务信号" value={counts.customer_signals ?? 0} unit="ACTION" tone="amber" />
      <Metric label="知识片段" value={counts.chunks ?? 0} unit="KNOWLEDGE" tone="green" />
    </section>
    <section className="two-column">
      <Panel title="领域分布" code="DOMAIN MAP">
        <div className="domain-map">
          {[{ key: "work", label: "业务 / 工作", tone: "cyan" }, { key: "self", label: "自我 / 对话", tone: "amber" }, { key: "shared", label: "共享知识", tone: "violet" }, { key: "distill", label: "蒸馏资料", tone: "green" }].map((item) => {
            const value = dashboard?.domains?.[item.key] ?? 0;
            const total = Math.max(1, counts.sources ?? 0);
            return <div className="domain-row" key={item.key}><span>{item.label}<b>{value}</b></span><div><i className={item.tone} style={{ width: `${Math.max(value ? 8 : 0, (value / total) * 100)}%` }} /></div></div>;
          })}
        </div>
      </Panel>
      <Panel title="最近进入知识库" code="PROVENANCE">
        <ItemList items={dashboard?.recent_sources ?? []} empty="还没有资料。进入“资料接入”指定一个明确路径。" render={(item) => <button className="line-item" onClick={() => setEvidence(item)}><span className="file-mark">{String(item.source_type ?? "FILE").slice(0, 4).toUpperCase()}</span><span><strong>{item.original_name}</strong><small>{item.domain} · {item.privacy}</small></span><time>{formatTime(item.ingested_at)}</time></button>} />
      </Panel>
    </section>
    <Panel title="最近运行" code="ACTIVITY TRACE">
      <ItemList items={dashboard?.recent_runs ?? []} empty="尚无工作流运行。" render={(item) => <div className="run-row"><span className={`status-dot ${item.status}`} /><span><strong>{workflowName(item.workflow_name)}</strong><small>{item.id}</small></span><b className={`status-tag ${item.status}`}>{statusLabel(item.status)}</b><time>{formatTime(item.created_at)}</time></div>} />
    </Panel>
  </>;
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
        <ItemList items={sources} empty="尚未导入资料。" render={(item) => <button className="source-card" onClick={() => setEvidence(item)}><span className="file-mark">{String(item.source_type).toUpperCase()}</span><span><strong>{item.original_name}</strong><small>{compactPath(item.original_uri)}</small></span><b>{formatBytes(item.byte_size)}</b></button>} />
      </Panel>
    </section>
    <div className="section-divider"><span>CONTINUOUS KNOWLEDGE SOURCES</span><b>现成资料地图与增量同步</b></div>
    <section className="sync-overview">
      <Panel title={`持续资料源 · ${syncRoots.length}`} code="AUTO REFRESH / 03:30">
        <div className="sync-root-list">
          {syncRoots.length ? syncRoots.map((root) => <article className="sync-root-card" key={root.id}>
            <header><span className={`status-dot ${root.error_count ? "warning" : "completed"}`} /><div><strong>{root.name}</strong><small>{root.connector_type === "codex_sessions" ? "CODEX TASK STREAM" : root.sync_mode === "index" ? "FULL TEXT INDEX" : "FILE CATALOG"}</small></div><button disabled={busy} onClick={() => scanRoot(root.id)}>增量刷新</button></header>
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
        <ItemList items={catalogResults} empty="这里可以定位尚未抽取正文的现成项目文件。" render={(item) => <button className="source-card" onClick={() => setEvidence(item)}><span className="file-mark">MAP</span><span><strong>{item.relative_path}</strong><small>{item.root_name} · {item.state}</small></span><b>{formatBytes(item.byte_size)}</b></button>} />
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

  if (!customers.length) return <div className="customer-empty"><span>CX / 00</span><h2>还没有微信客户会话</h2><p>进入“WeFlow / 资料”，读取 WeFlow 已导出的 XLSX，检查并选择需要进入客户知识库的会话。</p></div>;
  return <section className="customer-workbench">
    <aside className="customer-rail">
      <header><strong>客户会话</strong><span>{customers.length}</span></header>
      <div>{customers.map((item) => <button key={item.id} className={selected?.id === item.id ? "active" : ""} onClick={() => loadTimeline(item)}><span className="customer-avatar">{String(item.display_name).slice(0, 1)}</span><span><strong>{item.display_name}</strong><small>{item.company || customerTypeLabel(item.customer_type)} · {item.message_count} 条</small></span>{item.open_signals > 0 && <b>{item.open_signals}</b>}</button>)}</div>
    </aside>
    <div className="customer-main">
      {selected && <>
        <section className="customer-identity"><div><small>WECHAT / {selected.platform_id}</small><h2>{selected.display_name}</h2><p>{selected.summary || "尚未填写客户摘要；Codex 可以基于已审核信号辅助整理。"}</p></div><div className="customer-facts"><span><small>阶段</small><b>{customerStageLabel(selected.stage)}</b></span><span><small>消息</small><b>{selected.message_count}</b></span><span><small>待处理</small><b>{selected.open_signals}</b></span></div></section>
        <div className="customer-tabs">
          <form className="customer-search" onSubmit={search}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="检索这个客户的需求、报价、进度或承诺" /><button disabled={busy}>检索聊天证据</button></form>
        </div>
        {searchResults.length > 0 && <Panel title={`聊天检索 · ${searchResults.length}`} code="MATCHED EVIDENCE"><ItemList items={searchResults} empty="没有匹配消息。" render={(item) => <button className="result-card customer-result" onClick={() => setEvidence(item)}><span className="result-rank">{item.is_self ? "ME" : "CX"}</span><span><strong>{item.sender_name || item.customer_name}</strong><p>{item.snippet}</p><small>{formatUnixTime(item.sent_at)} · {item.platform_message_id || item.message_id}</small></span><b>证据 →</b></button>} /></Panel>}
        <section className="customer-columns">
          <Panel title={`会话时间线 · ${timeline.length}`} code="RESTRICTED / AUTHORIZED"><ItemList items={timeline} empty="点击左侧客户加载已授权的聊天时间线。" render={(item) => <button className={`chat-line ${item.is_self ? "self" : "customer"}`} onClick={() => setEvidence(item)}><span>{item.is_self ? "我" : (item.sender_name || selected.display_name)}</span><p>{item.content}</p><time>{formatUnixTime(item.sent_at)}</time></button>} /></Panel>
          <div className="customer-side-stack">
            <Panel title="客户档案" code="CURATED PROFILE"><form className="profile-form" onSubmit={saveProfile}><div><input value={company} onChange={(event) => setCompany(event.target.value)} placeholder="公司 / 组织" /><select value={stage} onChange={(event) => setStage(event.target.value)}><option value="lead">线索</option><option value="active">沟通中</option><option value="delivery">交付中</option><option value="after_sales">售后</option><option value="paused">暂停</option><option value="closed">已结束</option></select></div><input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="标签，用逗号分隔" /><textarea value={customerSummary} onChange={(event) => setCustomerSummary(event.target.value)} placeholder="只写经过核对的客户摘要" /><button disabled={busy}>保存已确认档案</button></form></Panel>
            <Panel title="Codex 回复上下文" code="DRAFT ONLY"><form className="form-stack" onSubmit={prepareReply}><Field label="客户当前问题或你的任务"><textarea value={replyTask} onChange={(event) => setReplyTask(event.target.value)} placeholder="例如：根据历史沟通，起草一份关于报价时间和三家门店数据看板的回复。" /></Field><button className="primary-button" disabled={busy}>准备完整回复依据</button></form>{replyContext && <div className="context-summary"><strong>{replyContext.summary}</strong><ol>{replyContext.plan.map((step: string) => <li key={step}>{step}</li>)}</ol><small>上下文已记录，可交给 Codex 生成回复草稿；系统不会直接发送微信。</small></div>}</Panel>
            <Panel title={`业务信号 · ${selectedSignals.length}`} code="HUMAN REVIEW"><form className="signal-form" onSubmit={addSignal}><select value={signalType} onChange={(event) => setSignalType(event.target.value)}><option value="requirement">需求</option><option value="commitment">承诺</option><option value="todo">待办</option><option value="risk">风险</option><option value="decision">决策</option><option value="follow_up">跟进</option><option value="preference">偏好</option></select><input value={signalStatement} onChange={(event) => setSignalStatement(event.target.value)} placeholder="写入一个有证据的候选事项" /><button disabled={busy}>保存候选</button></form><ItemList items={selectedSignals} empty="尚未沉淀需求、承诺或待办。" render={(item) => <article className="signal-card"><header><span>{signalTypeLabel(item.signal_type)}</span><b className={`approval ${item.approval_status}`}>{approvalLabel(item.approval_status)}</b></header><p>{item.statement}</p>{item.approval_status === "candidate" && <footer><button onClick={() => reviewSignal(item.id, "rejected")}>驳回</button><button onClick={() => reviewSignal(item.id, "approved")}>批准</button></footer>}</article>} /></Panel>
          </div>
        </section>
      </>}
    </div>
  </section>;
}

function ImportPanel({ run, busy }: { run: Runner; busy: boolean }) {
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
  const inspectChatLab = () => void run(() => post<Json>("/api/weflow/chatlab/inspect", { path: chatlabPath, session_id: chatlabSessionId || null }), setChatlabInspection);
  const importChatLab = () => void run(() => post<Json>("/api/weflow/chatlab/import", { path: chatlabPath, session_id: chatlabSessionId || null, inspection_token: chatlabInspection?.inspection_token, privacy: "restricted" }), () => setChatlabInspection(null));
  return <>
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
        {!exportInspection ? <button className="danger-safe-button sync-button" disabled={!selectedExports.length || busy} onClick={inspectExports}>只读检查所选 {selectedExports.length} 个 XLSX</button> : <div className="export-confirm"><div className="boundary-note"><strong>检查通过</strong><p>{exportInspection.selected_sessions} 个会话，共声明 {exportInspection.total_messages} 条消息、{formatBytes(exportInspection.total_bytes)}。确认后复制原始 XLSX 哈希快照并建立客户索引。</p></div><button className="danger-safe-button sync-button" disabled={busy} onClick={importExports}>确认导入所选客户会话</button></div>}
      </Panel>
    </section>
    <section className="import-layout offline-import">
      <Panel title="WeFlow ChatLab 离线文件" code="OFFLINE / JSON"><div className="form-stack"><Field label="ChatLab JSON 绝对路径"><input value={chatlabPath} onChange={(event) => { setChatlabPath(event.target.value); setChatlabInspection(null); }} placeholder="例如 E:\\WeFlow导出\\客户张经理.json" /></Field><Field label="私聊 wxid" hint="通常可自动识别；无法识别时填写"><input value={chatlabSessionId} onChange={(event) => { setChatlabSessionId(event.target.value); setChatlabInspection(null); }} placeholder="可选：wxid_xxx" /></Field><button className="primary-button" disabled={!chatlabPath || busy} onClick={inspectChatLab}>只读检查 ChatLab 文件</button></div></Panel>
      <Panel title="会话结构确认" code="CHATLAB / 0.0.2">{!chatlabInspection ? <Empty text="支持 WeFlow 生成的 ChatLab JSON；先核对会话 ID 和消息数量，再导入 restricted 客户区。" /> : <div className="inspection-report"><div className="scope-path"><small>WEFLOW CHAT SESSION</small><strong>{chatlabInspection.name} · {chatlabInspection.session_id}</strong></div><div className="inspection-grid"><MetricMini label="消息" value={chatlabInspection.message_count} /><MetricMini label="文件" value={chatlabInspection.total_files} /><MetricMini label="敏感" value={chatlabInspection.sensitive_files} /><MetricMini label="版本" value={Number(String(chatlabInspection.chatlab_version || "0").replace(/\D/g, ""))} /></div><div className="boundary-note"><strong>本次写入范围</strong><p>保存 WeFlow 原始 JSON 快照，建立客户时间线和聊天全文索引，默认 restricted；检查后文件变化会拒绝导入。</p></div><button className="danger-safe-button" disabled={busy} onClick={importChatLab}>确认导入客户聊天</button></div>}</Panel>
    </section>
    <div className="section-divider"><span>OTHER KNOWLEDGE SOURCES</span><b>普通业务 / 自我资料</b></div>
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
        <div className="scope-path"><small>CONFIRMED PATH</small><strong>{inspection.path}</strong></div>
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
      {definitions.map((item, index) => <article className="workflow-card" key={item.name}><span className="card-index">0{index + 1}</span><div><small>{item.name}</small><h3>{item.title}</h3><ol>{item.steps.map((step: string) => <li key={step}>{step}</li>)}</ol><p>{item.approval}</p></div>{item.name === "rebuild_search_index" && <button disabled={busy} onClick={() => void run(() => post<Json>("/api/workflows/rebuild-index", {}))}>立即运行</button>}</article>)}
    </section>
    <Panel title="运行记录" code={`${runs.length} RUNS`}>
      <ItemList items={runs} empty="还没有工作流运行记录。" render={(item) => <div className="run-row detailed"><span className={`status-dot ${item.status}`} /><span><strong>{workflowName(item.workflow_name)}</strong><small>{item.id}</small></span><b className={`status-tag ${item.status}`}>{statusLabel(item.status)}</b><time>{formatTime(item.created_at)}</time></div>} />
    </Panel>
  </>;
}

function AgentPanel({ runs, run, setEvidence, busy }: { runs: Json[]; run: Runner; setEvidence: (data: Json) => void; busy: boolean }) {
  const [task, setTask] = useState("");
  const [domain, setDomain] = useState("");
  const [restricted, setRestricted] = useState(false);
  const [context, setContext] = useState<Json | null>(null);
  const submit = (event: FormEvent) => { event.preventDefault(); if (!task.trim()) return; void run(() => post<Json>("/api/agent/context", { task, domain: domain || null, limit: 10, include_restricted: restricted }), setContext); };
  return <>
    <form className="agent-console" onSubmit={submit}>
      <div className="agent-orbit"><span>AGENT</span><i /><i /><i /></div>
      <div className="agent-input"><label>交给智能体的任务</label><textarea value={task} onChange={(event) => setTask(event.target.value)} placeholder="例如：结合我之前的门店项目资料，整理这次需求评审需要先确认的问题。" /><div><select value={domain} onChange={(event) => setDomain(event.target.value)}><option value="">自动选择领域</option><option value="work">业务 / 工作</option><option value="self">自我 / 对话</option><option value="shared">共享</option></select><label className="check-line"><input type="checkbox" checked={restricted} onChange={(event) => setRestricted(event.target.checked)} /> 包含 restricted</label><button disabled={busy}>准备证据上下文</button></div></div>
    </form>
    {context && <section className="context-board"><Panel title="智能体计划" code={context.selected_domain ? `AXIS / ${String(context.selected_domain).toUpperCase()}` : "AXIS / ALL"}><ol className="plan-list">{context.plan.map((step: string, index: number) => <li key={step}><span>{String(index + 1).padStart(2, "0")}</span>{step}</li>)}</ol></Panel><Panel title={`已选证据 · ${context.context.length}`} code="GROUNDING SET"><ItemList items={context.context} empty="没有找到相关资料。" render={(item) => <button className="evidence-row" onClick={() => setEvidence(item)}><span className="status-dot completed" /><span><strong>{item.title}</strong><small>{item.locator} · {item.domain}</small></span><b>证据</b></button>} /></Panel></section>}
    <Panel title="最近的智能体上下文运行" code={`${runs.length} RUNS`}><ItemList items={runs} empty="还没有智能体运行记录。" render={(item) => <div className="run-row"><span className="status-dot completed" /><span><strong>{item.task}</strong><small>{item.selected_domain || "all"} · {item.id}</small></span><time>{formatTime(item.created_at)}</time></div>} /></Panel>
  </>;
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
    <div className="distill-banner"><div><small>DATASET READINESS</small><strong>{items.filter((item) => item.approval_status === "approved").length}</strong><span>条已批准样本</span></div><p>这里只整理未来训练/微调需要的数据，不会自动训练模型。导出文件默认只包含你批准过的样本。</p><button onClick={exportData} disabled={busy}>导出已批准 JSONL</button></div>
    <section className="review-layout">
      <Panel title="沉淀一个高质量样本" code="CURATE"><form className="form-stack" onSubmit={create}><Field label="样本类型"><select value={type} onChange={(event) => setType(event.target.value)}><option value="decision">决策</option><option value="preference">偏好</option><option value="instruction">指令遵循</option><option value="conversation">对话</option></select></Field><Field label="输入 / 情境"><textarea value={input} onChange={(event) => setInput(event.target.value)} placeholder="当时的问题、任务或上下文" /></Field><Field label="你认可的输出"><textarea value={output} onChange={(event) => setOutput(event.target.value)} placeholder="你希望未来模型学会的回答或行动" /></Field><Field label="为什么这样更好"><input value={rationale} onChange={(event) => setRationale(event.target.value)} placeholder="可选：判断标准或偏好原因" /></Field><button className="primary-button" disabled={busy}>保存为候选样本</button></form></Panel>
      <Panel title={`样本队列 · ${items.length}`} code="QUALITY GATE"><ItemList items={items} empty="还没有蒸馏样本。可以从一次满意的 Codex 对话开始沉淀。" render={(item) => <article className="review-card distill"><header><span>{item.example_type}</span><b className={`approval ${item.approval_status}`}>{approvalLabel(item.approval_status)}</b></header><small>输入</small><p>{item.input_text}</p><small>偏好输出</small><p>{item.preferred_output}</p>{item.approval_status === "candidate" && <footer><button disabled={busy} onClick={() => review(item.id, "rejected")}>驳回</button><button className="approve" disabled={busy} onClick={() => review(item.id, "approved")}>批准进入数据集</button></footer>}</article>} /></Panel>
    </section>
  </>;
}

function SettingsPanel({ health, audit }: { health: Json | null; audit: Json[] }) {
  const mcpCommand = "uv --directory E:\\codex-kb run pkas-mcp";
  return <>
    <section className="settings-grid"><Panel title="本地运行状态" code="RUNTIME"><dl className="detail-list"><Detail label="服务状态" value={health ? "运行正常" : "连接中"} /><Detail label="应用版本" value={health?.app_version ?? "—"} /><Detail label="SQLite" value={health?.sqlite_version ?? "—"} /><Detail label="全文分词" value={health?.fts_tokenizer ?? "—"} /><Detail label="元数据位置" value={health?.database ?? "—"} /></dl></Panel><Panel title="Codex 接入" code="MCP / STDIO"><p className="panel-note">把这一条本地 MCP 命令配置给 Codex 后，每个任务都能按需检索知识和已授权的 WeFlow 客户聊天、读取原始快照并准备客户回复上下文。</p><code className="command-code">{mcpCommand}</code><div className="boundary-note"><strong>默认权限</strong><p>微信聊天默认 restricted；WeFlow 接入不读取数据库密钥且不依赖 HTTP API；客户事实只能创建候选；系统不直接发送微信消息。</p></div></Panel></section>
    <Panel title="审计轨迹" code={`${audit.length} EVENTS`}><ItemList items={audit} empty="关键操作发生后会记录在这里。" render={(item) => <div className="audit-row"><span>{String(item.id).padStart(4, "0")}</span><strong>{eventLabel(item.event_type)}</strong><small>{item.subject_type ?? "system"} · {item.subject_id ?? "—"}</small><time>{formatTime(item.created_at)}</time></div>} /></Panel>
  </>;
}

function EvidenceSpine({ evidence, close }: { evidence: Json | null; close: () => void }) {
  if (!evidence) return null;
  return <aside className="evidence-spine"><header><div><small>EVIDENCE SPINE</small><strong>来源与原文</strong></div><button onClick={close}>×</button></header><div className="spine-body"><span className="spine-index">SOURCE / VERIFIED</span><h2>{evidence.title ?? evidence.original_name ?? evidence.customer_name ?? evidence.sender_name ?? "证据详情"}</h2>{evidence.text && <pre>{evidence.text}</pre>}{evidence.content && !evidence.text && <p className="evidence-quote">{evidence.content}</p>}{evidence.snippet && !evidence.text && !evidence.content && <p className="evidence-quote">{evidence.snippet}</p>}<dl className="detail-list"><Detail label="资料 / 消息 ID" value={evidence.source_id ?? evidence.message_id ?? evidence.id ?? "—"} /><Detail label="平台消息 ID" value={evidence.platform_message_id ?? "—"} /><Detail label="发送者" value={evidence.sender_name ?? "—"} /><Detail label="消息时间" value={evidence.sent_at ? formatUnixTime(evidence.sent_at) : "—"} /><Detail label="原始来源" value={evidence.original_uri ?? evidence.source_uri ?? "—"} /><Detail label="仓内原件" value={evidence.vault_path ?? "—"} /><Detail label="定位" value={evidence.locator ?? evidence.platform_message_id ?? `字符 ${evidence.offset ?? 0} 起`} /><Detail label="领域 / 隐私" value={`${evidence.domain ?? "wechat"} / ${evidence.privacy ?? "—"}`} /></dl></div></aside>;
}

type Runner = <T>(job: () => Promise<Envelope<T>>, after?: (data: T) => void) => Promise<Envelope<T> | null>;
function Panel({ title, code, children }: { title: string; code: string; children: ReactNode }) { return <section className="panel"><header className="panel-head"><h2>{title}</h2><span>{code}</span></header><div className="panel-body">{children}</div></section>; }
function Metric({ label, value, unit, tone }: { label: string; value: number; unit: string; tone: string }) { return <article className={`metric-card ${tone}`}><span>{unit}</span><strong>{value.toLocaleString()}</strong><p>{label}</p><i /></article>; }
function MetricMini({ label, value }: { label: string; value: number }) { return <div className="metric-mini"><strong>{value}</strong><span>{label}</span></div>; }
function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) { return <label className="field"><span>{label}{hint && <small>{hint}</small>}</span>{children}</label>; }
function Empty({ text }: { text: string }) { return <div className="empty-state"><span>∅</span><p>{text}</p></div>; }
function ItemList({ items, empty, render }: { items: Json[]; empty: string; render: (item: Json, index: number) => ReactNode }) { return items.length ? <div className="item-list">{items.map((item, index) => <div key={item.id ?? `${index}`}>{render(item, index)}</div>)}</div> : <Empty text={empty} />; }
function Detail({ label, value }: { label: string; value: unknown }) { return <><dt>{label}</dt><dd>{String(value ?? "—")}</dd></>; }

function formatTime(value?: string) { if (!value) return "—"; return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function formatUnixTime(value?: number) { if (!value) return "—"; return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value * 1000)); }
function formatBytes(value?: number) { if (!value) return "0 B"; const units = ["B", "KB", "MB", "GB"]; const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1); return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`; }
function formatNumber(value?: number) { return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value ?? 0); }
function compactPath(value?: string) { if (!value) return "—"; return value.length > 48 ? `…${value.slice(-47)}` : value; }
function statusLabel(value: string) { return ({ completed: "完成", warning: "警告", failed: "失败", running: "运行中" } as Record<string, string>)[value] ?? value; }
function workflowName(value: string) { return ({ import_path: "资料导入与索引", rebuild_search_index: "全文索引重建", persona_review: "个人观察审核", weflow_xlsx_import: "WeFlow XLSX 客户导入", weflow_chatlab_import: "WeFlow ChatLab 导入" } as Record<string, string>)[value] ?? value; }
function approvalLabel(value: string) { return ({ candidate: "待审核", approved: "已批准", rejected: "已驳回" } as Record<string, string>)[value] ?? value; }
function customerTypeLabel(value: string) { return ({ private: "私聊客户", group: "客户群" } as Record<string, string>)[value] ?? value; }
function customerStageLabel(value: string) { return ({ lead: "线索", active: "沟通中", delivery: "交付中", after_sales: "售后", paused: "暂停", closed: "已结束" } as Record<string, string>)[value] ?? value; }
function signalTypeLabel(value: string) { return ({ requirement: "需求", commitment: "承诺", todo: "待办", risk: "风险", decision: "决策", follow_up: "跟进", preference: "偏好" } as Record<string, string>)[value] ?? value; }
function eventLabel(value: string) { return ({ source_ingested: "资料已接入", persona_candidate_created: "画像候选已创建", persona_reviewed: "画像已审核", distillation_candidate_created: "蒸馏候选已创建", distillation_reviewed: "蒸馏样本已审核", weflow_messages_ingested: "WeFlow 消息已接入", customer_signal_created: "客户信号候选已创建", customer_signal_reviewed: "客户信号已审核", customer_profile_updated: "客户档案已更新" } as Record<string, string>)[value] ?? value; }

export default App;
