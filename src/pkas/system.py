from dataclasses import dataclass

from pkas.agent import AgentService
from pkas.backup_manager import BackupManager
from pkas.config import Settings, get_settings
from pkas.customer_repository import CustomerRepository
from pkas.customer_semantic import (
    CHAT_VECTOR_COLLECTION,
    ChatSummaryVectorIndex,
    CustomerSemanticIndex,
)
from pkas.customer_service import CustomerService
from pkas.customer_workflows import CustomerWorkflowService
from pkas.db import Database
from pkas.distillation import DistillationService
from pkas.embeddings import SiliconFlowEmbeddingProvider
from pkas.index_outbox import IndexOutbox
from pkas.ingest import IngestionService
from pkas.machine_catalog import MachineCatalog
from pkas.personal_timeline import PersonalTimelineService
from pkas.profile_service import CapabilityProfileService
from pkas.rag_observability import RagObservabilityService
from pkas.readiness import CoreReadinessService
from pkas.repository import Repository
from pkas.reranker import SiliconFlowReranker
from pkas.retrieval import RetrievalService
from pkas.sync import SyncService
from pkas.vector_index import QdrantVectorIndex
from pkas.weflow import WeFlowService
from pkas.weflow_manual import WeFlowManualSyncService
from pkas.workflows import WorkflowService


@dataclass(slots=True)
class KnowledgeSystem:
    settings: Settings
    database: Database
    repository: Repository
    retrieval: RetrievalService
    ingestion: IngestionService
    workflows: WorkflowService
    agent: AgentService
    distillation: DistillationService
    customers: CustomerRepository
    customer_service: CustomerService
    weflow: WeFlowService
    weflow_manual: WeFlowManualSyncService
    customer_workflows: CustomerWorkflowService
    sync: SyncService
    rag: RagObservabilityService
    outbox: IndexOutbox
    backup: BackupManager
    profiles: CapabilityProfileService
    readiness: CoreReadinessService
    machine_catalog: MachineCatalog
    personal_timeline: PersonalTimelineService
    customer_semantic: CustomerSemanticIndex

    @classmethod
    def create(cls, settings: Settings | None = None) -> "KnowledgeSystem":
        resolved_settings = settings or get_settings()
        database = Database(resolved_settings)
        # The full application owns startup initialization.  The repositories
        # retain their standalone-safe default, but must not each replay the
        # schema setup against the same SQLite connection path at startup.
        database.initialize()
        repository = Repository(database, initialize=False)
        embedding = SiliconFlowEmbeddingProvider(resolved_settings)
        vector_index = QdrantVectorIndex(
            resolved_settings,
            database,
            repository,
            embedding,
        )
        reranker = SiliconFlowReranker(resolved_settings)
        machine_catalog = MachineCatalog(resolved_settings)
        retrieval = RetrievalService(repository, vector_index, reranker, machine_catalog)
        ingestion = IngestionService(resolved_settings, repository)
        chat_index_settings = resolved_settings.model_copy(
            update={"qdrant_collection": CHAT_VECTOR_COLLECTION}
        )
        chat_embedding = SiliconFlowEmbeddingProvider(chat_index_settings)
        chat_vector_index = ChatSummaryVectorIndex(
            chat_index_settings,
            database,
            repository,
            chat_embedding,
        )
        customer_semantic = CustomerSemanticIndex(
            settings=resolved_settings,
            database=database,
            repository=repository,
            ingestion=ingestion,
            vector_index=chat_vector_index,
        )
        workflows = WorkflowService(repository, ingestion)
        agent = AgentService(repository, settings=resolved_settings, retrieval=retrieval)
        distillation = DistillationService(resolved_settings, repository)
        customers = CustomerRepository(database, initialize=False)
        weflow = WeFlowService(
            settings=resolved_settings,
            customers=customers,
            ingestion=ingestion,
        )
        customer_service = CustomerService(customers=customers, knowledge=repository)
        customer_workflows = CustomerWorkflowService(repository=repository, weflow=weflow)
        weflow_manual = WeFlowManualSyncService(
            database=database,
            customers=customers,
            weflow=weflow,
            customer_workflows=customer_workflows,
        )
        sync = SyncService(
            settings=resolved_settings,
            database=database,
            repository=repository,
            ingestion=ingestion,
            initialize=False,
        )
        rag = RagObservabilityService(database, vector_index, retrieval)
        outbox = IndexOutbox(database, vector_index)
        backup = BackupManager(resolved_settings)
        profiles = CapabilityProfileService(database)
        readiness = CoreReadinessService(
            database,
            rag,
            profiles,
            resolved_settings.client_home,
            resolved_settings.project_root / "reports",
            resolved_settings.data_root,
        )
        personal_timeline = PersonalTimelineService(database, resolved_settings)
        return cls(
            settings=resolved_settings,
            database=database,
            repository=repository,
            retrieval=retrieval,
            ingestion=ingestion,
            workflows=workflows,
            agent=agent,
            distillation=distillation,
            customers=customers,
            customer_service=customer_service,
            weflow=weflow,
            weflow_manual=weflow_manual,
            customer_workflows=customer_workflows,
            sync=sync,
            rag=rag,
            outbox=outbox,
            backup=backup,
            profiles=profiles,
            readiness=readiness,
            machine_catalog=machine_catalog,
            personal_timeline=personal_timeline,
            customer_semantic=customer_semantic,
        )
