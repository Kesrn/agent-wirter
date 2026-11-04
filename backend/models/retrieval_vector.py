"""向量检索投影模型 —— RAG 检索索引（阶段二）

``retrieval_vectors`` 是独立于业务表的检索投影层（决策：向量存储独立于业务表、
不强行迁 pgvector）：每行 = 一个 retrieval_chunks 块对应的 embedding 向量。

- 与 ``retrieval_chunks`` 通过 ``(project_id, chunk_id)`` 关联（UNIQUE 约束），
  chunk_id 存字符串（retrieval_chunks.id 是 GUID 类型，FTS 侧也是 VARCHAR）；
- ``embedding`` 用项目既有 FloatList TypeDecorator：PostgreSQL 下为
  ARRAY(Float)，SQLite 下为 JSON 文本；
- ``dim`` 记录向量维度（provider 维度变化时可据此兼容/重建）；
- ``content_hash`` 复制自 retrieval_chunks.content_hash（SHA-256），
  幂等重建的依据：块内容未变则跳过重算，块删除时清理对应向量行。

本表只被向量检索层（services/vector_retriever）读写，不参与业务表约束；
可由 ``index_project_vectors`` 随时重建，不是业务事实来源。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, FloatList, GUID, TimestampMixin, UUIDMixin


class RetrievalVector(UUIDMixin, TimestampMixin, Base):
    """检索块向量投影：chunk_id → embedding（每项目每 chunk 至多一行）。"""

    __tablename__ = "retrieval_vectors"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "chunk_id",
            name="uq_retrieval_vectors_project_chunk",
        ),
    )

    project_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    chunk_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    embedding: Mapped[list[float]] = mapped_column(FloatList(), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    # 幂等依据：与 retrieval_chunks.content_hash 一致时跳过重算
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
