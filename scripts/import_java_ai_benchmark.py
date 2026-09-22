"""Import an explicitly selected Java AI/Skill/MCP benchmark corpus."""

import argparse
from pathlib import Path

from pkas.system import KnowledgeSystem

CURATED_PATHS = [
    "INDEX.md",
    "snail-ai-admin/README.md",
    "web_materials/feishu/JAVA-AI资料汇总_目录.txt",
    "web_materials/hr-recruitment-agent/architecture.json",
    "web_materials/hr-recruitment-agent/index.txt",
    "web_materials/java2ai-nacos-mcp/page.txt",
    "web_materials/processon/page.txt",
    "web_materials/snailai-mcp-docs/create.txt",
    "snail-ai-admin/src/views/home/modules/skills-dashboard.vue",
    "snail-ai-admin/src/views/mcp/index.vue",
    "snail-ai-admin/src/views/mcp/modules/mcp-detail-drawer.vue",
    "snail-ai-admin/src/views/mcp/modules/mcp-operate-drawer.vue",
    "snail-ai-admin/src/views/rag/index.vue",
    "snail-ai-admin/src/views/rag/detail.vue",
    "snail-ai-admin/src/views/rag/components/SearchTab.vue",
    "snail-ai-admin/src/views/rag/components/SlicesTab.vue",
    "snail-ai-admin/src/views/rag/composables/use-rag-config.ts",
    "snail-ai-admin/src/views/skills/index.vue",
    "snail-ai-admin/src/views/skills/components/SkillEditor.vue",
    "snail-ai-admin/src/views/store/vector/index.vue",
    "snail-ai-admin/src/views/store/vector/modules/vector-store-operate-drawer.vue",
]

GOLDEN_CASES = [
    ("MCP 管理页面如何搜索、查看和配置 MCP 服务？", "snail-ai-admin/src/views/mcp/index.vue"),
    ("RAG 知识库支持哪些检索和切片配置？", "snail-ai-admin/src/views/rag/index.vue"),
    (
        "RAG 的相似度、TopK、混合检索和去重参数在哪里配置？",
        "snail-ai-admin/src/views/rag/composables/use-rag-config.ts",
    ),
    (
        "Skill 编辑器如何管理技能文件？",
        "snail-ai-admin/src/views/skills/components/SkillEditor.vue",
    ),
    ("向量存储实例页面管理哪些字段？", "snail-ai-admin/src/views/store/vector/index.vue"),
    ("Nacos MCP 如何用于分布式服务注册？", "web_materials/java2ai-nacos-mcp/page.txt"),
    ("招聘 Agent 的系统架构包含哪些模块？", "web_materials/hr-recruitment-agent/architecture.json"),
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import the curated Java AI benchmark from an explicitly supplied directory."
    )
    parser.add_argument(
        "source_root",
        type=Path,
        help="Root directory containing the curated benchmark files.",
    )
    args = parser.parse_args()
    source_root = args.source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Benchmark source directory does not exist: {source_root}")

    system = KnowledgeSystem.create()
    system.database.initialize()
    source_ids: dict[str, str] = {}
    imported = 0
    reused = 0
    for relative in CURATED_PATHS:
        path = source_root / relative
        if not path.is_file():
            raise FileNotFoundError(path)
        result = system.ingestion.import_file(path, domain="work", privacy="private")
        source_ids[relative] = result["source_id"]
        if result.get("status") == "already_indexed":
            reused += 1
        else:
            imported += 1

    existing = {
        (item["query"], tuple(item["expected_source_ids"]))
        for item in system.rag.list_cases()
    }
    cases_created = 0
    for query, relative in GOLDEN_CASES:
        signature = (query, (source_ids[relative],))
        if signature in existing:
            continue
        system.rag.create_case(
            query=query,
            expected_source_ids=[source_ids[relative]],
            domain="work",
            include_restricted=False,
            tags=["java-ai-materials", "manual-curated"],
            review_status="draft",
        )
        cases_created += 1
    print(
        {
            "status": "completed",
            "selected_files": len(CURATED_PATHS),
            "imported": imported,
            "reused": reused,
            "golden_cases_created": cases_created,
        }
    )


if __name__ == "__main__":
    main()
