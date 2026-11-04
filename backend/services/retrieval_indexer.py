"""检索索引写入服务（阶段一核心：LexicalRetriever 的索引侧）。

职责
----
把业务数据（素材切片 / 定稿章节 / 结构化知识）投影为 ``retrieval_chunks``
（检索块）并显式双写到 ``retrieval_chunks_fts``（SQLite FTS5 虚拟表 /
PostgreSQL 普通表，见 :func:`ensure_fts_table`），供 LexicalRetriever 只读检索。

架构决策（D1：指针切换，不逐行 active）
--------------------------------------
- 一次重建 = 一个 ``retrieval_index_generation``（status=building → active）。
- 重建完成后 ``finish_generation_active`` 在同一事务内：
    1. 物理删除旧活动 generation 的全部 retrieval_chunks + fts 行（同步双删）；
    2. 旧 generation 标记 superseded；
    3. 指针 retrieval_index_source_state.active_generation_id 原子切到新 generation。
  因此投影表里永远只保留"当前活动版本"的数据，检索侧无需按 generation 过滤。
- content_hash 幂等：同源内容未变化时跳过重建（返回 None）。

事务边界（P0 约定）
------------------
本模块**绝不 commit / 绝不 rollback 外层事务**：所有状态修改只 flush（或依赖
自动 flush），事务提交/回滚完全由调用方（路由层 / Worker / 脚本）统一控制。
理由：索引服务常被嵌入业务事务（如章节定稿 finalize_chapter_content），若在
内部 commit，会提前提交调用方尚未完成的业务数据（人工审核状态等）。
- SQLite 写入路径用 ``db.begin_nested()``（savepoint）隔离：索引写入失败时只
  回滚到 savepoint，外层业务事务不受影响，generation 标记 failed 后继续抛出。
- 索引侧写失败的 recovery 也在独立 savepoint 内，任何情况都不触碰外层事务。

双后端
------
- SQLite（桌面端/评测库）：FTS5 虚拟表 + bm25 排序；索引写入同事务同步完成，
  指针切换由 ``finish_generation_active`` 完成（调用方随后统一提交）。
- PostgreSQL（服务端）：本阶段先落 retrieval_index_generation(building) 记录 +
  块数据（由调用方事务统一提交），指针切换（finish_generation_active）由后续
  PG worker 异步接入调用（本文件所有函数在 PG 上同样可用，只是不自动切 active）。

表访问约定
----------
retrieval_chunks / retrieval_chunks_fts 是投影表，无 ORM 映射（FTS5 虚拟表
不能用 ORM create_all 创建，必须 :func:`ensure_fts_table` 显式建表），统一用
原生 SQL（``text()``）读写；ORM 模型仅用于 generation / source_state 两张管理表。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.chapter import Chapter
from models.chapter_version import ChapterVersion
from models.project_source import ProjectSource
from models.project_source_chunk import ProjectSourceChunk
from models.retrieval_index import (
    RetrievalIndexGeneration,
    RetrievalIndexSourceState,
)
from models.structured_knowledge import AbilityProfile, CharacterProfile, EventTimeline
from models.world_entry import WorldEntry
from services.knowledge_source import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, chunk_text
from services.retrieval_tokenizer import build_project_vocabulary, get_project_tokenizer

logger = logging.getLogger(__name__)

# 全项目统一 token 上限/批处理阈值（SQLite 变量数上限 999，批量删除时按块切分）
_DELETE_BATCH = 400

# FTS5 虚拟表（SQLite）。中文检索用默认 unicode61 分词：search_text 由
# 项目分词器把中文切成空格分隔的 token，查询侧同样处理，两侧一致即可命中。
FTS5_CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_chunks_fts USING fts5(
    chunk_id UNINDEXED, project_id UNINDEXED, source_kind UNINDEXED,
    source_id UNINDEXED, chapter_no UNINDEXED, title, content, search_text
)
"""

