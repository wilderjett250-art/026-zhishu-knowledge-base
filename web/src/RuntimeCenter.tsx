import { useCallback, useEffect, useMemo, useState } from "react";
import { api, post } from "./api";
import "./runtime-center.css";

type Service = { id: string; name: string; status: string; owned: boolean; controllable: boolean; detail: string };
type Operation = { channel: string; operation: string; calls: number; failures: number; warnings: number; average_ms: number };
type ThreadJournal = {
  enabled: boolean; running: boolean; interval_days: number; last_status: string;
  last_completed_at: string | null; last_result: { summaries_written?: number; pending_files?: number; completed_files?: number } | null;
};
type State = {
  services: Service[];
  usage: { days: number; calls: number | null; success_rate: number | null; operations: Operation[]; coverage: string };
  queues: Record<string, Record<string, number>>;
  thread_journal: ThreadJournal;
};
type BriefState = Pick<State, "services" | "thread_journal">;
type ConnectionState = "checking" | "connected" | "offline";
type CachedState = { savedAt: number; data: State };

const RUNTIME_CACHE_KEY = "pkas.runtime.overview.v2";
const RUNTIME_CACHE_MAX_AGE_MS = 12 * 60 * 60 * 1000;
const serviceLabels: Record<string, string> = { running: "可用", starting: "正在启动", stopping: "正在停止", stopped: "已关闭", external: "可用", not_managed: "未开启", failed: "需要检查" };
const queueLabels: Record<string, string> = { pending: "等待处理", processing: "处理中", completed: "历史完成", failed: "失败", skipped: "无需处理" };

function total(states: Record<string, number> | undefined, names: string[]) {
  return names.reduce((sum, name) => sum + (states?.[name] ?? 0), 0);
}

