"""检索索引 outbox Worker —— PG 异步索引构建消费端（阶段二，P0 闭环）。

业务侧（章节定稿 finalize 等）在 PG 模式下只写 ``retrieval_index_jobs``
pending 任务（见 :func:`enqueue_index_job` 与 indexer 的 request_* 入口），
不直接同步构建；本模块的 :func:`poll_and_process_jobs` 轮询消费：
原子领取 → 按 source_kind 分发到 indexer 的 build_* 纯构建函数 →
finish_generation_active（切 active 指针）→ done，每 job 一个独立事务。

同源合并策略（P0）
------------------
- 同源存在 **pending** job：enqueue 直接 UPDATE 该 job 的 source_version_id /
  payload 为最新请求（合并，不新增）—— v1 未消费时再次定稿 v2，v1 任务升级为
  v2，v2 不会丢失。
- 同源存在 **processing** job：允许新增一条 pending（最新版本），消费端由激活前
  校验（source_version_id == chapter.final_version_id）保证不会把旧版本指针切回去。
- 同源 done / failed：允许新增 pending（内容再次变化可重新入队）。
- DB 层兜底：部分唯一索引 ``uq_retrieval_index_job_active_source``
  (project_id, source_kind, source_id) WHERE status='pending'（同源最多一条
  pending，processing 不锁死）；并发 INSERT 竞态由 enqueue 捕获 IntegrityError
  后读回合并。

领取与事务（P0）
---------------
- 原子领取：PG 每 job 在自身事务内 ``SELECT ... WHERE id=:id AND status='pending'
  FOR UPDATE SKIP LOCKED``（锁持有到该 job 事务提交）；SQLite（测试场景）用条件
  UPDATE（``SET status='processing', claimed_at=now WHERE id=:id AND
  status='pending'``，rowcount==1 才算领取成功）。多 Worker 并发不会重复消费。
- 每 job 一个事务：领取 + 构建 + finish_generation_active + done 一次 commit。
  构建异常先 rollback（回滚领取与半成品，不留可见 generation/块），再开新事务记
  job status=failed（attempts+1、error 落库）；单 job 失败不中断本批其他 job。
- 崩溃恢复：processing 且 claimed_at 超过 ``JOB_TIMEOUT_SECONDS`` 的 job 重置回
  pending 重新消费；同源已有 pending 时（更新版本已入队）不重置（避免与部分唯一
  索引冲突），直接记 failed（superseded_by_newer_pending）收尾。

激活前校验（P0）
---------------
chapter_version 类 job 构建前读取 Chapter：``job.source_version_id ==
chapter.final_version_id`` 才允许构建/切指针；不匹配（旧版本任务，例如 v1 未
消费时已再次定稿 v2）标记 failed（error 含 "stale_version"），绝不把旧版本指针
切回去。source_chunk / 结构化类无版本语义，按 source_id 幂等构建。

运行入口（P0）
--------------
- :func:`run_worker_loop`：无限轮询（once=True 单轮退出），捕获 SIGINT/SIGTERM
  优雅退出（当前批处理完再停）。
- ``python -m services.retrieval_index_worker`` 或
  ``backend/scripts/run_index_worker.py`` 启动独立 Worker 进程；SQLite 方言
  （桌面端）不启动 Worker —— finalize 由 request_chapter_index 同步构建。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.chapter import Chapter
from models.retrieval_index import RetrievalIndexGeneration
from models.retrieval_index_job import RetrievalIndexJob
from services.retrieval_indexer import (
    build_chapter_index,
    build_source_index,
    build_structured_index,
    finish_generation_active,
)

logger = logging.getLogger(__name__)

JOB_TIMEOUT_SECONDS = 300

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_STRUCTURED_KINDS = (
    "character_profile",
    "ability_profile",
    "timeline_event",
    "world_entry",
)


def _dialect(db: AsyncSession) -> str:
    """当前会话绑定的数据库方言（sqlite / postgresql）。"""
    bind = db.bind
    return bind.dialect.name if bind is not None else "sqlite"


class StaleVersionError(ValueError):
    """chapter_version job 指向的版本已不是章节当前定稿版本（跳过构建）。"""


# ── 入队（同源合并策略） ──────────────────────────────────

def _apply_request(job: RetrievalIndexJob, source_version_id, payload) -> None:
    """把最新请求合并进已存在的 pending job（不新增行）。"""
    if source_version_id is not None:
        job.source_version_id = source_version_id
    if payload is not None:
        job.payload = payload


async def enqueue_index_job(
    db: AsyncSession,
    project_id,
    source_kind: str,
    source_id,
    source_version_id=None,
    payload: dict | None = None,
) -> bool:
    """写一条 pending outbox job（P0 同源合并策略）。

    - 同源存在 **pending** job：UPDATE 该 job 的 source_version_id / payload 为
      最新请求（合并，不新增），返回 True —— 旧版本任务升级为最新版本，不丢失。
    - 同源存在 processing / done / failed：允许新增一条 pending，返回 True。
    - 并发竞态（两条 enqueue 同时 INSERT 同源）：部分唯一索引
      （WHERE status='pending'）兜底，IntegrityError 后读回并合并，返回 True。

    事务边界：不 commit，由调用方（路由层等）统一提交 —— 与业务写入保持同一事务
    语义，业务回滚时 outbox 也不会留下孤儿任务。
    """
    pid = str(project_id)
    sid = str(source_id)
    svid = str(source_version_id) if source_version_id is not None else None
    existing = (
        await db.execute(
            select(RetrievalIndexJob).where(
                RetrievalIndexJob.project_id == pid,
                RetrievalIndexJob.source_kind == source_kind,
                RetrievalIndexJob.source_id == sid,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status == STATUS_PENDING:
        _apply_request(existing, svid, payload)
        logger.info(
            "检索索引 job 已存在 pending，合并为最新请求: kind=%s source=%s svid=%s",
            source_kind, sid, svid,
        )
        return True

    job = RetrievalIndexJob(
        project_id=pid,
        source_kind=source_kind,
        source_id=sid,
        source_version_id=svid,
        payload=payload,
        status=STATUS_PENDING,
        attempts=0,
    )
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
        logger.info("检索索引 job 入队: kind=%s source=%s job=%s", source_kind, sid, job.id)
        return True
    except IntegrityError:
        # 并发入队竞态：另一事务刚插入同源 pending → 读回并合并（部分唯一索引兜底）
        pending = (
            await db.execute(
                select(RetrievalIndexJob).where(
                    RetrievalIndexJob.project_id == pid,
                    RetrievalIndexJob.source_kind == source_kind,
                    RetrievalIndexJob.source_id == sid,
                    RetrievalIndexJob.status == STATUS_PENDING,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if pending is None:
            return False
        _apply_request(pending, svid, payload)
        logger.info(
            "检索索引 job 并发竞态后合并: kind=%s source=%s svid=%s",
            source_kind, sid, svid,
        )
        return True


# ── 轮询处理 ──────────────────────────────────────────────

async def poll_and_process_jobs(
    db: AsyncSession,
    batch_size: int = 10,
    timeout_seconds: int = JOB_TIMEOUT_SECONDS,
) -> dict:
    """轮询并处理一批 outbox 任务（P0 闭环，每个 job 一个独立事务）。

    1. 超时回收：status=processing 且 claimed_at 早于 ``now - timeout`` 的 job
       重置回 pending（同源已有 pending 时记 failed 收尾），单独提交。
    2. 取候选 pending ids（PG 用 FOR UPDATE SKIP LOCKED 预览排他，SQLite 普通
       SELECT；正式领取在每 job 事务内）。
    3. 逐 job 处理（每 job 一个事务）：原子领取 → 激活前校验（chapter_version）
       → 构建 → finish_generation_active → done，一次 commit。异常：先 rollback
       （回滚领取与半成品），再开新事务记 failed（attempts+1、error 落库）。
       单 job 失败不中断本批其他 job。

    返回 {"reclaimed": int, "processed": int, "failed": int, "skipped": int}。
    """
    result = {"reclaimed": 0, "processed": 0, "failed": 0, "skipped": 0}
    now = datetime.now(timezone.utc)

    # 1) 超时回收（重启/崩溃后的 processing 残留；独立事务提交）。
    #    reclaimed：重置回 pending 的数量；stuck_failed：同源已有 pending 被
    #    直接收尾 failed 的数量（计入整体 failed 统计）。
    reclaimed, stuck_failed = await _reclaim_stale(db, now, timeout_seconds)
    result["reclaimed"] += reclaimed
    result["failed"] += stuck_failed

    # 2) 候选批次（PG 预览时 SKIP LOCKED 排他，commit 后释放，正式领取在 job 事务内）
    job_ids = await _peek_pending_ids(db, batch_size)

    # 3) 逐 job 处理（每个 job 一个事务）
    for job_id in job_ids:
        try:
            outcome = await _process_one_job_tx(db, job_id)
            if outcome == "processed":
                result["processed"] += 1
            else:
                result["skipped"] += 1
                await db.commit()  # 关闭本 job 的只读事务
        except Exception as exc:  # noqa: BLE001 — job 级失败不中断批量轮询
            await _mark_failed(db, job_id, exc)
            result["failed"] += 1
    return result


async def _reclaim_stale(
    db: AsyncSession, now: datetime, timeout_seconds: int
) -> tuple[int, int]:
    """超时回收：processing 且 claimed_at 过期的 job 重置回 pending（单独 commit）。

    同源已有 pending 时（更新版本已入队）不重置 —— 避免违反部分唯一索引
    （WHERE status='pending'），直接记 failed（superseded_by_newer_pending）收尾，
    防止该 processing 行永久卡死。

    返回 (reclaimed, stuck_failed)：分别是被重置回 pending 的 job 数、被收尾为
    failed 的 job 数。
    """
    deadline = now - timedelta(seconds=timeout_seconds)
    stale = (
        await db.execute(
            select(RetrievalIndexJob).where(
                RetrievalIndexJob.status == STATUS_PROCESSING,
                RetrievalIndexJob.claimed_at.is_not(None),
                RetrievalIndexJob.claimed_at < deadline,
            )
        )
    ).scalars().all()
    if not stale:
        return 0, 0
    reclaimed = 0
    stuck_failed = 0
    for job in stale:
        newer_pending = (
            await db.execute(
                select(RetrievalIndexJob.id).where(
                    RetrievalIndexJob.project_id == job.project_id,
                    RetrievalIndexJob.source_kind == job.source_kind,
                    RetrievalIndexJob.source_id == job.source_id,
                    RetrievalIndexJob.status == STATUS_PENDING,
                    RetrievalIndexJob.id != job.id,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if newer_pending is not None:
            job.status = STATUS_FAILED
            job.attempts += 1
            job.error = "superseded_by_newer_pending"
            job.claimed_at = None
            stuck_failed += 1
            logger.warning(
                "检索索引 job 超时且同源已有 pending，直接收尾 failed: job=%s", job.id
            )
        else:
            job.status = STATUS_PENDING
            job.claimed_at = None
            job.error = None  # 重新消费前清掉上次尝试的残留错误
            reclaimed += 1
            logger.warning("检索索引 job 超时回收（重置为 pending 重试）: job=%s", job.id)
    await db.commit()
    return reclaimed, stuck_failed


async def _peek_pending_ids(db: AsyncSession, batch_size: int) -> list:
    """取本批候选 pending job id（created_at 升序，LIMIT batch_size）。

    PG：FOR UPDATE SKIP LOCKED 预览（其他 Worker 正在处理的 job 被跳过），
    commit 释放预览锁；正式领取在每 job 事务内再做（见 :func:`_claim_for_processing`）。
    SQLite：普通 SELECT（领取靠条件 UPDATE 的 rowcount 保证原子）。
    """
    if batch_size <= 0:
        return []
    stmt = (
        select(RetrievalIndexJob.id)
        .where(RetrievalIndexJob.status == STATUS_PENDING)
        .order_by(RetrievalIndexJob.created_at.asc(), RetrievalIndexJob.id.asc())
        .limit(batch_size)
    )
    if _dialect(db) == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    ids = (await db.execute(stmt)).scalars().all()
    await db.commit()  # 结束预览事务（释放 SKIP LOCKED 排他锁）
    return list(ids)


async def _claim_for_processing(
    db: AsyncSession, job_id
) -> RetrievalIndexJob | None:
    """原子领取（P0）：把一条 pending job 置为 processing（并记录 claimed_at）。

    - PG：``SELECT ... WHERE id=:id AND status='pending' FOR UPDATE SKIP LOCKED``
      —— 锁持有到本 job 事务提交，其他 Worker 无法重复领取。
    - SQLite（测试场景）：条件 UPDATE（rowcount==1 才算领取成功）。

    返回领取到的 job ORM 实例；已被其他 Worker 领取/完成时返回 None。
    """
    if _dialect(db) == "postgresql":
        job = (
            await db.execute(
                select(RetrievalIndexJob)
                .where(
                    RetrievalIndexJob.id == job_id,
                    RetrievalIndexJob.status == STATUS_PENDING,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        job.status = STATUS_PROCESSING
        job.claimed_at = datetime.now(timezone.utc)
        return job

    result = await db.execute(
        update(RetrievalIndexJob)
        .where(
            RetrievalIndexJob.id == job_id,
            RetrievalIndexJob.status == STATUS_PENDING,
        )
        .values(
            status=STATUS_PROCESSING,
            claimed_at=datetime.now(timezone.utc),
        )
    )
    if result.rowcount != 1:
        return None
    return (
        await db.execute(
            select(RetrievalIndexJob)
            .where(RetrievalIndexJob.id == job_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _ensure_version_is_current(db: AsyncSession, job: RetrievalIndexJob) -> None:
    """激活前校验（P0）：chapter_version job 的版本必须仍是章节当前定稿版本。

    读取 Chapter（source_id = chapter.id 稳定标识），校验 ``job.source_version_id ==
    chapter.final_version_id``；不匹配（旧版本任务，例如 v1 尚未消费时已再次定稿 v2）
    → 抛 :class:`StaleVersionError`：不构建、不切指针（旧版本不得把 active 指针
    切回去），由调用方把 job 标记 failed（error 含 "stale_version"）。
    """
    chapter = (
        await db.execute(select(Chapter).where(Chapter.id == job.source_id))
    ).scalar_one_or_none()
    final_vid = str(chapter.final_version_id) if chapter is not None else None
    if job.source_version_id is None or str(job.source_version_id) != final_vid:
        raise StaleVersionError(
            f"stale_version: job source_version_id={job.source_version_id} 与 "
            f"chapter.final_version_id={final_vid} 不一致，跳过构建（不切指针）"
        )


async def _dispatch_build(db: AsyncSession, job: RetrievalIndexJob) -> str | None:
    """按 source_kind 分发到 indexer 的 build_* 纯构建函数，返回 generation_id。"""
    if job.source_kind == "chapter_version":
        # source_id = chapter 稳定标识；真正要索引的版本 id 在 source_version_id
        # （_ensure_version_is_current 已保证其为当前定稿版本）
        return await build_chapter_index(db, job.project_id, job.source_version_id)
    if job.source_kind == "source_chunk":
        return await build_source_index(db, job.project_id, job.source_id)
    if job.source_kind in _STRUCTURED_KINDS:
        payload = job.payload or {}
        return await build_structured_index(
            db, job.project_id, job.source_kind, job.source_id,
            title=payload.get("title") or "",
            content=payload.get("content") or "",
            chapter_no=payload.get("chapter_no"),
        )
    raise ValueError(
        f"未知 source_kind {job.source_kind!r}（job id={job.id}），"
        f"无法分发到索引构建函数"
    )


async def _finish_generation_if_building(
    db: AsyncSession, generation_id: str
) -> None:
    """把构建结果切 active（P0：Worker 侧显式激活，一次 commit）。

    SQLite 同步路径（build_* 内 _finish_after_write）已切 active，这里只对仍处于
    building 的 generation 调 finish_generation_active，双后端 Worker 流程一致。
    """
    gen = (
        await db.execute(
            select(RetrievalIndexGeneration).where(
                RetrievalIndexGeneration.generation_id == generation_id
            )
        )
    ).scalar_one_or_none()
    if gen is not None and gen.status == "building":
        await finish_generation_active(db, generation_id)


async def _process_one_job_tx(db: AsyncSession, job_id) -> str:
    """处理单个 job（每 job 一个事务）：领取 → 校验 → 构建 → 激活 → done。

    返回 "processed"（构建成功/幂等跳过，job 置 done）或 "skipped"
    （领取失败：其他 Worker 正在处理/已完成）。
    """
    job = await _claim_for_processing(db, job_id)
    if job is None:
        return "skipped"
    if job.source_kind == "chapter_version":
        await _ensure_version_is_current(db, job)
    generation_id = await _dispatch_build(db, job)
    if generation_id is not None:
        await _finish_generation_if_building(db, generation_id)
    job.status = STATUS_DONE
    job.claimed_at = None
    job.error = None
    await db.commit()
    logger.info(
        "检索索引 job 完成: job=%s kind=%s source=%s generation=%s",
        job.id, job.source_kind, job.source_id, generation_id,
    )
    return "processed"


async def _mark_failed(db: AsyncSession, job_id, exc: Exception) -> None:
    """job 置 failed：先回滚当前事务（领取与半成品），再开新事务落 failed。

    构建失败不留下可见半成品：generation / 块写入都在被回滚的事务内，回滚后无
    building 残留、无 active 块；stale 校验失败同理。
    """
    try:
        await db.rollback()
    except Exception:  # pragma: no cover — 事务已坏时兜底
        pass
    try:
        job = await db.get(RetrievalIndexJob, job_id)
        if job is None:
            return
        job.status = STATUS_FAILED
        job.attempts += 1
        job.error = str(exc)
        job.claimed_at = None
        await db.commit()
        logger.warning(
            "检索索引 job 处理失败: job=%s kind=%s source=%s attempts=%d err=%s",
            job.id, job.source_kind, job.source_id, job.attempts, exc,
        )
    except Exception:  # pragma: no cover — 事务已坏时兜底回滚
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning(
            "检索索引 job 失败状态未能落库（事务可能已回滚）: job=%s err=%s", job_id, exc
        )


# ── 独立 Worker 进程入口 ─────────────────────────────────

async def run_worker_loop(
    interval_sec: float = 5.0,
    batch_size: int = 10,
    once: bool = False,
    session_factory=None,
) -> dict | None:
    """独立 Worker 主循环：无限轮询 outbox（once=True 时单轮退出）。

    优雅退出：捕获 SIGINT/SIGTERM 设置 stop 事件，当前批处理完（含最后一个 job
    的事务）后退出，不中断进行中的构建。``session_factory`` 仅测试注入用，
    默认使用 db.session 的全局 async_session（PG 服务端）。

    SQLite 方言不启动本循环（桌面端 finalize 由 request_chapter_index 同步构建）。
    """
    if session_factory is None:
        from db.session import async_session  # 延迟导入：避免桌面端误建 PG engine
        session_factory = async_session

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover — Windows/非主线程
            pass
    logger.info(
        "检索索引 Worker 启动（interval=%ss batch=%d once=%s）",
        interval_sec, batch_size, once,
    )

    last_stats: dict | None = None
    while not stop_event.is_set():
        try:
            async with session_factory() as db:
                last_stats = await poll_and_process_jobs(db, batch_size=batch_size)
            logger.info("检索索引 Worker 一轮完成: %s", last_stats)
        except Exception:  # noqa: BLE001 — 单轮失败不终止进程，下轮重试
            logger.exception("检索索引 Worker 单轮轮询失败（下轮重试）")
        if once:
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            pass
    logger.info("检索索引 Worker 退出")
    return last_stats


def main(argv: list[str] | None = None) -> int:
    """Worker 进程入口（``python -m services.retrieval_index_worker`` 或
    ``backend/scripts/run_index_worker.py``）。

    SQLite 方言（桌面端）打印提示并退出：桌面端 finalize 同步构建，不启动 Worker。
    """
    parser = argparse.ArgumentParser(
        prog="retrieval-index-worker",
        description="检索索引 outbox Worker：轮询消费 retrieval_index_jobs，"
                    "构建检索索引并切换 active 指针（PG 服务端部署）。",
    )
    parser.add_argument("--db", default=None, help="数据库 URL（默认取 settings.DATABASE_URL）")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒数（默认 5）")
    parser.add_argument("--batch", type=int, default=10, help="每批最多处理的 job 数（默认 10）")
    parser.add_argument("--once", action="store_true", help="只处理一轮后退出（默认持续运行）")
    args = parser.parse_args(argv)

    from config.settings import settings  # noqa: PLC0415 — 延迟导入：--db 覆盖需先生效
    url = args.db or settings.DATABASE_URL
    if not url.startswith("postgresql"):
        print(
            f"检索索引 Worker 不适用于 SQLite 方言（桌面端 finalize 同步构建，"
            f"不启动 Worker）；当前 DATABASE_URL={url}",
            file=sys.stderr,
        )
        return 1
    if args.db:
        os.environ["DATABASE_URL"] = args.db  # db.session 延迟导入，engine 使用新 URL

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(
        run_worker_loop(
            interval_sec=args.interval,
            batch_size=args.batch,
            once=args.once,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "JOB_TIMEOUT_SECONDS",
    "enqueue_index_job",
    "poll_and_process_jobs",
    "run_worker_loop",
    "main",
]
