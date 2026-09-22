# 实在 Agent 本机知识库 HTTP 桥

这个桥把“实在 Agent 的 HTTP 请求节点”接到知域本机知识服务，而不是让 RPA 直连 SQLite。

```text
本机 SQLite / 文件索引 / 向量索引
        │
        ▼
知域本机 HTTP 桥（127.0.0.1，独立令牌，受限接口）
        │
        ▼
实在 Agent HTTP 请求节点 → LLM 回复草稿
```

它不是数据库代理：不支持 SQL、表名、原始文件路径、原文下载或任意文件读取。RPA 只能写入一条最新消息，或获得数量受限、带来源定位的检索依据。

## 安全边界

- 服务只接受 `127.0.0.1`、`::1` 或 `localhost` 的 TCP 客户端；命令行也拒绝 `0.0.0.0` 和局域网地址。即使有人绕过命令行直接以错误地址启动 Uvicorn，服务层也会在任何 API 路由前拒绝非回环客户端。
- 桥默认关闭。首次启用时生成独立 Bearer Token，并以当前 Windows 用户的 DPAPI 加密保存；状态接口永不返回令牌。
- RPA 消息一律作为 `restricted` 运行记录保存，不进入普通 FTS 或向量知识库。
- 检索结果不返回原始文件绝对路径、SQLite 内容或通用文档读取权限；标题、定位符和片段中的绝对本机路径都会替换掉。每条依据只保留标题、片段、来源 ID 和安全定位符。
- RPA 请求体上限为 32 KiB；无论是否声明或伪造 `Content-Length`，流式请求也会在进入业务逻辑前被拒绝。
- 默认仅用本地 FTS5（`A`）检索，不调用 Embedding/Rerank 服务。只有显式加 `--hybrid-search` 后才会启用向量检索；这可能把查询发送给已配置的 Embedding 服务。
- 默认不向 RPA 返回 `restricted` 资料，也不查询聊天。只有用户显式加 `--allow-restricted-context` 才会改变这一限制。
- 这能阻止局域网和公网调用；同一 Windows 用户权限下的恶意程序仍属于同一信任边界，不能靠本地 HTTP 服务完全防御。
- 实在 Agent 的 HTTP 节点必须实际运行在**这台电脑**上。若节点在云端执行，`127.0.0.1` 指向的是云端机器而不是你的电脑；不要为了连通而把本机端口映射到公网。

## 首次配置

先安装并正常打开最新版知域一次，让它完成本地运行环境准备。配置和使用期间让知域保持在托盘运行；从托盘“退出”后，本机知识服务也会停止。不要在 `.env`、工作流 JSON 或聊天正文中保存令牌。然后在自己的 PowerShell 运行安装包内的配置脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\知域\pkas-app\scripts\configure_rpa_loopback.ps1" -Action Provision
```

脚本会自动定位当前知域实际使用的数据目录，不需要手填 Python 路径或数据盘。命令只会显示一次 `rpa_bearer_token`。把它保存到实在 Agent 的**凭据/密钥字段**，不要放到普通节点文本、日志或截图中。

如果是在当前源码工作区联调而不是安装版，使用：

```powershell
$repoRoot = (Resolve-Path .).Path
& "$repoRoot\scripts\configure_rpa_loopback.ps1" -Action Provision -ProjectRoot $repoRoot
```

可选的高权限模式必须单独确认：

```powershell
# 允许受限资料，并使用混合检索；可能产生 Embedding API 调用。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\知域\pkas-app\scripts\configure_rpa_loopback.ps1" -Action Enable -AllowRestrictedContext -HybridSearch
```

状态、停用和令牌轮换：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\知域\pkas-app\scripts\configure_rpa_loopback.ps1" -Action Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\知域\pkas-app\scripts\configure_rpa_loopback.ps1" -Action Disable
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\知域\pkas-app\scripts\configure_rpa_loopback.ps1" -Action Rotate
```

轮换令牌后，旧令牌立即失效，需要同步更新实在 Agent 的凭据字段。

## 实在 Agent 画布配置

服务启动后，两个 HTTP 请求节点都使用同一个请求头：

```text
Authorization: Bearer <保存在实在 Agent 凭据字段中的令牌>
Content-Type: application/json
```

### 节点 1：记录最新消息

```text
POST http://127.0.0.1:8765/api/integrations/rpa/v1/messages
```

请求体模板（变量名按实在 Agent 画布实际字段替换）：

```json
{
  "conversation_key": "{{conversation_id}}",
  "source_event_id": "{{message_id}}",
  "sender_key": "{{sender_id}}",
  "message_at": "{{message_time}}",
  "text": "{{latest_message}}",
  "metadata": {
    "channel": "rpa"
  }
}
```

`source_event_id` 强烈建议传入消息唯一 ID；有它时重试不会重复写入。没有唯一 ID 时，桥会用会话、发送者、时间和消息内容生成保守的幂等键。

### 节点 2：检索回复依据

```text
POST http://127.0.0.1:8765/api/integrations/rpa/v1/search
```

```json
{
  "query": "{{latest_message}}",
  "scopes": ["files"],
  "limit": 5
}
```

把响应中的 `evidence` 传给 LLM 节点。LLM 提示词应明确：只根据 `evidence` 生成**回复草稿**；找不到依据时如实说明，不得声称项目完成、报价已确认或消息已经发送。

`/status` 用于连通性检查，同样必须带 Bearer Token：

```text
GET http://127.0.0.1:8765/api/integrations/rpa/v1/status
```

## 返回边界

`/search` 最多返回 5 条（配置上限 8 条）证据，每条最多约 1,200 个字符，并包含：

- `source_id`：可审计来源标识；
- `title` 和 `locator`：定位资料位置；
- `excerpt`：截断、脱敏后的局部上下文；
- `retrieval_channels` 和 `evidence_status`：命中渠道与证据状态。

它不会自动发送微信/客户消息。发送行为仍须由实在 Agent 画布的独立人工审核或发送节点控制。
