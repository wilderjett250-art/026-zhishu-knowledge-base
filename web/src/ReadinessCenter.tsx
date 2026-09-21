import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import "./readiness.css";

type Gate = { status: string; metrics: Record<string, unknown>; evidence: string; next_action: string; checked_at: string; cached: boolean; snapshot_state?: string; snapshot_error?: string | null; refreshing?: boolean };
const sections = [
  ["database", "资料与全文索引"], ["retrieval", "向量与混合检索"],
  ["capability", "客户端与扩展"], ["evaluation", "检索质量"],
  ["runtime", "运行设置"], ["quality", "测试记录"],
] as const;
const labels: Record<string, string> = { passed: "正常", partial: "需完善", paused: "未运行", blocked: "需处理", unknown: "待核实" };
const metrics: Record<string, string> = {
  autostart_enabled: "开机自启动", visible_terminal_required: "需要终端窗口",
  schema: "数据库版本", chunks: "文本片段", chunk_fts: "全文索引片段", customer_messages: "聊天消息", customer_message_fts: "消息索引", orphan_links: "无效关联",
  eligible_vectors: "待覆盖片段", indexed_vectors: "已索引片段", coverage: "索引覆盖率", vector_runtime: "向量服务", skills: "Skill 数量", mcp_servers: "MCP 配置", profiles: "使用配置",
  silver_passed: "自动评测通过", silver_hit_rate: "自动评测命中率", gold_eligible: "有效人工样本", gold_total: "评测样本", tests: "测试数", failures: "失败", errors: "错误", skipped: "跳过",
};

function CheckCard({ id, title, refreshKey }: { id: string; title: string; refreshKey: number }) {
  const [result, setResult] = useState<Gate | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const hasKnownResult = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    if (!hasKnownResult.current) setBusy(true);
    setError("");
    const timer = window.setTimeout(() => controller.abort(), 12000);
    api<Gate>(`/api/core/readiness/${id}`, { signal: controller.signal }).then(response => {
      if (active) { hasKnownResult.current = true; setResult(response.data); }
    }).catch(reason => {
      if (active) setError(controller.signal.aborted ? "检查超过 12 秒，请稍后重试。您可以继续使用其他页面。" : (reason instanceof Error ? reason.message : "检查失败，请重试。"));
    }).finally(() => { window.clearTimeout(timer); if (active) setBusy(false); });
    return () => { active = false; window.clearTimeout(timer); controller.abort(); };
  }, [id, attempt, refreshKey]);
  const refreshing = result?.snapshot_state === "refreshing" || Boolean(result?.refreshing);
  const pending = busy || (refreshing && !result?.cached);
  useEffect(() => {
    if (!refreshing) return;
    const timer = window.setTimeout(() => setAttempt(value => value + 1), 1200);
    return () => window.clearTimeout(timer);
  }, [refreshing]);
  return <article className={error ? "blocked" : pending ? "unknown" : result?.status ?? "unknown"} aria-busy={busy || refreshing}>
    <header><h3>{title}</h3><span>{pending ? "检查中" : refreshing ? "后台复查中" : error ? "暂未完成" : labels[result?.status ?? "unknown"]}</span></header>
    {pending && <p role="status">正在后台读取本项状态，不会阻塞其他页面。</p>}
    {refreshing && result?.cached && <p role="status">正在后台复查，当前先保留已知结果。</p>}
    {error && <p role="alert">{error}</p>}
    {result?.snapshot_error && <p role="alert">{result.snapshot_error}</p>}
    {result && <>
      {(busy || error) && result.cached && <p>以下是上一次检查结果，不代表当前状态。</p>}
      <dl>{Object.entries(result.metrics).map(([key, value]) => <div key={key}><dt>{metrics[key] ?? key}</dt><dd>{typeof value === "boolean" ? value ? "是" : "否" : typeof value === "number" && (key.includes("coverage") || key.includes("rate")) ? `${(value * 100).toFixed(1)}%` : ({ offline: "未连接", disabled: "已关闭", ready: "在线", warning: "异常" }[String(value)] ?? String(value ?? "待核实"))}</dd></div>)}</dl>
      <footer><p>{result.evidence}</p>{result.status !== "passed" && <strong>{result.next_action}</strong>}<p>检查于 {new Date(result.checked_at).toLocaleTimeString()}{result.cached ? " · 30 秒内缓存" : ""}</p></footer>
    </>}
    {!busy && <button className="quiet-button" onClick={() => setAttempt(value => value + 1)}>{error ? "重试此项" : "重新检查"}</button>}
  </article>;
}

export default function ReadinessCenter({ refreshKey = 0 }: { refreshKey?: number }) {
  return <>
    <p className="panel-note">各项独立检查，无需等待全部完成。这里不会自动启动服务、同步资料或调用模型；测试记录与自动评测不等于真实使用验收。</p>
    <section className="readiness-grid">{sections.map(([id, title]) => <CheckCard key={id} id={id} title={title} refreshKey={refreshKey} />)}</section>
  </>;
}
