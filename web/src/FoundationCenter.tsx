import { useEffect, useState } from "react";
import { api, post } from "./api";
import "./foundation.css";
import AutomatedIntake from "./AutomatedIntake";
import DirectorySummaryPlanner from "./DirectorySummaryPlanner";

type Data = Record<string, any>;
type FoundationTab = "overview" | "intake" | "classification" | "ledger";
const labels: Record<string, string> = {
  cataloged: "仅登记路径", indexed: "曾登记入库", skipped: "按规则跳过 / 无文字",
  missing: "上次未找到", error: "处理失败",
};

function parserLabel(version: unknown) {
  return version === "hybrid-v2" ? "解析 V2（当前）" : "解析旧版 / 待核查";
}

function parsingAttentionLabel(document: Data) {
  const labels: Record<string, string> = {
    empty_native_text: "未提取到正文",
    low_text_or_scanned_pdf_pages: "疑似扫描或低文字 PDF 页",
    visual_pixels_not_read: "图片像素文字未读取",
    embedded_images_not_ocrd: "内嵌图片文字未读取",
    charts_need_semantic_parsing: "图表语义尚未解析",
    replacement_characters: "存在异常字符",
    invalid_json_text_fallback: "JSON 结构无效，按文本读取",
  };
  const reasons = (document.quality_reasons ?? [])
    .map((reason: string) => labels[reason] ?? "存在解析质量提示")
    .filter((value: string, index: number, values: string[]) => values.indexOf(value) === index);
  if (Number(document.visual_pages?.length || 0)) {
    reasons.push(`页 ${document.visual_pages.join("、")} 待视觉处理`);
  }
  return reasons.length ? reasons.join("；") : "请查看解析质量提示";
}

function useRead(path: string | null) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    setData(null); setError("");
    if (!path) { setLoading(false); return; }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    let active = true;
    setLoading(true);
    fetch(path, { signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error(`读取失败（${response.status}），请重试`);
      return response.json();
    }).then(value => { if (active) setData(value.data); })
      .catch(err => { if (active) setError(err.name === "AbortError" ? "读取超时，请重试" : err.message); })
      .finally(() => { clearTimeout(timer); if (active) setLoading(false); });
    return () => { active = false; clearTimeout(timer); controller.abort(); };
  }, [path, version]);
  return { data, error, loading, retry: () => setVersion(n => n + 1) };
}

function State({ value }: { value: ReturnType<typeof useRead> }) {
  if (value.error) return <p role="alert">{value.error} <button onClick={value.retry}>重试</button></p>;
  if (value.loading) return <p role="status">正在读取本地台账…</p>;
  return null;
}