function readCachedState(): CachedState | null {
  try {
    const raw = window.sessionStorage.getItem(RUNTIME_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedState;
    if (!parsed?.data || !Number.isFinite(parsed.savedAt) || Date.now() - parsed.savedAt > RUNTIME_CACHE_MAX_AGE_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeCachedState(data: State) {
  try {
    window.sessionStorage.setItem(RUNTIME_CACHE_KEY, JSON.stringify({ savedAt: Date.now(), data }));
  } catch {
    // Cache is only a responsiveness enhancement. A blocked storage policy must not block the page.
  }
}

function requestSignal(signal: AbortSignal | undefined, milliseconds: number) {
  const timeout = AbortSignal.timeout(milliseconds);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

export default function RuntimeCenter() {
  const initialCache = useMemo(readCachedState, []);
  const [data, setData] = useState<State | null>(initialCache?.data ?? null);
  const [brief, setBrief] = useState<BriefState | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [connectionError, setConnectionError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [detailsLoading, setDetailsLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<{ id: string; action: string } | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setConnection("checking");
    setConnectionError("");
    setDetailError("");
    setDetailsLoading(true);

    // Start both calls together. The small endpoint gives an immediate usable state;
    // the historical queue query is intentionally allowed to finish later.
    const briefRequest = api<BriefState>("/api/runtime/brief", { signal: requestSignal(signal, 3000) });
    const overviewRequest = api<State>("/api/runtime/overview", { signal: requestSignal(signal, 25000) });

    try {
      const result = await briefRequest;
      if (!signal?.aborted) {
        setBrief(result.data);
        setConnection("connected");
      }
    } catch (exception) {
      if (!signal?.aborted) {
        setConnection("offline");
        setConnectionError(exception instanceof Error ? exception.message : "无法连接本机知识服务");
      }
    }

    try {
      const result = await overviewRequest;
      if (!signal?.aborted) {
        setData(result.data);
        setBrief(result.data);
        setConnection("connected");
        setConnectionError("");
        writeCachedState(result.data);
      }
    } catch (exception) {
      if (!signal?.aborted) {
        setDetailError(exception instanceof Error ? exception.message : "资料处理详情暂时无法读取");
      }
    } finally {
      if (!signal?.aborted) setDetailsLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const serviceState = brief ?? data;
  const services = useMemo(
    () => Object.fromEntries((serviceState?.services ?? []).map((service) => [service.id, service])),
    [serviceState],
  );
  const semanticReady = ["running", "external"].includes(services.qdrant?.status);
  const indexRunning = ["running", "starting", "stopping"].includes(services.indexer?.status);
  const vectorQueue = data?.queues.index_outbox_actionable;
  const backgroundQueue = data?.queues.index_outbox_background;
  const waiting = total(vectorQueue, ["pending", "processing"]);
  const failed = total(vectorQueue, ["failed"]);
  const backgroundPending = total(backgroundQueue, ["pending", "processing", "failed"]);
  const threadJournal = serviceState?.thread_journal;
  const systemState = connection === "offline" ? "本机服务需要检查" : connection === "connected" ? (detailsLoading ? "本机服务已连接" : "知识库状态已更新") : "正在连接本机服务";
  const systemDescription = connection === "offline"
    ? "没有改动资料或后台进程。请检查本机知识服务后再重试。"
    : connection === "connected" && detailsLoading
      ? "基础服务已连接；资料处理进度和历史统计正在后台读取。"
      : semanticReady
        ? "全文检索与语义检索均已就绪。"
        : "全文检索可用；需要按相近含义搜索时，再开启语义搜索。";

  async function act() {
    if (!confirm) return;
    setBusy(true);
    try {
      await post(`/api/runtime/services/${confirm.id}`, { action: confirm.action, confirmed: true, confirm_cloud: confirm.id === "indexer" });
      setConfirm(null);
      await load();
    } catch (exception) {
      setDetailError(exception instanceof Error ? exception.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function refreshJournal() {
    setBusy(true);
    try {
      await post("/api/thread-journal/refresh", {});
      await load();
    } catch (exception) {
      setDetailError(exception instanceof Error ? exception.message : "会话整理检查失败");
    } finally {
      setBusy(false);
    }
  }

  return <div className="runtime-center">
    <section className={`runtime-hero ${connection === "offline" ? "warning" : "ready"}`}>
      <div className="runtime-hero-copy">
        <small>本机服务</small>
        <h2>{systemState}</h2>
        <p>{systemDescription}</p>
        <span className={`runtime-state-chip ${connection}`}><i />{connection === "connected" ? "服务已响应" : connection === "offline" ? "需要检查" : "连接中"}</span>
      </div>
      <button className="quiet-button" disabled={busy} onClick={() => void load()}>刷新状态</button>
    </section>

    {connectionError && <p className="runtime-error" role="alert">{connectionError}</p>}
    {detailError && <p className="runtime-detail-warning" role="status">{detailError}；基础服务状态仍可使用。</p>}

    {!serviceState && connection !== "offline" && <section className="runtime-connect-panel" role="status"><i /><div><strong>正在连接本机知识服务</strong><span>不会扫描资料、导入聊天或启动语义处理。</span></div></section>}

    {serviceState && <>
      <section className="runtime-actions" aria-label="常用运行操作">
        <article className={`runtime-action ${connection === "connected" ? "ready" : "optional"}`}><span>01</span><div><small>基础服务</small><h3>{connection === "connected" ? "已连接" : "等待连接"}</h3><p>{connection === "connected" ? "本机知识服务已经响应；详细状态会在下方自动更新。" : "服务没有连接前，不会尝试启动资料处理任务。"}</p></div><b>{connection === "connected" ? "正常" : "等待中"}</b></article>
        <article className={`runtime-action ${semanticReady ? "ready" : "optional"}`}><span>02</span><div><small>语义搜索</small><h3>{semanticReady ? "已开启" : "按需要开启"}</h3><p>{semanticReady ? "可以查找意思相近、措辞不同的资料。" : "普通关键词搜索不受影响；复杂问题再开启即可。"}</p></div>{services.qdrant?.controllable ? <button disabled={busy} onClick={() => setConfirm({ id: "qdrant", action: services.qdrant.owned ? "stop" : "start" })}>{services.qdrant.owned ? "关闭" : "开启"}</button> : <b>{serviceLabels[services.qdrant?.status] ?? "读取中"}</b>}</article>
        <article className={`runtime-action ${waiting || failed ? "attention" : "ready"}`}><span>03</span><div><small>资料语义处理</small><h3>{detailsLoading && !data ? "正在读取处理进度" : waiting ? `${waiting} 项等待处理` : failed ? `${failed} 项需要检查` : "当前没有待处理资料"}</h3><p>{detailsLoading && !data ? "统计与队列在后台加载，不影响服务连接。" : waiting ? "只处理已确认进入正式资料库的内容；可能调用 Embedding API。" : backgroundPending ? `正式资料已完成。另有 ${backgroundPending} 项历史或隔离事件仅保留在技术详情。` : "只有新资料进入正式向量队列时才需要操作。"}</p></div>{waiting > 0 && !indexRunning ? <button disabled={busy || !semanticReady} title={!semanticReady ? "请先开启语义搜索" : ""} onClick={() => setConfirm({ id: "indexer", action: "start" })}>开始处理</button> : indexRunning ? <button disabled={busy} onClick={() => setConfirm({ id: "indexer", action: "stop" })}>停止</button> : <b>{detailsLoading && !data ? "加载中" : "无需操作"}</b>}</article>
        <article className={`runtime-action ${threadJournal?.last_status === "failed" ? "attention" : "ready"}`}><span>04</span><div><small>会话整理</small><h3>{threadJournal?.running ? "正在后台检查" : threadJournal?.last_result?.pending_files ? `首次整理还剩 ${threadJournal.last_result.pending_files} 个` : threadJournal?.last_completed_at ? "本轮已完成" : "等待首次检查"}</h3><p>{threadJournal?.last_completed_at ? `最近检查 ${new Date(threadJournal.last_completed_at).toLocaleString()}；每 ${threadJournal.interval_days || 7} 天只复查有变化的会话文件。` : "首次登记会话状态；以后只处理新增或改动的会话文件。"}</p></div><button disabled={busy || Boolean(threadJournal?.running)} onClick={() => void refreshJournal()}>检查更新</button></article>
      </section>

      {detailsLoading && !data && <div className="runtime-detail-loading" role="status"><i />正在读取资料处理统计与历史任务…</div>}

      {data && <>
        <section className="runtime-usage">
          <header><div><small>最近 {data.usage.days ?? 7} 天</small><h2>使用情况</h2></div><p>这里只表示系统调用是否成功，不代表回答一定准确。</p></header>
          <div className="runtime-metrics"><article><strong>{data.usage.calls ?? "—"}</strong><span>已观测调用</span></article><article><strong>{data.usage.success_rate === null ? "—" : `${(data.usage.success_rate * 100).toFixed(1)}%`}</strong><span>技术调用成功</span></article><article><strong>{data.usage.operations.length}</strong><span>使用过的功能</span></article></div>
          <details><summary>查看调用明细</summary>{data.usage.operations.length ? <table><thead><tr><th>功能</th><th>次数</th><th>失败</th><th>平均耗时</th></tr></thead><tbody>{data.usage.operations.map((item) => <tr key={item.channel + item.operation}><td>{item.operation.replace("/api/", "")}</td><td>{item.calls}</td><td>{item.failures}</td><td>{item.average_ms} ms</td></tr>)}</tbody></table> : <p>还没有可显示的使用记录。</p>}<p className="runtime-footnote">{data.usage.coverage}</p></details>
        </section>
        <details className="runtime-advanced"><summary>技术详情与历史任务</summary>
          <div className="runtime-service-list">{data.services.map((service) => <article key={service.id}><div><strong>{service.name}</strong><span>{serviceLabels[service.status] ?? service.status}</span></div><p>{service.detail}</p></article>)}</div>
          <div className="runtime-queue-list">{Object.entries(data.queues).map(([name, states]) => <article key={name}><strong>{({ index_outbox: "全部索引事件（诊断）", index_outbox_actionable: "正式资料待语义化", index_outbox_background: "历史/隔离事件", agent_jobs: "历史遗留任务（已停用）", workflow_runs: "历史工作流" } as Record<string, string>)[name] ?? name}</strong><p>{Object.entries(states).map(([status, count]) => `${queueLabels[status] ?? status} ${count}`).join(" · ") || "无记录"}</p></article>)}</div>
        </details>
      </>}
    </>}

    {confirm && <section className="runtime-confirm" role="dialog" aria-label="确认运行操作">
      <small>确认操作</small><h3>{confirm.id === "qdrant" ? (confirm.action === "start" ? "开启语义搜索？" : "关闭语义搜索？") : (confirm.action === "start" ? "处理待索引资料？" : "停止处理资料？")}</h3>
      <p>{confirm.id === "indexer" ? "只处理已经进入队列的资料，可能调用 Embedding API；不会同步微信，也不会运行 Agent。" : "关闭后仍能按关键词搜索，但暂时不能按相近含义搜索。"}</p>
      <footer><button disabled={busy} onClick={() => setConfirm(null)}>取消</button><button className="primary" disabled={busy} onClick={() => void act()}>确认</button></footer>
    </section>}
  </div>;
}
