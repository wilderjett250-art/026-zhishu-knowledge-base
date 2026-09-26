import { useEffect, useState, type CSSProperties } from "react";
import { api, ApiError, post, put } from "./api";

type Data = Record<string, any>;

const profileOrder = ["work_efficiency", "complete_personal", "lightweight", "custom"];
const ruleGroups = [
  { key: "markdown", label: "Markdown 笔记", help: "项目说明、笔记和知识卡片" },
  { key: "documents", label: "文档与表格", help: "Word、PDF、Excel 等正文资料" },
  { key: "code", label: "源码与脚本", help: "默认只登记项目位置" },
  { key: "images", label: "图片与截图", help: "默认只登记，不假装看懂像素" },
  { key: "other", label: "其他文件", help: "压缩包、安装包等按规则处理" },
];
const modeOptions = [
  ["catalog", "只登记位置"],
  ["extract", "建立本地摘要"],
  ["full", "读取正文，可关键词搜索"],
  ["semantic", "读取正文和语义搜索"],
  ["md_only", "只收现有 Markdown"],
  ["md_fallback", "优先 Markdown，缺失时建目录说明"],
  ["exclude", "不接入"],
] as const;
const reviewReasons: Record<string, string> = {
  source_changed_since_catalog: "索引后文件已变化，先更新资料地图",
  structure_parse_failed: "正文结构解析失败",
  no_text_sample: "未取得可用文字样本",
  boundary_or_sensitive_path: "路径边界或敏感范围已变化",
  missing: "文件已不存在",
  permission_denied: "没有读取权限",
  io_error: "本地读取异常",
};

function messageFrom(error: unknown, fallback: string) {
  if (error instanceof ApiError) return error.problem.message ?? error.message;
  return error instanceof Error ? error.message : fallback;
}

function ProgressRing({ label, value, detail, tone = "cyan" }: { label: string; value: number; detail: string; tone?: "cyan" | "blue" | "amber" }) {
  const percent = Math.max(0, Math.min(100, value));
  return <article className={`intake-progress-ring ${tone}`}>
    <div className="intake-ring" style={{ "--progress": `${percent}%` } as CSSProperties}><strong>{percent < 0.01 && percent > 0 ? "<0.01%" : `${percent.toFixed(percent < 1 ? 2 : 1)}%`}</strong></div>
    <div><small>{label}</small><p>{detail}</p></div>
  </article>;
}

