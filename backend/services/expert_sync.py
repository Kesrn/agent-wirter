"""Expert System v2 同步服务 — 为已有项目补齐 v2 专家 + 标记旧大师 deprecated。

幂等：按 (project_id, expert_key) 去重，已存在的不重复创建。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.expert_templates import BUILTIN_EXPERTS_V2
from models.expert import Expert

logger = logging.getLogger(__name__)


async def sync_v2_experts(db: AsyncSession, project_id: uuid.UUID) -> dict:
    """为已有项目补齐 v2 内置专家，并把旧内置专家标记 deprecated。

    1. 查项目已有 builtin 专家
    2. 对每个 v2 模板：若该项目无此 expert_key 的 builtin 专家 → 创建
    3. 对每个旧 builtin 专家（expert_key IS NULL 且 is_builtin）：若 deprecated 还是 False → 标记 True
    4. 返回 {created, deprecated_marked, skipped}
    """
    result = (
        await db.execute(
            select(Expert).where(
                Expert.project_id == project_id,
                Expert.is_builtin == True,  # noqa: E712
            )
        )
    ).scalars().all()

    # 已有 v2 专家的 expert_key 集合
    existing_v2_keys: set[str] = {
        e.expert_key for e in result if e.expert_key
    }

    created = 0
    for tpl in BUILTIN_EXPERTS_V2:
        key = tpl["expert_key"]
        if key in existing_v2_keys:
            continue
        expert = Expert(
            project_id=project_id,
            is_builtin=True,
            **tpl,
        )
        db.add(expert)
        created += 1

    # 标记旧大师 deprecated（expert_key 为空且 is_builtin 且尚未 deprecated）
    deprecated_marked = 0
    for e in result:
        if not e.expert_key and not e.deprecated:
            e.deprecated = True
            deprecated_marked += 1

    skipped = len(result) - deprecated_marked  # 已存在的 v2 + 已 deprecated 的旧专家

    logger.info(
        "sync_v2_experts: project=%s created=%d deprecated_marked=%d skipped=%d",
        project_id, created, deprecated_marked, skipped,
    )
    return {
        "created": created,
        "deprecated_marked": deprecated_marked,
        "skipped": skipped,
    }