export default function FoundationCenter() {
  const requestedTab = new URLSearchParams(window.location.search).get("foundationTab");
  const initialTab = ["overview", "intake", "classification", "ledger"].includes(requestedTab || "")
    ? requestedTab as "overview" | "intake" | "classification" | "ledger"
    : "overview";
  const [tab, setTab] = useState<FoundationTab>(initialTab);
  const ledgerActive = tab === "ledger";
  const overview = useRead(ledgerActive ? "/api/foundation/overview" : null);
  const scopes = useRead(ledgerActive ? "/api/foundation/scopes" : null);
  const [root, setRoot] = useState("");
  const [state, setState] = useState("cataloged");
  const [offset, setOffset] = useState(0);
  const [docOffset, setDocOffset] = useState(0);
  const documents = useRead(ledgerActive ? `/api/foundation/documents?offset=${docOffset}&limit=25` : null);
  const files = useRead(ledgerActive && root ? `/api/foundation/files?root_id=${encodeURIComponent(root)}&state=${state}&offset=${offset}&limit=25` : null);
  const roots: Data[] = overview.data?.roots ?? [];
  return <div className="foundation-center">
    <FoundationWorkflow activeTab={tab} onNavigate={setTab} />
    <nav className="foundation-tabs" aria-label="资料底座功能">
      <button className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")}><strong>底座总览</strong><small>全机索引与占用</small></button>
      <button className={tab === "intake" ? "active" : ""} onClick={() => setTab("intake")}><strong>资料接入</strong><small>选择范围与入库等级</small></button>
      <button className={tab === "classification" ? "active" : ""} onClick={() => setTab("classification")}><strong>知识分类</strong><small>两级分类、摘要与MD</small></button>
      <button className={tab === "ledger" ? "active" : ""} onClick={() => setTab("ledger")}><strong>索引台账</strong><small>来源、状态与排错</small></button>
    </nav>
    {tab === "overview" && <><MachineCatalogPanel onNavigate={setTab} /><IncludedFilesPanel /></>}
    {tab === "intake" && <AutomatedIntake onNext={() => setTab("classification")} />}
    {tab === "classification" && <DirectorySummaryPlanner />}
    {tab === "ledger" && <>
    <div className="foundation-note">以下台账为只读检查，不会主动扫描或同步。新分类接入记录见上方任务历史，旧同步范围见下表。登记路径 ≠ 内容可搜；向量登记 ≠ 服务在线。</div>
    <section className="foundation-panel">
      <h2>已接入范围与处理台账</h2><State value={overview} />
      {overview.data && <>
        <p>核对时间：{new Date(overview.data.checked_at).toLocaleString()}。以下文件状态来自上次扫描，并非实时磁盘检查。</p>
        <div className="foundation-metrics">{Object.entries(labels).map(([key, label]) => <div key={key}><span>{label}</span><strong>{(overview.data?.catalog_states[key] ?? 0).toLocaleString()}</strong></div>)}</div>
        {roots.length ? <div className="foundation-scroll"><table><thead><tr><th>资料源</th><th>路径</th><th>处理方式</th><th>最后扫描</th></tr></thead><tbody>{roots.map(r => <tr key={r.id}><td>{r.name}<small>{r.enabled ? "已登记启用，不等于正在运行" : "已停用"}</small></td><td>{r.root_uri}</td><td>{r.sync_mode === "catalog" ? "仅登记目录" : "解析并索引"}<small>{r.connector_type}</small></td><td>{r.last_scan_at ? new Date(r.last_scan_at).toLocaleString() : "未扫描"}</td></tr>)}</tbody></table></div> : <p>尚无已登记资料源。</p>}
      </>}
    </section>
    <section className="foundation-panel">
      <h2>为什么某个文件搜不到</h2>
      <div className="foundation-filters"><label>资料源<select aria-label="资料源" value={root} onChange={e => {setRoot(e.target.value); setOffset(0);}}><option value="">请选择已接入资料源</option>{roots.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}</select></label><label>处理状态<select aria-label="处理状态" value={state} onChange={e => {setState(e.target.value); setOffset(0);}}>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label></div>
      <State value={files} />
      {files.data && <><div className="foundation-scroll"><table><thead><tr><th>文件</th><th>当前含义</th><th>原因代码</th></tr></thead><tbody>{files.data.items.map((f: Data) => <tr key={f.id}><td>{f.relative_path}</td><td>{f.explanation}</td><td>{f.reason || "—"}</td></tr>)}</tbody></table></div>{!files.data.items.length && <p>该资料源没有此状态的记录。</p>}<div className="foundation-pager"><button disabled={offset === 0} onClick={() => setOffset(n => Math.max(0, n - 25))}>上一页</button><span>第 {offset / 25 + 1} 页</span><button disabled={!files.data.has_more} onClick={() => setOffset(n => n + 25)}>下一页</button></div></>}
    </section>
    <section className="foundation-panel">
      <h2>实际内容索引 · 不含 Codex 任务记录及聊天消息</h2>
      <State value={documents} />
      {documents.data && <><p>{documents.data.total} 份当前有效文件。{documents.data.note}</p><div className="foundation-scroll"><table><thead><tr><th>文件 / 来源</th><th>全文索引</th><th>向量登记</th><th>解析与切片</th></tr></thead><tbody>{documents.data.items.map((d: Data) => <tr key={d.id}><td>{d.name}<small>{d.path}</small></td><td>{d.fts_chunks ?? "—"}/{d.chunks}<small>{d.fulltext === "indexed" ? "全文索引全库计数一致" : "全文索引需核查"}</small></td><td>{d.vector_chunks}/{d.chunks}<small>{d.vector === "recorded" ? "已登记，在线效果未验" : "登记不完整，未必已排队"}</small></td><td>{d.quality === "attention" ? "存在解析提示，需核查" : "完整性未人工验证"}<small>{parserLabel(d.parser_version)} · 新版切片 {d.typed_v2}/{d.chunks}</small>{d.quality === "attention" && <small>{parsingAttentionLabel(d)}</small>}</td></tr>)}</tbody></table></div><div className="foundation-pager"><button disabled={!docOffset} onClick={() => setDocOffset(n => Math.max(0, n - 25))}>上一组文件</button><span>{docOffset + (documents.data.total ? 1 : 0)}–{Math.min(docOffset + 25, documents.data.total)} / {documents.data.total}</span><button disabled={docOffset + 25 >= documents.data.total} onClick={() => setDocOffset(n => n + 25)}>下一组文件</button></div></>}
    </section>
    <section className="foundation-panel">
      <h2>默认范围建议 · 尚未授权接入</h2><State value={scopes} />
      {scopes.data && <><p>{scopes.data.note}</p><ul>{scopes.data.candidates.map((c: Data) => <li key={c.path}>{c.path} <small>{c.kind === "fixed_drive" ? "本地固定磁盘" : "当前用户常用目录（含重定向）"} · 尚未应用</small></li>)}</ul><details><summary>建议排除的系统、缓存和依赖目录</summary><p>{scopes.data.proposed_exclusions.join("、")}</p></details><p>{scopes.data.external_sources}</p><p>建议不会自动应用。请在上方分类接入中填写具体目录；大范围需分目录处理，不注册自动同步。</p></>}
    </section>
    </>}
  </div>;
}

