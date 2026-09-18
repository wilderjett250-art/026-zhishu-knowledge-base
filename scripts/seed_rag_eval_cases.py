"""Seed source-located RAG questions as drafts; never self-approve generated gold data."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pkas.config import get_settings
from pkas.system import KnowledgeSystem


@dataclass(frozen=True)
class Seed:
    query: str
    source_suffix: str
    category: str
    difficulty: str = "normal"


SEEDS = [
    Seed(
        "本地 Java AI 资料固定保存了哪三个源码仓库？",
        "java-ai-materials_20260803/INDEX.md",
        "project",
        "easy",
    ),
    Seed(
        "Java AI 资料整理时明确排除了哪些视频类内容？",
        "java-ai-materials_20260803/INDEX.md",
        "project",
        "easy",
    ),
    Seed(
        "本地 Java AI 资料的源码仓库分别固定在哪些提交？",
        "java-ai-materials_20260803/INDEX.md",
        "project",
    ),
    Seed("RAG 搜索页如何区分图片直接命中和上下文关联？", "rag/components/SearchTab.vue", "code"),
    Seed("RAG 搜索页会展示图片证据的哪些元数据？", "rag/components/SearchTab.vue", "code"),
    Seed("RAG 调试搜索支持哪些重排和阈值参数？", "rag/components/SearchTab.vue", "code"),
    Seed(
        "RAG 搜索执行前为什么先调用 flushConfig？", "rag/components/SearchTab.vue", "code", "hard"
    ),
    Seed("知识片段页面如何加载、创建、修改和删除切片？", "rag/components/SlicesTab.vue", "code"),
    Seed(
        "知识片段页面支持哪两种查看模式和哪些文档筛选状态？", "rag/components/SlicesTab.vue", "code"
    ),
    Seed("切片详情会显示哪些来源和长度元数据？", "rag/components/SlicesTab.vue", "code"),
    Seed(
        "RAG 默认返回条数、进入重排条数和相似度阈值分别是多少？",
        "rag/composables/use-rag-config.ts",
        "code",
    ),
    Seed("RAG 配置支持哪两种融合策略？", "rag/composables/use-rag-config.ts", "code", "easy"),
    Seed("RAG 提示词要求参考资料没有答案时怎么处理？", "rag/composables/use-rag-config.ts", "code"),
    Seed(
        "RAG 配置如何防止频繁保存并规范化重排数量？",
        "rag/composables/use-rag-config.ts",
        "code",
        "hard",
    ),
    Seed("RAG 详情页包含哪些主要标签页？", "rag/detail.vue", "code", "easy"),
    Seed("从文档跳转查看切片时详情页如何传递文档筛选条件？", "rag/detail.vue", "code"),
    Seed(
        "创建知识库时如何同时约束 Embedding 模型和向量库的最大维度？",
        "rag/index.vue",
        "code",
        "hard",
    ),
    Seed("知识库创建页支持哪些切片模式？", "rag/index.vue", "code", "easy"),
    Seed("知识库创建页有哪些 Docling OCR 与表格结构选项？", "rag/index.vue", "code"),
    Seed("知识库的短片段合并、重叠长度和最大 Token 在哪里配置？", "rag/index.vue", "code"),
    Seed("知识库创建时如何加载 Embedding、聊天模型和向量库实例？", "rag/index.vue", "code", "hard"),
    Seed(
        "Skill 编辑器为什么只允许对 SKILL.md 使用 AI 生成和优化？",
        "skills/components/SkillEditor.vue",
        "code",
    ),
    Seed(
        "Skill 编辑器如何跟踪未保存文件内容？", "skills/components/SkillEditor.vue", "code", "hard"
    ),
    Seed("Skill 编辑器为什么禁止删除 SKILL.md？", "skills/components/SkillEditor.vue", "code"),
    Seed(
        "Skill 编辑器保存文件后如何处理 AI 返回的警告？",
        "skills/components/SkillEditor.vue",
        "code",
    ),
    Seed("Skill 管理页上传时为什么只接受 ZIP？", "skills/index.vue", "code", "easy"),
    Seed("Skill 管理页通过哪些接口完成列表、上传、下载和删除？", "skills/index.vue", "code"),
    Seed("Skill 下载时生成的本地 ZIP 文件名是什么格式？", "skills/index.vue", "code", "easy"),
    Seed(
        "技能仪表盘如何计算技能总数和格式化文件大小？", "home/modules/skills-dashboard.vue", "code"
    ),
    Seed(
        "技能仪表盘在没有数据时显示什么状态？", "home/modules/skills-dashboard.vue", "code", "easy"
    ),
    Seed("MCP 管理页如何加载列表、删除服务和测试连接？", "mcp/index.vue", "code"),
    Seed("MCP 管理页如何根据 transportType 展示连接地址？", "mcp/index.vue", "code"),
    Seed("MCP 管理页的分页查询支持哪些筛选参数？", "mcp/index.vue", "code"),
    Seed("MCP 详情页对 STDIO 传输展示哪些字段？", "mcp/modules/mcp-detail-drawer.vue", "code"),
    Seed(
        "MCP 详情页在什么条件下展示 HTTP Headers？",
        "mcp/modules/mcp-detail-drawer.vue",
        "code",
        "easy",
    ),
    Seed(
        "MCP 编辑器支持哪两种有效传输类型？", "mcp/modules/mcp-operate-drawer.vue", "code", "easy"
    ),
    Seed("MCP 编辑器如何解析 STDIO 环境变量文本？", "mcp/modules/mcp-operate-drawer.vue", "code"),
    Seed(
        "MCP 编辑器如何检查重复的 HTTP Header 名称？",
        "mcp/modules/mcp-operate-drawer.vue",
        "code",
        "hard",
    ),
    Seed(
        "MCP 编辑器提交时如何区分 HTTP URL 与 STDIO command？",
        "mcp/modules/mcp-operate-drawer.vue",
        "code",
    ),
    Seed("向量库实例列表如何执行分页、删除和状态更新？", "store/vector/index.vue", "code"),
    Seed("向量库实例页面编辑时会把哪些字段传给更新接口？", "store/vector/index.vue", "code"),
    Seed(
        "Snail AI Admin 项目 README 如何说明本地启动和构建方式？",
        "snail-ai-admin/README.md",
        "project",
    ),
    Seed(
        "向量库实例操作抽屉如何区分新增与编辑模式？",
        "store/vector/modules/vector-store-operate-drawer.vue",
        "code",
        "easy",
    ),
]


def main() -> None:
    system = KnowledgeSystem.create(get_settings())
    system.database.initialize()
    with system.database.connect() as connection:
        rows = connection.execute(
            """SELECT id, original_uri FROM sources
            WHERE status='indexed' AND source_type<>'codex-turn'"""
        ).fetchall()
    indexed = [(row["id"], str(row["original_uri"]).replace("\\", "/")) for row in rows]
    created = updated = 0
    missing: list[str] = []
    for seed in SEEDS:
        suffix = seed.source_suffix.replace("\\", "/")
        matches = [(source_id, uri) for source_id, uri in indexed if uri.endswith(suffix)]
        if len(matches) != 1:
            missing.append(f"{suffix}:{len(matches)}")
            continue
        result = system.rag.create_case(
            query=seed.query,
            expected_source_ids=[matches[0][0]],
            domain="work",
            include_restricted=False,
            tags=["source-located-seed", seed.category],
            category=seed.category,
            difficulty=seed.difficulty,
            review_status="draft",
        )
        updated += int(result.get("status") == "updated")
        created += int(result.get("status") != "updated")
    print(
        json.dumps(
            {
                "status": "completed" if not missing else "warning",
                "seed_count": len(SEEDS),
                "created": created,
                "updated": updated,
                "missing": missing,
                "review_status": "draft",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
