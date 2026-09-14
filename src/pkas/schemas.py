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
    rerank_mode: Literal["auto", "never", "always"] = "auto"
    expand_parent: bool = True
    retrieval_mode: Literal["a", "b", "ab"] = "ab"
    include_unverified_claims: bool = Field(
        default=False,
        description="兼容旧客户端；助手回答已不入库，因此该参数不改变检索范围。",
    )


class ClientConfigPreviewRequest(BaseModel):
    enabled: bool


class ClientConfigApplyRequest(ClientConfigPreviewRequest):
    preview_token: str = Field(min_length=64, max_length=64)
    confirmed: bool = False


class ClientConfigRollbackRequest(BaseModel):
    backup_id: str = Field(min_length=10, max_length=200)
    confirmed: bool = False


class McpProbeRequest(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64)
    confirmed: bool = False
    timeout_seconds: float = Field(default=12, ge=3, le=30)


class CapabilityProfileBindingRequest(BaseModel):
    asset_kind: Literal["skill", "mcp_server"]
    asset_id: str = Field(min_length=1, max_length=300)
    enabled: bool = True


class CapabilityProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    client_id: str | None = Field(default=None, max_length=120)
    status: Literal["active", "disabled"] = "active"
    knowledge_domains: list[Domain] = Field(default_factory=list, max_length=4)
    allowed_privacy: list[Privacy] = Field(
        default_factory=lambda: ["public", "private"], max_length=3
    )
    daily_input_token_budget: int = Field(default=0, ge=0, le=10_000_000)
    daily_output_token_budget: int = Field(default=0, ge=0, le=1_000_000)
    bindings: list[CapabilityProfileBindingRequest] = Field(default_factory=list, max_length=500)


class CapabilityProfileReplaceRequest(CapabilityProfileRequest):
    expected_revision: int = Field(ge=1)


class RagEvalCaseRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    expected_source_ids: list[str] = Field(min_length=1, max_length=50)
    domain: Domain | None = None
    include_restricted: bool = False
    tags: list[str] = Field(default_factory=list, max_length=20)
    match_policy: Literal["any", "all"] = "any"
    category: str = Field(default="general", min_length=2, max_length=50)
    difficulty: Literal["easy", "normal", "hard"] = "normal"
    review_status: Literal["draft", "reviewed"] = "reviewed"
    judgments: list["RagEvalJudgmentInput"] = Field(default_factory=list, max_length=50)
    judgment_scope_source_ids: list[str] = Field(default_factory=list, max_length=50)
    replace_expected: bool = False


class RagEvalJudgmentInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    relevance_grade: int = Field(ge=0, le=3)
    judgment_basis: Literal["human", "source-grounded", "expected-source"] = "human"


class RagEvalJudgmentRequest(BaseModel):
    judgments: list[RagEvalJudgmentInput] = Field(min_length=1, max_length=50)


class RagEvalReviewRequest(BaseModel):
    judgments: list[RagEvalJudgmentInput] = Field(min_length=1, max_length=50)
    judgment_scope_source_ids: list[str] = Field(min_length=1, max_length=50)
    category: str = Field(default="general", min_length=2, max_length=50)
    difficulty: Literal["easy", "normal", "hard"] = "normal"
    match_policy: Literal["any", "all"] = "any"


class RagEvalRejectRequest(BaseModel):
    reason_code: Literal[
        "ambiguous",
        "unanswerable",
        "duplicate",
        "bad_source",
        "out_of_scope",
    ]


class RagEvalRunRequest(BaseModel):
    top_k: int = Field(default=5, ge=1, le=20)
    rerank_mode: Literal["auto", "never", "always"] = "auto"
    retrieval_mode: Literal["a", "b", "ab"] = "ab"


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


class AgentRunRequest(BaseModel):
    task: str = Field(min_length=2, max_length=12000)
    workspace_path: str | None = Field(default=None, max_length=2000)
    domain: Domain | None = None
    include_restricted: bool = False
    persist_result: bool = False
    complexity: Literal["simple", "complex"] = "simple"


class AgentCloseoutRequest(BaseModel):
    source_id: str = Field(min_length=5, max_length=100)
    workspace_path: str | None = Field(default=None, max_length=2000)


class PersonaCandidateRequest(BaseModel):
    observation_type: str = Field(min_length=2, max_length=80)
    statement: str = Field(min_length=2, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "low"


class PersonalTimelineBuildRequest(BaseModel):
    local_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    provider: Literal["codex"] = "codex"
    model: str = Field(default="gpt-5.6-luna", min_length=2, max_length=100)


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


class WeFlowManualSyncRequest(BaseModel):
    weflow_root: str | None = Field(default=None, min_length=3, max_length=2000)
    records_path: str | None = Field(default=None, min_length=3, max_length=2000)


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
