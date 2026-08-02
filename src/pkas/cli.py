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


@app.command()
def mcp() -> None:
    """通过标准输入输出启动 Codex MCP 知识工具。"""
    from pkas.mcp_server import main

    main()


if __name__ == "__main__":
    app()
