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


class CaseGenerationSummary(BaseModel):
    id: UUID
    feature_name: str
    strategy: Literal["full", "quick"]
    source: Literal["real", "mock"]
    model: str
    case_count: int
    created_at: datetime
    outdated: bool

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

