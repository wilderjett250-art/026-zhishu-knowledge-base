import { useEffect, useState } from "react";
import { api, post, put } from "./api";
type Data = Record<string, any>;
const groups: Record<string, string> = { markdown: "Markdown 笔记", documents: "文档 / 表格 / 文本", code: "源码 / 脚本", images: "图片 / 截图", other: "其他文件" };
const modes: Record<string, string> = { semantic: "L3 全文切块＋向量", full: "L2 全文切块＋搜索", extract: "L1 本地摘录＋MD", md_only: "只收现有 MD", md_fallback: "收 MD；没有则生成目录 MD", catalog: "L0 只登记位置", exclude: "不接入" };
const states: Record<string, string> = { scanning: "扫描预览中", ready: "等待确认", backing_up: "校验入库恢复点", running: "正在处理", completed: "本批处理完成", warning: "部分内容需要处理", failed: "任务失败", cancelled: "已取消", interrupted: "任务已中断", limited: "范围过大，未完整预览", pending: "待处理", deferred: "已拆分，等待L3确认", indexed: "全文已入库", duplicate: "已有同内容索引", cataloged: "仅登记位置", excluded: "已排除", skipped: "未处理", error: "失败" };
const initial = { markdown: "full", documents: "full", code: "catalog", images: "catalog", other: "catalog" };
const bytes = (n: number) => n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(2)} MB` : n >= 1000 ? `${(n / 1000).toFixed(1)} KB` : `${n} B`;
const qualityText = (quality: Data | undefined) => {
  if (!quality || quality.status === "not_assessed") return "解析质量：未评估";
  const score = typeof quality.score === "number" ? ` · 评分 ${Math.round(quality.score * 100)}%` : "";
  const notes = [...(quality.reasons || []), ...(quality.warnings || [])];
  return quality.status === "attention" ? `解析需注意${score}${notes.length ? ` · ${notes.join(" / ")}` : ""}` : `解析就绪${score}`;
};

export default function ClassifiedIntake() {
  const [path, setPath] = useState("");
  const [scanner, setScanner] = useState("everything");
  const [scope, setScope] = useState<Data | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [scopeFilter, setScopeFilter] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [component, setComponent] = useState<Data | null>(null);
  const [profiles, setProfiles] = useState<Data | null>(null);
  const [profileId, setProfileId] = useState("custom");
  const [profileBusy, setProfileBusy] = useState(false);
  useEffect(() => { api<Data>("/api/foundation/everything").then(r => setComponent(r.data)).catch(() => setComponent({ available: false, backend_outdated: true })); }, []);
  const [rules, setRules] = useState<Record<string, string>>(initial);
  const [exclude, setExclude] = useState("");
  const [recoveryRoot, setRecoveryRoot] = useState("");
  const [job, setJob] = useState<Data | null>(null);
  const [recent, setRecent] = useState<Data[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [vectorConfirmed, setVectorConfirmed] = useState(false);
  const [offset, setOffset] = useState(0);
  const [version, setVersion] = useState(0);
  const active = job && ["scanning", "running", "backing_up"].includes(job.state);
  const vectorRequested = !job?.deferred_semantic_plan_id && (Number(job?.actions?.semantic ?? 0) > 0 || Object.values(rules).includes("semantic"));
  const selectable = (scope?.items ?? []).filter((item: Data) => item.selectable);
  const visibleScope = (scope?.items ?? []).filter((item: Data) => !scopeFilter.trim() || item.name.toLocaleLowerCase().includes(scopeFilter.trim().toLocaleLowerCase()));
  useEffect(() => {
    api<Data>("/api/foundation/processing-profiles")
      .then(r => {
        setProfiles(r.data);
        setProfileId(r.data.current?.profile_id ?? "custom");
        setRules(r.data.current?.rules ?? initial);
      })
      .catch(e => setError(e.message));
  }, []);
  useEffect(() => { api<Data[]>("/api/foundation/intake").then(r => setRecent(r.data)).catch(e => setError(e.message)); }, [version]);
  useEffect(() => {
    if (job || active || !/^[a-zA-Z]:[\\/]/.test(path.trim())) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      setBrowsing(true); setError("");
      post<Data>("/api/foundation/intake/browse", {path: path.trim()})
        .then(result => { if (!cancelled) { setScope(result.data); setSelected([]); } })
        .catch(e => { if (!cancelled) { setScope(null); setSelected([]); setError(`无法读取第一层：${e.message}`); } })
        .finally(() => { if (!cancelled) setBrowsing(false); });
    }, 600);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [path, job, active]);
  useEffect(() => {
    if (!job?.id) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const value = await api<Data>(`/api/foundation/intake/${job.id}?offset=${offset}`);
        if (cancelled) return;
        setJob(value.data);
        if (["scanning", "running", "backing_up"].includes(value.data.state)) timer = setTimeout(refresh, 1500);
      } catch (e) { if (!cancelled) setError(`状态读取失败，请点刷新：${(e as Error).message}`); }
    };
    refresh();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [job?.id, offset, version]);
  const perform = async (fn: () => Promise<void>) => {
    setError(""); setBusy(true);
    try { await fn(); setVersion(v => v + 1); } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const chooseProfile = async (nextId: string) => {
    const selectedProfile = (profiles?.profiles ?? []).find((item: Data) => item.profile_id === nextId);
    if (!selectedProfile) return;
    setProfileBusy(true); setError("");
    try {
      const saved = await put<Data>("/api/foundation/processing-profile", {
        profile_id: nextId,
        rules: nextId === "custom" ? selectedProfile.rules : null,
        exclusions: selectedProfile.exclusions ?? [],
      });
      setProfiles(saved.data); setProfileId(nextId); setRules(saved.data.current.rules);
      setJob(null); setScope(null); setSelected([]); setConfirmed(false); setVectorConfirmed(false);
    } catch (e) { setError((e as Error).message); }
    finally { setProfileBusy(false); }
  };
  const persistCustomRules = async () => {
    const saved = await put<Data>("/api/foundation/processing-profile", {
      profile_id: "custom",
      rules,
      exclusions: exclude.split(/[,，]/).map(s => s.trim()).filter(Boolean),
    });
    setProfiles(saved.data); setProfileId("custom");
  };
  return <section className="foundation-panel intake-panel">
    <header><div><small>LOCAL INTAKE</small><h2>资料接入</h2></div><span className="intake-badge">原件留在原处</span></header>
    <section className="processing-profile-picker" aria-label="资料处理方案">
      <header><div><small>PERSONAL PROCESSING POLICY</small><strong>先选整理方式，再扫描资料</strong><p>前三种方案使用预设规则；自定义方案会先完成 AI 速判和摘要，再由你确认哪些内容进入知识库。</p></div><span>{profiles?.current?.label ?? "自定义方案"}</span></header>
      <div className="processing-profile-grid">
        {(profiles?.profiles ?? []).map((item: Data) => <button key={item.profile_id} type="button" className={profileId === item.profile_id ? "active" : ""} disabled={profileBusy || !!active} onClick={() => void chooseProfile(item.profile_id)}>
          <span><strong>{item.label}</strong><small>{item.short_label}</small></span>
          <p>{item.description}</p>
          <ul>{(item.highlights ?? []).map((highlight: string) => <li key={highlight}>{highlight}</li>)}</ul>
        </button>)}
      </div>
      <p className="processing-profile-note">选择方案本身不会读取正文。自定义方案的下一步是 AI 速判：先说明文件用途、分类和建议处理深度，再由你确认，随后才生成 MD、全文索引或向量。</p>
    </section>
    <p>输入一个盘或总目录，系统自动列出第一层。先勾选本次需要的目录或文件，再由 Everything 递归扫描所选范围。</p>
    <label className="intake-path">从哪里选择资料<input aria-label="接入目录" value={path} placeholder="例如 E:\资料 或 G:\项目" disabled={!!active} onChange={e => { setPath(e.target.value); setScope(null); setSelected([]); setJob(null); setConfirmed(false); setVectorConfirmed(false); }} /></label>
    {browsing && <p role="status">正在读取第一层目录…</p>}
    {scope && <div className="scope-picker">
      <header><div><strong>选择本次接入范围</strong><small>{scope.root} · 只读取了第一层，尚未扫描里面的内容</small></div><b>{selected.length} / {scope.selectable_count} 已选</b></header>
      <div className="scope-picker-tools"><input aria-label="筛选第一层目录" value={scopeFilter} onChange={e => setScopeFilter(e.target.value)} placeholder="筛选目录或文件名"/><button onClick={() => setSelected(selectable.map((item: Data) => item.name))}>全选</button><button onClick={() => setSelected([])}>清空</button></div>
      <div className="scope-choice-list">{visibleScope.map((item: Data) => <label key={item.name} className={`${selected.includes(item.name) ? "selected" : ""} ${item.selectable ? "" : "disabled"}`}><input type="checkbox" disabled={!item.selectable || !!active} checked={selected.includes(item.name)} onChange={() => setSelected(current => current.includes(item.name) ? current.filter(name => name !== item.name) : [...current, item.name])}/><span className="scope-kind">{item.kind === "directory" ? "DIR" : "FILE"}</span><span><strong>{item.name}</strong><small>{item.selectable ? (item.kind === "directory" ? "勾选后递归扫描" : bytes(item.bytes ?? 0)) : item.reason}</small></span></label>)}</div>
    </div>}
    <details><summary>扫描设置与处理边界</summary><label className="intake-path">文件发现方式<select aria-label="文件发现方式" value={scanner} disabled={!!active} onChange={e => { setScanner(e.target.value); setJob(null); setConfirmed(false); }}><option value="everything">Everything 免费标准版 · 仅扫描已勾选范围</option><option value="native">普通目录扫描</option></select></label><p>{component?.backend_outdated && "新版后端尚未加载或不可达，请重启本地知识库服务后刷新；暂不允许提交。"} Everything {component ? (component.available ? `${component.version} · 文件校验通过` : "组件不可用，请修复或手动选择普通扫描") : "检查组件中…"}。不安装后台服务，不添加开机启动。</p><label className="intake-path">额外排除目录名（逗号分隔）<input aria-label="额外排除目录" value={exclude} placeholder="例如 archives, 临时文件" disabled={!!active} onChange={e => { setExclude(e.target.value); setJob(null); setConfirmed(false); }} /></label><label className="intake-path">恢复副本位置（可选）<input aria-label="恢复副本位置" value={recoveryRoot} placeholder="留空使用知识库所在盘；例如 I:\PKAS-Recovery" disabled={!!active} onChange={e => { setRecoveryRoot(e.target.value); setJob(null); setConfirmed(false); }} /></label><p>恢复副本只会在你最终确认接入时创建；留空保持原位置。填写备用盘可解决主库所在盘空间不足，不移动原件或主知识库。</p><p>默认排除系统目录、依赖、构建产物、密钥文件和链接。本批最多 2,000 个文件；超过会明确要求减少勾选范围，不会偷偷截断。L1只抽取部分原文；L2进入全文搜索；L3在L2基础上调用Embedding生成向量并需要单独确认。</p></details>
    <div className="foundation-pager"><button disabled={busy || !!active || !path.trim() || !selected.length || !component || component.backend_outdated || (scanner === "everything" && !component?.available)} onClick={() => perform(async () => { const r = await post<Data>("/api/foundation/intake/preview", { path: path.trim(), selected_entries: selected, rules, scanner, exclusions: exclude.split(/[,，]/).map(s => s.trim()).filter(Boolean), recovery_root: recoveryRoot.trim() || null }); setJob(r.data); setRules(r.data.request.rules); setOffset(0); setConfirmed(false); })}>{busy ? "正在提交…" : `扫描并分类已选 ${selected.length} 项`}</button><button disabled={busy} onClick={() => setVersion(v => v + 1)}>刷新任务</button></div>
    {error && <p role="alert">{error}</p>}
    {job && <div className="intake-result" aria-live="polite">
      <h3>{states[job.state] || job.state}</h3><small>{job.root} · 实际扫描器：{job.scanner || "native"}</small>
      {job.scan_complete && <><h3>系统分类结果</h3><p>{job.classification_basis || "文件类型规则分类"}。当前只是初步分类；自定义方案还会在此基础上进行 AI 速判和摘要，之后再确认入库范围。</p><div className="intake-rules">{Object.entries(groups).map(([key,label]) => <label key={key}>{label} · {job.categories?.[key]?.count ?? 0} 个 / {bytes(job.categories?.[key]?.bytes ?? 0)}<select aria-label={`${label}处理方式`} value={rules[key]} disabled={busy || job.state !== "ready"} onChange={e => {setRules({...rules,[key]:e.target.value});setProfileId("custom");setConfirmed(false);}}>{Object.entries(modes).map(([mode,name]) => <option key={mode} value={mode}>{name}</option>)}</select></label>)}</div>{job.state === "ready" && <button disabled={busy} onClick={() => perform(async () => {await persistCustomRules();const r=await post<Data>(`/api/foundation/intake-policy/${job.id}`,{rules});setJob(r.data);setConfirmed(false);})}>保存自定义规则并更新预览</button>}</>}
      <p>{job.total ?? 0} 个文件 · 原件合计 {bytes(job.source_bytes ?? 0)} · 原件复制 0 B</p>
      <p>若写入正文/MD，数据库恢复点约 {bytes(job.recovery_bytes_estimate ?? 0)}（额外空间，不是索引大小）。新批成功且恢复点校验后，轮换本功能的旧成功批次备份；异常批次保留。</p>
      {job.recovery_capacity && <p className={job.recovery_capacity.ready ? "" : "intake-capacity-warning"}>{job.recovery_capacity.ready ? `恢复空间已就绪：${job.recovery_capacity.recovery_root} 可用 ${bytes(job.recovery_capacity.available_free_bytes ?? 0)} / 本批至少需要 ${bytes(job.recovery_capacity.required_free_bytes ?? 0)}。` : job.recovery_capacity.reason || `恢复空间不足：${job.recovery_capacity.recovery_root} 可用 ${bytes(job.recovery_capacity.available_free_bytes ?? 0)}，本批至少需要 ${bytes(job.recovery_capacity.required_free_bytes ?? 0)}，还差 ${bytes(job.recovery_capacity.shortfall_bytes ?? 0)}。确认按钮已禁用，原件和现有知识库不会被改动。`}</p>}
      {job.actions && <p>{Object.entries(job.actions).map(([key, count]) => `${modes[key] || (key === "map" ? "生成目录清单" : key)}：${count}`).join(" / ")}</p>}
      {job.semantic_preflight?.required_confirmation && <section className="intake-semantic-preflight" aria-label="L3向量化预检">
        <h4>L3 向量化预检</h4>
        <p>本批有 {job.semantic_preflight.pending_files} 个待处理文件，共 {bytes(job.semantic_preflight.source_bytes ?? 0)}。{job.semantic_preflight.cloud_called ? "本计划已记录过云端调用。" : "尚未调用 Embedding 服务，也没有产生本次费用。"}</p>
        <p>{job.semantic_preflight.remote_scope}；向量只保存在本机知识库。</p>
        <ol>{(job.semantic_preflight.execution_order ?? []).map((step: string) => <li key={step}>{step}</li>)}</ol>
        <p className="intake-outcome-summary">费用预估：{job.semantic_preflight.cost_estimate?.reason || "本机暂无可用估算。"}</p>
      </section>}
      {job.counts && <p>{Object.entries(job.counts).map(([key, count]) => `${states[key] || key} ${count}`).join(" · ")}</p>}
      {!!job.outcome_summary?.reason_counts?.length && <p className="intake-outcome-summary">未处理原因汇总：{job.outcome_summary.reason_counts.map((item: Data) => `${item.reason} ${item.count}`).join(" · ")}。单项详情仍可在下方列表查看。</p>}
      {job.error && <p role="alert">{job.error}</p>}
      {job.retention_warning && <p role="alert">{job.retention_warning}</p>}
      {!!job.superseded_recovery_bytes_released && <p>已释放本功能旧恢复点 {bytes(job.superseded_recovery_bytes_released)}；保留最新已校验恢复点。</p>}
      <p>按安全规则排除的目录/条目：{job.excluded_directories ?? 0}。</p>
      {job.map_source_id && <p>已生成并入库目录 MD（仅文件清单）。</p>}
      <div className="foundation-scroll"><table><thead><tr><th>文件</th><th>处理方式</th><th>状态 / 解析质量</th></tr></thead><tbody>{job.items?.map((i: Data) => <tr key={i.path}><td>{i.relative}<small>{bytes(i.bytes)}</small></td><td>{modes[i.action] || "目录 MD"}</td><td>{states[i.state]}<small>{i.reason || i.vector || ""}</small>{i.extraction_quality && <small>{qualityText(i.extraction_quality)}</small>}</td></tr>)}</tbody></table></div>
      <div className="foundation-pager"><button disabled={!offset} onClick={() => setOffset(n => Math.max(0, n - 100))}>上一页</button><span>第 {offset / 100 + 1} 页</span><button disabled={!job.has_more} onClick={() => setOffset(n => n + 100)}>下一页</button></div>
      {job.deferred_semantic_plan_id && <p>已拆出 {job.deferred_semantic_count} 项 L3资料，单独计划：{job.deferred_semantic_plan_id}。当前批只处理本地 L0/L1/L2；L3仍需你单独确认。</p>}
      {job.scan_complete && ["ready", "cancelled", "interrupted", "failed", "warning"].includes(job.state) && <><label className="intake-confirm"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />我确认按这份预览处理所选资料，并保存衍生文字、索引和恢复点。</label>{vectorRequested && <label className="intake-confirm"><input type="checkbox" checked={vectorConfirmed} onChange={e => setVectorConfirmed(e.target.checked)} />我确认L3资料可发送给已配置的Embedding服务并生成向量，可能产生费用。</label>}{job.state === "ready" && vectorRequested && !job.deferred_semantic_plan_id && <button disabled={!confirmed || busy || job.recovery_capacity?.ready === false || JSON.stringify(rules) !== JSON.stringify(job.request.rules)} onClick={() => perform(async () => { const r = await post<Data>(`/api/foundation/intake/${job.id}/split_l3`, {}); setJob(r.data); setConfirmed(false); setVectorConfirmed(false); })}>先拆出L3，只接入L0/L1/L2</button>}<button disabled={!confirmed || (vectorRequested && !vectorConfirmed) || busy || job.recovery_capacity?.ready === false || (job.state === "ready" && JSON.stringify(rules) !== JSON.stringify(job.request.rules))} onClick={() => perform(async () => { const r = await post<Data>(`/api/foundation/intake/${job.id}/confirm?confirmed=true&confirmed_vector=${vectorRequested && vectorConfirmed}`, {}); setJob(r.data); setConfirmed(false); setVectorConfirmed(false); })}>{job.state === "ready" ? (vectorRequested ? "确认切块并向量化" : "确认接入") : "继续未完成 / 重试失败项"}</button></>}
      {active && <button disabled={busy} onClick={() => perform(async () => { await post(`/api/foundation/intake/${job.id}/cancel`, {}); })}>取消（当前文件完成后停止）</button>}
      {job.vector_coverage && <p>向量覆盖：{job.vector_coverage.indexed}/{job.vector_coverage.eligible}，待处理 {job.vector_coverage.pending}。</p>}
      {job.state === "completed" && <p>本批处理结束。请分别使用A、B、AB模式检查文件、语义和混合结果。</p>}
    </div>}
    {!!recent.length && <details><summary>最近接入记录 · 可恢复未完成任务</summary>{recent.map(r => <button className="intake-history" key={r.id} disabled={!!active || busy} onClick={() => perform(async () => { const v = await api<Data>(`/api/foundation/intake/${r.id}`); setJob(v.data); setRules(v.data.request.rules); setPath(v.data.root); setSelected(v.data.request.selected_entries ?? []); setScanner(v.data.scanner || "native"); setRecoveryRoot(v.data.request.recovery_root || v.data.recovery_root || ""); setOffset(0); setConfirmed(false); })}>{states[r.state] || r.state} · {r.root} · {new Date(r.created_at).toLocaleString()}</button>)}</details>}
  </section>;
}
