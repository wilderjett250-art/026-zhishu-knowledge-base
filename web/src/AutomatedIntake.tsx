import { useEffect, useState } from "react";
import { api, post, put } from "./api";

type Data = Record<string, any>;

const profileOrder = ["work_efficiency", "complete_personal", "lightweight", "custom"];

export default function AutomatedIntake() {
  const [profileData, setProfileData] = useState<Data | null>(null);
  const [sourceData, setSourceData] = useState<Data | null>(null);
  const [catalog, setCatalog] = useState<Data | null>(null);
  const [coverage, setCoverage] = useState<Data | null>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = async () => {
    setError("");
    const [profiles, sources, catalogResult] = await Promise.all([
      api<Data>("/api/foundation/processing-profiles"),
      api<Data>("/api/foundation/auto-sources"),
      api<Data>("/api/foundation/machine-catalog").catch(() => ({ data: { state: "not_started" } } as Data)),
    ]);
    setProfileData(profiles.data);
    setSourceData(sources.data);
    setCatalog(catalogResult.data);
    setEditing(Boolean(profiles.data.first_run_required));
  };

  useEffect(() => { void load().catch(exception => setError(exception instanceof Error ? exception.message : "读取自动接入状态失败")); }, []);
  useEffect(() => {
    if (catalog?.state !== "running") return;
    const timer = window.setInterval(() => {
      api<Data>("/api/foundation/machine-catalog")
        .then(result => setCatalog(result.data))
        .catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [catalog?.state]);
  useEffect(() => {
    const refresh = () => api<Data>("/api/foundation/summary-jobs/catalog-progress")
      .then(result => setCoverage(result.data))
      .catch(() => undefined);
    void refresh();
    const timer = window.setInterval(refresh, catalog?.state === "running" ? 3000 : 15000);
    return () => window.clearInterval(timer);
  }, [catalog?.state]);

  const chooseProfile = async (profileId: string) => {
    const selected = (profileData?.profiles ?? []).find((item: Data) => item.profile_id === profileId);
    if (!selected) return;
    setBusy(true); setError("");
    try {
      const result = await put<Data>("/api/foundation/processing-profile", {
        profile_id: profileId,
        rules: profileId === "custom" ? selected.rules : null,
        exclusions: selected.exclusions ?? [],
      });
      setProfileData(result.data);
      setEditing(false);
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : "保存方案失败");
    } finally { setBusy(false); }
  };

  const startAutomaticCatalog = async () => {
    setBusy(true); setError("");
    try {
      const result = await post<Data>("/api/foundation/machine-catalog/start", { confirmed: true });
      setCatalog(result.data);
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : "自动接入启动失败");
    } finally { setBusy(false); }
  };

  const current = profileData?.current;
  const scopes: Data[] = sourceData?.scopes ?? [];
  const state = String(catalog?.state ?? "not_started");
  const scopeText = scopes.length ? scopes.map((item: Data) => item.path).join("、") : "正在读取系统范围…";
  const stateLabel: Record<string, string> = {
    not_started: "尚未开始",
    running: "正在自动整理",
    paused: "已暂停，可继续",
    completed: "自动索引已完成",
    warning: "部分范围需要检查",
  };

  return <section className="foundation-panel automated-intake-panel">
    <header>
      <div><small>AUTOMATED LOCAL INTAKE</small><h2>自动资料接入</h2><p>系统自己判断范围、排除系统目录并建立全盘索引；普通使用不需要填写路径。</p></div>
      <span className={`catalog-state ${state}`}>{stateLabel[state] ?? state}</span>
    </header>

    {editing ? <section className="first-run-profile">
      <div className="automated-section-heading"><div><strong>第一次使用，先选一次整理方式</strong><p>前三种方案直接按预设执行；自定义方案会先做 AI 速判，再让你确认入库范围。</p></div><span>方案保存后可随时修改</span></div>
      <div className="processing-profile-grid">
        {profileOrder.map(profileId => {
          const item = (profileData?.profiles ?? []).find((value: Data) => value.profile_id === profileId);
          if (!item) return null;
          return <button key={profileId} type="button" className={current?.profile_id === profileId ? "active" : ""} disabled={busy} onClick={() => void chooseProfile(profileId)}>
            <span><strong>{item.label}</strong><small>{item.short_label}</small></span>
            <p>{item.description}</p>
            <ul>{(item.highlights ?? []).map((highlight: string) => <li key={highlight}>{highlight}</li>)}</ul>
          </button>;
        })}
      </div>
    </section> : <section className="current-profile-bar">
      <div><small>当前自动整理方案</small><strong>{current?.label ?? "自定义方案"}</strong><span>{current?.description ?? "按本机设置自动处理资料"}</span></div>
      <button type="button" disabled={busy || state === "running"} onClick={() => setEditing(true)}>修改方案</button>
    </section>}

    <section className="automated-source-card">
      <div className="automated-section-heading"><div><strong>系统自动选择资料范围</strong><p>{sourceData?.policy ?? "C盘保守处理，其他固定盘建立目录索引，系统和敏感目录自动排除。"}</p></div><b>{scopes.length} 个自动范围</b></div>
      <div className="automated-scope-list">{scopes.map((item: Data) => <span key={item.path}><i>{item.kind === "fixed_data_drive" ? "盘" : "目录"}</i>{item.path}</span>)}</div>
      <details><summary>查看当前自动范围</summary><p>{scopeText}</p></details>
    </section>

    {coverage && <section className="foundation-insight-metrics intake-coverage-card" aria-label="文件整理与内容理解进度">
      <div><span>全盘处置覆盖</span><strong>{Number(coverage.decision_coverage_percent ?? 0).toFixed(2)}%</strong><small>已理解＋明确跳过 / 已索引</small></div>
      <div><span>需要内容理解</span><strong>{Number(coverage.ai_eligible ?? 0).toLocaleString()}</strong><small>已索引－明确跳过</small></div>
      <div><span>内容理解覆盖</span><strong>{Number(coverage.ai_coverage_percent ?? 0).toFixed(2)}%</strong><small>AI已完成 / 需要理解范围</small></div>
      <div><span>待 AI 速判</span><strong>{Number(coverage.ai_pending ?? 0).toLocaleString()}</strong><small>失败 {Number(coverage.ai_failed ?? 0).toLocaleString()}</small></div>
    </section>}
    {coverage && <p className="chart-footnote intake-coverage-note">“全盘处置覆盖”包含本地规则明确跳过的低价值文件；“内容理解覆盖”只统计真正需要 AI 判断的文件，两者不能混为一个百分比。</p>}

    <div className="automated-actions">
      <button className="primary" disabled={busy || state === "running" || !current} onClick={() => void startAutomaticCatalog()}>{state === "not_started" ? "开始自动整理" : state === "paused" || state === "warning" ? "继续自动整理" : "刷新自动索引"}</button>
      <span>原文件留在原处；先建立A库目录索引和目录MD，重要内容再按当前方案进入知识库。</span>
    </div>
    {catalog && <div className="automated-progress"><strong>{catalog.message ?? stateLabel[state]}</strong><span>已发现 {Number(catalog.catalog_files ?? catalog.files_seen ?? 0).toLocaleString()} 个文件 · 已处理目录 {Number(catalog.directories_seen ?? 0).toLocaleString()} 个 · 排除 {Number(catalog.excluded ?? 0).toLocaleString()} 个</span></div>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
