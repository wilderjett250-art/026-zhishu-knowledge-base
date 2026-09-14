from dataclasses import asdict, dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from pkas.project_state import ProjectInspectionError, ProjectInspector
from pkas.repository import Repository
from pkas.retrieval import RetrievalService


class SearchKnowledgeInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    domain: str | None = None
    limit: int = Field(default=8, ge=1, le=50)
    include_restricted: bool = False


class SearchCatalogInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=8, ge=1, le=100)


class InspectProjectInput(BaseModel):
    workspace_path: str = Field(min_length=3, max_length=2000)


class ReadSourceInput(BaseModel):
    source_id: str = Field(min_length=5, max_length=100)


@dataclass(slots=True)
class AgentToolset:
    """LangChain tools bound to PKAS business services.

    Every tool returns the same operational envelope so graph nodes can make
    deterministic decisions without parsing prose.
    """

    repository: Repository
    inspector: ProjectInspector
    retrieval: RetrievalService | None = None
    search_knowledge: StructuredTool = field(init=False)
    search_source_catalog: StructuredTool = field(init=False)
    inspect_project_state: StructuredTool = field(init=False)
    read_source_document: StructuredTool = field(init=False)

    def __post_init__(self) -> None:
        self.search_knowledge = StructuredTool.from_function(
            func=self._search_knowledge,
            name="search_knowledge",
            description="Search indexed PKAS knowledge and return evidence with source locations.",
            args_schema=SearchKnowledgeInput,
        )
        self.search_source_catalog = StructuredTool.from_function(
            func=self._search_source_catalog,
            name="search_source_catalog",
            description="Locate files already cataloged under authorized synchronization roots.",
            args_schema=SearchCatalogInput,
        )
        self.inspect_project_state = StructuredTool.from_function(
            func=self._inspect_project_state,
            name="inspect_project_state",
            description="Read Git state from an already authorized local project workspace.",
            args_schema=InspectProjectInput,
        )
        self.read_source_document = StructuredTool.from_function(
            func=self._read_source_document,
            name="read_source_document",
            description="Read one indexed source document by its PKAS source identifier.",
            args_schema=ReadSourceInput,
        )

    def _search_knowledge(
        self,
        query: str,
        domain: str | None = None,
        limit: int = 8,
        include_restricted: bool = False,
    ) -> dict[str, Any]:
        retrieval_meta: dict[str, Any]
        if self.retrieval is not None:
            response = self.retrieval.search(
                query,
                domain=domain,
                limit=limit,
                include_restricted=include_restricted,
            )
            items = response.results
            retrieval_meta = {
                "mode": response.mode,
                "warnings": response.warnings,
                "health": asdict(response.health) if response.health else None,
            }
        else:
            items = self.repository.search(
                query,
                domain=domain,
                limit=limit,
                include_restricted=include_restricted,
            )
            retrieval_meta = {
                "mode": "fts_only_compatibility",
                "warnings": ["Unified retrieval service is unavailable."],
                "health": None,
            }
        artifacts = list(
            dict.fromkeys(str(item["original_uri"]) for item in items if item.get("original_uri"))
        )
        return {
            "status": "warning" if retrieval_meta["warnings"] else "success",
            "summary": f"Retrieved {len(items)} knowledge evidence items.",
            "next_actions": ["Use source identifiers and locations when synthesizing conclusions."],
            "artifacts": artifacts,
            "items": items,
            "retrieval": retrieval_meta,
        }

    def _search_source_catalog(self, query: str, limit: int = 8) -> dict[str, Any]:
        items = self.repository.search_source_catalog(query, limit=limit)
        artifacts = list(
            dict.fromkeys(str(item["source_uri"]) for item in items if item.get("source_uri"))
        )
        return {
            "status": "success",
            "summary": f"Located {len(items)} cataloged source files.",
            "next_actions": ["Read only a relevant indexed document when its content is required."],
            "artifacts": artifacts,
            "items": items,
        }

    def _inspect_project_state(self, workspace_path: str) -> dict[str, Any]:
        try:
            state = self.inspector.inspect(workspace_path)
        except ProjectInspectionError as exc:
            return {
                "status": "warning",
                "summary": str(exc),
                "next_actions": [
                    "Use an existing authorized sync root before inspecting the project."
                ],
                "artifacts": [],
                "state": {"status": "warning", "summary": str(exc)},
            }
        return {
            "status": "success",
            "summary": "Read the authorized project state.",
            "next_actions": [
                "Compare source changes, tests, and target-environment evidence separately."
            ],
            "artifacts": [workspace_path],
            "state": state,
        }

    def _read_source_document(self, source_id: str) -> dict[str, Any]:
        item = self.repository.source_document(source_id)
        if item is None:
            return {
                "status": "error",
                "summary": "The requested indexed source does not exist.",
                "next_actions": ["Refresh the source index or use a current source identifier."],
                "artifacts": [],
                "item": None,
            }
        artifacts = [str(item["original_uri"])] if item.get("original_uri") else []
        return {
            "status": "success",
            "summary": "Read the indexed source document.",
            "next_actions": [
                "Redact secrets and bound content before sending evidence to a model."
            ],
            "artifacts": artifacts,
            "item": item,
        }
