"""暗线模型

暗线是贯穿多个章节的隐藏线索。
K-3: 新增生命周期字段 (status/thread_type/planted_chapter/reveal_chapter/resolved_chapter/payoff_summary/risk_level)
"""

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class HiddenThread(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "hidden_threads"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_nums: Mapped[list | None] = mapped_column("chapter_nums", JSONValue(), nullable=True)

    # ── K-3: 伏笔生命周期 ──
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PLANNED", server_default="PLANNED", index=True
    )
    thread_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # FORESHADOWING / SECRET / RELATIONSHIP / WORLD_RULE
    planted_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reveal_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    resolved_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payoff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # LOW / MEDIUM / HIGH


# ── 状态优先级（数值越大越推进，用于只进不退）──
STATUS_PRIORITY = {
    "PLANNED": 0,
    "PLANTED": 1,
    "ACTIVE": 2,
    "REVEALED": 3,
    "RESOLVED": 4,
    "DROPPED": 99,
}
