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
