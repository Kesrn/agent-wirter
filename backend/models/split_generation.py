"""切分代次（P-1 阶段四：章节切分的版本化与原子切换）。

三张表构成章节切分的代际体系（方案 v3 §3.5）：

split_generations     一次切分代（legacy / building / ready / active / ...）
source_split_state    每个 source 的活动代指针（CAS 切换的唯一入口）
split_exclusions      代内的排除噪音区间（16 段 drop + 首部 preface）

关键设计：
- 章节行写入 split_generation_id，旧代不删除、至少保留上一代支持回滚；
- 活动指针用条件更新切换（必须传旧代，不能只用 IS NULL），并发下先到者生效；
- legacy 是迁移占位代：source_ordinal = 旧 chapter_no、display_chapter_no = NULL
  （旧数据无可靠展示号，标记待重切），不参与 active 切换竞态；
- status 枚举：building / validating / ready / active / failed / superseded / legacy。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin


class SplitGeneration(UUIDMixin, TimestampMixin, Base):
    """一次章节切分代。

    source_content_hash：切分基于的原文哈希，激活前必须与当前原文一致；
    decisions_hash：决策账本哈希（renumber/drop/keep_duplicate 结果固化）；
    decision_ledger：决策明细快照（16 drop / 7 renumber / 8 keep_duplicate）；
    validation_report：验收报告（地标/区间/不变量/全文完整性）。
    """

    __tablename__ = "split_generations"

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(50), nullable=False)
    decisions_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_ledger: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="building", index=True
    )
    validation_report: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SourceSplitState(Base):
    """每个 source 的活动切分代指针（一行一个 source，联合主键）。"""

    __tablename__ = "source_split_state"

    project_id: Mapped[str] = mapped_column(GUID(), primary_key=True)
    source_id: Mapped[str] = mapped_column(GUID(), primary_key=True)
    active_split_generation_id: Mapped[str | None] = mapped_column(
        GUID(), nullable=True, index=True
    )


class SplitExclusion(UUIDMixin, TimestampMixin, Base):
    """代内的排除噪音区间。

    kind：noise（drop 的伪标题/残片段）/ preface（首部站点尾巴）；
    ordinal_ref：noise 时对应的被排除章节 ordinal。
    """

    __tablename__ = "split_exclusions"

    split_generation_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # noise / preface
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ordinal_ref: Mapped[int | None] = mapped_column(Integer, nullable=True)
