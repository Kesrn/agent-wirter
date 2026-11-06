"""Evaluation dataset models."""

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, TimestampMixin, UUIDMixin


class EvaluationDataset(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_datasets"

    project_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="creative")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class EvaluationCase(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_cases"

    dataset_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False, default="creative_generation")
    input_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    actual_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_properties: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    rubric: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    tags: Mapped[list | None] = mapped_column(JSONValue(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class EvaluationRun(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_runs"

    dataset_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    generation_mode: Mapped[str] = mapped_column(String(30), nullable=False, default="generate_and_judge")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    model_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvaluationResult(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_results"

    run_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("evaluation_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generated_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    scores: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
