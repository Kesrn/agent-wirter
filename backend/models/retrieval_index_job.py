"""检索索引 outbox 任务模型 —— PG 异步索引构建队列（阶段二）

``retrieval_index_jobs`` 是"写后投递"的 outbox：业务侧（如章节定稿
finalize）在 PG 模式下不直接同步构建检索索引，而是写一条 pending job，
由 :mod:`services.retrieval_index_worker` 的 ``poll_and_process_jobs`` 轮询
消费，调用 :mod:`services.retrieval_indexer` 的 build_* 纯构建函数完成投影
写入 + 指针切换。

同源合并策略（P0）：同源存在 pending job 时 enqueue 直接合并（UPDATE 该 job 的
source_version_id / payload 为最新请求，不新增）；同源存在 processing / done /
failed 时允许新增 pending（最新版本）。部分唯一索引 ``uq_retrieval_index_job_
active_source`` 只约束 (project_id, source_kind, source_id) 中 **status='pending'**
的行 —— 同源最多一条 pending，processing 不锁死（v1 处理中仍可入队 v2）。
并发 INSERT 竞态由该索引抛 IntegrityError，enqueue 捕获后读回合并。

- 状态机：pending → processing（记录 claimed_at）→ done | failed；
  失败时 attempts+1 且 error 落库；processing 且 claimed_at 超过超时阈值
  （默认 300s）的 job 会被 worker 重置回 pending（同源已有 pending 时记
  failed 收尾），用于进程崩溃/重启后的重新消费（幂等由 indexer 的
  content_hash + generation_id 保证）。
- SQLite 桌面端不使用本表（索引同步构建），但模型保持跨方言可建表，
  便于用内存 SQLite 覆盖 worker 逻辑的单元测试。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, TimestampMixin, UUIDMixin


class RetrievalIndexJob(UUIDMixin, TimestampMixin, Base):
    """一条待处理的检索索引构建任务。

    source_kind 与检索投影层枚举一致（chapter_version | source_chunk |
    character_profile | ability_profile | timeline_event | world_entry）。
    payload 携带结构化来源的构建入参（title/content/chapter_no 等），
    chapter_version / source_chunk 类 job 可在处理时从业务表直接读取。
    """

    __tablename__ = "retrieval_index_jobs"
    __table_args__ = (
        Index(
            "ix_retrieval_index_jobs_poll",
            "status", "created_at",
        ),
        # 防重复 pending 的 DB 层兜底：同一源最多一条 pending job（合并策略：
        # processing/done/failed 不锁死，允许入队最新版本）。逻辑层 enqueue
        # 先查后写 + 并发竞态下 IntegrityError 后读回合并。
        Index(
            "uq_retrieval_index_job_active_source",
            "project_id", "source_kind", "source_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source_version_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
