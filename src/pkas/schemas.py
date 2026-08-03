from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["success", "warning", "error"]
Domain = Literal["work", "self", "shared", "distill"]
Privacy = Literal["public", "private", "restricted"]


class Envelope(BaseModel):
    status: Status
    summary: str
    data: Any = None
    next_actions: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    domain: Domain | None = None
    limit: int = Field(default=10, ge=1, le=50)
    include_restricted: bool = False


class ImportInspectRequest(BaseModel):
    path: str = Field(min_length=3)
    recursive: bool = True


class ImportRunRequest(ImportInspectRequest):
    domain: Domain = "work"
    privacy: Privacy = "private"
    inspection_token: str = Field(min_length=64, max_length=64)


class CatalogSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    root_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class AgentContextRequest(BaseModel):
    task: str = Field(min_length=2, max_length=2000)
    domain: Domain | None = None
    limit: int = Field(default=8, ge=1, le=30)
    include_restricted: bool = False


class PersonaCandidateRequest(BaseModel):
    observation_type: str = Field(min_length=2, max_length=80)
    statement: str = Field(min_length=2, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "low"


class ReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str = Field(default="", max_length=1000)


class DistillationCandidateRequest(BaseModel):
    example_type: Literal["instruction", "preference", "decision", "conversation"]
    input_text: str = Field(min_length=2, max_length=12000)
    preferred_output: str = Field(min_length=2, max_length=20000)
    rejected_output: str | None = Field(default=None, max_length=20000)
    rationale: str = Field(default="", max_length=4000)
    source_ids: list[str] = Field(default_factory=list)
    privacy: Privacy = "restricted"


class DistillationExportRequest(BaseModel):
    approved_only: bool = True
    dataset_split: Literal["train", "validation", "test"] | None = None


class WeFlowExportDiscoverRequest(BaseModel):
    records_path: str | None = None
    keyword: str = Field(default="", max_length=200)
    limit: int = Field(default=500, ge=1, le=1000)


class WeFlowExportInspectRequest(BaseModel):
    records_path: str | None = None
    session_ids: list[str] = Field(min_length=1, max_length=1000)


class WeFlowExportImportRequest(WeFlowExportInspectRequest):
    inspection_token: str = Field(min_length=64, max_length=64)
    privacy: Privacy = "restricted"


class ChatLabInspectRequest(BaseModel):
    path: str = Field(min_length=3)
    session_id: str | None = Field(default=None, max_length=300)


class ChatLabImportRequest(ChatLabInspectRequest):
    inspection_token: str = Field(min_length=64, max_length=64)
    privacy: Privacy = "restricted"


class CustomerSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    customer_id: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    include_restricted: bool = False


class CustomerReplyContextRequest(BaseModel):
    customer_id: str
    task: str = Field(min_length=2, max_length=2000)
    recent_limit: int = Field(default=40, ge=1, le=200)
    search_limit: int = Field(default=20, ge=1, le=100)
    include_restricted: bool = False


class CustomerSignalRequest(BaseModel):
    customer_id: str
    signal_type: Literal[
        "requirement",
        "commitment",
        "todo",
        "risk",
        "decision",
        "follow_up",
        "preference",
    ]
    statement: str = Field(min_length=2, max_length=4000)
    status: Literal["open", "done", "cancelled"] = "open"
    due_at: str | None = Field(default=None, max_length=80)
    evidence_message_ids: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "low"


class CustomerUpdateRequest(BaseModel):
    company: str | None = Field(default=None, max_length=300)
    stage: Literal["lead", "active", "delivery", "after_sales", "paused", "closed"] | None = None
    tags: list[str] | None = Field(default=None, max_length=50)
    summary: str | None = Field(default=None, max_length=8000)
    review_status: Literal["candidate", "approved"] | None = None
