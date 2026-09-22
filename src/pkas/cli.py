import json
import webbrowser
from dataclasses import asdict
from getpass import getpass
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from pkas.config import get_settings
from pkas.db import Database
from pkas.local_secrets import save_user_secret
from pkas.rpa_bridge import RpaBridgeConfigError, RpaBridgeService, require_loopback_bind_host
from pkas.system import KnowledgeSystem

app = typer.Typer(
    help="个人知识与智能协作系统",
    no_args_is_help=True,
    add_completion=False,
)


def print_json(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.command()
def init() -> None:
    """初始化本地数据库和私有数据目录。"""
    system = KnowledgeSystem.create()
    print_json({"status": "success", "health": system.database.health()})


@app.command("configure-embedding")
def configure_embedding() -> None:
    """在当前 Windows 用户下用 DPAPI 保存 Embedding 密钥，不写入源码或 .env。"""
    settings = get_settings()
    value = getpass("Paste the SiliconFlow API key (input is hidden): ").strip()
    if len(value) < 20:
        raise typer.BadParameter("API key is empty or too short")
    path = save_user_secret(settings, "embedding_api_key", value)
    print_json(
        {
            "status": "success",
            "summary": "Embedding API key encrypted for the current Windows user.",
            "secret_path": str(path),
        }
    )


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="监听地址，默认仅本机")] = None,
    port: Annotated[int | None, typer.Option(help="监听端口")] = None,
    reload: Annotated[bool, typer.Option(help="开发时自动重载")] = False,
    open_browser: Annotated[bool, typer.Option(help="启动后打开管理界面")] = False,
) -> None:
    """启动 API、管理界面和本地知识服务。"""
    settings = get_settings()
    resolved_host = host or settings.host
    resolved_port = port or settings.port
    try:
        resolved_host = require_loopback_bind_host(resolved_host)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--host") from exc
    if open_browser:
        browser_host = f"[{resolved_host}]" if ":" in resolved_host else resolved_host
        webbrowser.open(f"http://{browser_host}:{resolved_port}")
    uvicorn.run(
        "pkas.api:app",
        host=resolved_host,
        port=resolved_port,
        reload=reload,
    )


def rpa_bridge_service() -> RpaBridgeService:
    settings = get_settings()
    return RpaBridgeService(settings, Database(settings))


@app.command("rpa-bridge-provision")
def rpa_bridge_provision(
    yes: Annotated[bool, typer.Option("--yes", help="确认创建本机 RPA 令牌并启用接口")] = False,
    allow_restricted_context: Annotated[
        bool,
        typer.Option("--allow-restricted-context", help="允许 RPA 读取 restricted 资料"),
    ] = False,
    hybrid_search: Annotated[
        bool,
        typer.Option("--hybrid-search", help="启用向量检索；查询可能调用已配置的 Embedding 服务"),
    ] = False,
) -> None:
    """首次创建仅本机可用的 RPA HTTP 令牌，并只显示一次。"""
    if not yes:
        raise typer.BadParameter("必须添加 --yes 确认创建 RPA 本机桥令牌")
    try:
        token, status = rpa_bridge_service().provision(
            allow_restricted_context=allow_restricted_context,
            retrieval_mode="ab" if hybrid_search else "a",
        )
    except RpaBridgeConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_json(
        {
            "status": "success",
            "summary": "RPA 本机桥已启用；请立即将令牌保存到实在 Agent 的凭据字段。",
            "rpa_bearer_token": token,
            "token_display": "本令牌只会在本次创建命令中显示，之后不会从状态接口返回。",
            "bridge": status,
        }
    )


@app.command("rpa-bridge-enable")
def rpa_bridge_enable(
    yes: Annotated[bool, typer.Option("--yes", help="确认启用已配置的本机 RPA 接口")] = False,
    allow_restricted_context: Annotated[
        bool,
        typer.Option("--allow-restricted-context", help="允许 RPA 读取 restricted 资料"),
    ] = False,
    hybrid_search: Annotated[
        bool,
        typer.Option("--hybrid-search", help="启用向量检索；查询可能调用已配置的 Embedding 服务"),
    ] = False,
) -> None:
    """使用现有令牌重新启用本机 RPA HTTP 接口。"""
    if not yes:
        raise typer.BadParameter("必须添加 --yes 确认启用 RPA 本机桥")
    try:
        status = rpa_bridge_service().configure(
            enabled=True,
            allow_restricted_context=allow_restricted_context,
            retrieval_mode="ab" if hybrid_search else "a",
        )
    except RpaBridgeConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_json({"status": "success", "bridge": status})


