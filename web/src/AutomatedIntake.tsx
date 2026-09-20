import { useEffect, useState } from "react";
import { api, post, put } from "./api";

type Data = Record<string, any>;

const profileOrder = ["work_efficiency", "complete_personal", "lightweight", "custom"];

export default function AutomatedIntake({ onNext }: { onNext?: () => void }) {
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
      await put<Data>("/api/foundation/processing-profile", {
        profile_id: profileId,
        rules: profileId === "custom" ? selected.rules : null,
        exclusions: selected.exclusions ?? [],
      });
      const refreshed = await api<Data>("/api/foundation/processing-profiles");
      setProfileData(refreshed.data);
      setEditing(false);
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : "保存方案失败");
    } finally { setBusy(false); }
  };

  const startAutomaticCatalog = async () => {
    setBusy(true); setError("");
    try {
      const result = catalog?.id && ["paused", "warning"].includes(state)
        ? await post<Data>(`/api/foundation/machine-catalog/${catalog.id}/resume`, {})
        : await post<Data>("/api/foundation/machine-catalog/start", { confirmed: true });
      setCatalog(result.data);
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : "自动接入启动失败");
    } finally { setBusy(false); }
  };

  const startRecommendedFirstRun = async () => {
    setBusy(true); setError("");
    try {
      const selectedProfile = "work_efficiency";
      const selected = (profileData?.profiles ?? []).find((item: Data) => item.profile_id === selectedProfile);
      if (!selected) throw new Error("推荐整理方案尚未加载完成，请重试");
      const saved = await put<Data>("/api/foundation/processing-profile", {
        profile_id: selectedProfile,
        rules: null,
        exclusions: selected.exclusions ?? [],
      });
      setProfileData(previous => previous ? {
        ...previous,
        current: saved.data,
        first_run_required: false,
        profiles: (previous.profiles ?? []).map((item: Data) => ({
          ...item,
          selected: item.profile_id === selectedProfile,
        })),
      } : previous);
      setEditing(false);
      const result = await post<Data>("/api/foundation/machine-catalog/start", { confirmed: true });
      setCatalog(result.data);
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : "一键整理启动失败");
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
      <div className="automated-section-heading"><div><strong>{profileData?.first_run_required ? "第一次使用：直接一键开始，或先选方案" : "修改整理方式"}</strong><p>默认一键使用“工作提效型”；前三种方案按预设处理，自定义方案会先做 AI 速判，再让你确认入库范围。</p></div><span>方案保存后可随时修改</span></div>
      <div className="processing-profile-grid">
        {profileOrder.map(profileId => {
          const item = (profileData?.profiles ?? []).find((value: Data) => value.profile_id === profileId);
          if (!item) return null;
          return <button key={profileId} type="button" className={!profileData?.first_run_required && current?.profile_id === profileId ? "active" : ""} disabled={busy} onClick={() => void chooseProfile(profileId)}>
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
      {profileData?.first_run_required ? <button className="primary" disabled={busy || state === "running" || !sourceData || scopes.length === 0} onClick={() => void startRecommendedFirstRun()}>{busy ? "正在准备…" : "一键启动本地索引 · 推荐方案"}</button> : <button className="primary" disabled={busy || state === "running" || !current} onClick={() => void startAutomaticCatalog()}>{state === "not_started" ? "开始本机索引" : state === "paused" || state === "warning" ? "继续未完成扫描" : state === "completed" ? "重新核对索引" : "开始本机索引"}</button>}
      <span>{profileData?.first_run_required ? "一键保存“工作提效型”并开始上方列出的目录索引；原文件不移动，不会导入微信或修改 Codex 设置。" : "原文件留在原处；先建立 A 库目录索引和目录 MD，重要内容再按当前方案进入知识库。"}</span>
    </div>
    {catalog && <div className="automated-progress"><strong>{catalog.message ?? stateLabel[state]}</strong><span>已发现 {Number(catalog.catalog_files ?? catalog.files_seen ?? 0).toLocaleString()} 个文件 · 已处理目录 {Number(catalog.directories_seen ?? 0).toLocaleString()} 个 · 排除 {Number(catalog.excluded ?? 0).toLocaleString()} 个</span></div>}
    {["completed", "warning"].includes(state) && <div className="automated-next-step"><div><strong>{state === "completed" ? "文件地图已建立，下一步做内容理解" : "索引有少量范围需要复核；已有文件仍可继续整理"}</strong><span>前往 AI 批次页面继续分类、生成摘要；处理完成后可按分类预览并加入全文或向量知识库。</span></div><button type="button" disabled={!onNext} onClick={onNext}>继续主流程 <span aria-hidden="true">→</span></button></div>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
