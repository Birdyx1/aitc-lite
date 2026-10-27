from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_serializer, field_validator, model_validator


class ProjectInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)


class TimestampOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_time(self, value: datetime) -> str:
        # SQLite 不保留时区；数据库约定统一存 UTC，响应显式附上时区。
        return value.replace(tzinfo=timezone.utc).isoformat()


class ProjectOutput(TimestampOutput):
    id: int
    name: str
    description: str


class RequirementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=200)
    content: str = Field(min_length=1, max_length=50000)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("需求标题不能为空")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("需求正文不能为空")
        # 正文是用户原文，保留空格、换行和缩进。
        return value


class RequirementSummary(TimestampOutput):
    id: int
    project_id: int
    title: str


class RequirementOutput(RequirementSummary):
    content: str


class GenerationSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_url: str = Field(default="", max_length=1000)
    model: str = Field(default="", max_length=200)
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    mock_mode: bool = False
    timeout_seconds: int = Field(default=20, ge=5, le=60)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value:
            return value
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment or any(c.isspace() for c in value)):
            raise ValueError("请填写不含凭据、查询参数或片段的 HTTP/HTTPS API 根地址")
        # 读取 port 也能校验非数字端口、超出范围等输入。
        _ = url.port
        value = value.rstrip("/")
        if url.path.rstrip("/").endswith("/chat/completions"):
            raise ValueError("请填写 API 根地址，不要包含 /chat/completions")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        key = value.get_secret_value().strip()
        if len(key) > 4096 or any(c.isspace() for c in key):
            raise ValueError("API Key 格式不正确")
        return SecretStr(key)

    @model_validator(mode="after")
    def validate_configuration(self):
        if not self.mock_mode and (not self.base_url or not self.model):
            raise ValueError("真实模式需要填写 API 根地址和模型名称")
        if self.clear_api_key and self.api_key and self.api_key.get_secret_value():
            raise ValueError("不能同时替换和清除 API Key")
        return self


class GenerationSettingsOutput(BaseModel):
    base_url: str
    model: str
    has_api_key: bool
    mock_mode: bool
    timeout_seconds: int
    configured: bool


class ConnectionTestOutput(BaseModel):
    mode: str
    message: str
    reply: str
    elapsed_ms: int


class FeatureFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    module: str = Field(min_length=1, max_length=100)
    feature: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    acceptance_criteria: str = Field(default="", max_length=4000)
    constraints: str = Field(default="", max_length=2000)
    priority: Literal["P0", "P1", "P2"] = "P1"


class FeatureItem(FeatureFields):
    id: UUID = Field(default_factory=uuid4)
    included: bool = True


class FeatureSaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: list[FeatureItem] = Field(max_length=50)
    source: Literal["manual", "mock", "real"] = "manual"
    model: str = Field(default="", max_length=200)
    confirmed: bool = False

    @model_validator(mode="after")
    def validate_items(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("功能点 ID 不能重复")
        if self.confirmed and not any(item.included for item in self.items):
            raise ValueError("至少选择一个功能点后才能确认")
        return self


class FeatureSheetOutput(BaseModel):
    revision: int
    current_hash: str
    source_hash: str
    source: Literal["manual", "mock", "real"]
    model: str
    confirmed: bool
    stale: bool
    items: list[FeatureItem]


class FeaturePreviewOutput(BaseModel):
    base_revision: int
    input_hash: str
    source: Literal["mock", "real"]
    model: str
    items: list[FeatureItem]


class CaseFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    priority: Literal["P0", "P1", "P2"]
    case_type: Literal["functional", "boundary", "exception"]
    is_smoke: bool
    precondition: str = Field(default="", max_length=2000)
    steps: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]] = Field(min_length=1, max_length=20)
    expected_result: str = Field(min_length=1, max_length=4000)


class CaseDraftOutput(CaseFields):
    id: UUID


class CaseGenerateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    feature_id: UUID
    expected_revision: int = Field(ge=1)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy: Literal["full", "quick"] = "full"
    skill_ids: list[Literal["api", "security"]] = Field(default_factory=list, max_length=2)

    @field_validator("skill_ids")
    @classmethod
    def valid_skills(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("专项不可重复")
        return sorted(value)

    use_rag: bool = Field(default=False, strict=True)
    rag_query: str = Field(default="", max_length=200)
    rag_top_k: int = Field(default=3, ge=1, le=5, strict=True)
    rag_rerank: bool = Field(default=False, strict=True)
    rag_allow_empty: bool = Field(default=False, strict=True)

    @field_validator("rag_query")
    @classmethod
    def valid_rag_query(cls, value):
        if any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("知识查询需为有效 Unicode")
        return value.strip()


class CaseGenerationSummary(BaseModel):
    id: UUID
    feature_name: str
    strategy: Literal["full", "quick"]
    source: Literal["real", "mock"]
    model: str
    case_count: int
    created_at: datetime
    outdated: bool
    skill_names: list[str] = Field(default_factory=list)

    @field_serializer("created_at")
    def serialize_time(self, value: datetime) -> str:
        return value.replace(tzinfo=timezone.utc).isoformat()


class CaseGenerationOutput(CaseGenerationSummary):
    sheet_revision: int
    input_hash: str
    feature_snapshot: FeatureItem
    requirement_snapshot: dict[str, str]
    prompt_version: str
    graph_nodes: list[str]
    cases: list[CaseDraftOutput]
    knowledge: dict | None = None
    skills: dict | None = None


class CaseReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=0)
    action: Literal["edit", "accept", "reject"]
    fields: CaseFields | None = None
    rejection_reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def check_action(self):
        if self.action == "edit" and self.fields is None:
            raise ValueError("编辑操作必须提供用例字段")
        if self.action != "edit" and self.fields is not None:
            raise ValueError("请先保存编辑，再进行采纳或驳回")
        if self.action == "reject" and not self.rejection_reason:
            raise ValueError("驳回原因不能为空")
        if self.action != "reject" and self.rejection_reason:
            raise ValueError("当前操作不接受驳回原因")
        return self


class CaseReviewOutput(BaseModel):
    draft_id: UUID
    revision: int
    status: Literal["pending", "accepted", "rejected"]
    original: CaseFields
    current: CaseFields
    was_edited: bool
    rejection_reason: str


class CaseReviewsOutput(BaseModel):
    generation_id: UUID
    items: list[CaseReviewOutput]


class AcceptedCaseOutput(CaseFields):
    id: UUID
    generation_id: UUID
    requirement_id: int
    requirement_title: str
    module: str
    feature_name: str
    source: Literal["real", "mock"]
    outdated: bool
    was_edited: bool


class CaseExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["xlsx", "markdown"]
    case_ids: list[UUID] = Field(min_length=1, max_length=1000)

    @field_validator("case_ids")
    @classmethod
    def unique_case_ids(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("用例 ID 不能重复")
        return value



class GenerationTaskOutput(BaseModel):
    skill_ids: list[str] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    requirement_id: int
    feature_name: str
    strategy: Literal["full", "quick"]
    source: Literal["real", "mock"]
    model: str
    status: Literal["queued", "running", "paused", "succeeded", "failed", "interrupted"]
    stage: str
    progress: int
    completed_nodes: list[str]
    error_message: str
    pause_requested: bool = False
    can_resume: bool = False
    requires_model_retry: bool = False
    requires_knowledge_retry: bool = False
    checkpoint_nodes: list[str] = Field(default_factory=list)
    resume_block_reason: str = ""
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def serialize_time(self, value: datetime) -> str:
        return value.replace(tzinfo=timezone.utc).isoformat()


class TaskResumeInput(BaseModel):
    retry_uncertain_knowledge_call: bool = Field(default=False, strict=True)
    model_config = ConfigDict(extra="forbid")
    retry_uncertain_model_call: bool = False


class KnowledgeDocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=200)
    content: str = Field(min_length=1, max_length=100000)
    source_filename: str = Field(default="", max_length=255)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("文档标题不能为空")
        return value

    @field_validator("title", "content", "source_filename")
    @classmethod
    def valid_text(cls, value):
        if any(0xD800 <= ord(char) <= 0xDFFF or ord(char) == 0 for char in value):
            raise ValueError("文本含无法保存的字符，请检查文件编码")
        return value

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value):
        if not value.strip():
            raise ValueError("文档正文不能为空")
        return value

    @field_validator("source_filename")
    @classmethod
    def filename_only(cls, value):
        # 仅保存来源标签；不按用户输入访问任何服务器文件。
        value = value.strip()
        if any(char in value for char in ('/', '\\')) or any(ord(char) < 32 for char in value):
            raise ValueError("来源名称只能是文件名，不能包含路径或控制字符")
        return value


class KnowledgeCreateInput(KnowledgeDocumentInput):
    document_id: UUID


