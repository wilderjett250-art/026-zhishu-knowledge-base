import { useEffect, useMemo, useState } from "react";
import { api, post } from "./api";

type Data = Record<string, any>;

const modes: Record<string, string> = {
  catalog: "L0 · 仅保留位置与分类",
  extract: "L1 · 本地摘录，保留来源与哈希",
  full: "L2 · 解析正文，支持全文搜索",
  semantic: "L3 · 全文搜索＋向量语义搜索",
  recommended: "按 Luna 的逐文件建议混合处理",
};

export default function ProjectMapPanel() {
  const [plan, setPlan] = useState<Data | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [mode, setMode] = useState("full");
  const [allowRemote, setAllowRemote] = useState(false);
  const [preview, setPreview] = useState<Data | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = async () => {
    const result = await api<Data | null>("/api/foundation/project-map/latest");
    setPlan(result.data || null);
    setSelected(current => current.filter(id => result.data?.units?.some((unit: Data) => unit.id === id && unit.state === "done")));
  };
  useEffect(() => { load().catch(error => setError(error.message)); }, []);
  const perform = async (action: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await action(); } catch (error) { setError((error as Error).message); } finally { setBusy(false); }
  };
  const completed = useMemo(() => (plan?.units || []).filter((unit: Data) => unit.state === "done"), [plan]);
  const historic = useMemo(() => [
    ...(plan?.units || []).filter((unit: Data) => unit.state === "stale" && unit.overview),
    ...(plan?.superseded_cards || []).map((entry: Data) => entry.unit).filter((unit: Data) => unit?.overview),
  ], [plan]);
  const toggle = (id: string, checked: boolean) => setSelected(current => checked ? [...new Set([...current, id])] : current.filter(value => value !== id));
  const vectorUsed = mode === "semantic" || (mode === "recommended" && Number(preview?.actions?.semantic || 0) > 0);
  return <section className="project-map-panel">
    <header>
      <div><small>项目资料</small><h3>按项目选择资料入库</h3><p>项目卡仅复用已通过证据校验的文件级理解，不重读代码、文档或聊天原件。它帮你按项目选择资料，再生成可撤回检查的入库预览。</p></div>
      {!plan ? <button disabled={busy} onClick={() => perform(async () => { const result = await post<Data>("/api/foundation/project-map", {}); setPlan(result.data); setSelected([]); setPreview(null); setNotice("已生成候选项目清单，尚未调用 Luna 或写入知识库。"); })}>生成候选项目</button> : <button disabled={busy} onClick={() => perform(async () => { const result = await post<Data>(`/api/foundation/project-map/${plan.id}/refresh`, {}); setPlan(result.data); setSelected([]); setPreview(null); const summary = result.data.refresh_summary || {}; setNotice(`已增量核对：新增 ${summary.added || 0} 项，证据变化待复核 ${summary.changed || 0} 项。`); })}>刷新新增资料</button>}
    </header>
    {error && <p role="alert" className="project-map-error">{error}</p>}
    {notice && <p role="status" className="project-map-notice">{notice}</p>}
    {!plan && <p className="project-map-empty">尚未生成项目候选。候选只基于已经完成 Luna 理解且通过本地校验的资料。</p>}
    {plan && <>
      <div className="project-map-status"><span>候选 {plan.total_units || 0}</span><span>已完成 {plan.counts?.done || 0}</span><span>待处理 {plan.counts?.pending || 0}</span><span>已拒绝 {plan.counts?.rejected || 0}</span><small>{plan.remote_called ? "本次已调用 Luna 处理摘要档案" : "尚未调用 Luna"}</small></div>
      {Number(plan.stale_evidence_count || 0) > 0 && <p role="status" className="project-map-notice">有 {plan.stale_evidence_count} 张旧项目卡的资料版本尚未通过当前规则复核，暂不能选入知识库。点击“刷新新增资料”后，再按需重新生成项目卡；旧卡原始记录不会直接删除。</p>}
      {historic.length > 0 && <details className="project-map-history"><summary>查看旧项目卡（{historic.length} 张，只读留档）</summary><div>{historic.map((unit: Data, index: number) => <article key={`${unit.id}-${index}`}><strong>{unit.overview?.title || unit.top_group || "未命名项目"}</strong><small>旧证据版本 · 不能直接入库</small><p>{unit.overview?.what || "暂无内容说明"}</p></article>)}</div></details>}
      {Number(plan.counts?.pending || 0) > 0 && <div className="project-map-run"><label><input type="checkbox" checked={allowRemote} onChange={event => setAllowRemote(event.target.checked)} />允许 Luna 只处理已验证的摘要档案</label><button disabled={busy || !allowRemote} onClick={() => perform(async () => { const result = await post<Data>(`/api/foundation/project-map/${plan.id}/run`, {confirmed: true, allow_remote_processing: true, max_units: 20}); setPlan(result.data); setNotice("项目候选已处理；请审阅后再决定是否入库。"); })}>生成项目卡</button></div>}
      {completed.length > 0 && <div className="project-map-workbench"><div className="project-map-cards">{completed.map((unit: Data) => { const overview = unit.overview || {}; const categories = [...new Set((unit.materials || []).map((item: Data) => item.category).filter(Boolean))]; return <article key={unit.id} className={selected.includes(unit.id) ? "selected" : ""}><label className="project-map-card-head"><input type="checkbox" checked={selected.includes(unit.id)} onChange={event => toggle(unit.id, event.target.checked)} /><span><strong>{overview.title || "未命名项目"}</strong><small>{overview.project_type || "资料集合"} · {unit.evidence_file_count || 0} 份证据资料 · {overview.confidence === "medium" ? "证据较充分" : "证据有限"}</small></span></label><p><b>做什么：</b>{overview.what || "暂不明确"}</p><p><b>为什么：</b>{overview.why || "暂不明确"}</p><p><b>如何组织：</b>{overview.how || "暂不明确"}</p><footer><span>{categories.join(" · ") || "未分类"}</span><em>{overview.uncertainty || "需人工复核"}</em></footer></article>; })}</div>
        <aside className="project-map-actions"><label>处理深度<select value={mode} onChange={event => { setMode(event.target.value); setPreview(null); }}><>{Object.entries(modes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</></select></label><div><span>已选项目</span><strong>{selected.length}</strong><small>只会选中这些项目卡引用的资料，不会扩展到全盘同类文件。</small></div><p>生成预览时不读原件；真正确认前会重新校验每个原文件的大小和修改时间。</p><button disabled={busy || selected.length === 0} onClick={() => perform(async () => { const result = await post<Data>(`/api/foundation/project-map/${plan.id}/promote-preview`, {unit_ids: selected, mode}); setPreview(result.data); setNotice("项目资料入库预览已生成，尚未写入知识库。"); })}>生成入库预览</button>{preview && <section className="project-map-preview"><strong>执行前预览</strong><p>{preview.total} 份资料；可处理 {preview.counts?.pending || 0}，跳过 {preview.counts?.skipped || 0}。</p>{mode === "recommended" && <p>L0 {preview.actions?.catalog || 0} · L1 {preview.actions?.extract || 0} · L2 {preview.actions?.full || 0} · L3 {preview.actions?.semantic || 0}</p>}<button disabled={busy || preview.state !== "ready"} onClick={() => perform(async () => { const result = await post<Data>(`/api/foundation/intake/${preview.id}/confirm?confirmed=true&confirmed_vector=${vectorUsed}`, {}); setPreview(result.data); setNotice(vectorUsed ? "已确认执行，包含向量处理。" : "已确认执行。" ); })}>{vectorUsed ? "确认加入知识库（含向量）" : "确认加入知识库"}</button></section>}</aside></div>}
    </>}
  </section>;
}
