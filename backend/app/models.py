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

