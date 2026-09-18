import { useCallback, useEffect, useMemo, useState } from "react";
import { api, post } from "./api";
import "./runtime-center.css";

type Service = { id: string; name: string; status: string; owned: boolean; controllable: boolean; detail: string };
type Operation = { channel: string; operation: string; calls: number; failures: number; warnings: number; average_ms: number };
type State = {
  services: Service[];
  usage: { days: number; calls: number | null; success_rate: number | null; operations: Operation[]; coverage: string };
  queues: Record<string, Record<string, number>>;
  thread_journal: {
    enabled: boolean; running: boolean; interval_days: number; last_status: string;
    last_completed_at: string | null; last_result: { summaries_written?: number; pending_files?: number; completed_files?: number } | null;
  };
};

const serviceLabels: Record<string, string> = { running: "可用", starting: "正在启动", stopping: "正在停止", stopped: "已关闭", external: "可用", not_managed: "未开启", failed: "需要检查" };
const queueLabels: Record<string, string> = { pending: "等待处理", processing: "处理中", completed: "历史完成", failed: "失败", skipped: "无需处理" };
function total(states: Record<string, number> | undefined, names: string[]) { return names.reduce((sum, name) => sum + (states?.[name] ?? 0), 0); }

export default function RuntimeCenter() {
  const [data, setData] = useState<State | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<{ id: string; action: string } | null>(null);
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const timeout = AbortSignal.timeout(8000);
      const result = await api<State>("/api/runtime/overview", { signal: signal ? AbortSignal.any([signal, timeout]) : timeout });
      setData(result.data); setError("");
    } catch (exception) {
      if (!signal?.aborted) setError(exception instanceof Error ? exception.message : "读取失败");
    }
  }, []);
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort(); }, [load]);
  const services = useMemo(() => Object.fromEntries((data?.services ?? []).map((service) => [service.id, service])), [data]);
  const semanticReady = ["running", "external"].includes(services.qdrant?.status);
  const indexRunning = ["running", "starting", "stopping"].includes(services.indexer?.status);
  const vectorQueue = data?.queues.index_outbox_actionable;
  const backgroundQueue = data?.queues.index_outbox_background;
  const waiting = total(vectorQueue, ["pending", "processing"]);
  const failed = total(vectorQueue, ["failed"]);
  const backgroundPending = total(backgroundQueue, ["pending", "processing", "failed"]);
  const systemState = error ? "状态读取失败" : data ? "基础功能可以正常使用" : "正在检查";

  async function act() {
    if (!confirm) return;
    setBusy(true);
    try {
      await post(`/api/runtime/services/${confirm.id}`, { action: confirm.action, confirmed: true, confirm_cloud: confirm.id === "indexer" });
      setConfirm(null); await load();
    } catch (exception) { setError(exception instanceof Error ? exception.message : "操作失败"); }
    finally { setBusy(false); }
  }

  async function refreshJournal() {
    setBusy(true);
    try {
      await post("/api/thread-journal/refresh", {});
      await load();
    } catch (exception) { setError(exception instanceof Error ? exception.message : "线程日报更新失败"); }
    finally { setBusy(false); }
  }

  return <div className="runtime-center">
    <section className={`runtime-hero ${error ? "warning" : "ready"}`}>
      <div><small>CURRENT STATUS</small><h2>{systemState}</h2><p>{semanticReady ? "全文搜索和语义搜索都已可用。" : "全文搜索可用；需要理解相近含义时，再开启语义搜索。"}</p></div>
      <button className="quiet-button" disabled={busy} onClick={() => void load()}>重新检查</button>
    </section>
    {error && <p className="runtime-error" role="alert">{error}。页面保留的数据可能已经过期。</p>}
    {!data && !error && <div className="runtime-loading" role="status">正在读取本机状态…</div>}
    {confirm && <section className="runtime-confirm" role="dialog" aria-label="确认运行操作">
      <small>CONFIRM ACTION</small><h3>{confirm.id === "qdrant" ? (confirm.action === "start" ? "开启语义搜索？" : "关闭语义搜索？") : (confirm.action === "start" ? "处理待索引资料？" : "停止处理资料？")}</h3>
      <p>{confirm.id === "indexer" ? "只处理已经进入队列的资料，可能调用 Embedding API；不会同步微信，也不会运行 Agent。" : "关闭后仍能按关键词搜索，但暂时不能按相近含义搜索。"}</p>
      <footer><button disabled={busy} onClick={() => setConfirm(null)}>取消</button><button className="primary" disabled={busy} onClick={() => void act()}>确认</button></footer>
    </section>}
    {data && <>
      <section className="runtime-actions">
        <article className="runtime-action ready"><span>01</span><div><small>基础搜索</small><h3>随知枢自动可用</h3><p>打开知枢后即可搜索文件、聊天和每日足迹，无需手动操作。</p></div><b>正常</b></article>
        <article className={`runtime-action ${semanticReady ? "ready" : "optional"}`}><span>02</span><div><small>语义搜索</small><h3>{semanticReady ? "已开启" : "按需要开启"}</h3><p>{semanticReady ? "可以查找意思相近、措辞不同的资料。" : "普通关键词搜索不受影响；复杂问题再开启即可。"}</p></div>{services.qdrant?.controllable ? <button disabled={busy} onClick={() => setConfirm({ id: "qdrant", action: services.qdrant.owned ? "stop" : "start" })}>{services.qdrant.owned ? "关闭" : "开启"}</button> : <b>{serviceLabels[services.qdrant?.status] ?? "可用"}</b>}</article>
        <article className={`runtime-action ${waiting || failed ? "attention" : "ready"}`}><span>03</span><div><small>正式资料语义处理</small><h3>{waiting ? `${waiting} 项等待处理` : failed ? `${failed} 项需要检查` : "当前没有待处理资料"}</h3><p>{waiting ? "只把正式资料转成语义索引；处理结束后会自动停在当前状态。" : backgroundPending ? `正式资料已完成。另有 ${backgroundPending} 项历史或隔离事件只在技术详情保留，不需要启动处理。` : "只有新资料进入正式向量队列时才需要操作。"}</p></div>{waiting > 0 && !indexRunning ? <button disabled={busy || !semanticReady} title={!semanticReady ? "请先开启语义搜索" : ""} onClick={() => setConfirm({ id: "indexer", action: "start" })}>开始处理</button> : indexRunning ? <button disabled={busy} onClick={() => setConfirm({ id: "indexer", action: "stop" })}>停止</button> : <b>无需操作</b>}</article>
        <article className={`runtime-action ${data.thread_journal?.last_status === "failed" ? "attention" : "ready"}`}><span>04</span><div><small>Codex 会话摘要</small><h3>{data.thread_journal?.running ? "Luna 正在后台整理" : data.thread_journal?.last_result?.pending_files ? `首次整理还剩 ${data.thread_journal.last_result.pending_files} 个` : data.thread_journal?.last_completed_at ? "本轮已完成" : "等待首次整理"}</h3><p>{data.thread_journal?.last_completed_at ? `最近检查 ${new Date(data.thread_journal.last_completed_at).toLocaleString()}；每 ${data.thread_journal.interval_days || 7} 天复查文件变化，未变化不调用 Luna。` : "首次登记全部会话；以后只总结新增或改动的会话文件。"}</p></div><button disabled={busy || data.thread_journal?.running} onClick={() => void refreshJournal()}>后台检查</button></article>
      </section>
      <section className="runtime-usage">
        <header><div><small>LAST {data.usage.days ?? 7} DAYS</small><h2>最近使用情况</h2></div><p>这里只表示系统是否调用成功，不代表回答一定准确。</p></header>
        <div className="runtime-metrics"><article><strong>{data.usage.calls ?? "—"}</strong><span>已观测调用</span></article><article><strong>{data.usage.success_rate === null ? "—" : `${(data.usage.success_rate * 100).toFixed(1)}%`}</strong><span>技术调用成功</span></article><article><strong>{data.usage.operations.length}</strong><span>使用过的功能</span></article></div>
        <details><summary>查看调用明细</summary>{data.usage.operations.length ? <table><thead><tr><th>功能</th><th>次数</th><th>失败</th><th>平均耗时</th></tr></thead><tbody>{data.usage.operations.map((item) => <tr key={item.channel + item.operation}><td>{item.operation.replace("/api/", "")}</td><td>{item.calls}</td><td>{item.failures}</td><td>{item.average_ms} ms</td></tr>)}</tbody></table> : <p>还没有可显示的使用记录。</p>}<p className="runtime-footnote">{data.usage.coverage}</p></details>
      </section>
      <details className="runtime-advanced"><summary>技术详情与历史任务</summary>
        <div className="runtime-service-list">{data.services.map((service) => <article key={service.id}><div><strong>{service.name}</strong><span>{serviceLabels[service.status] ?? service.status}</span></div><p>{service.detail}</p></article>)}</div>
        <div className="runtime-queue-list">{Object.entries(data.queues).map(([name, states]) => <article key={name}><strong>{({ index_outbox: "全部索引事件（诊断）", index_outbox_actionable: "正式资料待语义化", index_outbox_background: "历史/隔离事件", agent_jobs: "历史遗留任务（已停用）", workflow_runs: "历史工作流" } as Record<string, string>)[name] ?? name}</strong><p>{Object.entries(states).map(([status, count]) => `${queueLabels[status] ?? status} ${count}`).join(" · ") || "无记录"}</p></article>)}</div>
      </details>
    </>}
  </div>;
}
