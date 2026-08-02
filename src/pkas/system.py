from dataclasses import dataclass

from pkas.agent import AgentService
from pkas.config import Settings, get_settings
from pkas.db import Database
from pkas.distillation import DistillationService
from pkas.ingest import IngestionService
from pkas.repository import Repository
from pkas.workflows import WorkflowService


@dataclass(slots=True)
class KnowledgeSystem:
    settings: Settings
    database: Database
    repository: Repository
    ingestion: IngestionService
    workflows: WorkflowService
    agent: AgentService
    distillation: DistillationService

    @classmethod
    def create(cls, settings: Settings | None = None) -> "KnowledgeSystem":
        resolved_settings = settings or get_settings()
        database = Database(resolved_settings)
        repository = Repository(database)
        ingestion = IngestionService(resolved_settings, repository)
        workflows = WorkflowService(repository, ingestion)
        agent = AgentService(repository)
        distillation = DistillationService(resolved_settings, repository)
        return cls(
            settings=resolved_settings,
            database=database,
            repository=repository,
            ingestion=ingestion,
            workflows=workflows,
            agent=agent,
            distillation=distillation,
        )
