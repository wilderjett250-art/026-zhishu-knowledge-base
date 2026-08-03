from dataclasses import dataclass

from pkas.agent import AgentService
from pkas.config import Settings, get_settings
from pkas.customer_repository import CustomerRepository
from pkas.customer_service import CustomerService
from pkas.customer_workflows import CustomerWorkflowService
from pkas.db import Database
from pkas.distillation import DistillationService
from pkas.ingest import IngestionService
from pkas.repository import Repository
from pkas.sync import SyncService
from pkas.weflow import WeFlowService
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
    customers: CustomerRepository
    customer_service: CustomerService
    weflow: WeFlowService
    customer_workflows: CustomerWorkflowService
    sync: SyncService

    @classmethod
    def create(cls, settings: Settings | None = None) -> "KnowledgeSystem":
        resolved_settings = settings or get_settings()
        database = Database(resolved_settings)
        repository = Repository(database)
        ingestion = IngestionService(resolved_settings, repository)
        workflows = WorkflowService(repository, ingestion)
        agent = AgentService(repository)
        distillation = DistillationService(resolved_settings, repository)
        customers = CustomerRepository(database)
        weflow = WeFlowService(
            settings=resolved_settings,
            customers=customers,
            ingestion=ingestion,
        )
        customer_service = CustomerService(customers=customers, knowledge=repository)
        customer_workflows = CustomerWorkflowService(repository=repository, weflow=weflow)
        sync = SyncService(
            settings=resolved_settings,
            database=database,
            repository=repository,
            ingestion=ingestion,
        )
        return cls(
            settings=resolved_settings,
            database=database,
            repository=repository,
            ingestion=ingestion,
            workflows=workflows,
            agent=agent,
            distillation=distillation,
            customers=customers,
            customer_service=customer_service,
            weflow=weflow,
            customer_workflows=customer_workflows,
            sync=sync,
        )