function FoundationWorkflow({
  activeTab,
  onNavigate,
}: {
  activeTab: FoundationTab;
  onNavigate: (tab: FoundationTab) => void;
}) {
  const [snapshot, setSnapshot] = useState<Data>({});
  const [storage, setStorage] = useState<Data | null>(null);
  useEffect(() => {
    let active = true;
    api<Data>("/api/runtime/storage")
      .then(value => { if (active) setStorage(value.data); })
      .catch(() => { if (active) setStorage(null); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    let active = true;
    const refresh = async () => {
      const [catalog, classification, knowledge] = await Promise.all([
        api<Data>("/api/foundation/machine-catalog").then(value => value.data).catch(() => null),
        api<Data>("/api/foundation/summary-jobs/catalog-progress").then(value => value.data).catch(() => null),
        api<Data>("/api/foundation/included-files?limit=1").then(value => value.data).catch(() => null),
      ]);
      if (active) setSnapshot({ catalog, classification, knowledge });
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), snapshot.catalog?.state === "running" ? 4000 : 20000);
    return () => { active = false; window.clearInterval(timer); };
  }, [snapshot.catalog?.state]);

  const catalog = snapshot.catalog ?? {};
  const classification = snapshot.classification ?? {};
  const knowledge = snapshot.knowledge ?? {};
  const catalogState = String(catalog.state ?? "not_started");
  const catalogFinished = catalogState === "completed" || catalogState === "warning";
  const files = Number(catalog.catalog_files ?? catalog.files_seen ?? 0);
  const aiDone = Number(classification.ai_done ?? 0);
  const aiPending = Number(classification.ai_pending ?? 0);
  const aiFailed = Number(classification.ai_failed ?? 0);
  const aiEligible = Number(classification.ai_eligible ?? 0);
  const included = Number(knowledge.total ?? 0);
  const levels = knowledge.level_counts ?? {};
  const aiState = aiFailed > 0 && aiPending === 0
    ? "attention"
    : aiEligible > 0 && aiPending === 0
      ? "done"
      : aiDone > 0
        ? "running"
        : catalogFinished
          ? "ready"
          : "waiting";
  const catalogLabel = catalogState === "completed" ? "已完成" : catalogState === "warning" ? "部分完成" : catalogState === "running" ? "正在扫描" : catalogState === "paused" ? "已暂停" : "待开始";
  const steps = [
    {
      id: "catalog",
      title: "本机文件索引",
      tab: "intake" as const,
      status: catalogState === "completed" ? "done" : catalogState === "warning" ? "attention" : catalogState === "running" ? "running" : "waiting",
      metric: files.toLocaleString(),
      detail: `${catalogLabel} · 只登记来源与文件信息，不复制原文件`,
      action: catalogState === "not_started" ? "开始本机索引" : catalogState === "running" ? "查看扫描进度" : catalogState === "paused" || catalogState === "warning" ? "继续扫描" : "查看索引",
    },
    {
      id: "understanding",
      title: "AI分类与摘要",
      tab: "classification" as const,
      status: aiState,
      metric: aiEligible ? `${aiDone.toLocaleString()} / ${aiEligible.toLocaleString()}` : "待开始",
      detail: aiEligible ? `待处理 ${aiPending.toLocaleString()} · 失败 ${aiFailed.toLocaleString()}；按批次续跑` : catalogFinished ? "尚无速判记录；可开始首批AI整理" : "本机索引完成后进入此步",
      action: aiDone || aiPending || aiFailed ? "继续AI整理" : "去AI整理",
    },
    {
      id: "knowledge",
      title: "加入可检索知识库",
      tab: "classification" as const,
      status: included > 0 ? "done" : "waiting",
      metric: `${included.toLocaleString()} 份`,
      detail: `全文 ${Number(levels.fulltext ?? 0).toLocaleString()} · 向量 ${Number(levels.semantic ?? 0).toLocaleString()}；AI建议先预览，确认后才入库`,
      action: "查看入库与检索",
    },
  ];

  return <section className="foundation-workflow" aria-label="资料整理快速流程">
    <header><div><small>QUICK START · SAFE BY DEFAULT</small><h2>从电脑文件到可检索知识</h2><p>从上到下按推荐流程走；已有进度会自动读取，退出后可继续。</p></div><span>一条主流程</span></header>
    <div className="foundation-workflow-steps">{steps.map((step, index) => <article key={step.id} className={`foundation-workflow-step ${step.status} ${activeTab === step.tab ? "current" : ""}`}>
      <div className="workflow-step-index">0{index + 1}</div>
      <div className="workflow-step-content"><div className="workflow-step-title"><strong>{step.title}</strong><b>{step.metric}</b></div><p>{step.detail}</p><button type="button" onClick={() => onNavigate(step.tab)}>{step.action} <span aria-hidden="true">→</span></button></div>
    </article>)}</div>
    <p className="foundation-workflow-safety">
      <span>知识库：{storage ? <><code>{storage.data_root}</code>{storage.data_on_system_drive && <strong>系统盘</strong>}</> : "暂不可读取"}</span>
      {storage?.runtime_root && <span>运行环境：<code>{storage.runtime_root}</code>{storage.runtime_on_system_drive && <strong>系统盘</strong>}</span>}
      <span>安全边界：第一步仅本地扫描；发送文字样本给 Luna/DeepSeek 要单独勾选同意；加入知识库前会显示预览并要求确认。源文件不会搬动，向量只在选定 L3 时生成。</span>
    </p>
  </section>;
}

type ChartItem = { label: string; value: number; color: string };

const chartColors = ["#42d5c7", "#4f8cff", "#a17dff", "#f0b35a", "#e66b87", "#6fc98c", "#6aa7b8", "#8a9aa7"];

function DonutChart({ title, subtitle, items }: { title: string; subtitle: string; items: ChartItem[] }) {
  const visible = items.filter(item => item.value > 0);
  const total = visible.reduce((sum, item) => sum + item.value, 0);
  const radius = 52;
  const circumference = 2 * Math.PI * radius;
  let consumed = 0;
  return <article className="foundation-chart-card">
    <header><div><h3>{title}</h3><p>{subtitle}</p></div></header>
    <div className="donut-layout">
      <svg className="donut-chart" viewBox="0 0 140 140" role="img" aria-label={`${title}，总计 ${total.toLocaleString()}`}>
        <circle className="donut-track" cx="70" cy="70" r={radius} />
        {visible.map(item => {
          const length = total ? item.value / total * circumference : 0;
          const offset = -consumed;
          consumed += length;
          return <circle key={item.label} className="donut-segment" cx="70" cy="70" r={radius}
            stroke={item.color} strokeDasharray={`${length} ${circumference - length}`} strokeDashoffset={offset}>
            <title>{item.label}：{item.value.toLocaleString()}</title>
          </circle>;
        })}
        <text x="70" y="66" textAnchor="middle" className="donut-total">{compactNumber(total)}</text>
        <text x="70" y="84" textAnchor="middle" className="donut-caption">合计</text>
      </svg>
      <div className="chart-legend">{visible.map(item => <div key={item.label}>
        <i style={{ background: item.color }} /><span>{item.label}</span><strong>{item.value.toLocaleString()}</strong>
      </div>)}</div>
    </div>
  </article>;
}

function TrendChart({ items }: { items: Data[] }) {
  const width = 660, height = 190, padX = 28, padTop = 20, padBottom = 34;
  const max = Math.max(1, ...items.map(item => Number(item.count || 0)));
  const points: Array<{ date: string; count: number; x: number; y: number }> = items.map((item, index) => ({
    date: String(item.date || ""),
    count: Number(item.count || 0),
    x: padX + (items.length <= 1 ? 0 : index / (items.length - 1) * (width - padX * 2)),
    y: padTop + (1 - Number(item.count || 0) / max) * (height - padTop - padBottom),
  }));
  const line = points.map(point => `${point.x},${point.y}`).join(" ");
  const area = points.length ? `${padX},${height - padBottom} ${line} ${width - padX},${height - padBottom}` : "";
  const labels = points.length ? [points[0], points[Math.floor(points.length / 2)], points[points.length - 1]] : [];
  return <article className="foundation-chart-card trend-card">
    <header><div><h3>近30天新增可搜索文件</h3><p>只统计实际完成解析入库的文件，不包含全盘目录登记和 Codex 任务记录。</p></div><strong className="trend-peak">峰值 {max}</strong></header>
    <svg className="trend-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="近30天新增可搜索文件折线图">
      {[0, .5, 1].map(ratio => <line key={ratio} x1={padX} x2={width - padX} y1={padTop + ratio * (height - padTop - padBottom)} y2={padTop + ratio * (height - padTop - padBottom)} />)}
      {area && <polygon points={area} className="trend-area" />}
      {line && <polyline points={line} className="trend-line" />}
      {points.map(point => <circle key={point.date} cx={point.x} cy={point.y} r="3" className="trend-point"><title>{point.date}：新增 {point.count} 份</title></circle>)}
      {labels.map(point => <text key={point.date} x={point.x} y={height - 10} textAnchor={point === labels[0] ? "start" : point === labels[labels.length - 1] ? "end" : "middle"}>{String(point.date).slice(5)}</text>)}
    </svg>
  </article>;
}

function groupedSourceTypes(rows: Data[]): ChartItem[] {
  const groups: Record<string, number> = { "文本 / Markdown": 0, "Office / PDF": 0, "代码": 0, "其他": 0 };
  const office = new Set(["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"]);
  const code = new Set(["ts", "tsx", "js", "jsx", "vue", "py", "java", "kt", "c", "cpp", "h", "hpp", "cs", "go", "rs", "html", "css", "json", "yaml", "yml"]);
  rows.forEach(row => {
    const type = String(row.type || "").toLowerCase();
    const target = ["md", "txt", "text"].includes(type) ? "文本 / Markdown" : office.has(type) ? "Office / PDF" : code.has(type) ? "代码" : "其他";
    groups[target] += Number(row.count || 0);
  });
  return Object.entries(groups).map(([label, value], index) => ({ label, value, color: chartColors[index] }));
}

const includedLevelLabels: Record<string, string> = {
  record_only: "只登记",
  fulltext: "全文可搜",
  semantic: "全文＋向量",
  summary: "AI摘要",
};

function levelCount(data: Data | null, level: string) {
  return Number(data?.canonical_level_counts?.[level] ?? 0);
}

function IncludedFilesPanel() {
  const [drive, setDrive] = useState("ALL");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    fetch(`/api/foundation/included-files?drive=${encodeURIComponent(drive)}&offset=${offset}&limit=100`, { signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error(`读取失败（${response.status}）`); return response.json(); })
      .then(payload => setData(payload.data))
      .catch(exception => { if (!controller.signal.aborted) setError(exception instanceof Error ? exception.message : "读取失败"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [drive, offset]);
  const disks: Data[] = data?.disks ?? [];
  return <section className="foundation-panel included-browser">
    <header><div><small>LAYER 2 · SEARCHABLE KNOWLEDGE</small><h2>第二层：知识库明细</h2><p>正文只保存一份；同内容的其他原始路径会单列为覆盖路径，不重复占用切片和向量。</p></div><span>{Number(data?.total ?? 0).toLocaleString()} 路径<small>{Number(data?.canonical_total ?? 0).toLocaleString()} 份正文</small></span></header>
    {error && <p role="alert">{error}</p>}
    {data && <div className="included-level-summary" aria-label="当前资料处理层级概览">
      <div><small>规范正文</small><strong>{Number(data.canonical_total ?? 0).toLocaleString()}</strong><span>不含重复路径</span></div>
      <div><small>全文可搜</small><strong>{levelCount(data, "fulltext").toLocaleString()}</strong><span>本地正文与切片</span></div>
      <div><small>语义已登记</small><strong>{levelCount(data, "semantic").toLocaleString()}</strong><span>不等于服务在线</span></div>
      <div><small>派生摘要</small><strong>{levelCount(data, "summary").toLocaleString()}</strong><span>与原件分开</span></div>
      <div><small>仅登记</small><strong>{levelCount(data, "record_only").toLocaleString()}</strong><span>无法按正文搜索</span></div>
    </div>}
    <div className="included-disk-grid">
      {disks.map(disk => <button key={disk.key} className={drive === disk.key ? "active" : ""} onClick={() => { setDrive(disk.key); setOffset(0); }}>
        <div><strong>{disk.label}</strong><span>{Number(disk.included_files || 0).toLocaleString()} 条已覆盖路径</span></div>
        {disk.key !== "DERIVED" ? <>
          <div className="disk-usage-track"><i style={{ width: `${Math.min(100, Number(disk.used_bytes || 0) / Math.max(1, Number(disk.total_bytes || 0)) * 100)}%` }} /></div>
          <p>原文件合计 {formatSize(Number(disk.included_bytes || 0))} · 占全盘 {formatPercent(Number(disk.disk_share_percent || 0))}</p>
          <small>{Number(disk.canonical_files || 0).toLocaleString()} 份规范正文 · A库发现 {Number(disk.catalog_files || 0).toLocaleString()} 个文件 · 路径覆盖 {formatPercent(Number(disk.file_coverage_percent || 0))}</small>
        </> : <><p>{Number(disk.summary_files || 0).toLocaleString()} 份自动摘要</p><small>派生内容单列，不冒充原盘文件</small></>}
      </button>)}
    </div>
    <div className="included-toolbar">
      <div className="included-breadcrumb"><button className={drive === "ALL" ? "active" : ""} onClick={() => { setDrive("ALL"); setOffset(0); }}>全部位置</button><span>›</span><strong>{drive === "ALL" ? "所有已纳入资料" : disks.find(item => item.key === drive)?.label ?? drive}</strong></div>
      <div><span>内容数据库 {formatSize(Number(data?.database_bytes || 0))}（含聊天与任务记录）</span><span>微信消息 {Number(data?.customer_messages || 0).toLocaleString()} 条（独立内容集合）</span></div>
    </div>
    {loading && <p role="status">正在读取纳入清单…</p>}
    {!loading && data && <div className="included-file-list" role="table" aria-label="知识库已纳入文件">
      <div className="included-file-head" role="row"><span>名称</span><span>所在位置</span><span>纳入程度</span><span>原文件大小</span></div>
      {data.items.map((item: Data) => <div className="included-file-row" role="row" key={item.id}>
        <div><i>{item.level === "summary" ? "◇" : "▤"}</i><strong>{item.name}</strong><small>{item.is_alias ? "同内容覆盖路径 · 不重复存正文" : item.source_type}</small></div>
        <code title={item.path}>{item.path}</code>
        <div><b className={`included-level ${item.level}`}>{includedLevelLabels[item.level] ?? item.level}</b><small>{Number(item.chunks || 0)} 块 · {Number(item.vectors || 0)} 向量</small></div>
        <span>{formatSize(Number(item.byte_size || 0))}</span>
      </div>)}
      {!data.items.length && <p>这块盘目前还没有正式纳入内容的文件；A库轻索引可能已经记录了路径。</p>}
    </div>}
    {data && <div className="foundation-pager"><button disabled={!offset || loading} onClick={() => setOffset(value => Math.max(0, value - 100))}>上一页</button><span>{data.total ? `${offset + 1}–${Math.min(offset + 100, data.total)}` : "0"} / {data.total}</span><button disabled={!data.has_more || loading} onClick={() => setOffset(value => value + 100)}>下一页</button></div>}
    {data?.note && <p className="chart-footnote">{data.note}</p>}
  </section>;
}

function MachineCatalogPanel({ onNavigate }: { onNavigate: (tab: "overview" | "intake" | "classification" | "ledger") => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [analytics, setAnalytics] = useState<Data | null>(null);
  const [coverage, setCoverage] = useState<Data | null>(null);
  const [notice, setNotice] = useState("");
  const [working, setWorking] = useState(false);
  const load = async () => {
    const response = await fetch("/api/foundation/machine-catalog");
    const payload = await response.json();
    setData(payload.data);
  };
  useEffect(() => {
    void load().catch(error => setNotice(String(error)));
    fetch("/api/foundation/summary-jobs/catalog-progress").then(response => response.json()).then(payload => setCoverage(payload.data)).catch(() => setNotice("AI速判进度暂时无法读取，文件索引不受影响。"));
    fetch("/api/foundation/analytics?days=30").then(response => response.json()).then(payload => setAnalytics(payload.data)).catch(() => setNotice("可视化统计暂时无法读取，核心索引不受影响。"));
  }, []);
  useEffect(() => {
    if (data?.state !== "running") return;
    const timer = window.setInterval(() => void load().catch(() => undefined), 3000);
    return () => window.clearInterval(timer);
  }, [data?.state]);
  const action = async (kind: "start" | "pause" | "resume") => {
    setWorking(true);
    try {
      const path = kind === "start"
        ? "/api/foundation/machine-catalog/start"
        : `/api/foundation/machine-catalog/${data?.id}/${kind}`;
      const response = await post<Data>(path, kind === "start" ? { confirmed: true } : {});
      setData(response.data); setNotice(response.summary);
      const progress = await fetch("/api/foundation/summary-jobs/catalog-progress");
      if (progress.ok) setCoverage((await progress.json()).data);
    } catch (error) { setNotice(error instanceof Error ? error.message : "操作失败"); }
    finally { setWorking(false); }
  };
  const scopes = data?.scopes ?? [];
  const pending = Number(data?.queue?.pending ?? 0) + Number(data?.queue?.working ?? 0);
  const scopeChart = scopes.map((scope: Data, index: number) => ({ label: scopeLabel(scope.root_path), value: Number(scope.files_seen || 0), color: chartColors[index % chartColors.length] }));
  const typeChart = groupedSourceTypes(analytics?.source_types ?? []);
  const coveragePercent = coverage ? formatPercent(Number(coverage.decision_coverage_percent ?? 0)) : "—";
  return <section className="foundation-machine-panel foundation-panel">
    <header><div><small>KNOWLEDGE FOUNDATION · TWO LAYERS</small><h2>知识底座分层总览</h2><p>第一层负责找到电脑里有什么；第二层才是读取正文后可供 Codex 检索的知识。两层容量和数量不混算。</p></div><span className={`catalog-state ${data?.state ?? "not_started"}`}>{data?.state ?? "未开始"}</span></header>
    <div className="foundation-layer-grid">
      <article className="foundation-layer-card catalog-layer">
        <header><div><small>LAYER 1 · FILE CATALOG</small><h3>索引库</h3></div><b>只存路径和属性</b></header>
        <p>用于秒级定位全盘文件，不读取正文，也不代表文件已经进入知识库。</p>
        <div className="layer-metrics">
          <div><span>已发现文件</span><strong>{Number(data?.catalog_files ?? 0).toLocaleString()}</strong></div>
          <div><span>代表原文件</span><strong>{formatSize(Number(data?.represented_bytes ?? 0))}</strong></div>
          <div><span>索引自身占用</span><strong>{formatSize(Number(data?.database_bytes ?? 0))}</strong></div>
        </div>
      </article>
      <article className="foundation-layer-card knowledge-layer">
        <header><div><small>LAYER 2 · SEARCHABLE KNOWLEDGE</small><h3>知识库</h3></div><b>正文＋全文＋向量</b></header>
        <p>文件经过解析和切片后才能全文搜索；完成向量化后还能按语义搜索。</p>
        <div className="layer-metrics">
          <div><span>规范正文资料</span><strong>{Number(analytics?.original_documents ?? 0).toLocaleString()}</strong><small>去重后存一份</small></div>
          <div><span>已覆盖原始路径</span><strong>{Number(analytics?.represented_original_paths ?? 0).toLocaleString()}</strong><small>含 {Number(analytics?.source_aliases ?? 0).toLocaleString()} 条同内容路径</small></div>
          <div><span>可搜索切片</span><strong>{Number(analytics?.searchable_chunks ?? 0).toLocaleString()}</strong></div>
          <div><span>向量切片</span><strong>{Number(analytics?.vector_chunks ?? 0).toLocaleString()}</strong><small>覆盖 {formatPercent(Number(analytics?.vector_coverage_percent ?? 0))}</small></div>
          <div><span>派生摘要</span><strong>{Number(analytics?.derived_summaries ?? 0).toLocaleString()}</strong></div>
          <div><span>内容库占用</span><strong>{formatSize(Number(analytics?.database_bytes ?? 0))}</strong><small>含微信和任务记录</small></div>
        </div>
      </article>
    </div>
    <div className="foundation-promotion-line"><span>全盘文件</span><i>按需读取正文 → 解析切片 → 向量化</i><strong>可搜索知识</strong></div>
    <div className="foundation-metrics compact">
      <div><span>本轮目录</span><strong>{Number(data?.directories_seen ?? 0).toLocaleString()}</strong></div>
      <div><span>待扫目录</span><strong>{pending.toLocaleString()}</strong></div>
      <div><span>目录MD</span><strong>{Number(data?.summary_files ?? 0).toLocaleString()}</strong></div>
    </div>
    {coverage && <div className="foundation-insight-metrics">
      <div><span>文件处置结论覆盖</span><strong>{coveragePercent}</strong><small>AI速判＋明确本地跳过</small></div>
      <div><span>待AI速判</span><strong>{Number(coverage.ai_pending ?? 0).toLocaleString()}</strong><small>失败 {Number(coverage.ai_failed ?? 0).toLocaleString()}</small></div>
      <div><span>AI已速判</span><strong>{Number(coverage.ai_done ?? 0).toLocaleString()}</strong><small>应处理 {Number(coverage.ai_eligible ?? 0).toLocaleString()}</small></div>
      <div><span>本地明确跳过</span><strong>{Number(coverage.local_skipped ?? 0).toLocaleString()}</strong><small>临时输出类文件</small></div>
    </div>}
    {coverage && <p className="chart-footnote">{coverage.coverage_policy}。100%只表示每个文件都有处置结论，不表示每个文件都做了全文解析或向量化。</p>}
    <div className="foundation-quick-actions" aria-label="资料底座快捷操作">
      <button className="primary" onClick={() => onNavigate("intake")}><strong>接入新资料</strong><small>选目录并决定索引深度</small></button>
      <button onClick={() => onNavigate("classification")}><strong>整理知识分类</strong><small>查看摘要并调整分类</small></button>
      <button onClick={() => onNavigate("ledger")}><strong>检查索引台账</strong><small>定位为什么资料搜不到</small></button>
    </div>
    {analytics && <>
      <div className="foundation-insight-metrics">
        <div><span>正式可搜索资料</span><strong>{Number(analytics.searchable_documents || 0).toLocaleString()}</strong></div>
        <div><span>完整向量化资料</span><strong>{Number(analytics.vectorized_documents || 0).toLocaleString()}</strong></div>
        <div><span>微信消息全文索引</span><strong>{Number(analytics.customer_messages || 0).toLocaleString()}</strong></div>
        <div><span>Codex 用户任务记录</span><strong>{Number(analytics.codex_records || 0).toLocaleString()}</strong></div>
      </div>
      <div className="foundation-chart-grid">
        <DonutChart title="全盘文件分布" subtitle="轻索引发现的文件按磁盘范围分布。" items={scopeChart} />
        <DonutChart title="可搜索文件类型" subtitle="已解析入库的文件构成，不含聊天和 Codex 记录。" items={typeChart} />
        <TrendChart items={analytics.ingest_trend ?? []} />
      </div>
      <p className="chart-footnote">{analytics.note}</p>
    </>}
    <div className="catalog-actions">
      {!data || data.state === "not_started" ? <button disabled={working} onClick={() => void action("start")}>开始全盘轻索引</button> : null}
      {data?.state === "running" ? <button disabled={working} onClick={() => void action("pause")}>暂停并保留进度</button> : null}
      {data && ["paused", "warning"].includes(data.state) ? <button disabled={working} onClick={() => void action("resume")}>继续索引</button> : null}
      <span>{data?.message ?? "图片本轮只登记，OCR和视觉切分在下一阶段执行。"}</span>
    </div>
    {notice && <p>{notice}</p>}
    {scopes.length > 0 && <div className="scope-progress">{scopes.map((scope: Data) => <div key={scope.root_path}><strong>{scope.root_path}</strong><span>{Number(scope.files_seen).toLocaleString()} 文件 · {Number(scope.directories_seen).toLocaleString()} 目录 · 错误 {scope.errors}</span></div>)}</div>}
    {data?.summary_path && <p>派生MD：<code>{data.summary_path}</code></p>}
  </section>;
}

function formatSize(value: number) {
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function formatPercent(value: number) {
  if (!value) return "0%";
  if (value < 0.0001) return "<0.0001%";
  if (value < 0.01) return `${value.toFixed(4)}%`;
  return `${value.toFixed(2)}%`;
}

function compactNumber(value: number) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}K`;
  return String(value);
}

function scopeLabel(path: string) {
  const normalized = String(path || "");
  const drive = normalized.match(/^([A-Z]):\\/i)?.[1]?.toUpperCase();
  if (!drive) return normalized || "其他";
  if (/\\Documents$/i.test(normalized)) return `${drive}盘文档`;
  if (/\\Downloads$/i.test(normalized)) return `${drive}盘下载`;
  return `${drive}盘`;
}
