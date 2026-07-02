"""写作记忆 staging 服务 — 抽取结果写入 staging + 后台抽取入口。

approve 后 asyncio.create_task 调 run_fact_extraction，用独立 session。
失败写 ai_run_step extract_memory FAILED，不影响 run COMPLETED。
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.writing_memory_staging import WritingMemoryStaging
from models.harness_enums import MemoryStagingStatus
from harness.step_logger import start_step, finish_step, fail_step

logger = logging.getLogger(__name__)


async def create_staging_from_extraction(
    db: AsyncSession,
    *,
    project_id: str | uuid.UUID,
    chapter_id: str | uuid.UUID | None,
    chapter_version_id: str | uuid.UUID | None,
    chapter_sequence_number: int | None,
    run_id: str | uuid.UUID | None,
    facts: list[dict[str, Any]],
) -> list[WritingMemoryStaging]:
    """将抽取结果写入 writing_memory_staging。

    幂等：同一 chapter_version_id 已存在任何 staging 记录（不限 status）则 skip。

    Args:
        db: 数据库会话
        project_id: 项目 ID
        chapter_id: 章节 ID
        chapter_version_id: 章节版本 ID（幂等键）
        chapter_sequence_number: 章节序号
        run_id: AI Run ID
        facts: parse_facts 返回的记忆列表

    Returns:
        创建的 WritingMemoryStaging 列表（幂等 skip 时为空列表）
    """
    if not facts:
        return []

    # 幂等：查同一 chapter_version_id 是否已有任何记录
    if chapter_version_id is not None:
        existing = await db.execute(
            select(WritingMemoryStaging.id).where(
                WritingMemoryStaging.chapter_version_id == str(chapter_version_id)
            ).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            logger.info("memory_staging: chapter_version_id=%s 已有记录，skip", chapter_version_id)
            return []

    created: list[WritingMemoryStaging] = []
    for i, fact in enumerate(facts):
        item = WritingMemoryStaging(
            project_id=project_id,
            run_id=run_id,
            chapter_id=chapter_id,
            chapter_version_id=chapter_version_id,
            chapter_sequence_number=chapter_sequence_number,
            memory_type=fact.get("memory_type", "PLOT_FACT"),
            title=fact.get("title", ""),
            payload=fact.get("payload", {}),
            evidence=fact.get("evidence"),
            status=MemoryStagingStatus.GENERATED,
            idempotency_key=f"{chapter_version_id}:{i}" if chapter_version_id else None,
        )
        db.add(item)
        created.append(item)
    await db.flush()
    return created


async def run_fact_extraction(
    *,
    project_id: str,
    chapter_id: str | None,
    chapter_version_id: str | None,
    chapter_sequence_number: int | None,
    run_id: str,
    content: str,
    context: str,
    llm_config: dict | None,
) -> None:
    """后台任务：抽取写作记忆并写入 staging。

    用独立 session（get_engine + async_sessionmaker），失败不影响 approve。
    失败时写 ai_run_step extract_memory FAILED，但 run 保持已完成的 COMPLETED 状态。

    Args:
        project_id: 项目 ID
        chapter_id: 章节 ID
        chapter_version_id: 章节版本 ID（精确绑定）
        chapter_sequence_number: 章节序号
        run_id: AI Run ID
        content: approve 时的最终正文
        context: 章节上下文
        llm_config: 用户 LLM 配置（从 current_values 传入）
    """
    from db.session import get_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # 幂等键：同一 run + chapter_version 不重复插 step
    step_idempotency_key = f"{run_id}:extract_memory:{chapter_version_id}"

    try:
        engine = get_engine()
        sf = async_sessionmaker(engine, expire_on_commit=False)

        async with sf() as session:
            # 写 ai_run_step（extract_memory, step_order=5）
            step = await start_step(
                session,
                run_id=run_id,
                step_order=5,
                step_name="extract_memory",
                agent_name="FactExtractionAgent",
                revision_round=0,
                input_data={"chapter_version_id": chapter_version_id, "content_len": len(content)},
            )
            # start_step 幂等：如果 step 已存在且非 RUNNING（已 FAILED/SUCCESS），
            # 不重复执行
            from models.harness_enums import RunStepStatus
            if step.status not in (RunStepStatus.RUNNING, RunStepStatus.PENDING):
                logger.info("fact_extraction: step already %s, skip", step.status)
                return

            await session.commit()

            try:
                from agents.llm_provider import get_llm_provider
                from agents.fact_extraction import extract_facts

                llm = get_llm_provider(llm_config)
                facts = await extract_facts(llm, content, context)

                created = await create_staging_from_extraction(
                    session,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    chapter_version_id=chapter_version_id,
                    chapter_sequence_number=chapter_sequence_number,
                    run_id=run_id,
                    facts=facts,
                )
                await session.commit()

                await finish_step(
                    session, step,
                    output={"fact_count": len(facts), "staging_count": len(created)},
                )
                await session.commit()
                logger.info(
                    "fact_extraction: run=%s chapter_version=%s facts=%d staging=%d",
                    run_id, chapter_version_id, len(facts), len(created),
                )
            except Exception:
                logger.exception(
                    "fact_extraction: failed run=%s chapter_version=%s",
                    run_id, chapter_version_id,
                )
                await fail_step(
                    session, step,
                    error_message=f"FactExtractionAgent failed: {type(Exception).__name__}",
                )
                await session.commit()
    except Exception:
        # 顶层兜底：DB 连接/表不存在等环境问题不应影响 approve
        logger.exception(
            "fact_extraction: environment error run=%s chapter_version=%s",
            run_id, chapter_version_id,
        )
