from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class Requirement(Base):
    __tablename__ = "requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True,
    )
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class GenerationSettings(Base):
    """单人本地版本仅维护一条生成模型配置（id=1）。"""

    __tablename__ = "generation_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    base_url: Mapped[str] = mapped_column(String(1000), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    mock_mode: Mapped[bool] = mapped_column(default=False)
    timeout_seconds: Mapped[int] = mapped_column(default=20)


class FeatureSheet(Base):
    __tablename__ = "feature_sheets"

    requirement_id: Mapped[int] = mapped_column(ForeignKey("requirements.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    source_hash: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(20), default="manual")
    model: Mapped[str] = mapped_column(String(200), default="")
    confirmed: Mapped[bool] = mapped_column(default=False)


class Feature(Base):
    __tablename__ = "features"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requirement_id: Mapped[int] = mapped_column(ForeignKey("feature_sheets.requirement_id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String(100))
    feature: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[str] = mapped_column(Text, default="")
    constraints: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(2), default="P1")
    included: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(default=0)


class CaseGeneration(Base):
    __tablename__ = "case_generations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requirement_id: Mapped[int] = mapped_column(ForeignKey("requirements.id", ondelete="CASCADE"), index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    sheet_revision: Mapped[int]
    feature_name: Mapped[str] = mapped_column(String(200))
    feature_snapshot: Mapped[dict] = mapped_column(JSON)
    requirement_snapshot: Mapped[dict] = mapped_column(JSON)
    strategy: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(10))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(40))
    graph_nodes: Mapped[list] = mapped_column(JSON)
    case_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class CaseDraft(Base):
    __tablename__ = "case_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation_id: Mapped[str] = mapped_column(ForeignKey("case_generations.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    priority: Mapped[str] = mapped_column(String(2))
    case_type: Mapped[str] = mapped_column(String(20))
    is_smoke: Mapped[bool]
    precondition: Mapped[str] = mapped_column(Text)
    steps: Mapped[list] = mapped_column(JSON)
    expected_result: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int]