@app.command("rpa-bridge-disable")
def rpa_bridge_disable(
    yes: Annotated[bool, typer.Option("--yes", help="确认停用本机 RPA 接口")] = False,
) -> None:
    """立即停用接口；已保护的令牌保留，以便后续恢复时不必重新配置。"""
    if not yes:
        raise typer.BadParameter("必须添加 --yes 确认停用 RPA 本机桥")
    status = rpa_bridge_service().configure(
        enabled=False,
        allow_restricted_context=False,
        retrieval_mode="a",
    )
    print_json({"status": "success", "bridge": status})


@app.command("rpa-bridge-rotate-token")
def rpa_bridge_rotate_token(
    yes: Annotated[bool, typer.Option("--yes", help="确认使旧令牌立即失效")] = False,
) -> None:
    """轮换泄露或遗失的 RPA 令牌；需要在实在 Agent 中更新凭据。"""
    if not yes:
        raise typer.BadParameter("必须添加 --yes 确认轮换 RPA 本机桥令牌")
    try:
        token = rpa_bridge_service().create_token(rotate=True)
    except RpaBridgeConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_json(
        {
            "status": "success",
            "summary": "旧 RPA 令牌已失效；请立即更新实在 Agent 的凭据字段。",
            "rpa_bearer_token": token,
        }
    )


@app.command("rpa-bridge-status")
def rpa_bridge_status() -> None:
    """读取本机 RPA 桥状态；不会输出令牌、聊天正文或资料路径。"""
    print_json(rpa_bridge_service().status())


@app.command("inspect")
def inspect_path(
    path: Annotated[Path, typer.Argument(help="要检查的绝对路径")],
    recursive: Annotated[bool, typer.Option(help="递归检查子目录")] = True,
) -> None:
    """只读检查导入范围，不复制文件。"""
    system = KnowledgeSystem.create()
    print_json(system.ingestion.inspect_path(str(path), recursive))


@app.command("ingest")
def ingest_path(
    path: Annotated[Path, typer.Argument(help="明确授权导入的绝对路径")],
    domain: Annotated[str, typer.Option(help="work、self、shared 或 distill")] = "work",
    privacy: Annotated[str, typer.Option(help="public、private 或 restricted")] = "private",
    recursive: Annotated[bool, typer.Option(help="递归导入子目录")] = True,
    yes: Annotated[bool, typer.Option("--yes", help="确认执行复制与索引")] = False,
) -> None:
    """导入明确路径中的资料并建立来源可追溯索引。"""
    if not yes:
        raise typer.BadParameter("必须添加 --yes 明确确认导入范围")
    system = KnowledgeSystem.create()
    print_json(
        system.workflows.run_import(
            path=str(path),
            recursive=recursive,
            domain=domain,
            privacy=privacy,
        )
    )


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="检索问题或关键词")],
    domain: Annotated[str | None, typer.Option(help="限定知识领域")] = None,
    limit: Annotated[int, typer.Option(help="返回数量")] = 10,
    include_restricted: Annotated[
        bool,
        typer.Option(help="允许返回 restricted 资料"),
    ] = False,
) -> None:
    """检索带原始来源定位的知识片段。"""
    system = KnowledgeSystem.create()
    print_json(
        asdict(system.retrieval.search(
            query,
            domain=domain,
            limit=limit,
            include_restricted=include_restricted,
        ))
    )


@app.command()
def stats() -> None:
    """查看资料、工作流、智能体和蒸馏数据概览。"""
    print_json(KnowledgeSystem.create().repository.stats())


