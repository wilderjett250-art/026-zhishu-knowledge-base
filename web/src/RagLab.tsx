import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, post } from "./api";
import FoundationSearch from "./FoundationSearch";

type Json = Record<string, any>;

const colors: Record<string, string> = {
  work: "#51d6d0",
  self: "#e0a54f",
  shared: "#a986e8",
  distill: "#75d48e",
};

export default function RagLab({ busy, setEvidence }: { busy: boolean; setEvidence: (item: Json) => void }) {
  const [status, setStatus] = useState<Json | null>(null);
  const [map, setMap] = useState<Json>({ points: [] });
  const [cases, setCases] = useState<Json[]>([]);
  const [runs, setRuns] = useState<Json[]>([]);
  const [silver, setSilver] = useState<Json | null>(null);
  const [reviewProgress, setReviewProgress] = useState<Json | null>(null);
  const [fusionTuning, setFusionTuning] = useState<Json | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Json[]>([]);
  const [expected, setExpected] = useState<string[]>([]);
  const [grades, setGrades] = useState<Record<string, number>>({});
  const [category, setCategory] = useState("general");
  const [difficulty, setDifficulty] = useState("normal");
  const [activeCaseId, setActiveCaseId] = useState("");
  const [retrievalHealth, setRetrievalHealth] = useState<Json | null>(null);
  const [actualRetrievalMode, setActualRetrievalMode] = useState("");
  const [rerankMode, setRerankMode] = useState("auto");
  const [retrievalMode, setRequestedRetrievalMode] = useState("ab");
  const [rejectionReason, setRejectionReason] = useState("ambiguous");
  const [notice, setNotice] = useState("");
  const [working, setWorking] = useState(false);

  const refreshStatus = useCallback(async () => {
    const statusResult = await api<Json>("/api/rag/status");
    setStatus(statusResult.data);
  }, []);

  const refresh = useCallback(async () => {
    const [, mapResult, casesResult, runsResult, silverResult, progressResult, fusionResult] = await Promise.all([
      refreshStatus(),
      api<Json>("/api/rag/vector-map?limit=300"),
      api<Json[]>("/api/rag/eval/cases"),
      api<Json[]>("/api/rag/eval/runs"),
      api<Json | null>("/api/rag/eval/silver/latest"),
      api<Json>("/api/rag/eval/review/progress"),
      api<Json>("/api/rag/eval/fusion-tuning-status"),
    ]);
    setMap(mapResult.data);
    setCases(casesResult.data);
    setRuns(runsResult.data);
    setSilver(silverResult.data);
    setReviewProgress(progressResult.data);
    setFusionTuning(fusionResult.data);
  }, [refreshStatus]);

  useEffect(() => { void refresh().catch((error) => setNotice(String(error))); }, [refresh]);
  useEffect(() => {
    if (status?.snapshot_state !== "refreshing") return;
    const timer = window.setTimeout(() => { void refreshStatus().catch((error) => setNotice(String(error))); }, 1200);
    return () => window.clearTimeout(timer);
  }, [refreshStatus, status?.snapshot_state]);

  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (activeCaseId) {
      await loadReviewCase(activeCaseId);
      return;
    }
    if (!query.trim()) return;
    setWorking(true);
    try {
      const response = await post<Json>("/api/rag/search-debug", { query, limit: 10, include_restricted: false, rerank_mode: rerankMode, retrieval_mode: retrievalMode, expand_parent: true });
      setResults(response.data.results);
      setRetrievalHealth(response.data.health);
      setActualRetrievalMode(response.data.mode);
      if (activeCaseId) {
        const active = cases.find((item) => item.id === activeCaseId);
        const restored = Object.fromEntries((active?.judgments ?? []).map((item: Json) => [item.source_id, item.relevance_grade]));
        setGrades(restored);
        setExpected(active?.expected_source_ids ?? []);
      } else {
        setExpected([]);
        setGrades({});
      }
      setNotice(response.next_actions.join("；"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "检索失败");
    } finally { setWorking(false); }
  };

  const saveCase = async () => {
    if (!query.trim() || !expected.length) return;
    setWorking(true);
    try {
      const scope = results.map((item) => item.source_id);
      if (activeCaseId) {
        await post(`/api/rag/eval/cases/${activeCaseId}/review`, {
          judgment_scope_source_ids: scope,
          judgments: scope.map((source_id) => ({ source_id, relevance_grade: grades[source_id], judgment_basis: "human" })),
          category,
          difficulty,
          match_policy: "any",
        });
      } else {
        await post("/api/rag/eval/cases", {
          query,
          expected_source_ids: expected,
          tags: ["manual-ui", category],
          category,
          difficulty,
          review_status: "reviewed",
          judgment_scope_source_ids: scope,
          judgments: scope.map((source_id) => ({ source_id, relevance_grade: grades[source_id], judgment_basis: "human" })),
        });
      }
      const currentIndex = draftCases.findIndex((item) => item.id === activeCaseId);
      const nextCase = activeCaseId
        ? draftCases.find((item, index) => item.id !== activeCaseId && index >= Math.max(0, currentIndex))
          ?? draftCases.find((item) => item.id !== activeCaseId)
        : undefined;
      setNotice("本题全部候选已由人工确认并通过正式门禁。自动来源绑定没有被当成人工结论。");
      await refresh();
      if (nextCase) await loadReviewCase(nextCase.id);
      else {
        setActiveCaseId("");
        setResults([]);
        setGrades({});
        setExpected([]);
      }
    } catch (error) { setNotice(error instanceof Error ? error.message : "保存失败"); }
    finally { setWorking(false); }
  };

  const runEval = async (mode = retrievalMode) => {
    setWorking(true);
    try {
      const response = await post<Json>("/api/rag/eval/run", { top_k: 5, rerank_mode: rerankMode, retrieval_mode: mode });
      setNotice(response.summary);
      await refresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : "测评失败"); }
    finally { setWorking(false); }
  };

  const runComparison = async () => {
    setWorking(true);
    try {
      const outcomes = [];
      for (const mode of ["a", "b", "ab"]) {
        const response = await post<Json>("/api/rag/eval/run", { top_k: 5, rerank_mode: mode === "ab" ? rerankMode : "never", retrieval_mode: mode });
        outcomes.push(`${mode.toUpperCase()} Hit@5 ${(Number(response.data.hit_rate) * 100).toFixed(1)}% / ${response.data.case_count}题`);
      }
      setNotice(outcomes.join("；")); await refresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : "A/B/AB对比失败"); }
    finally { setWorking(false); }
  };

  const tuneFusion = async () => {
    setWorking(true);
    try {
      const response = await post<Json>("/api/rag/eval/fusion-tune", {});
      setEvidence(response);
      setNotice(response.data?.recommendation
        ? `离线调权完成，建议 FTS:Vector = ${response.data.recommendation.weights.fts}:${response.data.recommendation.weights.vector}；尚未自动应用。`
        : response.summary);
      await refresh();
    } catch (error) { setNotice(error instanceof Error ? error.message : "离线调权失败"); }
    finally { setWorking(false); }
  };

  const rejectActiveCase = async () => {
    if (!activeCaseId || !window.confirm("确认剔除这道题？它不会进入正式指标，并需要后续补一条新题。")) return;
    setWorking(true);
    try {
      const currentIndex = draftCases.findIndex((item) => item.id === activeCaseId);
      const nextCase = draftCases.find((item, index) => item.id !== activeCaseId && index >= Math.max(0, currentIndex))
        ?? draftCases.find((item) => item.id !== activeCaseId);
      await post(`/api/rag/eval/cases/${activeCaseId}/reject`, { reason_code: rejectionReason });
      await refresh();
      setNotice("本题已保留审计并退出正式评测；系统已增加一条待补题缺口。");
      if (nextCase) await loadReviewCase(nextCase.id);
      else setActiveCaseId("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "剔除失败");
    } finally { setWorking(false); }
  };

  const latest = runs.find((item) => String(item.eval_protocol).startsWith("graded-scope-v3"))
    ?? runs.find((item) => item.eval_protocol === "graded-scope-v2");
  const reviewedCases = cases.filter((item) => item.review_eligible).length;
  const draftCases = cases
    .filter((item) => !item.review_eligible && item.review_status !== "rejected")
    .sort((left, right) => String(left.created_at ?? "").localeCompare(String(right.created_at ?? "")));

  const loadReviewCase = async (caseId: string) => {
    if (!caseId) return;
    setWorking(true);
    try {
      const response = await api<Json>(`/api/rag/eval/cases/${caseId}/review-context?limit=10&rerank_mode=${rerankMode}`);
      const item = response.data.case;
      const humanGrades = Object.fromEntries(
        Object.entries(response.data.judgments ?? {})
          .filter(([, judgment]: [string, any]) => judgment.judgment_basis === "human")
          .map(([sourceId, judgment]: [string, any]) => [sourceId, judgment.relevance_grade]),
      );
      setActiveCaseId(caseId);
      setQuery(item.query);
      setCategory(item.category ?? "general");
      setDifficulty(item.difficulty ?? "normal");
      setResults(response.data.candidates ?? []);
      setGrades(humanGrades);
      setExpected(Object.entries(humanGrades).filter(([, grade]) => Number(grade) > 0).map(([sourceId]) => sourceId));
      setRetrievalHealth(response.data.retrieval?.health ?? null);
      setActualRetrievalMode(response.data.retrieval?.mode ?? "");
      const seeded = (response.data.candidates ?? []).filter((candidate: Json) => candidate.seeded && !candidate.retrieved).length;
      setNotice(`已载入人工复核上下文：${response.data.scope_count} 个来源，其中 ${seeded} 个是未召回但需核对的绑定来源。请逐一点击 0–3。`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "复核上下文加载失败");
    } finally { setWorking(false); }
  };

  const selectDraft = (caseId: string) => {
    if (!caseId) {
      setActiveCaseId("");
      return;
    }
    const item = cases.find((candidate) => candidate.id === caseId);
    if (!item) return;
    void loadReviewCase(caseId);
  };
  const activeDraftIndex = draftCases.findIndex((item) => item.id === activeCaseId);
  const hasVectorScope = Number(status?.eligible_chunks ?? 0) > 0;
  const coverage = typeof status?.coverage === "number" ? Math.round(status.coverage * 10000) / 100 : null;
  const metricsRefreshing = status?.snapshot_state === "refreshing";
  const qdrantState = String(status?.qdrant?.status ?? "reading");
  const semanticRuntime = ({ ready: "可用", disabled: "未配置", offline: "离线", not_indexed: "未建库", warning: "不可用", reading: "读取中" } as Record<string, string>)[qdrantState] ?? qdrantState;
  const grouped = useMemo(() => Object.entries(status?.domains ?? {}), [status]);
  const sampledGroups = useMemo(() => Object.entries(map?.sample_distribution ?? {}), [map]);
  const coverageRows = status?.coverage_matrix ?? [];
  const coverageScope = status?.coverage_scope ?? {};
  const retrievalSurfaces = status?.retrieval_surfaces ?? {};
  return <>
    <FoundationSearch />
    {notice && <div className="rag-notice">{notice}</div>}
    <section className="rag-metrics">
      <Metric label="向量登记覆盖" value={metricsRefreshing ? "读取中" : hasVectorScope && coverage !== null ? `${coverage}%` : "暂无候选"} sub={metricsRefreshing ? "完整索引统计正在后台读取；不会阻塞页面或普通检索" : hasVectorScope ? `${status?.indexed_chunks ?? 0} / ${status?.eligible_chunks ?? 0} 片段 · 范围 ${status?.scope === "selected_l3" ? "仅 L3" : "全部正式资料"}；不等同于实时语义可用` : `当前范围 ${status?.scope === "selected_l3" ? "仅 L3" : "全部正式资料"}，暂无符合条件的片段；全文检索不受影响`} />
      <Metric label="待向量化" value={metricsRefreshing ? "—" : String(status?.pending_chunks ?? 0)} sub={metricsRefreshing ? "后台读取完成后显示真实增量队列" : "增量队列，不影响全文检索"} />
      <Metric label="语义检索运行态" value={semanticRuntime} sub={semanticRuntime === "可用" ? `${status?.model ?? "Embedding"} · ${status?.qdrant?.dimension ?? "—"} 维` : "需同时配置 Embedding 并连接 Qdrant"} />
      <Metric label="银标 Hit@5" value={silver ? percent(silver.hit_rate) : "—"} sub={silver ? `${silver.hit_count}/${silver.case_count} 来源命中 · MRR ${Number(silver.mrr).toFixed(3)}` : "运行银标门禁后可测"} />
    </section>

    <section className="rag-grid">
      <article className="vector-panel">
        <header><div><small>向量视图</small><h2>知识分布图</h2></div><span>抽样 {map.sampled ?? 0} / 300</span></header>
        <div className="vector-stage">
          <svg viewBox="0 0 1000 560" role="img" aria-label="知识向量二维投影">
            <defs><filter id="cellGlow"><feGaussianBlur stdDeviation="5" result="blur" /><feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge></filter></defs>
            <g className="vector-grid-lines"><path d="M0 140H1000M0 280H1000M0 420H1000M250 0V560M500 0V560M750 0V560" /></g>
            {(map.points ?? []).map((point: Json) => <circle key={point.id} cx={point.x * 1000} cy={point.y * 560} r={point.radius ?? 6} fill={colors[point.domain] ?? "#8b9ba5"} opacity="0.66" filter="url(#cellGlow)" onClick={() => setEvidence(point)}><title>{point.title} · {point.domain} · {point.source_type}</title></circle>)}
          </svg>
          {!map.points?.length && <div className="vector-empty">向量集合尚未建立或暂时无法读取</div>}
        </div>
        <footer><span>投影：{map.projection ?? "—"}</span><span>全量 {map.population ?? map.sampled ?? 0} · 分层抽样 {map.sampled ?? 0} · 坐标和大小不参与检索评分</span></footer>
        <div className="vector-legend">{grouped.map(([domain, count]) => <span key={domain}><i style={{ background: colors[domain] ?? "#8b9ba5" }} />{domain} · {String(count)}</span>)}</div>
        <div className="sample-groups">{sampledGroups.map(([name, count]) => <span key={name}>{name} · {String(count)}</span>)}</div>
      </article>

      <aside className="rag-side">
        <section><small>索引状态</small><h3>真实索引状态</h3><dl><Row label="Qdrant" value={status?.qdrant?.status ?? "读取中"} /><Row label="集合" value={status?.collection ?? "—"} /><Row label="向量点" value={status?.qdrant?.points ?? 0} /><Row label="内容块" value={status?.structure?.blocks ?? 0} /><Row label="关联" value={status?.structure?.chunk_block_relations ?? 0} /><Row label="孤立项" value={status?.structure?.orphan_chunks ?? 0} /><Row label="切分器" value={Object.keys(status?.structure?.chunker_versions ?? {}).join(" / ") || "—"} /><Row label="Codex MCP" value="已暂停测试" /></dl></section>
        <section><small>问题定位</small><h3>召回失败归因</h3><dl><Row label="候选未找到" value={(silver?.diagnostics?.candidate_miss ?? 0) + (silver?.diagnostics?.candidate_miss_no_vector ?? 0)} /><Row label="未进入前五" value={silver?.diagnostics?.ranking_miss ?? 0} /><Row label="重排救回" value={silver?.diagnostics?.rerank_rescue ?? 0} /><Row label="重排误伤" value={silver?.diagnostics?.rerank_drop ?? 0} /><Row label="来源不可用" value={silver?.diagnostics?.source_unavailable ?? 0} /></dl><p>同一道题同时跑不重排基线与正式重排，在前 {silver?.diagnostic_depth ?? 20} 个候选中定位故障阶段；这不是模型猜测。</p>{silver && <a className="report-link" href="/api/rag/eval/silver/latest/report">下载逐题回归报告</a>}</section>
        <section><small>人工评测</small><h3>人工黄金集</h3><strong className="case-count">{reviewedCases}<i> / 150</i></strong><div className="review-progress"><i style={{ width: `${Math.round((reviewProgress?.gold_completion ?? 0) * 100)}%` }} /></div><p>银标负责自动来源召回回归；正式金标要求候选范围内每个判断都由你亲自确认。当前剩余 {reviewProgress?.pending ?? draftCases.length} 题，剔除后待补 {reviewProgress?.replacement_needed ?? 0} 题。</p><p>融合调优：{fusionTuning?.may_apply ? "已达到门槛" : `等待人工金标 ${fusionTuning?.eligible_gold_cases ?? 0}/${fusionTuning?.minimum_gold_cases ?? 30}`}；当前 FTS:Vector = {fusionTuning?.current_weights?.fts ?? 1}:{fusionTuning?.current_weights?.vector ?? 1}。</p><div className="review-breakdown">{Object.entries(reviewProgress?.pending_by_category ?? {}).map(([name, count]) => <span key={name}>{name} · {String(count)}</span>)}</div><button disabled={working || !reviewProgress?.next_case_id} onClick={() => { const nextId = reviewProgress?.next_case_id; if (nextId) void loadReviewCase(nextId); }}>继续下一道待复核题</button><label className="rerank-choice"><span>待复核题 · {draftCases.length}</span><select value={activeCaseId} onChange={(event) => selectDraft(event.target.value)}><option value="">选择待复核题</option>{draftCases.map((item, index) => <option key={item.id} value={item.id}>{index + 1}. {item.query}</option>)}</select></label>{activeCaseId && <div className="review-navigation"><button disabled={working || activeDraftIndex <= 0} onClick={() => void loadReviewCase(draftCases[activeDraftIndex - 1]?.id)}>上一题</button><span>{activeDraftIndex + 1} / {draftCases.length}</span><button disabled={working || activeDraftIndex < 0 || activeDraftIndex >= draftCases.length - 1} onClick={() => void loadReviewCase(draftCases[activeDraftIndex + 1]?.id)}>下一题</button></div>}<label className="rerank-choice"><span>测评重排</span><select value={rerankMode} onChange={(event) => setRerankMode(event.target.value)}><option value="auto">自动 · 复杂问题才调用</option><option value="never">禁用 · 测 RRF 基线</option><option value="always">强制 · 对照重排效果</option></select></label><button disabled={busy || working || !reviewedCases} onClick={() => void runEval()}>运行当前模式测评</button><button disabled={busy || working || !reviewedCases} onClick={() => void runComparison()}>同题对比 A / B / AB</button><button disabled={busy || working || !fusionTuning?.may_apply} onClick={tuneFusion}>离线搜索融合权重</button></section>
      </aside>
    </section>

    <section className="coverage-matrix-panel">
      <header><div><small>向量范围</small><h2>全库向量覆盖矩阵</h2><p>100%只表示符合策略的片段全部入向量库；这里同时展示全库总量和明确排除原因。</p></div><div className="coverage-scope-metrics"><span><small>全部片段</small><b>{formatInteger(coverageScope.all_indexed_chunks)}</b></span><span><small>符合条件</small><b>{formatInteger(coverageScope.vector_eligible_chunks)}</b></span><span><small>已向量化</small><b>{formatInteger(coverageScope.vector_indexed_chunks)}</b></span><span><small>Codex任务排除</small><b>{formatInteger(coverageScope.excluded_codex_user_tasks)}</b></span><span><small>受限策略排除</small><b>{formatInteger(coverageScope.excluded_restricted_policy)}</b></span></div></header>
      <div className="coverage-table-wrap"><table><thead><tr><th>领域</th><th>来源类型</th><th>隐私</th><th>全部片段</th><th>符合条件</th><th>已向量化</th><th>覆盖/原因</th></tr></thead><tbody>{coverageRows.map((row: Json) => <tr key={`${row.domain}-${row.source_type}-${row.privacy}`}><td>{row.domain}</td><td>{row.source_type}</td><td>{row.privacy}</td><td>{formatInteger(row.total_chunks)}</td><td>{formatInteger(row.eligible_chunks)}</td><td>{formatInteger(row.indexed_chunks)}</td><td>{coverageReason(row, coverageScope)}</td></tr>)}</tbody></table></div>
      <div className="retrieval-surfaces">{Object.entries(retrievalSurfaces).map(([name, raw]) => { const surface = raw as Json; return <article key={name}><small>{surfaceLabel(name)}</small><strong>{formatInteger(surface.indexed)}<i> / {formatInteger(surface.records)}</i></strong><span>{surface.engine}{surface.semantic ? " · 语义" : " · 本地"}{surface.physical_rows !== undefined && Number(surface.physical_rows) !== Number(surface.indexed) ? ` · 物理行 ${formatInteger(surface.physical_rows)}` : ""}</span></article>; })}</div>
    </section>

    <section className="search-lab">
      <header><div><small>检索验证</small><h2>检索与标注实验台</h2></div>{latest && <div className="latest-metrics"><span>Hit {percent(latest.hit_rate)}</span><span>标注P@5 {percent(latest.precision_at_k)}</span><span>nDCG {Number(latest.ndcg_at_k ?? 0).toFixed(3)}</span><span>覆盖 {percent(latest.judgment_coverage ?? 0)}</span></div>}</header>
      <form onSubmit={search}><input value={query} readOnly={Boolean(activeCaseId)} onChange={(event) => setQuery(event.target.value)} placeholder="输入一个你知道答案在哪份资料里的问题" /><select value={retrievalMode} onChange={(event) => setRequestedRetrievalMode(event.target.value)} aria-label="检索库模式"><option value="a">A · 目录/关键词</option><option value="b">B · 向量语义</option><option value="ab">AB · 混合检索</option></select><select value={rerankMode} onChange={(event) => setRerankMode(event.target.value)} aria-label="重排模式"><option value="auto">自动重排</option><option value="never">不重排</option><option value="always">强制重排</option></select><button disabled={working}>{activeCaseId ? "按当前模式重新检索" : "执行检索"}</button></form>
      {retrievalHealth && <div className={`retrieval-health ${retrievalHealth.sufficiency}`}><div><small>检索状态</small><strong>{healthLabel(retrievalHealth.sufficiency)}</strong><span>{actualRetrievalMode} · 候选 {retrievalHealth.candidate_count} · 返回 {retrievalHealth.result_count} · 来源 {retrievalHealth.source_count}</span></div><dl><Row label="检索策略" value={queryPlanLabel(retrievalHealth.query_plan)} /><Row label="策略候选" value={retrievalHealth.query_plan?.candidate_limit ?? "—"} /><Row label="目录索引" value={retrievalHealth.channels?.catalog ?? 0} /><Row label="全文检索" value={retrievalHealth.channels?.fts ?? 0} /><Row label="向量检索" value={retrievalHealth.channels?.vector ?? 0} /><Row label="重排" value={retrievalHealth.rerank_status} /><Row label="覆盖率" value={percent(retrievalHealth.vector_coverage)} /></dl>{retrievalHealth.query_plan?.reasons?.length > 0 && <p>{retrievalHealth.query_plan.reasons.join("；")}</p>}{retrievalHealth.reasons?.length > 0 && <p>{retrievalHealth.reasons.join("；")}</p>}</div>}
      <div className="lab-results">
        {results.map((item, index) => <article key={`${item.source_id}-${index}`} className={`${(grades[item.source_id] ?? -1) > 0 ? "selected" : ""} ${item.seeded && !item.retrieved ? "seeded-source" : ""}`}>
          <label className="grade-picker"><span>{item.seeded && !item.retrieved ? "绑定来源" : `候选 #${item.rank ?? index + 1}`}</span>{[0, 1, 2, 3].map((grade) => <button type="button" title={gradeLabel(grade)} key={grade} className={grades[item.source_id] === grade ? "active" : ""} onClick={() => { setGrades((current) => ({ ...current, [item.source_id]: grade })); setExpected((current) => grade > 0 ? Array.from(new Set([...current, item.source_id])) : current.filter((id) => id !== item.source_id)); }}>{grade}</button>)}</label>
          <button onClick={() => setEvidence(item)}><b>{item.seeded && !item.retrieved ? "标" : `#${item.rank ?? index + 1}`}</b><span><strong>{item.title}</strong><p>{item.snippet || "该来源当前没有可显示的片段，请通过证据抽屉核对来源状态。"}</p><small>{item.seeded && !item.retrieved ? "前 10 未召回 · 需要人工核对" : `${item.match_strategy ?? "source"} · ${item.domain ?? "—"}`} · {item.locator ?? item.source_id}</small></span></button>
        </article>)}
        {!results.length && <div className="lab-empty">先输入一个问题；检索结果会显示真实来源、通道和片段。</div>}
      </div>
      {results.length > 0 && <footer><div className="case-meta"><select value={category} onChange={(event) => setCategory(event.target.value)}><option value="general">通用</option><option value="business">业务</option><option value="project">项目</option><option value="operations">运维</option><option value="hardware">硬件</option><option value="customer">客户</option><option value="chat">聊天</option><option value="code">代码</option><option value="table">表格</option><option value="cross-document">跨文档</option></select><select value={difficulty} onChange={(event) => setDifficulty(event.target.value)}><option value="easy">简单</option><option value="normal">一般</option><option value="hard">困难</option></select><span>人工标注 {results.filter((item) => grades[item.source_id] !== undefined).length}/{results.length}，相关 {expected.length} · 0无关 / 1背景 / 2有用 / 3直接回答</span></div>{activeCaseId && <div className="reject-case"><select value={rejectionReason} onChange={(event) => setRejectionReason(event.target.value)}><option value="ambiguous">问题含糊</option><option value="unanswerable">资料无法回答</option><option value="duplicate">重复问题</option><option value="bad_source">绑定来源错误</option><option value="out_of_scope">超出范围</option></select><button disabled={working} onClick={rejectActiveCase}>剔除并记录原因</button></div>}<button disabled={working || !expected.length || results.some((item) => grades[item.source_id] === undefined)} onClick={saveCase}>{activeCaseId ? "确认本题并进入下一题" : "保存已复核黄金题"}</button></footer>}
    </section>
  </>;
}