class CaseReview(Base):
    __tablename__ = "case_reviews"

    draft_id: Mapped[str] = mapped_column(ForeignKey("case_drafts.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    edited_case: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    was_edited: Mapped[bool] = mapped_column(default=False)
    rejection_reason: Mapped[str] = mapped_column(String(1000), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)



class GenerationTask(Base):
    __tablename__ = "generation_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requirement_id: Mapped[int] = mapped_column(ForeignKey("requirements.id", ondelete="CASCADE"), index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    settings_signature: Mapped[str] = mapped_column(String(64))
    feature_name: Mapped[str] = mapped_column(String(200))
    strategy: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(10))
    model: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[int] = mapped_column(default=0)
    completed_nodes: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class GenerationTaskControl(Base):
    __tablename__ = "generation_task_controls"

    task_id: Mapped[str] = mapped_column(ForeignKey("generation_tasks.id", ondelete="CASCADE"), primary_key=True)
    workflow_version: Mapped[str] = mapped_column(String(64))
    pause_requested: Mapped[bool] = mapped_column(default=False)
    model_call_pending: Mapped[bool] = mapped_column(default=False)


class GraphCheckpoint(Base):
    __tablename__ = "graph_checkpoints"

    task_id: Mapped[str] = mapped_column(ForeignKey("generation_tasks.id", ondelete="CASCADE"), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(200), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint: Mapped[dict] = mapped_column(JSON)
    checkpoint_metadata: Mapped[dict] = mapped_column(JSON)


class GraphCheckpointWrite(Base):
    __tablename__ = "graph_checkpoint_writes"

    task_id: Mapped[str] = mapped_column(ForeignKey("generation_tasks.id", ondelete="CASCADE"), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(200), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    index: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(200))
    value: Mapped[dict | list | str | int | bool | None] = mapped_column(JSON)
    task_path: Mapped[str] = mapped_column(Text, default="")


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    source_filename: Mapped[str] = mapped_column(String(255), default="")
    revision: Mapped[int] = mapped_column(default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    char_count: Mapped[int]
    chunk_count: Mapped[int]
    chunker_version: Mapped[str] = mapped_column(String(40))
    chunk_size: Mapped[int]
    chunk_overlap: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    document_revision: Mapped[int]
    position: Mapped[int]
    start_offset: Mapped[int]
    end_offset: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))


class EmbeddingSettings(Base):
    __tablename__ = "embedding_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    base_url: Mapped[str] = mapped_column(String(1000), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    mock_mode: Mapped[bool] = mapped_column(default=False)
    timeout_seconds: Mapped[int] = mapped_column(default=20)
    expected_dimensions: Mapped[int | None] = mapped_column(nullable=True)


class VectorIndex(Base):
    __tablename__ = "vector_indexes"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    collection: Mapped[str] = mapped_column(String(100))
    corpus_signature: Mapped[str] = mapped_column(String(64))
    profile: Mapped[str] = mapped_column(String(64))
    dimensions: Mapped[int]
    chunk_count: Mapped[int]
    model: Mapped[str] = mapped_column(String(200))
    mock_mode: Mapped[bool]
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class VectorBuild(Base):
    __tablename__ = "vector_builds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    corpus_signature: Mapped[str] = mapped_column(String(64))
    profile: Mapped[str] = mapped_column(String(64))
    completed: Mapped[int] = mapped_column(default=0)
    total: Mapped[int]
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class RerankSettings(Base):
    __tablename__ = "rerank_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    base_url: Mapped[str] = mapped_column(String(1000), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    mock_mode: Mapped[bool] = mapped_column(default=False)
    timeout_seconds: Mapped[int] = mapped_column(default=20)


class GenerationKnowledge(Base):
    __tablename__ = "generation_knowledge"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requirement_id: Mapped[int] = mapped_column(ForeignKey("requirements.id", ondelete="CASCADE"), index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempt_pending: Mapped[bool] = mapped_column(default=False)
    case_citations: Mapped[dict] = mapped_column(JSON, default=dict)


class GenerationSkills(Base):
    __tablename__ = "generation_skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requirement_id: Mapped[int] = mapped_column(ForeignKey("requirements.id", ondelete="CASCADE"), index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[list] = mapped_column(JSON)
    case_tags: Mapped[dict] = mapped_column(JSON, default=dict)


class JudgeSettings(Base):
    __tablename__ = "judge_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    use_generation_model: Mapped[bool] = mapped_column(default=True)
    base_url: Mapped[str] = mapped_column(String(1000), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    api_key: Mapped[str] = mapped_column(Text, default="")
    mock_mode: Mapped[bool] = mapped_column(default=False)
    timeout_seconds: Mapped[int] = mapped_column(default=20)


class JudgeRun(Base):
    __tablename__ = "judge_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation_id: Mapped[str] = mapped_column(ForeignKey("case_generations.id", ondelete="CASCADE"), index=True)
    retry_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    input_hash: Mapped[str] = mapped_column(String(64))
    input_snapshot: Mapped[dict] = mapped_column(JSON)
    settings_signature: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict] = mapped_column(JSON)
    model_source: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(200))
    mock_mode: Mapped[bool]
    prompt_version: Mapped[str] = mapped_column(String(64))
    prompt_snapshot: Mapped[str] = mapped_column(Text)
    rubric_version: Mapped[str] = mapped_column(String(64))
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    model_call_pending: Mapped[bool] = mapped_column(default=False)
    elapsed_ms: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(80), default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AgentTurn(Base):
    __tablename__ = "agent_turns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    error_message: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(200))
    mock_mode: Mapped[bool]
    settings_signature: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    prompt_snapshot: Mapped[str] = mapped_column(Text)
    tools_snapshot: Mapped[list] = mapped_column(JSON)
    model_calls: Mapped[int] = mapped_column(default=0)
    tool_calls: Mapped[int] = mapped_column(default=0)
    model_call_pending: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("agent_turns.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int]
    source_id: Mapped[str] = mapped_column(String(20))
    native_id: Mapped[str] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    tool_name: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="running")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    elapsed_ms: Mapped[int | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class McpServer(Base):
    __tablename__ = 'mcp_servers'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    transport: Mapped[str] = mapped_column(String(20))
    endpoint: Mapped[str] = mapped_column(Text, default='')
    command: Mapped[str] = mapped_column(Text, default='')
    args: Mapped[list] = mapped_column(JSON, default=list)
    credentials: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    timeout_seconds: Mapped[int] = mapped_column(default=20)
    max_result_chars: Mapped[int] = mapped_column(default=12000)
    revision: Mapped[int] = mapped_column(default=1)
    tools: Mapped[list] = mapped_column(JSON, default=list)
    protocol_version: Mapped[str] = mapped_column(String(40), default='')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class McpBinding(Base):
    __tablename__ = 'mcp_bindings'
    project_id: Mapped[int] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), primary_key=True)
    server_id: Mapped[str] = mapped_column(ForeignKey('mcp_servers.id', ondelete='CASCADE'), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    server_revision: Mapped[int]
    tools: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentMcpContext(Base):
    __tablename__ = 'agent_mcp_contexts'
    turn_id: Mapped[str] = mapped_column(ForeignKey('agent_turns.id', ondelete='CASCADE'), primary_key=True)
    tools: Mapped[dict] = mapped_column(JSON, default=dict)


class McpInvocation(Base):
    __tablename__ = 'mcp_invocations'
    tool_call_id: Mapped[str] = mapped_column(ForeignKey('agent_tool_calls.id', ondelete='CASCADE'), primary_key=True)
    server_id: Mapped[str] = mapped_column(String(36))
    server_name: Mapped[str] = mapped_column(String(80))
    remote_tool: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(20))
    context_snapshot: Mapped[dict] = mapped_column(JSON)
    argument_hash: Mapped[str] = mapped_column(String(64))
    approval_token: Mapped[str] = mapped_column(String(36))
    decision: Mapped[str] = mapped_column(String(20), default='pending')
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatched: Mapped[bool] = mapped_column(default=False)


