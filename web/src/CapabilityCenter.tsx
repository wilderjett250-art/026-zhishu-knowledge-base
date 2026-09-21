import { useEffect, useMemo, useState } from "react";
import { api, post, put } from "./api";

type Json = Record<string, any>;

type ProfileDraft = {
  id?: string;
  revision?: number;
  name: string;
  description: string;
  client_id: string;
  status: "active" | "disabled";
  knowledge_domains: string[];
  allowed_privacy: string[];
  daily_input_token_budget: number;
  daily_output_token_budget: number;
  bindings: Json[];
};

const emptyProfile = (): ProfileDraft => ({
  name: "",
  description: "",
  client_id: "",
  status: "active",
  knowledge_domains: ["work"],
  allowed_privacy: ["public", "private"],
  daily_input_token_budget: 0,
  daily_output_token_budget: 0,
  bindings: [],
});

function initialCapabilityTab(): "skills" | "mcp" | "profiles" | "clients" {
  const requested = new URLSearchParams(window.location.search).get("tab");
  return requested === "mcp" || requested === "profiles" || requested === "clients" ? requested : "skills";
}

function statusLabel(status: string) {
  return ({
    ready: "就绪",
    available: "可用",
    detected: "已发现",
    configured: "已配置",
    paused: "已暂停",
    planned: "待建设",
    warning: "需检查",
    unknown: "未知",
    not_detected: "未发现",
    offline: "离线",
  } as Record<string, string>)[status] ?? status;
}