function Metric({ label, value, sub }: { label: string; value: string; sub: string }) { return <article><small>{label}</small><strong>{value}</strong><span>{sub}</span></article>; }
function Row({ label, value }: { label: string; value: unknown }) { return <><dt>{label}</dt><dd>{String(value)}</dd></>; }
function percent(value: number) { return `${(Number(value) * 100).toFixed(1)}%`; }
function healthLabel(value: string) { return ({ sufficient: "证据充分", partial: "证据有限", insufficient: "证据不足" } as Record<string, string>)[value] ?? value; }
function queryPlanLabel(plan: Json | undefined) { return ({ precise_lookup: "精确查找", cross_source_synthesis: "跨资料综合", exploration: "普通探索" } as Record<string, string>)[String(plan?.intent ?? "")] ?? "默认策略"; }
function gradeLabel(value: number) { return ["无关", "背景相关", "有用但不完整", "直接完整回答"][value] ?? String(value); }
function formatInteger(value: unknown) { return new Intl.NumberFormat("zh-CN").format(Number(value ?? 0)); }
function coverageReason(row: Json, scope: Json) {
  if (row.source_type === "codex-turn") return "任务事实保留，按策略不做向量";
  if (row.privacy === "restricted" && !scope.restricted_embedding_enabled) return "受限资料，仅本地全文检索";
  if (Number(row.pending_chunks) > 0) return `待处理 ${formatInteger(row.pending_chunks)}`;
  return row.coverage === null ? "不适用" : `${(Number(row.coverage) * 100).toFixed(1)}%`;
}
function surfaceLabel(value: string) { return ({ document_fts: "文档与任务全文", document_vectors: "文档语义向量", customer_message_fts: "微信客户消息全文", source_catalog: "现成资料目录", approved_knowledge: "已批准长期知识" } as Record<string, string>)[value] ?? value; }