class ExecutionTask(Base):
    __tablename__ = 'execution_tasks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(100))
    environment: Mapped[str] = mapped_column(String(500))
    executor: Mapped[str] = mapped_column(String(80), default='')
    note: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(20), default='open')
    revision: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutionItem(Base):
    __tablename__ = 'execution_items'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey('execution_tasks.id', ondelete='CASCADE'), index=True)
    position: Mapped[int]
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default='not_run')
    actual_result: Mapped[str] = mapped_column(Text, default='')
    defect_note: Mapped[str] = mapped_column(Text, default='')
    revision: Mapped[int] = mapped_column(default=0)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutionResultEvent(Base):
    __tablename__ = 'execution_result_events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey('execution_items.id', ondelete='CASCADE'), index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int]
    status: Mapped[str] = mapped_column(String(20))
    actual_result: Mapped[str] = mapped_column(Text, default='')
    defect_note: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class EvaluationSample(Base):
    __tablename__ = 'evaluation_samples'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(default=1)
    content: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class EvaluationRun(Base):
    __tablename__ = 'evaluation_runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('projects.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(100))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='queued')
    config_snapshot: Mapped[dict] = mapped_column(JSON)
    config_signature: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[str] = mapped_column(String(64))
    error_message: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class EvaluationResult(Base):
    __tablename__ = 'evaluation_results'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('evaluation_runs.id', ondelete='CASCADE'), index=True)
    position: Mapped[int]
    sample_id: Mapped[str] = mapped_column(String(36))
    sample_snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default='queued')
    generation_status: Mapped[str] = mapped_column(String(20), default='not_run')
    judge_status: Mapped[str] = mapped_column(String(20), default='not_run')
    knowledge: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    cases: Mapped[list] = mapped_column(JSON, default=list)
    judge_input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    judge_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    matches: Mapped[list] = mapped_column(JSON, default=list)
    duplicate_findings: Mapped[list] = mapped_column(JSON, default=list)
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    model_call_pending: Mapped[bool] = mapped_column(default=False)
    error_message: Mapped[str] = mapped_column(Text, default='')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
