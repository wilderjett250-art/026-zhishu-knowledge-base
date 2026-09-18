import { FormEvent, useEffect, useRef, useState } from "react";
import "./foundation.css";

type Data = Record<string, any>;
export default function FoundationSearch() {
  const [query, setQuery] = useState("");
  const [files, setFiles] = useState(true);
  const [chats, setChats] = useState(false);
  const [restricted, setRestricted] = useState(false);
  const [rerank, setRerank] = useState("auto");
  const [result, setResult] = useState<Data | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function search(e: FormEvent) {
    e.preventDefault(); if (!files && !chats) {setError("请选择文件或聊天范围"); return;}
    controller.current?.abort(); const current = new AbortController(); controller.current = current;
    const timer = setTimeout(() => current.abort(), 30000);
    setBusy(true); setError(""); setResult(null);
    try {
      const response = await fetch("/api/foundation/search", { method:"POST", signal:current.signal,
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({query,
          scopes:[...(files ? ["files"] : []), ...(chats ? ["chats"] : [])],
          include_restricted:restricted, limit:10, rerank_mode:rerank, expand_parent:true}) });
      if (!response.ok) throw new Error(`本地搜索失败（${response.status}）`);
      const value = await response.json(); setResult(value.data);
    } catch (err) {setError(err instanceof Error && err.name === "AbortError" ? "搜索超时，请缩小关键词后重试" : String(err));}
    finally {clearTimeout(timer); setBusy(false);}
  }
  return <section className="foundation-panel">
    <h2>统一本机资料搜索</h2>
    <p>这是日常查资料的入口。它与后续 Codex 接入使用同一检索服务；这里不启动 MCP、不调用模型，也不会改动资料。检索实验和评测仍在下方单独区域。</p>
    <form onSubmit={search}>
      <div className="foundation-filters"><input aria-label="本地搜索关键词" value={query} onChange={e=>setQuery(e.target.value)} placeholder="输入项目名、需求关键词或文档里的内容" required maxLength={300} style={{minWidth:280,flex:1}}/><button disabled={busy || !query.trim() || (!files && !chats)}>{busy ? "搜索中…" : "搜索本机资料"}</button></div>
      <div className="foundation-search-scopes">
        <label><input type="checkbox" checked={files} onChange={e=>setFiles(e.target.checked)}/>文件内容</label>
        <label><input type="checkbox" checked={chats} onChange={e=>setChats(e.target.checked)}/>已导入聊天（含待归类）</label>
        <label><input type="checkbox" checked={restricted} onChange={e=>setRestricted(e.target.checked)}/>包含受限资料（不改变云端授权）</label>
        <label>重排<select aria-label="重排策略" value={rerank} onChange={e=>setRerank(e.target.value)}><option value="auto">按需</option><option value="never">不重排</option><option value="always">始终尝试</option></select></label>
      </div>
    </form>
    {error && <p role="alert">{error}</p>}
    {result && <div aria-label="本地搜索结果"><p>本次范围：{result.scopes.map((s:string)=>s === "files" ? "文件" : "聊天").join("、")}；受限资料{result.include_restricted ? "已包含" : "未包含"}；{result.elapsed_ms} ms。{result.note}</p>
      <p>实际模式：{result.mode} · 协议：{result.contract} · 结果充分性：{result.health?.sufficiency ?? "聊天未评估"}（不是准确率）</p>
      {result.warnings.map((w:string)=><p role="alert" key={w}>{w}</p>)}
      {Object.entries(result.groups).map(([scope, raw]) => {const group=raw as Data; return <div key={scope}><h3>{scope === "files" ? "文件命中" : "聊天命中"} · {group.results.length} 条</h3>
        {group.status === "error" ? <p>此通道读取失败，不能视为没有资料。</p> : !group.results.length ? <p>此范围未找到匹配内容。仅登记文件、未勾选的范围不会参与搜索。</p> : group.results.map((item:Data,i:number)=><article className="foundation-hit" key={i}>
          <strong>{displayTitle(item)}</strong>
          <div className="foundation-hit-meta"><span>{matchLabel(item)}</span><span>{item.source_type || (scope === "chats" ? "聊天记录" : "资料文件")}</span></div>
          <p>{item.snippet}</p><small>来源：{item.original_uri || item.source_uri || "来源路径未提供"}</small>
          <small>定位：{item.locator || (item.sent_at ? new Date(item.sent_at*1000).toLocaleString() : "定位信息未提供")}</small>
        </article>)}
      </div>;})}</div>}
  </section>;
}

function displayTitle(item: Data) {
  const raw = String(item.title || item.conversation_name || "").trim();
  const location = String(item.original_uri || item.source_uri || "").trim();
  const fallback = location.split(/[\\/]/).filter(Boolean).pop() || "来源片段";
  // Derived Markdown records may carry a generated UUID as their title. Keep
  // the source filename primary so users can recognize the underlying file.
  return raw && !/^[a-f0-9-]{28,}(?:\.[a-z0-9]{1,8})?$/i.test(raw) ? raw : fallback;
}

function matchLabel(item: Data) {
  const channels = Array.isArray(item.retrieval_channels) ? item.retrieval_channels : [];
  if (channels.includes("vector")) return "语义命中";
  if (channels.includes("fts")) return "全文命中";
  if (channels.includes("catalog")) return "目录命中";
  return item.match_strategy ? `匹配：${item.match_strategy}` : "本地命中";
}
