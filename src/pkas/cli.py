import json
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from pkas.config import get_settings
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
    if open_browser:
        webbrowser.open(f"http://{resolved_host}:{resolved_port}")
    uvicorn.run(
        "pkas.api:app",
        host=resolved_host,
        port=resolved_port,
        reload=reload,
    )


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
        system.repository.search(
            query,
            domain=domain,
            limit=limit,
            include_restricted=include_restricted,
        )
    )


@app.command()
def stats() -> None:
    """查看资料、工作流、智能体和蒸馏数据概览。"""
    print_json(KnowledgeSystem.create().repository.stats())


@app.command("weflow-check")
def weflow_check(
    access_token: Annotated[
        str,
        typer.Option(prompt="WeFlow Access Token", hide_input=True),
    ],
    base_url: Annotated[
        str,
        typer.Option(help="WeFlow 本地 API 地址"),
    ] = "http://127.0.0.1:5031",
) -> None:
    """检查 WeFlow 本地 API；Access Token 不会保存。"""
    result = KnowledgeSystem.create().weflow.check_connection(
        base_url=base_url,
        access_token=access_token,
    )
    print_json(result)


@app.command("weflow-sessions")
def weflow_sessions(
    access_token: Annotated[
        str,
        typer.Option(prompt="WeFlow Access Token", hide_input=True),
    ],
    base_url: Annotated[str, typer.Option(help="WeFlow 本地 API 地址")] = ("http://127.0.0.1:5031"),
    keyword: Annotated[str, typer.Option(help="客户名称或 wxid 过滤词")] = "",
) -> None:
    """只读列出 WeFlow 会话，不同步聊天正文。"""
    items = KnowledgeSystem.create().weflow.list_sessions(
        base_url=base_url,
        access_token=access_token,
        keyword=keyword,
    )
    print_json(items)


@app.command("weflow-sync")
def weflow_sync(
    session_ids: Annotated[list[str], typer.Argument(help="明确选择的一个或多个会话 ID")],
    access_token: Annotated[
        str,
        typer.Option(prompt="WeFlow Access Token", hide_input=True),
    ],
    base_url: Annotated[str, typer.Option(help="WeFlow 本地 API 地址")] = ("http://127.0.0.1:5031"),
    yes: Annotated[bool, typer.Option("--yes", help="确认同步所列会话")] = False,
) -> None:
    """增量同步明确选择的 WeFlow 客户会话。"""
    if not yes:
        raise typer.BadParameter("必须添加 --yes 明确确认所列微信会话")
    result = KnowledgeSystem.create().customer_workflows.sync_weflow(
        base_url=base_url,
        access_token=access_token,
        session_ids=session_ids,
        incremental=True,
        privacy="restricted",
        max_messages_per_session=50000,
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