# PostgreSQL 没有 FTS5 虚拟表：本阶段用同构普通表 + to_tsvector('simple') 检索
# （见 lexical_retriever 的 PG 分支 docstring），写入 SQL 与 SQLite 完全一致。
PG_FTS_TABLE_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS retrieval_chunks_fts (
    chunk_id VARCHAR(100) NOT NULL,
    project_id VARCHAR(100) NOT NULL,
    source_kind VARCHAR(50) NOT NULL,
    source_id VARCHAR(100) NOT NULL,
    chapter_no INTEGER,
    title TEXT,
    content TEXT NOT NULL,
    search_text TEXT NOT NULL
)
"""

# 检索块表列（与 models.retrieval_index.RetrievalChunk 对齐；created_at/updated_at
# 有 server_default，INSERT 省略）。chapter_no/title 只存在 FTS 侧（DDL 契约）。
_CHUNK_COLUMNS = (
    "id, project_id, source_kind, source_id, source_version_id, "
    "index_generation_id, chunk_index, content, search_text, content_hash, metadata"
)
_FTS_COLUMNS = (
    "chunk_id, project_id, source_kind, source_id, chapter_no, title, content, search_text"
)


def _content_hash(text_: str) -> str:
    """内容指纹：同一内容的块不重复写入（幂等重建的依据）。"""
    return hashlib.sha256((text_ or "").encode("utf-8")).hexdigest()


def _dialect(db: AsyncSession) -> str:
    """当前会话绑定的数据库方言（sqlite / postgresql）。"""
    bind = db.bind
    return bind.dialect.name if bind is not None else "sqlite"


# ── FTS 虚拟表初始化 ─────────────────────────────────────

async def ensure_fts_table(conn) -> None:
    """建 FTS 索引表（幂等）。

    - SQLite：创建 FTS5 虚拟表 retrieval_chunks_fts（虚拟表无法靠 ORM
      create_all 创建，必须显式执行 DDL）。先查 sqlite_master 判断是否已存在，
      存在则直接返回，避免每次写入重复执行 DDL（in-memory 测试库每次新建，
      不能依赖模块级缓存）。
    - PostgreSQL：创建同构普通表（PG 无 FTS5，检索走 to_tsvector，见
      lexical_retriever 的 PG 分支注释）。

    兼容 AsyncSession（无 .dialect，经 .bind 取）与 AsyncConnection（有 .dialect）。
    """
    dialect = getattr(conn, "dialect", None)
    if dialect is None:
        bind = getattr(conn, "bind", None)
        dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", None) or "sqlite"
    if name == "postgresql":
        await conn.execute(text(PG_FTS_TABLE_CREATE_SQL))
    else:
        exists = await conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='retrieval_chunks_fts'"
            )
        )
        if exists.scalar():
            return
        await conn.execute(text(FTS5_CREATE_SQL))


# ── 原生 SQL 读写检索投影表 ──────────────────────────────

async def _insert_chunks(db: AsyncSession, rows: list[dict]) -> None:
    """批量写入 retrieval_chunks + retrieval_chunks_fts（显式双写，同一事务）。

    rows 每项需含：id/project_id/source_kind/source_id/source_version_id/
    index_generation_id/chunk_index/content/search_text/content_hash/metadata/
    chapter_no/title。
    """
    if not rows:
        return
    chunk_params = [
        {
            "id": r["id"],
            "project_id": r["project_id"],
            "source_kind": r["source_kind"],
            "source_id": r["source_id"],
            "source_version_id": r["source_version_id"],
            "index_generation_id": r["index_generation_id"],
            "chunk_index": r["chunk_index"],
            "content": r["content"],
            "search_text": r["search_text"],
            "content_hash": r["content_hash"],
            "metadata": json.dumps(r["metadata"], ensure_ascii=False) if r.get("metadata") else None,
        }
        for r in rows
    ]
    await db.execute(
        text(
            f"INSERT INTO retrieval_chunks ({_CHUNK_COLUMNS}) VALUES "
            "(:id, :project_id, :source_kind, :source_id, :source_version_id, "
            ":index_generation_id, :chunk_index, :content, :search_text, "
            ":content_hash, :metadata)"
        ),
        chunk_params,
    )
    fts_params = [
        {
            "chunk_id": r["id"],
            "project_id": r["project_id"],
            "source_kind": r["source_kind"],
            "source_id": r["source_id"],
            "chapter_no": r.get("chapter_no"),
            "title": r.get("title") or "",
            "content": r["content"],
            "search_text": r["search_text"],
        }
        for r in rows
    ]
    await db.execute(
        text(
            f"INSERT INTO retrieval_chunks_fts ({_FTS_COLUMNS}) VALUES "
            "(:chunk_id, :project_id, :source_kind, :source_id, :chapter_no, "
            ":title, :content, :search_text)"
        ),
        fts_params,
    )


async def _delete_generation_chunks(db: AsyncSession, generation_id: str) -> None:
    """删除某 generation 的全部检索块：retrieval_chunks + fts 行同步双删。"""
    ids = (
        await db.execute(
            text("SELECT id FROM retrieval_chunks WHERE index_generation_id = :g"),
            {"g": generation_id},
        )
    ).scalars().all()
    if ids:
        for start in range(0, len(ids), _DELETE_BATCH):
            # PG 下 retrieval_chunks.id 是 UUID 列，chunk_id 是 VARCHAR：
            # 绑定前统一 str()，避免 asyncpg DataError（SQLite 宽松不受影响）
            batch = [str(v) for v in ids[start : start + _DELETE_BATCH]]
            placeholders = ", ".join(f":p{i}" for i in range(len(batch)))
            params = {f"p{i}": v for i, v in enumerate(batch)}
            await db.execute(
                text(f"DELETE FROM retrieval_chunks_fts WHERE chunk_id IN ({placeholders})"),
                params,
            )
    await db.execute(
        text("DELETE FROM retrieval_chunks WHERE index_generation_id = :g"),
        {"g": generation_id},
    )


async def _active_generation_id(
    db: AsyncSession, project_id: str, source_kind: str, source_id: str
) -> str | None:
    """读当前活动 generation 指针（没有则 None）。"""
    state = (
        await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == project_id,
                RetrievalIndexSourceState.source_kind == source_kind,
                RetrievalIndexSourceState.source_id == source_id,
            )
        )
    ).scalar_one_or_none()
    return state.active_generation_id if state is not None else None


async def _existing_chunk_hashes(db: AsyncSession, generation_id: str) -> list[tuple[int, str]]:
    rows = (
        await db.execute(
            text(
                "SELECT chunk_index, content_hash FROM retrieval_chunks "
                "WHERE index_generation_id = :g ORDER BY chunk_index"
            ),
            {"g": generation_id},
        )
    ).all()
    return [(int(r[0]), str(r[1])) for r in rows]


# ── generation 生命周期（D1：指针切换） ──────────────────

async def start_generation(
    db: AsyncSession, project_id: str, source_kind: str, source_id: str
) -> str:
    """开启一次索引构建：写入 building 记录，返回 generation_id。"""
    generation_id = str(uuid.uuid4())
    db.add(
        RetrievalIndexGeneration(
            project_id=str(project_id),
            generation_id=generation_id,
            source_kind=source_kind,
            source_id=str(source_id),
            status="building",
            started_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()
    return generation_id


async def finish_generation_active(db: AsyncSession, generation_id: str) -> None:
    """把 generation 置 active 并原子切换指针（同一事务内完成）。

    步骤（全部在同一事务内，由调用方统一提交；本函数只 flush 不 commit）：
      1. 校验 generation 处于 building；
      2. 若该源已有活动指针且指向旧 generation：物理删除旧 generation 的
         retrieval_chunks + fts 行，并把旧 generation 标记 superseded；
      3. 新 generation 置 active；
      4. upsert retrieval_index_source_state.active_generation_id 指向新 generation。

    P0 说明：本函数不得调用 db.commit()——它可能运行在调用方（章节定稿 /
    HITL 审核）的业务事务内，内部 commit 会提前提交业务数据。
    """
    gen = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.generation_id == generation_id
            )
        )
    ).scalar_one_or_none()
    if gen is None:
        raise ValueError(f"generation 不存在: {generation_id}")
    if gen.status != "building":
        raise ValueError(
            f"generation {generation_id} 当前状态为 {gen.status!r}，只能从 building 切换到 active"
        )

    state = (
        await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == gen.project_id,
                RetrievalIndexSourceState.source_kind == gen.source_kind,
                RetrievalIndexSourceState.source_id == gen.source_id,
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if state is not None and state.active_generation_id != generation_id:
        old_gid = state.active_generation_id
        await _delete_generation_chunks(db, old_gid)
        old_gen = (
            await db.execute(
                select(RetrievalIndexGeneration).where(
                    RetrievalIndexGeneration.generation_id == old_gid
                )
            )
        ).scalar_one_or_none()
        if old_gen is not None and old_gen.status == "active":
            old_gen.status = "superseded"
            old_gen.finished_at = now
        state.active_generation_id = generation_id
    elif state is None:
        db.add(
            RetrievalIndexSourceState(
                project_id=gen.project_id,
                source_kind=gen.source_kind,
                source_id=gen.source_id,
                active_generation_id=generation_id,
            )
        )

    gen.status = "active"
    gen.finished_at = now
    await db.flush()


async def fail_generation(db: AsyncSession, generation_id: str, error) -> None:
    """标记 generation 失败，并清理该 generation 已写入的块（含 fts 行）。

    P0 说明：只 flush 不 commit，调用方统一提交（本函数通常运行在调用方业务
    事务内，commit 会提前提交业务数据）。调用方应保证在独立 savepoint 内调用，
    使失败处理自身失败时也不污染外层事务。
    """
    gen = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.generation_id == generation_id
            )
        )
    ).scalar_one_or_none()
    if gen is not None and gen.status == "building":
        await _delete_generation_chunks(db, generation_id)
        gen.status = "failed"
        gen.error = str(error)
        gen.finished_at = datetime.now(timezone.utc)
        await db.flush()
        logger.warning("generation %s 标记失败: %s", generation_id, error)


async def _finish_after_write(db: AsyncSession, generation_id: str) -> None:
    """写入完成后收尾：

    - SQLite：同一事务内 finish_generation_active（同步完成 + 指针切换）。
    - PostgreSQL：什么都不做（保留 building 状态；指针切换
      （finish_generation_active）由后续 PG worker 接入后异步调用）。
    两种后端的事务提交都由调用方统一控制，本函数不做 commit。
    """
    if _dialect(db) == "sqlite":
        await finish_generation_active(db, generation_id)


# ── 项目分词器（索引侧一次性构建，进程内缓存） ───────────

_vocab_lock = threading.RLock()
_vocab_built: set[str] = set()


async def _ensure_project_vocab(db: AsyncSession, project_id: str) -> None:
    """构建/填充项目分词器（词表内容不变则跳过，进程内缓存）。"""
    pid = str(project_id)
    with _vocab_lock:
        if pid in _vocab_built:
            return
    data = await build_project_vocabulary(db, pid)
    get_project_tokenizer(pid, data["version"])  # 替换同项目旧实例
    with _vocab_lock:
        _vocab_built.add(pid)


def clear_indexer_cache() -> None:
    """清空索引侧分词器缓存（测试隔离用）。"""
    with _vocab_lock:
        _vocab_built.clear()


def _tokenize_search_text(tok, content: str, title: str = "") -> str:
    """检索文本：分词后用空格连接（中文 token 化，供 FTS5 unicode61 逐词匹配）。

    title 的 token 追加进 search_text（FTS 的 title 列是原文，无法直接匹配中文）。
    """
    tokens = tok.tokenize(content or "")
    if title:
        tokens.extend(tok.tokenize(title))
    return " ".join(tokens)


def _base_chunk_rows(
    project_id: str,
    source_kind: str,
    source_id: str,
    generation_id: str,
    pieces: list[str],
    tok,
    *,
    source_version_id: str | None = None,
    title: str = "",
    chapter_no: int | None = None,
) -> list[dict]:
    """把切片列表转成检索块行（幂等哈希、search_text 一次算好）。"""
    rows: list[dict] = []
    for i, piece in enumerate(pieces):
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "source_version_id": source_version_id,
                "index_generation_id": generation_id,
                "chunk_index": i,
                "content": piece,
                "search_text": _tokenize_search_text(tok, piece, title),
                "content_hash": _content_hash(piece),
                "metadata": {"chapter_no": chapter_no} if chapter_no is not None else None,
                "chapter_no": chapter_no,
                "title": title,
            }
        )
    return rows


async def _rebuild_with_generation(
    db: AsyncSession,
    project_id: str,
    source_kind: str,
    source_id: str,
    pieces: list[str],
    *,
    source_version_id: str | None = None,
    title: str = "",
    chapter_no: int | None = None,
) -> str | None:
    """通用重建：幂等校验 → 开 generation → 双写块 → 收尾。

    返回新 generation_id；内容未变化（幂等跳过）返回 None。

    Savepoint 隔离（P0）：
    - ``ensure_fts_table`` 放 savepoint 外（DDL 幂等，避免嵌套事务 DDL 问题）；
    - building generation 记录写在外层事务（start_generation 在 savepoint 外），
      这样写入失败回滚 savepoint 后，generation 行仍然存在，可被
      fail_generation 标记 failed（审计记录不丢）；
    - 块双写 + 指针切换（``_insert_chunks`` + ``_finish_after_write``）在
      ``db.begin_nested()``（savepoint）内：写入失败只回滚到 savepoint，
      外层业务事务不受影响（D2：索引失败不回滚章节定稿）；
    - failed 标记在独立 savepoint 内调用 fail_generation：标记失败自身失败时
      也只回滚该 savepoint，不污染外层事务。本函数最终仍重新抛出原始异常，
      由调用方（chapter_save / rebuild_project 等）决定如何降级。
    """
    if not pieces:
        return None
    # 写入前确保 FTS 表存在（savepoint 外：DDL 幂等，避免嵌套事务 DDL 问题）
    await ensure_fts_table(db)
    pid = str(project_id)
    sid = str(source_id)
    new_hashes = [(i, _content_hash(p)) for i, p in enumerate(pieces)]

    active_gid = await _active_generation_id(db, pid, source_kind, sid)
    if active_gid is not None:
        existing = await _existing_chunk_hashes(db, active_gid)
        if existing == new_hashes:
            return None

    await _ensure_project_vocab(db, pid)
    tok = get_project_tokenizer(pid)
    generation_id = await start_generation(db, pid, source_kind, sid)
    rows = _base_chunk_rows(
        pid, source_kind, sid, generation_id, pieces, tok,
        source_version_id=source_version_id, title=title, chapter_no=chapter_no,
    )
    try:
        # savepoint：索引写入失败只回滚索引侧改动，外层业务事务不受影响
        async with db.begin_nested():
            await _insert_chunks(db, rows)
            await _finish_after_write(db, generation_id)
        return generation_id
    except Exception as exc:  # noqa: BLE001 — 写失败必须把 generation 标记 failed
        try:
            # failed 标记独立 savepoint：fail 自身失败也不影响外层事务
            async with db.begin_nested():
                await fail_generation(db, generation_id, str(exc))
        except Exception:  # pragma: no cover — 事务已坏时 fail 也会失败，仅告警
            logger.warning("fail_generation 未能落库（外层事务不受影响）: %s", exc)
        raise


# ── 各来源索引（纯构建 build_* / 请求入口 request_* 拆分） ──
#
# P0 约定：build_* 是纯构建（不判断方言、不入队、不 commit，事务由调用方控制）；
# request_* 是业务侧请求入口：PG 方言 → enqueue_index_job 写 outbox（由 Worker
# 异步消费构建）；SQLite 方言（桌面端，不启动 Worker）→ 直接同步构建。

async def build_source_index(db: AsyncSession, project_id: str, source_id: str) -> str | None:
    """把 ProjectSourceChunk 投影到检索索引（source_kind="source_chunk"）。

    ProjectSourceChunk 已按资料原文切片，这里逐块投影（不重新切片），
    chunk_index / content 原样保留；title 取所属 ProjectSource.title。
    幂等：与活动 generation 的 (chunk_index, content_hash) 完全一致时返回 None。
    """
    pid = str(project_id)
    sid = str(source_id)
    chunks = (
        await db.execute(
            select(ProjectSourceChunk)
            .where(
                ProjectSourceChunk.project_id == pid,
                ProjectSourceChunk.source_id == sid,
            )
            .order_by(ProjectSourceChunk.chunk_index)
        )
    ).scalars().all()
    if not chunks:
        return None
    source = (
        await db.execute(
            select(ProjectSource).where(ProjectSource.id == sid, ProjectSource.project_id == pid)
        )
    ).scalar_one_or_none()
    title = source.title if source is not None else ""

    new_hashes = [(int(c.chunk_index), _content_hash(c.content or "")) for c in chunks]
    active_gid = await _active_generation_id(db, pid, "source_chunk", sid)
    if active_gid is not None:
        existing = await _existing_chunk_hashes(db, active_gid)
        if existing == new_hashes:
            return None

    await _ensure_project_vocab(db, pid)
    tok = get_project_tokenizer(pid)
    generation_id = await start_generation(db, pid, "source_chunk", sid)
    rows = [
        {
            "id": str(uuid.uuid4()),
            "project_id": pid,
            "source_kind": "source_chunk",
            "source_id": sid,
            "source_version_id": None,
            "index_generation_id": generation_id,
            "chunk_index": int(c.chunk_index),
            "content": c.content or "",
            "search_text": _tokenize_search_text(tok, c.content or "", title),
            "content_hash": _content_hash(c.content or ""),
            "metadata": None,
            "chapter_no": None,
            "title": title,
        }
        for c in chunks
    ]
    try:
        # savepoint：索引写入失败只回滚索引侧改动，外层业务事务不受影响
        async with db.begin_nested():
            await _insert_chunks(db, rows)
            await _finish_after_write(db, generation_id)
        return generation_id
    except Exception as exc:  # noqa: BLE001
        try:
            # failed 标记独立 savepoint：fail 自身失败也不影响外层事务
            async with db.begin_nested():
                await fail_generation(db, generation_id, str(exc))
        except Exception:  # pragma: no cover
            logger.warning("fail_generation 未能落库（外层事务不受影响）: %s", exc)
        raise


async def build_chapter_index(
    db: AsyncSession, project_id: str, version_id: str
) -> str | None:
    """纯构建：索引定稿章节版本（source_kind="chapter_version"）。

    读 ChapterVersion.content → chunk_text（1200 字/200 重叠，复用 knowledge_source
    的结构感知切片）→ 双写。chapter_no / title 取自对应 Chapter
    （chapter_no = Chapter.sequence_number，即第 N 章）。返回新 generation_id；
    内容未变化（幂等跳过）返回 None。

    稳定来源标识（P0）：
    - ``source_id = chapter.id``（章节稳定标识），``source_version_id = version.id``
      （本次定稿版本）。同一章节再次定稿（新 version.id）时，content_hash 变化
      → ``_rebuild_with_generation`` 以同 source_id 开新 generation，指针切换时
      物理删除旧 generation 的块，检索只会召回最新版正文（不会新旧并存）。
    - 幂等：同一章节内容未变时跳过（返回 None）。
    - 防御：version 找不到对应 Chapter 时无法生成稳定 source_id，logger.warning
      并跳过本次索引（返回 None，索引可重建，定稿流程不受影响）。

    本函数只构建、不判断方言、不入队（拆自原 index_chapter_version 的构建主体；
    入队/方言分发见 :func:`request_chapter_index`）。
    """
    pid = str(project_id)
    vid = str(version_id)
    version = (
        await db.execute(select(ChapterVersion).where(ChapterVersion.id == vid))
    ).scalar_one_or_none()
    if version is None or not (version.content or "").strip():
        return None

    chapter: Chapter | None = None
    try:
        chapter = (
            await db.execute(select(Chapter).where(Chapter.id == version.chapter_id))
        ).scalar_one_or_none()
    except Exception:  # pragma: no cover — chapters 表缺行/缺列时降级
        logger.warning("读取 Chapter %s 失败，跳过章节索引", version.chapter_id)
    if chapter is None:
        logger.warning(
            "ChapterVersion %s 无对应 Chapter（chapter_id=%s），跳过章节索引",
            vid, version.chapter_id,
        )
        return None

    chapter_no = chapter.sequence_number
    title = chapter.title

    pieces = chunk_text(version.content, DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP)
    return await _rebuild_with_generation(
        db, pid, "chapter_version", str(chapter.id), pieces,
        source_version_id=vid, title=title, chapter_no=chapter_no,
    )


async def request_chapter_index(
    db: AsyncSession, project_id: str, version_id: str
) -> None:
    """章节定稿（finalize）的检索索引请求入口（P0 闭环）。

    - PostgreSQL（服务端）：改写 retrieval_index_jobs outbox job
      （source_id=chapter.id 与同步路径的稳定来源标识一致，source_version_id=version.id
      供 worker 定位要索引的版本），由 services.retrieval_index_worker 轮询消费后
      异步完成构建与指针切换（D2 闭环）。返回 None，调用方（finalize 等）无需
      感知方言差异。
    - SQLite（桌面端）：直接 ``await build_chapter_index(...)`` 同步构建
      （与调用方同一事务，桌面端不启动 Worker）。
    """
    if _dialect(db) == "postgresql":
        from services.retrieval_index_worker import enqueue_index_job  # 延迟导入避免循环依赖
        pid = str(project_id)
        vid = str(version_id)
        version = (
            await db.execute(select(ChapterVersion).where(ChapterVersion.id == vid))
        ).scalar_one_or_none()
        if version is None or not (version.content or "").strip():
            return
        chapter = (
            await db.execute(select(Chapter).where(Chapter.id == version.chapter_id))
        ).scalar_one_or_none()
        if chapter is None:
            logger.warning(
                "ChapterVersion %s 无对应 Chapter（chapter_id=%s），跳过章节索引",
                vid, version.chapter_id,
            )
            return
        queued = await enqueue_index_job(
            db, pid, "chapter_version", str(chapter.id),
            source_version_id=vid,
        )
        logger.info(
            "PG 模式章节索引转 outbox job（chapter=%s version=%s，queued=%s）",
            chapter.id, vid, queued,
        )
        return
    await build_chapter_index(db, project_id, version_id)


async def build_structured_index(
    db: AsyncSession,
    project_id: str,
    source_kind: str,
    source_id: str,
    *,
    title: str,
    content: str,
    chapter_no: int | None = None,
) -> str | None:
    """纯构建：索引结构化知识行（character_profile / ability_profile /
    timeline_event / world_entry）。

    内容短，一般产出一个块；chunk_text 会保留短文本（>=2 字符即返回）。
    幂等：同一行内容未变时跳过。返回新 generation_id；跳过时返回 None。
    """
    text_ = (content or "").strip()
    if not text_:
        return None
    pieces = chunk_text(text_, DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP) or [text_]
    return await _rebuild_with_generation(
        db, project_id, source_kind, source_id, pieces,
        title=title, chapter_no=chapter_no,
    )


async def request_source_index(
    db: AsyncSession, project_id: str, source_id: str
) -> None:
    """资料源（source_chunk）索引请求入口：PG 写 outbox，SQLite 同步构建。

    与 :func:`request_chapter_index` 同构（P0 拆分约定）；Worker 消费 source_chunk
    job 时按 source_id 从 ProjectSourceChunk 幂等构建，无版本语义。
    """
    if _dialect(db) == "postgresql":
        from services.retrieval_index_worker import enqueue_index_job  # 延迟导入避免循环依赖
        await enqueue_index_job(db, project_id, "source_chunk", source_id)
        return
    await build_source_index(db, project_id, source_id)


async def request_structured_index(
    db: AsyncSession,
    project_id: str,
    source_kind: str,
    source_id: str,
    *,
    title: str,
    content: str,
    chapter_no: int | None = None,
) -> None:
    """结构化知识行（character_profile / ability_profile / timeline_event /
    world_entry）索引请求入口：PG 写 outbox（payload 携带 title/content/chapter_no），
    SQLite 同步构建。与 :func:`request_chapter_index` 同构（P0 拆分约定）。
    """
    if _dialect(db) == "postgresql":
        from services.retrieval_index_worker import enqueue_index_job  # 延迟导入避免循环依赖
        payload: dict = {"title": title, "content": content}
        if chapter_no is not None:
            payload["chapter_no"] = chapter_no
        await enqueue_index_job(db, project_id, source_kind, source_id, payload=payload)
        return
    await build_structured_index(
        db, project_id, source_kind, source_id,
        title=title, content=content, chapter_no=chapter_no,
    )


# ── 全量重建 ─────────────────────────────────────────────

def _profile_content(p: CharacterProfile) -> str:
    parts = [p.name]
    if p.aliases:
        parts.append("别名：" + "、".join(str(a) for a in p.aliases))
    if p.identity_desc:
        parts.append(p.identity_desc)
    if p.status_desc:
        parts.append(p.status_desc)
    return "\n".join(parts)


def _ability_content(a: AbilityProfile) -> str:
    parts = [a.character_name, a.ability_type, a.ability_name]
    if a.level_desc:
        parts.append(a.level_desc)
    if a.status:
        parts.append(a.status)
    return " ".join(parts)


def _event_content(e: EventTimeline) -> str:
    parts = [e.event_title]
    if e.event_desc:
        parts.append(e.event_desc)
    if e.characters:
        parts.append("角色：" + "、".join(str(c) for c in e.characters))
    if e.location_desc:
        parts.append("地点：" + e.location_desc)
    if e.cause_desc:
        parts.append("起因：" + e.cause_desc)
    if e.effect_desc:
        parts.append("影响：" + e.effect_desc)
    return "\n".join(parts)


async def rebuild_project(
    db: AsyncSession,
    project_id: str,
    progress_cb=None,
) -> dict:
    """全量重建项目检索索引（供后台任务调用）。

    覆盖来源（source_kind）：
      source_chunk（ProjectSourceChunk 投影，按 source 分组）
      chapter_version（ChapterVersion 定稿章节）
      character_profile / ability_profile / timeline_event（structured_knowledge）
      world_entry（world_entries 表）

    progress_cb(done: int, total: int, stage: str) 每处理完一个源调用一次。
    单源失败不中断整体，计入 failed 并继续。

    返回::

        {
            "by_kind": {source_kind: {"indexed": n, "skipped": n, "failed": n}},
            "total_rows": m, "failed_rows": k, "errors": [...],
        }
    """
    pid = str(project_id)
    by_kind: dict[str, dict[str, int]] = {}

    def _bump(kind: str, key: str) -> None:
        entry = by_kind.setdefault(kind, {"indexed": 0, "skipped": 0, "failed": 0})
        entry[key] += 1

    # 收集全部源行，先统计总量（供进度回调）
    sources = (
        await db.execute(select(ProjectSource).where(ProjectSource.project_id == pid))
    ).scalars().all()
    versions = (
        await db.execute(
            select(ChapterVersion)
            .where(ChapterVersion.project_id == pid)
            # 同一章节的多个定稿版本共享稳定 source_id（chapter.id），
            # 后索引的版本会替换旧版本（指针切换）。按 created_at 升序处理，
            # 保证最终生效的是最新版本，重建结果确定。
            .order_by(ChapterVersion.created_at.asc())
        )
    ).scalars().all()
    profiles = (
        await db.execute(select(CharacterProfile).where(CharacterProfile.project_id == pid))
    ).scalars().all()
    abilities = (
        await db.execute(select(AbilityProfile).where(AbilityProfile.project_id == pid))
    ).scalars().all()
    events = (
        await db.execute(select(EventTimeline).where(EventTimeline.project_id == pid))
    ).scalars().all()
    entries = (
        await db.execute(select(WorldEntry).where(WorldEntry.project_id == pid))
    ).scalars().all()

    total = len(sources) + len(versions) + len(profiles) + len(abilities) + len(events) + len(entries)
    done = 0
    errors: list[str] = []

    def _report(stage: str) -> None:
        nonlocal done
        done += 1
        if progress_cb is not None:
            progress_cb(done, total, stage)

    async def _safe_index(kind: str, task) -> None:
        try:
            gen_id = await task()
            _bump(kind, "indexed" if gen_id is not None else "skipped")
        except Exception as exc:  # noqa: BLE001 — 单源失败不中断重建
            logger.exception("rebuild_project %s 失败: %s", kind, exc)
            _bump(kind, "failed")
            errors.append(f"{kind}: {exc}")

    for s in sources:
        await _safe_index("source_chunk", lambda s=s: build_source_index(db, pid, str(s.id)))
        _report("source_chunk")
    for v in versions:
        await _safe_index("chapter_version", lambda v=v: build_chapter_index(db, pid, str(v.id)))
        _report("chapter_version")
    for p in profiles:
        await _safe_index(
            "character_profile",
            lambda p=p: build_structured_index(
                db, pid, "character_profile", str(p.id),
                title=p.name, content=_profile_content(p),
                chapter_no=p.first_seen_chapter,
            ),
        )
        _report("character_profile")
    for a in abilities:
        await _safe_index(
            "ability_profile",
            lambda a=a: build_structured_index(
                db, pid, "ability_profile", str(a.id),
                title=f"{a.character_name}·{a.ability_name}",
                content=_ability_content(a),
                chapter_no=a.first_seen_chapter,
            ),
        )
        _report("ability_profile")
    for e in events:
        await _safe_index(
            "timeline_event",
            lambda e=e: build_structured_index(
                db, pid, "timeline_event", str(e.id),
                title=e.event_title, content=_event_content(e),
                chapter_no=e.chapter_no,
            ),
        )
        _report("timeline_event")
    for w in entries:
        await _safe_index(
            "world_entry",
            lambda w=w: build_structured_index(
                db, pid, "world_entry", str(w.id),
                title=w.title, content=f"{w.title}\n{w.content or ''}",
                chapter_no=w.chapter_sequence_number,
            ),
        )
        _report("world_entry")

    failed_rows = sum(entry["failed"] for entry in by_kind.values())
    return {
        "by_kind": by_kind,
        "total_rows": total,
        "failed_rows": failed_rows,
        "errors": errors,
    }


# ── 兼容别名（历史函数名，行为 = 纯构建） ────────────────
# 旧名 index_chapter_version / index_source_chunks / index_structured_source
# 被 tests / retrieval_eval/index_dev_db.py / rebuild_project 引用，保留为
# build_* 的别名；PG 入队语义由 request_* 承担（finalize 等业务侧入口）。

index_chapter_version = build_chapter_index
index_source_chunks = build_source_index
index_structured_source = build_structured_index


__all__ = [
    "ensure_fts_table",
    "start_generation",
    "finish_generation_active",
    "fail_generation",
    "build_source_index",
    "build_chapter_index",
    "build_structured_index",
    "request_source_index",
    "request_chapter_index",
    "request_structured_index",
    "rebuild_project",
    "clear_indexer_cache",
    # 兼容别名（纯构建）
    "index_source_chunks",
    "index_chapter_version",
    "index_structured_source",
]