export default function AutomatedIntake({ onNext, onLedger }: { onNext?: () => void; onLedger?: () => void }) {
  const [profileData, setProfileData] = useState<Data | null>(null);
  const [sourceData, setSourceData] = useState<Data | null>(null);
  const [catalog, setCatalog] = useState<Data | null>(null);
  const [coverage, setCoverage] = useState<Data | null>(null);
  const [preflight, setPreflight] = useState<Data | null>(null);
  const [preflightCategories, setPreflightCategories] = useState<Data | null>(null);
  const [reviewPage, setReviewPage] = useState<Data | null>(null);
  const [editing, setEditing] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const [customRules, setCustomRules] = useState<Record<string, string>>({});
  const [customExclusions, setCustomExclusions] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = async () => {
    setError("");
    const [profiles, sources, catalogResult, progress, localRead] = await Promise.all([
      api<Data>("/api/foundation/processing-profiles"),
      api<Data>("/api/foundation/auto-sources"),
      api<Data>("/api/foundation/machine-catalog").catch(() => ({ data: { state: "not_started" } } as Data)),
      api<Data>("/api/foundation/summary-jobs/catalog-progress").catch(() => ({ data: null } as Data)),
      api<Data>("/api/foundation/summary-jobs/catalog-preflight").catch(() => ({ data: null } as Data)),
    ]);
    setProfileData(profiles.data);
    setSourceData(sources.data);
    setCatalog(catalogResult.data);
    setCoverage(progress.data);
    setPreflight(localRead.data);
    setEditing(Boolean(profiles.data.first_run_required));
    const current = profiles.data.current ?? {};
    setCustomRules(current.rules ?? {});
    setCustomExclusions((current.exclusions ?? []).join(", "));
    setCustomOpen(current.profile_id === "custom" && Boolean(profiles.data.first_run_required));
  };

  useEffect(() => { void load().catch(exception => setError(messageFrom(exception, "读取自动接入状态失败"))); }, []);
  useEffect(() => {
    if (preflight?.running) return;
    void api<Data>("/api/foundation/summary-jobs/catalog-preflight/categories")
      .then(result => setPreflightCategories(result.data))
      .catch(() => undefined);
  }, [preflight?.running]);
  useEffect(() => {
    if (catalog?.state !== "running" && coverage?.scope_state !== "building" && !preflight?.running) return;
    const timer = window.setInterval(() => {
      Promise.all([
        api<Data>("/api/foundation/machine-catalog"),
        api<Data>("/api/foundation/summary-jobs/catalog-progress"),
        api<Data>("/api/foundation/summary-jobs/catalog-preflight"),
      ]).then(([catalogResult, progress, localRead]) => {
        setCatalog(catalogResult.data);
        setCoverage(progress.data);
        setPreflight(localRead.data);
      }).catch(() => undefined);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [catalog?.state, coverage?.scope_state, preflight?.running]);

  const current = profileData?.current;
  const scopes: Data[] = sourceData?.scopes ?? [];
  const state = String(catalog?.state ?? "not_started");
  const stateLabel: Record<string, string> = {
    not_started: "尚未建立资料地图",
    running: "正在建立资料地图",
    paused: "已暂停，可继续",
    completed: "资料地图已完成",
    warning: "部分范围需要复查",
  };

  const savePreset = async (profileId: string) => {
    if (profileId === "custom") {
      setCustomRules(current?.rules ?? customRules);
      setCustomExclusions((current?.exclusions ?? []).join(", "));
      setCustomOpen(true);
      return;
    }
    const selected = (profileData?.profiles ?? []).find((item: Data) => item.profile_id === profileId);
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      const saved = await put<Data>("/api/foundation/processing-profile", { profile_id: profileId, rules: null, exclusions: [] });
      setProfileData(previous => previous ? { ...previous, current: saved.data, first_run_required: false, profiles: (previous.profiles ?? []).map((item: Data) => ({ ...item, selected: item.profile_id === profileId })) } : previous);
      setEditing(false);
      setCustomOpen(false);
    } catch (exception) { setError(messageFrom(exception, "保存整理方案失败")); }
    finally { setBusy(false); }
  };

  const saveCustom = async () => {
    setBusy(true);
    setError("");
    try {
      const saved = await put<Data>("/api/foundation/processing-profile", {
        profile_id: "custom",
        rules: customRules,
        exclusions: customExclusions.split(/[,，\n]/).map(item => item.trim()).filter(Boolean),
      });
      setProfileData(previous => previous ? { ...previous, current: saved.data, first_run_required: false, profiles: (previous.profiles ?? []).map((item: Data) => ({ ...item, selected: item.profile_id === "custom" })) } : previous);
      setEditing(false);
      setCustomOpen(false);
    } catch (exception) { setError(messageFrom(exception, "保存自定义方案失败")); }
    finally { setBusy(false); }
  };

  const startAutomaticCatalog = async () => {
    setBusy(true);
    setError("");
    try {
      const result = catalog?.id && ["paused", "warning"].includes(state)
        ? await post<Data>(`/api/foundation/machine-catalog/${catalog.id}/resume`, {})
        : await post<Data>("/api/foundation/machine-catalog/start", { confirmed: true });
      setCatalog(result.data);
    } catch (exception) { setError(messageFrom(exception, "自动接入启动失败")); }
    finally { setBusy(false); }
  };

  const startRecommendedFirstRun = async () => {
    setBusy(true);
    setError("");
    try {
      const saved = await put<Data>("/api/foundation/processing-profile", { profile_id: "work_efficiency", rules: null, exclusions: [] });
      setProfileData(previous => previous ? { ...previous, current: saved.data, first_run_required: false } : previous);
      setEditing(false);
      const result = await post<Data>("/api/foundation/machine-catalog/start", { confirmed: true });
      setCatalog(result.data);
    } catch (exception) { setError(messageFrom(exception, "一键建立资料地图失败")); }
    finally { setBusy(false); }
  };

  const openManualRange = () => {
    const url = new URL(window.location.href);
    url.searchParams.set("page", "import");
    url.searchParams.delete("foundationTab");
    window.location.assign(url.toString());
  };
  const scopeText = scopes.map((item: Data) => item.path).join("、");
  const aiFailed = Number(coverage?.ai_failed ?? 0);
  const reviewPending = Number(coverage?.review_pending ?? 0);

  const toggleLocalRead = async () => {
    setBusy(true);
    setError("");
    try {
      const action = preflight?.running ? "pause" : "start";
      const result = await post<Data>(`/api/foundation/summary-jobs/catalog-preflight/${action}`, {});
      setPreflight(result.data);
      const progress = await api<Data>("/api/foundation/summary-jobs/catalog-progress");
      setCoverage(progress.data);
    } catch (exception) { setError(messageFrom(exception, "本地逐文件轻读失败")); }
    finally { setBusy(false); }
  };
  const loadReviewPage = async (offset = 0) => {
    try {
      const result = await api<Data>(`/api/foundation/summary-jobs/catalog-preflight/reviews?offset=${offset}&limit=20`);
      setReviewPage(result.data);
    } catch (exception) { setError(messageFrom(exception, "读取待复查明细失败")); }
  };

  return <section className="foundation-panel automated-intake-panel">
    <header className="intake-page-head">
      <div><small>资料整理方案</small><h2>决定哪些资料值得被读懂</h2><p>这里先配置“范围”和“处理深度”。建立资料地图只登记文件信息；真实 AI 整理要到下一步显示范围、模型和确认后才会执行。</p></div>
      <span className={`catalog-state ${state}`}>{stateLabel[state] ?? state}</span>
    </header>

    <section className="intake-simple-guide">
      <article><i>1</i><div><strong>系统给出默认范围</strong><span>固定盘和常用资料夹优先，系统与缓存目录自动排除。</span></div></article>
      <article><i>2</i><div><strong>你选择处理深度</strong><span>四种预设；自定义方案可以直接修改每类文件的规则。</span></div></article>
      <article><i>3</i><div><strong>AI 分批理解重要内容</strong><span>不会把全盘所有文件都深读，也不会在此页面调用模型。</span></div></article>
    </section>

    {editing ? <section className="first-run-profile">
      <div className="automated-section-heading"><div><small>整理方式</small><strong>{profileData?.first_run_required ? "先选一个你想要的整理方式" : "修改资料整理方式"}</strong><p>前三种是可直接使用的预设；“自定义方案”会展开实际可编辑的规则，不会只给你一个模糊按钮。</p></div><span>随时可改，不会自动扫描</span></div>
      <div className="processing-profile-grid">
        {profileOrder.map(profileId => {
          const item = (profileData?.profiles ?? []).find((value: Data) => value.profile_id === profileId);
          if (!item) return null;
          const selected = current?.profile_id === profileId;
          return <button key={profileId} type="button" className={selected ? "active" : ""} disabled={busy} onClick={() => void savePreset(profileId)}>
            <span><strong>{item.label}</strong><small>{item.short_label}</small></span><p>{item.description}</p><ul>{(item.highlights ?? []).map((highlight: string) => <li key={highlight}>{highlight}</li>)}</ul>
          </button>;
        })}
      </div>
      {customOpen && <section className="custom-profile-editor">
        <header><div><small>自定义方案就在这里修改</small><h3>不同类型文件怎么处理？</h3><p>选项会保存到这台电脑；它们只决定后续确认后的处理方式，不会立即扫描或调用模型。</p></div><button type="button" onClick={() => setCustomOpen(false)} disabled={busy}>收起</button></header>
        <div className="custom-rule-grid">{ruleGroups.map(group => <label key={group.key}><span><strong>{group.label}</strong><small>{group.help}</small></span><select value={customRules[group.key] ?? "catalog"} onChange={event => setCustomRules(previous => ({ ...previous, [group.key]: event.target.value }))}>{modeOptions.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>)}</div>
        <label className="custom-exclusion"><span><strong>额外排除名称</strong><small>用逗号分隔，例如 archives，临时文件；只影响后续范围。</small></span><input value={customExclusions} onChange={event => setCustomExclusions(event.target.value)} placeholder="可选：archives, 临时文件" /></label>
        <footer><button type="button" onClick={() => void saveCustom()} disabled={busy}>{busy ? "正在保存…" : "保存自定义方案"}</button><span>需要通过 AI 评估下一批时，请在“AI 整理”页明确选择模型和范围。</span></footer>
      </section>}
    </section> : <section className="current-profile-bar redesigned">
      <div><small>当前整理方案</small><strong>{current?.label ?? "尚未选择"}</strong><span>{current?.description ?? "请先选择一套资料整理方式。"}</span><em>{current?.profile_id === "custom" ? "自定义规则已保存；点击右侧即可修改。" : "想调整每类文件的处理方式，可切换到自定义方案。"}</em></div>
      <div><button type="button" disabled={busy || state === "running"} onClick={() => { setEditing(true); setCustomOpen(current?.profile_id === "custom"); }}>修改方案</button><button type="button" className="quiet" onClick={openManualRange}>手动选范围</button></div>
    </section>}

    <section className="automated-source-card redesigned">
      <div className="automated-section-heading"><div><small>自动范围</small><strong>系统会先整理这些位置</strong><p>{sourceData?.policy ?? "C盘保守处理；其他固定盘建立资料地图；系统、缓存和敏感目录自动排除。"}</p></div><b>{scopes.length} 个范围</b></div>
      <div className="scope-visual-grid">{scopes.map((item: Data) => <article key={item.path}><i>{item.kind === "fixed_data_drive" ? "盘" : "夹"}</i><strong>{item.path}</strong><span>{item.kind === "fixed_data_drive" ? "建立目录索引" : "常用资料夹"}</span></article>)}</div>
      <footer><span>想只整理某个项目、移动盘或特定文件夹？</span><button type="button" onClick={openManualRange}>前往“资料导入”手动选择</button></footer>
      <details><summary>查看完整自动范围</summary><p>{scopeText || "正在读取自动范围…"}</p></details>
    </section>

    {coverage && <section className="intake-progress-board" aria-label="资料整理进度">
      <header><div><small>当前进度</small><h3>资料地图、内容理解、知识库，分别看</h3><p>这三个数字不是一回事：文件地图可覆盖全部文件；AI 只看需要理解的内容；进入知识库需要另行确认。</p></div><span>不会自动深读全盘</span></header>
      {coverage.scope_state === "building" ? <p role="status">正在按当前方案计算需要轻读的文件范围。A库目录索引不会被当作已理解内容。</p> : coverage.scope_state === "error" ? <p role="alert">候选范围计算失败（{coverage.scope_error}）。已保留之前的台账。<button type="button" disabled={busy} onClick={() => { setBusy(true); void post<Data>("/api/foundation/summary-jobs/catalog-progress/retry", {}).then(result => setCoverage(result.data)).catch(exception => setError(messageFrom(exception, "重新计算失败"))).finally(() => setBusy(false)); }}>重新计算</button></p> : <div className="intake-progress-grid">
        <ProgressRing label="文件处置结论" value={Number(coverage.decision_coverage_percent ?? 0)} detail="按规则保留L0 + AI已完成；L0不代表已读正文。" />
        <ProgressRing label="逐文件本地轻读" value={Number(coverage.local_read_coverage_percent ?? 0)} detail={`${Number(coverage.local_read_done ?? 0).toLocaleString()} / ${Number(coverage.ai_candidates_total ?? 0).toLocaleString()} 个候选已核对文件内容或读取状态。`} tone="amber" />
        <ProgressRing label="AI 内容理解" value={Number(coverage.ai_coverage_percent ?? 0)} detail={`${Number(coverage.ai_done ?? 0).toLocaleString()} / ${Number(coverage.ai_eligible ?? 0).toLocaleString()} 个需要理解的文件。`} tone="blue" />
        <article className="intake-progress-queue"><small>下一步候选</small><strong>{Number(coverage.ai_pending ?? 0).toLocaleString()}</strong><span>尚无可信 AI 结论</span><p>其中 {reviewPending.toLocaleString()} 个正文未抽出或读取异常，须单独复查；其余可按批交给 AI。入库仍须预览确认。</p>{reviewPending > 0 && <button type="button" onClick={() => reviewPage ? setReviewPage(null) : void loadReviewPage()}>查看本地待复查明细</button>}{aiFailed > 0 && <button type="button" onClick={onLedger}>查看 AI 失败任务</button>}</article>
      </div>}
      {reviewPage && <section className="intake-preflight-reviews"><h4>本地轻读待复查 · {Number(reviewPage.total ?? 0).toLocaleString()} 个</h4><p>这里只列本地逐文件轻读产生的异常；其他 AI 整理任务的失败请到任务页查看。</p><div className="intake-review-reasons">{(reviewPage.reasons ?? []).map((item: Data) => <span key={item.reason}>{reviewReasons[item.reason] ?? item.reason}：{Number(item.total).toLocaleString()}</span>)}</div><ul>{(reviewPage.items ?? []).map((item: Data) => <li key={`${item.path}:${item.inspected_at}`}><code>{item.path}</code><span>{reviewReasons[item.reason] ?? item.reason}</span></li>)}</ul><footer><button type="button" disabled={Number(reviewPage.offset ?? 0) === 0} onClick={() => void loadReviewPage(Math.max(0, Number(reviewPage.offset) - 20))}>上一页</button><span>{Number(reviewPage.offset ?? 0) + 1}–{Math.min(Number(reviewPage.total ?? 0), Number(reviewPage.offset ?? 0) + Number(reviewPage.items?.length ?? 0))} / {Number(reviewPage.total ?? 0)}</span><button type="button" disabled={Number(reviewPage.offset ?? 0) + Number(reviewPage.items?.length ?? 0) >= Number(reviewPage.total ?? 0)} onClick={() => void loadReviewPage(Number(reviewPage.offset) + 20)}>下一页</button></footer></section>}
      {coverage.scope_state === "ready" && <div className="intake-preflight-actions"><button type="button" disabled={busy || state === "running"} onClick={() => void toggleLocalRead()}>{preflight?.running ? "暂停本地逐文件轻读" : "继续本地逐文件轻读"}</button><span>只读原件，保存轻量分类依据；不会调用模型、生成大批 MD 或自动入库。{preflight?.state === "low_disk" ? "磁盘空间不足，已安全暂停。" : preflight?.state === "error" ? `发生 ${preflight.error_kind}，可检查后重试。` : preflight?.running ? `本轮已处理 ${Number(preflight.processed_this_run ?? 0).toLocaleString()} 个。` : "可随时暂停并从台账续跑。"}</span></div>}
      {Number(preflightCategories?.total ?? 0) > 0 && <details className="intake-preflight-categories"><summary>看本地暂定分类 · {Number(preflightCategories?.total ?? 0).toLocaleString()} 个已抽样文件</summary><p>这是本地关键词初判，不是 Luna 复核结果；未读到正文的文件不会被猜进某一类，也不能从这里直接入库。</p><div>{(preflightCategories?.categories ?? []).slice(0, 12).map((item: Data) => <article key={item.id}><span>{item.parent} / {item.name}</span><strong>{Number(item.count).toLocaleString()}</strong><i style={{ width: `${Math.max(2, Number(item.count) / Number(preflightCategories?.total ?? 1) * 100)}%` }} /></article>)}</div></details>}
      {Number(preflightCategories?.stale_total ?? 0) > 0 && <p className="intake-preflight-stale" role="status">另有 {Number(preflightCategories?.stale_total).toLocaleString()} 个文件仍沿用旧分类规则；继续本地轻读会按当前规则重新判断，不会把旧建议混入当前统计。</p>}
      <details><summary>为什么“文件处置结论”和“AI 内容理解”不是同一个百分比？</summary><p>系统临时文件、缓存、构建输出等会按本地规则明确跳过，这也算完成了处置判断；而 AI 内容理解只统计真正需要判断用途、分类和摘要的文件。两者分开显示，才不会把“跳过”误说成“读懂”。</p></details>
    </section>}

    <section className="intake-ai-guidance"><div><small>AI 整理助手</small><h3>想让 AI 帮你决定下一批？</h3><p>这里不会放一个没有实际能力的聊天框。点击后会进入真实的 AI 整理工作台：在那里选择范围、模型和是否允许处理，再生成分类和摘要预览。</p></div><button type="button" onClick={onNext}>打开 AI 整理工作台</button></section>

    <div className="automated-actions">
      {profileData?.first_run_required ? <button className="primary" disabled={busy || state === "running" || !sourceData || scopes.length === 0} onClick={() => void startRecommendedFirstRun()}>{busy ? "正在准备…" : "一键建立资料地图 · 推荐方案"}</button> : <button className="primary" disabled={busy || state === "running" || !current} onClick={() => void startAutomaticCatalog()}>{state === "not_started" ? "建立本机资料地图" : state === "paused" || state === "warning" ? "继续未完成索引" : state === "completed" ? "重新核对资料地图" : "建立本机资料地图"}</button>}
      <span>{profileData?.first_run_required ? "会保存“工作提效型”，并只建立上方范围的目录索引。" : "原文件留在原处；先更新文件地图，再按当前方案处理重要内容。"}</span>
    </div>
    {catalog && <div className="automated-progress"><strong>{catalog.message ?? stateLabel[state]}</strong><span>已发现 {Number(catalog.catalog_files ?? catalog.files_seen ?? 0).toLocaleString()} 个文件 · 已处理目录 {Number(catalog.directories_seen ?? 0).toLocaleString()} 个 · 已排除 {Number(catalog.excluded ?? 0).toLocaleString()} 个</span></div>}
    {["completed", "warning"].includes(state) && <div className="automated-next-step"><div><strong>{state === "completed" ? "资料地图已建立，下一步让 AI 分批理解" : "已有资料地图可用，少量范围仍可复查"}</strong><span>进入 AI 整理页后，系统会先显示候选范围和成本边界，再允许开始实际处理。</span></div><button type="button" disabled={!onNext} onClick={onNext}>继续到 AI 整理 <span aria-hidden="true">→</span></button></div>}
    {error && <p className="intake-error" role="alert">{error}</p>}
  </section>;
}