class KnowledgeUpdateInput(KnowledgeDocumentInput):
    expected_revision: int = Field(ge=1)


class KnowledgeDocumentSummary(TimestampOutput):
    id: UUID
    project_id: int
    title: str
    source_filename: str
    revision: int
    char_count: int
    chunk_count: int
    chunker_version: str
    chunk_size: int
    chunk_overlap: int


class KnowledgeChunkOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_revision: int
    position: int
    start_offset: int
    end_offset: int
    content: str
    content_hash: str


class KnowledgeDocumentOutput(KnowledgeDocumentSummary):
    content: str
    content_hash: str
    chunks: list[KnowledgeChunkOutput]


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=5, ge=1, le=20, strict=True)

    @field_validator("query")
    @classmethod
    def valid_query(cls, value):
        value = value.strip()
        if not value or any(0xD800 <= ord(char) <= 0xDFFF or ord(char) == 0 for char in value):
            raise ValueError("检索词需为有效的非空文本")
        return value


class KnowledgeSearchHit(BaseModel):
    rank: int
    score: float
    matched_tokens: list[str]
    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_filename: str
    document_revision: int
    position: int
    start_offset: int
    end_offset: int
    content: str
    content_hash: str


class KnowledgeSearchOutput(BaseModel):
    project_id: int
    query: str
    top_k: int
    query_tokens: list[str]
    algorithm: str
    tokenizer: str
    k1: float
    b: float
    corpus_signature: str
    corpus_chunk_count: int
    indexed_chunk_count: int
    total_matches: int
    elapsed_ms: int
    status: Literal["matched", "no_matches", "no_terms", "empty_corpus", "no_indexable_chunks"]
    message: str
    hits: list[KnowledgeSearchHit]


class EmbeddingSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_url: str = Field(default="", max_length=1000)
    model: str = Field(default="", max_length=200)
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    mock_mode: bool = False
    timeout_seconds: int = Field(default=20, ge=5, le=60, strict=True)
    expected_dimensions: int | None = Field(default=None, ge=1, le=32768, strict=True)

    @field_validator("base_url")
    @classmethod
    def valid_root(cls, value):
        if "?" in value or "#" in value:
            raise ValueError("根地址不能包含查询或片段")
        value = GenerationSettingsInput.validate_base_url(value)
        if urlsplit(value).path.rstrip("/").endswith("/embeddings"):
            raise ValueError("填写根地址，不包含 /embeddings")
        return value

    @field_validator("base_url", "model")
    @classmethod
    def valid_plain_text(cls, value):
        if any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("配置文本含不可用字符")
        return value

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if value is None:
            return value
        key = value.get_secret_value().strip()
        if len(key) > 4096 or any(not 33 <= ord(c) <= 126 for c in key):
            raise ValueError("Key 需为无空白的 ASCII 文本")
        return SecretStr(key)

    @model_validator(mode="after")
    def valid_configuration(self):
        if not self.mock_mode and (not self.base_url or not self.model):
            raise ValueError("真实 Embedding 需要根地址及模型名称")
        if self.clear_api_key and self.api_key and self.api_key.get_secret_value():
            raise ValueError("不能同时替换和清除 Key")
        return self


class EmbeddingSettingsOutput(GenerationSettingsOutput):
    expected_dimensions: int | None


class EmbeddingTestOutput(BaseModel):
    mode: Literal["real", "mock"]
    message: str
    dimensions: int
    vector_count: int
    elapsed_ms: int


class RerankSettingsInput(GenerationSettingsInput):
    timeout_seconds: int = Field(default=20, ge=5, le=60, strict=True)

    @field_validator("base_url")
    @classmethod
    def valid_rerank_root(cls, value):
        value = EmbeddingSettingsInput.valid_root(value)
        if urlsplit(value).path.rstrip('/').endswith('/rerank'):
            raise ValueError('填写根地址，不包含 /rerank')
        return EmbeddingSettingsInput.valid_plain_text(value)

    @field_validator("model")
    @classmethod
    def plain_model(cls, value):
        return EmbeddingSettingsInput.valid_plain_text(value)

    @field_validator("api_key")
    @classmethod
    def ascii_key(cls, value):
        return EmbeddingSettingsInput.valid_key(value)


class RerankTestOutput(BaseModel):
    mode: Literal['real', 'mock']
    message: str
    document_count: int
    order: list[int]
    scores: list[float]
    elapsed_ms: int