@app.command("reindex-outdated")
def reindex_outdated(
    limit: Annotated[int, typer.Option(help="单次最多重建的旧版文档数量")] = 10_000,
    yes: Annotated[bool, typer.Option("--yes", help="确认重建派生索引")] = False,
    source_type: Annotated[
        list[str] | None,
        typer.Option("--source-type", help="仅重建指定资料类型，可重复填写"),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="允许重建当前版本的指定资料类型")
    ] = False,
) -> None:
    """使用当前解析器重建旧版派生文本和全文索引，不修改来源原件。"""
    if not yes:
        raise typer.BadParameter("必须添加 --yes 明确确认重建派生索引")
    selected_types = {item.strip().lower() for item in (source_type or []) if item.strip()}
    if force and not selected_types:
        raise typer.BadParameter("--force 必须与至少一个 --source-type 一起使用")
    print_json(
        KnowledgeSystem.create().ingestion.reindex_outdated(
            limit=limit,
            source_types=selected_types or None,
            force=force,
        )
    )


@app.command("weflow-exports")
def weflow_exports(
    records_path: Annotated[
        Path | None,
        typer.Option(help="weflow-export-records.json 绝对路径；默认自动定位"),
    ] = None,
    keyword: Annotated[str, typer.Option(help="客户名称或 wxid 过滤词")] = "",
) -> None:
    """只读列出 WeFlow 已导出的 XLSX，不需要 API 或密钥。"""
    result = KnowledgeSystem.create().weflow.discover_exports(
        records_path=str(records_path) if records_path else None,
        keyword=keyword,
    )
    print_json(result)


@app.command("weflow-export-inspect")
def weflow_export_inspect(
    session_ids: Annotated[list[str], typer.Argument(help="明确选择的导出会话 ID")],
    records_path: Annotated[
        Path | None,
        typer.Option(help="weflow-export-records.json 绝对路径；默认自动定位"),
    ] = None,
) -> None:
    """检查所选 WeFlow XLSX 的结构和哈希，不导入聊天。"""
    result = KnowledgeSystem.create().weflow.inspect_export_selection(
        records_path=str(records_path) if records_path else None,
        session_ids=session_ids,
    )
    print_json(result)


@app.command("weflow-export-import")
def weflow_export_import(
    session_ids: Annotated[list[str], typer.Argument(help="已检查的导出会话 ID")],
    inspection_token: Annotated[
        str,
        typer.Option(help="检查时返回的 64 位令牌"),
    ],
    records_path: Annotated[
        Path | None,
        typer.Option(help="weflow-export-records.json 绝对路径；默认自动定位"),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="确认导入所列会话")] = False,
) -> None:
    """导入已确认的 WeFlow XLSX 客户会话。"""
    if not yes:
        raise typer.BadParameter("必须添加 --yes 明确确认所列 WeFlow 导出会话")
    result = KnowledgeSystem.create().customer_workflows.import_weflow_exports(
        records_path=str(records_path) if records_path else None,
        session_ids=session_ids,
        inspection_token=inspection_token,
        privacy="restricted",
    )
    print_json(result)


@app.command("chatlab-inspect")
def chatlab_inspect(
    path: Annotated[Path, typer.Argument(help="WeFlow ChatLab JSON 绝对路径")],
    session_id: Annotated[str | None, typer.Option(help="无法自动识别时提供私聊 wxid")] = None,
) -> None:
    """只读检查 WeFlow ChatLab 文件。"""
    result = KnowledgeSystem.create().weflow.inspect_chatlab_file(
        str(path),
        session_id=session_id,
    )
    print_json(result)


@app.command("chatlab-import")
def chatlab_import(
    path: Annotated[Path, typer.Argument(help="已检查的 WeFlow ChatLab JSON")],
    inspection_token: Annotated[str, typer.Option(help="检查时返回的 64 位令牌")],
    session_id: Annotated[str | None, typer.Option(help="无法自动识别时提供私聊 wxid")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="确认导入为 restricted 客户聊天")] = False,
) -> None:
    """导入已确认的 WeFlow ChatLab 客户会话。"""
    if not yes:
        raise typer.BadParameter("必须添加 --yes 明确确认导入")
    result = KnowledgeSystem.create().customer_workflows.import_chatlab(
        path=str(path),
        inspection_token=inspection_token,
        session_id=session_id,
        privacy="restricted",
    )
    print_json(result)


@app.command()
def mcp() -> None:
    """通过标准输入输出启动 Codex MCP 知识工具。"""
    from pkas.mcp_server import main

    main()


if __name__ == "__main__":
    app()