export default function CapabilityCenter() {
  const [overview, setOverview] = useState<Json | null>(null);
  const [clientConfigs, setClientConfigs] = useState<Json[]>([]);
  const [profiles, setProfiles] = useState<Json[]>([]);
  const [error, setError] = useState("");
  const [activeTab, setActiveTab] = useState<"skills" | "mcp" | "profiles" | "clients">(initialCapabilityTab);
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState<Json | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [selectedAsset, setSelectedAsset] = useState<Json | null>(null);
  const [mcpPreview, setMcpPreview] = useState<Json | null>(null);
  const [mcpConfirmed, setMcpConfirmed] = useState(false);
  const [mcpResult, setMcpResult] = useState<Json | null>(null);
  const [profileDraft, setProfileDraft] = useState<ProfileDraft | null>(() => new URLSearchParams(window.location.search).get("profile") === "new" ? emptyProfile() : null);

  useEffect(() => {
    void Promise.all([
      api<Json>("/api/capabilities/overview"),
      api<Json[]>("/api/capabilities/clients/config"),
      api<Json[]>("/api/capabilities/profiles"),
    ])
      .then(([catalog, configs, profileItems]) => {
        setOverview(catalog.data);
        setClientConfigs(configs.data);
        setProfiles(profileItems.data);
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "能力状态读取失败"));
  }, []);

  const items = useMemo(() => {
    const all = activeTab === "clients"
      ? clientConfigs
      : activeTab === "profiles"
        ? profiles
        : overview?.[activeTab === "mcp" ? "mcp_servers" : activeTab] ?? [];
    const keyword = query.trim().toLowerCase();
    if (!keyword) return all;
    return all.filter((item: Json) => `${item.name} ${item.description ?? ""}`.toLowerCase().includes(keyword));
  }, [activeTab, clientConfigs, overview, profiles, query]);

  function editProfile(item: Json) {
    setProfileDraft({
      id: item.id,
      revision: item.revision,
      name: item.name,
      description: item.description ?? "",
      client_id: item.client_id ?? "",
      status: item.status ?? "active",
      knowledge_domains: item.knowledge_domains ?? [],
      allowed_privacy: item.allowed_privacy ?? [],
      daily_input_token_budget: item.daily_input_token_budget ?? 0,
      daily_output_token_budget: item.daily_output_token_budget ?? 0,
      bindings: item.bindings ?? [],
    });
    setNotice("");
  }

  function toggleList(field: "knowledge_domains" | "allowed_privacy", value: string) {
    if (!profileDraft) return;
    const current = profileDraft[field];
    setProfileDraft({
      ...profileDraft,
      [field]: current.includes(value) ? current.filter((item) => item !== value) : [...current, value],
    });
  }

  function toggleBinding(asset_kind: "skill" | "mcp_server", asset_id: string) {
    if (!profileDraft) return;
    const exists = profileDraft.bindings.some((item) => item.asset_kind === asset_kind && item.asset_id === asset_id);
    const bindings = exists
      ? profileDraft.bindings.filter((item) => !(item.asset_kind === asset_kind && item.asset_id === asset_id))
      : [...profileDraft.bindings, { asset_kind, asset_id, enabled: true }];
    setProfileDraft({ ...profileDraft, bindings });
  }

  async function saveProfile() {
    if (!profileDraft || !profileDraft.name.trim()) return;
    setBusy("profile-save");
    setNotice("");
    const payload = {
      ...profileDraft,
      client_id: profileDraft.client_id || null,
      bindings: profileDraft.bindings.map(({ asset_kind, asset_id, enabled }) => ({ asset_kind, asset_id, enabled })),
    };
    try {
      const result = profileDraft.id
        ? await put<Json>(`/api/capabilities/profiles/${profileDraft.id}`, { ...payload, expected_revision: profileDraft.revision })
        : await post<Json>("/api/capabilities/profiles", payload);
      const refreshed = await api<Json[]>("/api/capabilities/profiles");
      setProfiles(refreshed.data);
      editProfile(result.data);
      setNotice("能力组合已保存到本地策略库；没有修改客户端配置，也没有启动 MCP。");
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "能力组合保存失败");
    } finally {
      setBusy("");
    }
  }

  async function previewClient(item: Json) {
    const desired = !item.pkas_enabled;
    setBusy(item.id);
    setNotice("");
    try {
      const result = await post<Json>(`/api/capabilities/clients/${item.id}/config/preview`, { enabled: desired });
      setPreview(result.data);
      setConfirmed(false);
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "配置预览失败");
    } finally {
      setBusy("");
    }
  }

  async function applyPreview() {
    if (!preview || !confirmed) return;
    setBusy(preview.client_id);
    setNotice("");
    try {
      const result = await post<Json>(`/api/capabilities/clients/${preview.client_id}/config/apply`, {
        enabled: preview.desired_enabled,
        preview_token: preview.preview_token,
        confirmed: true,
      });
      const configs = await api<Json[]>("/api/capabilities/clients/config");
      setClientConfigs(configs.data);
      setNotice(`${result.summary}；MCP握手：${result.data.connection?.status ?? "无需执行"}`);
      setPreview(null);
      setConfirmed(false);
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "配置写入失败，系统已尝试回滚");
    } finally {
      setBusy("");
    }
  }

  async function previewMcp(item: Json) {
    setBusy(item.id);
    setNotice("");
    setMcpResult(null);
    try {
      const result = await api<Json>(`/api/capabilities/mcp/${item.client_id}/${item.id}/preview`);
      setMcpPreview(result.data);
      setMcpConfirmed(false);
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "MCP 探测预览失败");
    } finally {
      setBusy("");
    }
  }

  async function probeMcp() {
    if (!mcpPreview || !mcpConfirmed) return;
    setBusy(mcpPreview.id);
    try {
      const result = await post<Json>(`/api/capabilities/mcp/${mcpPreview.client_id}/${mcpPreview.id}/probe`, {
        preview_token: mcpPreview.preview_token,
        confirmed: true,
        timeout_seconds: mcpPreview.timeout_seconds,
      });
      setMcpResult(result.data);
      setNotice("MCP 能力读取完成，单次临时进程已回收。");
      setMcpPreview(null);
      setMcpConfirmed(false);
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "MCP 探测失败");
    } finally {
      setBusy("");
    }
  }

  if (error) return <div className="capability-error">{error}</div>;
  if (!overview) return <div className="capability-loading">正在读取本机能力目录…</div>;

  const summary = overview.summary ?? {};
  return <>
    <section className="capability-hero">
      <div>
        <small>能力管理</small>
        <h2>统一管理 AI 客户端能力</h2>
        <p>知识、向量、工作流、Skill 与 MCP 独立于客户端运行。Codex 是当前主要使用入口。</p>
      </div>
      <div className="capability-safety">
        <span><i /> 配置事务保护已启用</span>
        <strong>安全写入</strong>
        <small>先预览 · DPAPI备份 · 原子写入 · MCP握手 · 失败回滚</small>
      </div>
    </section>

    <section className="capability-metrics">
      <article><small>客户端</small><strong>{summary.detected_clients ?? 0}</strong><span>已发现客户端</span></article>
      <article><small>Skill</small><strong>{summary.skills ?? 0}</strong><span>可用能力</span></article>
      <article><small>MCP 服务</small><strong>{summary.mcp_servers ?? 0}</strong><span>{summary.enabled_mcp_servers ?? 0} 个启用</span></article>
      <article><small>能力组合</small><strong>{summary.profiles ?? profiles.length}</strong><span>可用策略</span></article>
    </section>

    <section className="capability-service-grid">
      {(overview.services ?? []).map((service: Json) => <article key={service.id}>
        <header><span className={`cap-status ${service.status}`} /> <small>{service.engine}</small></header>
        <h3>{service.name}</h3>
        <footer><b>{service.records?.toLocaleString?.() ?? service.records ?? "—"}</b><span className={`cap-chip ${service.status}`}>{statusLabel(service.status)}</span></footer>
      </article>)}
    </section>

    <section className="capability-layout">
      <div className="capability-catalog">
        <header>
          <div><small>能力清单</small><h2>能力目录</h2></div>
          <div className="capability-tabs">
            <button className={activeTab === "skills" ? "active" : ""} onClick={() => setActiveTab("skills")}>Skill</button>
            <button className={activeTab === "mcp" ? "active" : ""} onClick={() => setActiveTab("mcp")}>MCP 服务</button>
            <button className={activeTab === "profiles" ? "active" : ""} onClick={() => setActiveTab("profiles")}>能力组合</button>
            <button className={activeTab === "clients" ? "active" : ""} onClick={() => setActiveTab("clients")}>客户端</button>
          </div>
        </header>
        <div className="capability-filter"><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称或说明…" /></div>
        <div className="capability-list">
          {items.length ? items.map((item: Json) => <article key={item.id}>
            <span className="capability-icon">{activeTab === "skills" ? "技" : activeTab === "mcp" ? "接" : activeTab === "profiles" ? "组" : "端"}</span>
            <div><strong>{item.name}</strong><p>{item.description || item.config_hint || `${item.transport ?? item.kind ?? "local"} · ${item.scope ?? "user"}`}</p></div>
            {activeTab === "clients" ? <button
              className={`client-config-action ${item.pkas_enabled ? "enabled" : ""}`}
              disabled={busy === item.id || !item.config_valid}
              onClick={() => void previewClient(item)}
            >{busy === item.id ? "检查中…" : item.pkas_enabled ? "暂停 PKAS" : "接入 PKAS"}</button>
              : activeTab === "profiles" ? <button className="client-config-action" onClick={() => editProfile(item)}>编辑</button>
              : <div className="asset-actions"><span className={`cap-chip ${item.status}`}>{statusLabel(item.status)}</span><button onClick={() => { setSelectedAsset(item); setMcpResult(null); }}>详情</button></div>}
          </article>) : <div className="capability-empty">没有匹配的能力记录</div>}
        </div>
        {activeTab === "profiles" && <button className="profile-new" onClick={() => setProfileDraft(emptyProfile())}>＋ 新建能力组合</button>}
        {notice && <div className="client-config-notice">{notice}</div>}
      </div>

      <aside className="interface-rail">
        <header><small>客户端接入</small><h2>统一接入</h2></header>
        {(overview.interfaces ?? []).map((item: Json) => <article key={item.id}>
          <div><span className={`cap-status ${item.status}`} /><strong>{item.name}</strong></div>
          <code>{item.route}</code>
          <footer><small>{item.audience}</small><span className={`cap-chip ${item.status}`}>{statusLabel(item.status)}</span></footer>
        </article>)}
        <div className="architecture-note">
          <small>接入原则</small>
          <strong>客户端适配</strong>
          <p>所有客户端只通过适配器读取或写入配置；知识内核不感知 Codex、Cursor 或其他应用。</p>
        </div>
      </aside>
    </section>
    {profileDraft && <section className="profile-editor">
      <header><div><small>能力组合</small><h2>{profileDraft.id ? "编辑能力组合" : "新建能力组合"}</h2></div><button onClick={() => setProfileDraft(null)}>关闭</button></header>
      <div className="profile-form-grid">
        <label><span>名称</span><input value={profileDraft.name} onChange={(event) => setProfileDraft({ ...profileDraft, name: event.target.value })} /></label>
        <label><span>客户端</span><select value={profileDraft.client_id} onChange={(event) => setProfileDraft({ ...profileDraft, client_id: event.target.value })}><option value="">通用</option>{(overview.clients ?? []).map((item: Json) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label className="profile-wide"><span>说明</span><textarea value={profileDraft.description} onChange={(event) => setProfileDraft({ ...profileDraft, description: event.target.value })} /></label>
        <label><span>每日输入 Token</span><input type="number" min="0" value={profileDraft.daily_input_token_budget} onChange={(event) => setProfileDraft({ ...profileDraft, daily_input_token_budget: Number(event.target.value) })} /></label>
        <label><span>每日输出 Token</span><input type="number" min="0" value={profileDraft.daily_output_token_budget} onChange={(event) => setProfileDraft({ ...profileDraft, daily_output_token_budget: Number(event.target.value) })} /></label>
      </div>
      <div className="profile-policy-grid">
        <ProfileChoice title="知识域" values={["work", "self", "shared", "distill"]} selected={profileDraft.knowledge_domains} onToggle={(value) => toggleList("knowledge_domains", value)} />
        <ProfileChoice title="隐私范围" values={["public", "private", "restricted"]} selected={profileDraft.allowed_privacy} onToggle={(value) => toggleList("allowed_privacy", value)} />
      </div>
      <div className="profile-assets">
        <AssetPicker title="Skill" kind="skill" items={overview.skills ?? []} bindings={profileDraft.bindings} onToggle={toggleBinding} />
        <AssetPicker title="MCP 服务" kind="mcp_server" items={overview.mcp_servers ?? []} bindings={profileDraft.bindings} onToggle={toggleBinding} />
      </div>
      <footer><p>保存只更新本地能力组合，不会写入客户端配置、安装 Skill 或启动 MCP。</p><button disabled={!profileDraft.name.trim() || busy === "profile-save"} onClick={() => void saveProfile()}>{busy === "profile-save" ? "保存中…" : "保存能力组合"}</button></footer>
    </section>}
    {selectedAsset && <section className="asset-inspector">
      <header><div><small>{activeTab === "mcp" ? "MCP 服务详情" : "Skill 详情"}</small><h2>{selectedAsset.name}</h2></div><button onClick={() => { setSelectedAsset(null); setMcpResult(null); }}>关闭</button></header>
      {activeTab === "skills" ? <>
        <div className="asset-facts"><span>范围<strong>{selectedAsset.scope}</strong></span><span>校验<strong>{selectedAsset.validation}</strong></span><span>内容哈希<strong>{selectedAsset.content_hash_short}</strong></span><span>资源<strong>{selectedAsset.resource_count}</strong></span><span>大小<strong>{Number(selectedAsset.total_bytes ?? 0).toLocaleString()} B</strong></span></div>
        <div className="asset-resources"><small>资源清单</small>{(selectedAsset.resources ?? []).length ? <ul>{selectedAsset.resources.map((value: string) => <li key={value}>{value}</li>)}</ul> : <p>仅有 SKILL.md，没有附属资源。</p>}</div>
      </> : <>
        <div className="asset-facts"><span>客户端<strong>{selectedAsset.client_id}</strong></span><span>传输<strong>{selectedAsset.transport}</strong></span><span>参数数量<strong>{selectedAsset.argument_count}</strong></span><span>环境变量数量<strong>{selectedAsset.environment_variable_count}</strong></span><span>秘密值<strong>不返回</strong></span></div>
        <div className="mcp-inspector-actions"><p>配置字段：{(selectedAsset.config_keys ?? []).join(" · ") || "无"}。单次探测必须先预览并确认；不会保存命令、环境变量值或 Token。</p><button disabled={!selectedAsset.probe_supported || busy === selectedAsset.id} onClick={() => void previewMcp(selectedAsset)}>{busy === selectedAsset.id ? "读取中…" : selectedAsset.probe_supported ? "预览单次探测" : "暂不支持该传输"}</button></div>
        {mcpResult && <CapabilityResult result={mcpResult} />}
      </>}
    </section>}
    {mcpPreview && <section className="client-config-preview mcp-probe-preview">
      <header><div><small>单次 MCP 探测</small><h2>{mcpPreview.name} 探测确认</h2></div><button onClick={() => setMcpPreview(null)}>关闭</button></header>
      <div className="client-preview-meta"><span>传输：{mcpPreview.transport}</span><span>配置指纹：{mcpPreview.config_fingerprint}</span><span>超时：{mcpPreview.timeout_seconds}s</span><span>秘密值：不返回</span></div>
      <pre>{mcpPreview.action}</pre>
      <footer><label><input type="checkbox" checked={mcpConfirmed} onChange={(event) => setMcpConfirmed(event.target.checked)} /> 我同意启动该 MCP 一次并在读取能力后立即关闭</label><button disabled={!mcpConfirmed || Boolean(busy)} onClick={() => void probeMcp()}>{busy ? "探测并回收中…" : "确认探测"}</button></footer>
    </section>}
    {preview && <section className="client-config-preview">
      <header>
        <div><small>配置变更预览</small><h2>{preview.client_name} 配置差异</h2></div>
        <button onClick={() => setPreview(null)}>关闭</button>
      </header>
      <div className="client-preview-meta">
        <span>目标：{preview.desired_enabled ? "接入 PKAS" : "暂停 PKAS"}</span>
        <span>配置：{preview.config_hint}</span>
        <span>回滚：{preview.rollback_backup}</span>
        <span>验证：{preview.connection_test}</span>
      </div>
      <pre>{(preview.redacted_diff ?? []).join("\n") || "配置已经处于目标状态。"}</pre>
      <footer>
        <label><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> 我已核对脱敏差异，同意写入该客户端配置</label>
        <button disabled={!confirmed || Boolean(busy)} onClick={() => void applyPreview()}>{busy ? "写入并验证中…" : "原子写入并测试"}</button>
      </footer>
    </section>}
  </>;
}

function ProfileChoice({ title, values, selected, onToggle }: { title: string; values: string[]; selected: string[]; onToggle: (value: string) => void }) {
  return <section><small>{title}</small><div>{values.map((value) => <label key={value}><input type="checkbox" checked={selected.includes(value)} onChange={() => onToggle(value)} /> {value}</label>)}</div></section>;
}

function AssetPicker({ title, kind, items, bindings, onToggle }: { title: string; kind: "skill" | "mcp_server"; items: Json[]; bindings: Json[]; onToggle: (kind: "skill" | "mcp_server", id: string) => void }) {
  return <section><header><small>{title}</small><span>{bindings.filter((item) => item.asset_kind === kind).length} selected</span></header><div>{items.map((item) => { const checked = bindings.some((binding) => binding.asset_kind === kind && binding.asset_id === item.id); return <label key={item.id}><input type="checkbox" checked={checked} onChange={() => onToggle(kind, item.id)} /><span><strong>{item.name}</strong><small>{item.client_id ?? item.scope ?? "local"}</small></span></label>; })}</div></section>;
}

function CapabilityResult({ result }: { result: Json }) {
  return <div className="mcp-capability-result"><header><strong>{result.server_name}</strong><span>{result.protocol_version} · {result.tool_count} tools · {result.resource_count} resources · {result.prompt_count} prompts</span></header><div>{(result.tools ?? []).map((tool: Json) => <article key={tool.name}><strong>{tool.name}</strong><p>{tool.description || "无说明"}</p><code>{JSON.stringify(tool.input_schema)}</code></article>)}</div></div>;
}
