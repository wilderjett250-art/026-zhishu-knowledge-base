import { useEffect, useState } from "react";
import { api, post } from "./api";
import ProjectMapPanel from "./ProjectMapPanel";
import SummaryJobsPanel from "./SummaryJobsPanel";
type Data = Record<string, any>;
type Category = {id: string; name: string; parent_id: string | null; keywords: string[]};

export default function DirectorySummaryPlanner() {
  const [status, setStatus] = useState<Data | null>(null);
  const [path, setPath] = useState("");
  const [plan, setPlan] = useState<Data | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [categories, setCategories] = useState<Category[]>([]);
  const [revision, setRevision] = useState("");
  const [customized, setCustomized] = useState(false);
  const [systemCategoryCount, setSystemCategoryCount] = useState(0);
  const [parent, setParent] = useState("");
  const [name, setName] = useState("");
  const [keywords, setKeywords] = useState("");
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(0);
  const [historyId, setHistoryId] = useState("");
  const [dirty, setDirty] = useState(false);
  const [selectedCategory, setSelectedCategory] = useState("");
  const [categoryQuery, setCategoryQuery] = useState("");
  const [expandedParents, setExpandedParents] = useState<Set<string>>(new Set());
  const load = async () => {
    const [a, b] = await Promise.all([api<Data>("/api/foundation/directory-summaries"), api<Data>("/api/foundation/content-categories")]);
    const loaded: Category[] = b.data.categories;
    setStatus(a.data); setCategories(loaded); setRevision(b.data.revision); setCustomized(Boolean(b.data.customized)); setSystemCategoryCount(b.data.system_category_count ?? 0); setDirty(false);
    setSelectedCategory(current => loaded.some(c => c.id === current) ? current : loaded[0]?.id || "");
    setExpandedParents(new Set(loaded.filter(c => !c.parent_id).map(c => c.id)));
  };
  useEffect(() => { load().catch(e => setError(e.message)); }, []);
  const perform = async (fn: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const parents = categories.filter(c => !c.parent_id);
  const children = categories.filter(c => c.parent_id);
  const records: Data[] = plan?.units.flatMap((u: Data) => u.inspected || []) || [];
  const filtered = records.filter(r => !filter || r.classification?.category_id === filter);
  const label = (c: Category) => `${parents.find(p => p.id === c.parent_id)?.name} / ${c.name}`;
  const selected = categories.find(c => c.id === selectedCategory) || null;
  const normalizedQuery = categoryQuery.trim().toLocaleLowerCase();
  const visibleParents = parents.filter(group => !normalizedQuery || group.name.toLocaleLowerCase().includes(normalizedQuery) || children.some(child => child.parent_id === group.id && (child.name + " " + child.keywords.join(" ")).toLocaleLowerCase().includes(normalizedQuery)));
  const updateCategory = (id: string, values: Partial<Category>) => { setCategories(categories.map(c => c.id === id ? {...c, ...values} : c)); setDirty(true); };
  const toggleParent = (id: string) => setExpandedParents(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  return <section className="foundation-panel directory-summary-panel">
    <h2>整机资料整理 · 首轮检查与分类</h2>
    <p>先轻读每个文件，再按内容用途分成两级分类。分类显示正文依据；信息不足或类别冲突的文件留在“待判断”。分类标签不会移动原文件。</p>
    <p>C 盘范围：{status?.c_policy || "正在读取"}。其他盘按用户所选范围整理。</p>
    {error && <p role="alert">{error} <button disabled={busy} onClick={() => perform(load)}>重新加载</button></p>}
    {notice && <p role="status">{notice}</p>}
    <SummaryJobsPanel categories={categories} />
    <section className="taxonomy-editor">
      <header className="taxonomy-editor-head">
        <div><small>分类管理</small><h3>知识分类</h3><p>按一级分类、二级分类和规则详情逐层查看；一次只编辑一个节点。</p></div>
        <div className="taxonomy-head-actions"><span className={dirty ? "dirty" : ""}>{dirty ? "有未保存修改" : customized ? "客户自定义版本" : "系统基准版本"}</span><button disabled={busy || !revision || !dirty} onClick={() => perform(async () => { const result = await post<Data>("/api/foundation/content-categories", {expected_revision: revision, categories: categories.map(c => ({...c, keywords: c.keywords.map(x => x.trim()).filter(Boolean)}))}); setRevision(result.data.revision); setCategories(result.data.categories); setCustomized(Boolean(result.data.customized)); setSystemCategoryCount(result.data.system_category_count ?? systemCategoryCount); setDirty(false); setNotice("分类已保存，下次检查生效；系统基准仍独立保留。"); })}>保存分类</button></div>
      </header>
      <div className="taxonomy-registry">
        <aside className="taxonomy-tree-pane">
          <header><strong>分类目录</strong><span>{parents.length} 组 / {children.length} 类</span></header>
          <input aria-label="搜索知识分类" value={categoryQuery} onChange={e => setCategoryQuery(e.target.value)} placeholder="搜索分类或关键词" />
          <div className="taxonomy-tree" role="tree">
            {visibleParents.map(group => { const groupChildren = children.filter(c => c.parent_id === group.id && (!normalizedQuery || (c.name + " " + c.keywords.join(" ")).toLocaleLowerCase().includes(normalizedQuery))); const expanded = expandedParents.has(group.id) || !!normalizedQuery; return <div className="taxonomy-branch" key={group.id}>
              <div className={`taxonomy-node parent ${selectedCategory === group.id ? "active" : ""}`}><button className="tree-toggle" aria-label={`${expanded ? "折叠" : "展开"}${group.name}`} onClick={() => toggleParent(group.id)}>{expanded ? "−" : "+"}</button><button role="treeitem" onClick={() => setSelectedCategory(group.id)}><i>◇</i><span>{group.name}</span><b>{children.filter(c => c.parent_id === group.id).length}</b></button></div>
              {expanded && <div className="taxonomy-children">{groupChildren.map(child => <button role="treeitem" aria-selected={selectedCategory === child.id} className={`taxonomy-node child ${selectedCategory === child.id ? "active" : ""}`} key={child.id} onClick={() => setSelectedCategory(child.id)}><i>└</i><span>{child.name}</span></button>)}</div>}
            </div>})}
          </div>
        </aside>
        <main className="taxonomy-detail-pane">
          {selected ? <>
            <header><div><small>{selected.parent_id ? "二级分类" : "一级分类"}</small><h3>{selected.name}</h3></div><code>{selected.id}</code></header>
            <div className="taxonomy-breadcrumb"><span>全部分类</span><b>›</b>{selected.parent_id && <><span>{parents.find(item => item.id === selected.parent_id)?.name}</span><b>›</b></>}<strong>{selected.name}</strong></div>
            <label>显示名称<input aria-label={`分类名称 ${selected.id}`} value={selected.name} disabled={busy} onChange={e => updateCategory(selected.id, {name: e.target.value})} /></label>
            {selected.parent_id ? <label>内容识别关键词<textarea aria-label={`关键词 ${selected.id}`} value={selected.keywords.join("，")} disabled={busy} onChange={e => updateCategory(selected.id, {keywords: e.target.value.split(/[,，]/)})} placeholder="多个关键词用逗号分隔"/><small>关键词用于本地初筛；Luna会结合文字样本理解用途，不会只靠关键词硬判。</small></label> : <div className="taxonomy-child-overview"><strong>包含的二级分类</strong>{children.filter(c => c.parent_id === selected.id).map(child => <button key={child.id} onClick={() => setSelectedCategory(child.id)}><span>{child.name}</span><small>{child.keywords.length ? child.keywords.slice(0, 3).join(" · ") : "由文件类型和Luna语义判断"}</small><b>查看 →</b></button>)}</div>}
          </> : <div className="taxonomy-empty">从左侧选择一个分类节点。</div>}
        </main>
        <aside className="taxonomy-control-pane">
          <section><small>系统分类</small><strong>{customized ? "当前为自定义副本" : "当前为系统基准"}</strong><p>系统基准永久保留 {systemCategoryCount || 38} 个节点。用户修改不会覆盖参考版本。</p><button disabled={busy || !revision || (!customized && !dirty)} onClick={() => { if (!window.confirm("恢复系统基准分类？当前修改会先保存为上一版本，旧扫描记录不会改变。")) return; void perform(async () => {const result = await post<Data>("/api/foundation/content-categories/restore", {expected_revision: revision}); setRevision(result.data.revision); setCategories(result.data.categories); setCustomized(false); setDirty(false); setNotice("已恢复系统基准分类；修改前版本仍保留。");}) }}>恢复系统分类</button></section>
          <section><small>新增分类</small><strong>新增分类</strong><label>所属<select aria-label="新增分类所属" value={parent} onChange={e => setParent(e.target.value)}><option value="">新建一级分类</option>{parents.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label><label>名称<input aria-label="新增分类名称" value={name} onChange={e => setName(e.target.value)} /></label>{parent && <label>关键词<input aria-label="新增分类关键词" value={keywords} onChange={e => setKeywords(e.target.value)} placeholder="逗号分隔" /></label>}<button disabled={!name.trim() || busy} onClick={() => {const id="custom_" + crypto.randomUUID().replaceAll("-", ""); setCategories([...categories, {id, name: name.trim(), parent_id: parent || null, keywords: parent ? keywords.split(/[,，]/).map(x => x.trim()).filter(Boolean) : []}]); if(parent)setExpandedParents(current => new Set(current).add(parent)); setSelectedCategory(id); setName(""); setKeywords(""); setDirty(true);}}>添加节点</button></section>
        </aside>
      </div>
    </section>
    <details className="legacy-taxonomy-flat"><summary>高级：旧版批量编辑全部分类</summary>
      <div className="taxonomy-baseline"><div><strong>{customized ? "正在使用客户自定义分类" : "正在使用系统基准分类"}</strong><small>系统参考固定保留 {systemCategoryCount || 38} 项；扫描资料时自动使用当前副本分类，旧检查记录保留当时版本。</small></div><button disabled={busy || !revision || (!customized && !dirty)} onClick={() => { if (!window.confirm("恢复系统基准分类？当前修改会先保存为上一版本，旧扫描记录不会改变。")) return; void perform(async () => {const result = await post<Data>("/api/foundation/content-categories/restore", {expected_revision: revision}); setRevision(result.data.revision); setCategories(result.data.categories); setCustomized(false); setDirty(false); setNotice("已恢复系统基准分类；修改前版本仍保留。")}); }}>恢复系统分类</button></div>
      <p>可修改名称与关键词、增加一级或二级分类。关键词匹配抽样正文；保存后用于下次检查，系统基准不会被客户修改覆盖。</p>
      {parents.map(group => <div key={group.id} className="foundation-hit">
        <label>一级名称<input aria-label={`一级名称 ${group.id}`} value={group.name} disabled={busy} onChange={e => {setCategories(categories.map(c => c.id === group.id ? {...c, name: e.target.value} : c)); setDirty(true);}} /></label>
        {children.filter(c => c.parent_id === group.id).map(child => <div className="foundation-filters" key={child.id}>
          <label>二级名称<input aria-label={`二级名称 ${child.id}`} value={child.name} disabled={busy} onChange={e => {setCategories(categories.map(c => c.id === child.id ? {...c, name: e.target.value} : c)); setDirty(true);}} /></label>
          <label>内容关键词<input aria-label={`关键词 ${child.id}`} value={child.keywords.join(",")} disabled={busy} onChange={e => {setCategories(categories.map(c => c.id === child.id ? {...c, keywords: e.target.value.split(/[,，]/)} : c)); setDirty(true);}} /></label>
        </div>)}
      </div>)}
      <div className="foundation-filters">
        <label>所属<select aria-label="新增分类所属" value={parent} onChange={e => setParent(e.target.value)}><option value="">新建一级分类</option>{parents.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
        <label>名称<input aria-label="新增分类名称" value={name} onChange={e => setName(e.target.value)} /></label>
        <label>关键词<input aria-label="新增分类关键词" value={keywords} onChange={e => setKeywords(e.target.value)} /></label>
        <button disabled={!name.trim() || busy} onClick={() => {setCategories([...categories, {id: "custom_" + crypto.randomUUID().replaceAll("-", ""), name: name.trim(), parent_id: parent || null, keywords: parent ? keywords.split(/[,，]/).map(x => x.trim()).filter(Boolean) : []}]); setName(""); setKeywords(""); setDirty(true);}}>添加分类</button>
      </div>
      <button disabled={busy || !revision || !dirty} onClick={() => perform(async () => {
        const result = await post<Data>("/api/foundation/content-categories", {expected_revision: revision, categories: categories.map(c => ({...c, keywords: c.keywords.map(x => x.trim()).filter(Boolean)}))});
        setRevision(result.data.revision); setCategories(result.data.categories); setCustomized(Boolean(result.data.customized)); setSystemCategoryCount(result.data.system_category_count ?? systemCategoryCount); setDirty(false); setNotice("分类已保存，下次检查生效；系统基准仍独立保留。");
      })}>保存分类规则</button>
    </details>
    <ProjectMapPanel />
    <details><summary>旧版小范围检查记录</summary>
    <label className="intake-path">检查目录<input aria-label="目录摘要目录" value={path} disabled={busy} onChange={e => setPath(e.target.value)} placeholder="例如 E:\资料\某个项目" /></label>
    <p>会读取文件签名及有限正文/结构样本。PDF抽样前中后页面，Office抽样内部结构。模型语义总结尚未接通，本次使用本地内容规则。</p>
    <button disabled={busy || !status || dirty || !path.trim()} onClick={() => perform(async () => {const result = await post<Data>("/api/foundation/directory-summaries/preview", {path: path.trim()}); setPlan(result.data); setHistoryId(result.data.id); setPage(0); setFilter("");})}>{busy ? "正在处理…" : "逐文件轻读并分类"}</button>
    {dirty && <p>请先保存分类规则。</p>}
    <div className="foundation-filters"><label>恢复检查记录<input aria-label="检查记录编号" value={historyId} onChange={e => setHistoryId(e.target.value)} /></label><button disabled={busy || !/^[0-9a-f]{32}$/.test(historyId)} onClick={() => perform(async () => {setPlan((await api<Data>(`/api/foundation/directory-summaries/${historyId}`)).data); setPage(0); setFilter("");})}>读取记录</button></div>
    {plan && <div className="intake-result">
      <h3>逐文件结果</h3><p>记录编号：{plan.id}。已检查 {plan.inspected_files} / {plan.discovered_files}；格式冲突 {plan.type_mismatches}；排除 {plan.excluded_files ?? 0}。</p><p>{plan.notice}</p>
      <label>按二级分类筛选<select aria-label="分类筛选" value={filter} onChange={e => {setFilter(e.target.value); setPage(0);}}><option value="">全部文件</option>{children.map(c => <option key={c.id} value={c.id}>{label(c)} ({plan.classification_counts?.[c.id] || 0})</option>)}</select></label>
      <div className="foundation-scroll"><table><thead><tr><th>文件与检查范围</th><th>内容用途</th><th>分类依据</th><th>人工调整</th></tr></thead><tbody>
        {filtered.slice(page * 25, page * 25 + 25).map(r => <tr key={r.relative}>
          <td>{r.relative}<small>实际格式：{r.detected_type || "未知"} · {r.coverage === "partial" ? "正文抽样" : r.coverage === "failed" ? "读取失败" : "仅文件签名"}</small><small>{r.type_mismatch ? "格式冲突；" : ""}{r.notes?.join("；")}</small></td>
          <td>{r.classification?.label || "旧记录未分类"}<small>{r.classification?.review_status === "confirmed" ? "用户已确认" : "建议，未确认"}</small></td>
          <td>{r.classification?.basis === "user_choice" ? "用户选择" : r.classification?.evidence?.join("、") || "信息不足或多类冲突"}<details><summary>查看内容样本</summary><p style={{whiteSpace: "pre-wrap"}}>{r.text_preview || "尚无可用文字样本"}</p></details></td>
          <td><select aria-label={`调整分类 ${r.relative}`} value={r.classification?.category_id || "unresolved_other"} disabled={busy || dirty} onChange={e => perform(async () => {const updated = await post<Data>(`/api/foundation/directory-summaries/${plan.id}/classification`, {relative: r.relative, category_id: e.target.value, expected_revision: plan.revision}); setPlan(updated.data);})}>{children.map(c => <option key={c.id} value={c.id}>{label(c)}</option>)}</select></td>
        </tr>)}
      </tbody></table></div>
      <div className="foundation-pager"><button disabled={!page} onClick={() => setPage(page - 1)}>上一页</button><span>{filtered.length}个文件 · 第{page + 1}页</span><button disabled={(page + 1) * 25 >= filtered.length} onClick={() => setPage(page + 1)}>下一页</button></div>
      <p>这是旧版有限范围检查记录。请使用上方新整理任务生成MD。</p>
    </div>}
    </details>
  </section>;
}
