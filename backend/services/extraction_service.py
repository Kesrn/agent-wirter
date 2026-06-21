"""LLM 结构化抽取服务 —— 状态流、轮询推进、校验、合并。

核心设计（文档 §6、§11、§12）：
- job 表 + 轮询推进：advance_extraction_job 每次推进若干章，状态落库，可续跑。
  不用 BackgroundTask（桌面端关进程即丢任务）。
- extraction_staging：raw_output TEXT NOT NULL 保证 LLM 任何输出都落库；
  raw_json nullable，parse 失败为 null。
- 状态流：EXTRACTED → VALIDATED → MERGED / VALIDATION_FAILED → RETRYING → FAILED
- 单章失败不影响其它章节。
- merge 到 4 张正式表，按优先级覆盖。
- MAX_EXTRACT_CHARS 截断超长章节并记录 warning。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, func, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from models.extraction_pipeline import ProjectSourceChapter, ExtractionJob, ExtractionStaging
from models.structured_knowledge import (
    CharacterProfile, AbilityProfile, EventTimeline, WorldRule,
)
from services.extraction_schema import (
    AbilityItem, AbilityStatus, AbilityType, ChapterExtraction, build_extraction_user_prompt,
    EXTRACTION_SYSTEM_PROMPT, SCHEMA_VERSION, TEMPLATE_NAME, MAX_EXTRACT_CHARS,
)
from services.magic_systems import normalize_magic_system_label

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
# 真实章节抽取容易因证据/事件描述过长截断 JSON；保持在全局上限内给抽取更多输出空间。
EXTRACTION_MAX_TOKENS = 8000
# 每次轮询推进的章节数（控制单次接口耗时）
CHAPTERS_PER_ADVANCE = 5
SKILL_ALIAS_TO_SYSTEM = {
    "风轨": "风系", "风刃": "风系",
    "火滋": "火系", "烈拳": "火系", "火浪": "火系",
    "雷印": "雷系", "霹雳": "雷系", "雷击": "雷系",
    "冰蔓": "冰系", "冰锁": "冰系", "冰封": "冰系",
    "地波": "土系", "岩障": "土系", "陨石": "土系",
    "遁影": "暗影系", "影遁": "暗影系",
}


# ── 状态常量 ─────────────────────────────────────────────

class JobStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    BATCH_DONE = "BATCH_DONE"        # 本批配额完成，但全书还有未处理章节，可继续推进
    COMPLETED = "COMPLETED"          # 全书范围内章节全部处理完（真正的完成）
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StagingStatus:
    EXTRACTED = "EXTRACTED"
    VALIDATED = "VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RETRYING = "RETRYING"
    MERGED = "MERGED"
    MERGE_FAILED = "MERGE_FAILED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


CANON_PRIORITY = {"manual": 100, "fanfic": 80, "original": 60}


async def _resolve_provider_name(user_id: str, db: AsyncSession) -> str:
    """解析本次抽取将使用的 LLM provider 名称。

    与 get_llm_provider 的选择逻辑保持一致：
      - 用户有配置 → 取 config["provider"]
      - 无配置 → 回退 settings.LLM_PROVIDER（默认 mock）

    用于在 job/staging 上记录 provider，以及状态接口暴露 is_mock。
    """
    from api.llm_deps import get_user_llm_config
    from config.settings import settings
    cfg = await get_user_llm_config(user_id, db)
    if cfg:
        return cfg.get("provider", settings.LLM_PROVIDER)
    return settings.LLM_PROVIDER


# ── Job 推进 ─────────────────────────────────────────────

async def advance_extraction_job(
    db: AsyncSession,
    project_id: str,
    source_id: str,
    *,
    user_id: str,
    genre: str = "magic_fantasy",
    canon_level: str = "original",
    origin: str = "llm_extracted",
    chapter_no_start: int = 1,
    chapter_no_end: int | None = None,
    max_chapters_per_run: int = 20,
    force_reextract: bool = False,
) -> dict:
    """创建或推进抽取 job，每次处理 CHAPTERS_PER_ADVANCE 章。

    返回 job 的当前状态字典（含 id/status/counts）。
    """
    pid = str(project_id)
    sid = str(source_id)

    # 解析本次抽取使用的 provider，记录到 job/staging 上（区分 mock / 真实数据）
    provider_name = await _resolve_provider_name(str(user_id), db)

    # 查找该 source 未完成的 job（PENDING/RUNNING），没有则创建
    job = await _get_or_create_job(
        db, pid, sid, genre, canon_level, origin,
        chapter_no_start, chapter_no_end, max_chapters_per_run, force_reextract,
        provider_name,
    )

    if job["status"] in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        return job

    # 标记 RUNNING
    job_obj = await db.get(ExtractionJob, uuid.UUID(job["id"]))
    job_obj.status = JobStatus.RUNNING
    job_obj.started_at = job_obj.started_at or datetime.now(timezone.utc)

    # 计算本次要处理的章节范围
    chapters_to_process = await _select_chapters_to_process(
        db, pid, sid, job_obj, force_reextract,
    )

    # 处理 CHAPTERS_PER_ADVANCE 章
    for chapter in chapters_to_process[:CHAPTERS_PER_ADVANCE]:
        await _process_single_chapter(
            db, job_obj, chapter, genre, canon_level, origin, user_id,
            force_reextract=force_reextract,
        )
        await db.flush()
        # 更新 job 计数
        await _refresh_job_counts(db, job_obj)

    # 判断 job 状态：所有已处理章节都到终态（MERGED/FAILED），无悬空态。
    total_in_range = await _count_chapters_in_range(db, pid, sid, job_obj)
    pending = await _count_pending_staging(db, job_obj)
    reached_range = job_obj.extracted_count >= total_in_range
    # 还有未处理章节 = 全书范围内已处理数 < 总章数
    has_more_chapters = job_obj.extracted_count < total_in_range

    if pending == 0:
        all_failed = job_obj.merged_count == 0 and job_obj.failed_count > 0
        has_failures = job_obj.failed_count > 0

        if reached_range and not has_more_chapters:
            # 全书范围内章节全部处理完 → 真正完成
            job_obj.status = JobStatus.FAILED if all_failed else (
                JobStatus.PARTIAL_FAILED if has_failures else JobStatus.COMPLETED
            )
            job_obj.finished_at = datetime.now(timezone.utc)
        else:
            # 本批处理完但全书还有未处理章节 → 批次完成，可继续推进
            # 不设 finished_at（任务未真正结束）
            job_obj.status = JobStatus.BATCH_DONE

    # ── 批次 summary 日志：汇总本批推进结果，便于真实 LLM 下定位问题 ──
    processed_chapters = chapters_to_process[:CHAPTERS_PER_ADVANCE]
    run_chapter_nos = [c.chapter_no for c in processed_chapters]
    run_merged = run_failed = run_validation_failed = run_retrying = run_skipped = 0
    if run_chapter_nos:
        from sqlalchemy import func as _func
        status_counts = (await db.execute(
            select(ExtractionStaging.status, _func.count(ExtractionStaging.id))
            .where(ExtractionStaging.job_id == str(job_obj.id))
            .where(ExtractionStaging.chapter_no.in_(run_chapter_nos))
            .group_by(ExtractionStaging.status)
        )).all()
        counts_by_status = {row[0]: row[1] for row in status_counts}
        run_merged = counts_by_status.get(StagingStatus.MERGED, 0)
        run_failed = counts_by_status.get(StagingStatus.FAILED, 0)
        run_validation_failed = (
            counts_by_status.get(StagingStatus.VALIDATION_FAILED, 0)
            + counts_by_status.get(StagingStatus.MERGE_FAILED, 0)
        )
        run_retrying = counts_by_status.get(StagingStatus.RETRYING, 0)
        # 本批应处理章节数 - 已落 staging 的 = 跳过/未落库
        run_skipped = max(0, len(run_chapter_nos) - sum(counts_by_status.values()))

    logger.info(
        "extraction batch done: job_id=%s source_id=%s genre=%s "
        "processed_chapters=%d merged=%d failed=%d retrying=%d "
        "validation_failed=%d skipped=%d progress=%d/%d status=%s",
        str(job_obj.id), sid, genre,
        len(run_chapter_nos), run_merged, run_failed, run_retrying,
        run_validation_failed, run_skipped,
        job_obj.extracted_count, job_obj.total_chapters, job_obj.status,
    )

    await db.commit()
    return _job_to_dict(job_obj)


async def _get_or_create_job(
    db: AsyncSession, pid: str, sid: str, genre: str, canon_level: str, origin: str,
    chapter_no_start: int, chapter_no_end: int | None, max_chapters_per_run: int,
    force_reextract: bool, provider_name: str,
) -> dict:
    """查找未完成 job 或创建新 job。"""
    result = await db.execute(
        select(ExtractionJob)
        .where(ExtractionJob.source_id == sid)
        .where(ExtractionJob.status.in_([
            JobStatus.PENDING, JobStatus.RUNNING, JobStatus.BATCH_DONE,
        ]))
        .order_by(ExtractionJob.created_at.desc())
        .limit(1)
    )
    job_obj = result.scalar_one_or_none()

    if not job_obj:
        job_obj = ExtractionJob(
            project_id=pid, source_id=sid, genre=genre,
            canon_level=canon_level, origin=origin,
            status=JobStatus.PENDING,
            provider=provider_name,
            chapter_no_start=chapter_no_start,
            chapter_no_end=chapter_no_end,
            max_chapters_per_run=max_chapters_per_run,
            force_reextract=force_reextract,
        )
        db.add(job_obj)
        await db.flush()
    else:
        # 复用 job：刷新 provider 为当前配置，避免 is_mock 陈旧
        # （用户可能在 mock 建任务后切换真实模型继续抽取）
        if job_obj.provider != provider_name:
            job_obj.provider = provider_name

    return _job_to_dict(job_obj)


async def _select_chapters_to_process(
    db: AsyncSession, pid: str, sid: str, job: ExtractionJob, force_reextract: bool,
) -> list[ProjectSourceChapter]:
    """选择本次要处理的章节：范围内、且未成功抽取（除非 force_reextract）。"""
    # 章节范围
    stmt = select(ProjectSourceChapter).where(ProjectSourceChapter.source_id == sid)
    if job.chapter_no_start:
        stmt = stmt.where(ProjectSourceChapter.chapter_no >= job.chapter_no_start)
    if job.chapter_no_end:
        stmt = stmt.where(ProjectSourceChapter.chapter_no <= job.chapter_no_end)
    stmt = stmt.order_by(ProjectSourceChapter.chapter_no)

    result = await db.execute(stmt)
    all_chapters = list(result.scalars().all())

    # 总章数（范围内）
    job.total_chapters = len(all_chapters)

    # 过滤已到终态的（MERGED/FAILED），除非 force_reextract
    if not force_reextract:
        # 查该 job 已到终态的 chapter_no（MERGED 或 FAILED）
        terminal_result = await db.execute(
            select(ExtractionStaging.chapter_no)
            .where(ExtractionStaging.job_id == str(job.id))
            .where(ExtractionStaging.status.in_([StagingStatus.MERGED, StagingStatus.FAILED]))
        )
        terminal_nos = {r for r in terminal_result.scalars().all() if r is not None}
        chapters = [c for c in all_chapters if c.chapter_no not in terminal_nos]
    else:
        chapters = all_chapters

    # max_chapters_per_run 是"单次接口调用最多推进的章节数"（每批配额），
    # 不是整个 job 的总量上限。续抽时每批都可处理最多 max_chapters_per_run 章。
    return chapters[:job.max_chapters_per_run]


async def _count_chapters_in_range(
    db: AsyncSession, pid: str, sid: str, job: ExtractionJob,
) -> int:
    stmt = select(func.count(ProjectSourceChapter.id)).where(ProjectSourceChapter.source_id == sid)
    if job.chapter_no_start:
        stmt = stmt.where(ProjectSourceChapter.chapter_no >= job.chapter_no_start)
    if job.chapter_no_end:
        stmt = stmt.where(ProjectSourceChapter.chapter_no <= job.chapter_no_end)
    return (await db.execute(stmt)).scalar() or 0


# ── 单章处理 ─────────────────────────────────────────────

async def _process_single_chapter(
    db: AsyncSession, job: ExtractionJob, chapter: ProjectSourceChapter,
    genre: str, canon_level: str, origin: str, user_id: str,
    force_reextract: bool = False,
) -> None:
    """处理单章：LLM 抽取 → 保存 raw_output → 校验 → 合并。"""
    content = chapter.content or ""
    warning = None

    # MAX_EXTRACT_CHARS 截断（文档 §5.4）
    if len(content) > MAX_EXTRACT_CHARS:
        content = content[:MAX_EXTRACT_CHARS]
        warning = f"chapter content exceeded MAX_EXTRACT_CHARS ({MAX_EXTRACT_CHARS}) and was truncated for MVP extraction"
        if chapter.warning:
            chapter.warning = chapter.warning + "\n" + warning
        else:
            chapter.warning = warning

    # 调 LLM
    user_prompt = build_extraction_user_prompt(
        genre, chapter.chapter_no, chapter.chapter_title or "", content,
    )

    try:
        from agents.llm_provider import get_llm_provider, LLMConfigError
        from api.llm_deps import get_user_llm_config
        llm_config = await get_user_llm_config(user_id, db)
        provider = get_llm_provider(llm_config)
        raw_output = await provider.generate(
            EXTRACTION_SYSTEM_PROMPT, user_prompt, temperature=0.2, max_tokens=EXTRACTION_MAX_TOKENS,
        )
    except LLMConfigError as e:
        # 模型不可用：记录失败，不影响其它章
        await _save_staging(db, job, chapter, genre, raw_output="",
                            status=StagingStatus.FAILED, error_message=f"LLM 不可用: {e}")
        return
    except Exception as e:
        logger.exception("LLM 调用异常 chapter=%s", chapter.chapter_no)
        await _save_staging(db, job, chapter, genre, raw_output="",
                            status=StagingStatus.FAILED, error_message=str(e))
        return

    # 保存 raw_output（无论合法与否都落库）
    staging = await _save_staging(db, job, chapter, genre, raw_output=raw_output,
                                  status=StagingStatus.EXTRACTED)

    # 尝试 parse JSON
    parsed = _try_parse_json(raw_output)
    if parsed is None:
        staging.status = StagingStatus.VALIDATION_FAILED
        staging.error_message = "JSON parse 失败"
        await _handle_retry_or_fail(db, staging)
        return

    staging.raw_json = parsed

    # Pydantic 校验
    try:
        extraction = ChapterExtraction.model_validate(parsed)
    except ValidationError as e:
        staging.status = StagingStatus.VALIDATION_FAILED
        staging.error_message = f"Schema 校验失败: {e}"[:1000]
        await _handle_retry_or_fail(db, staging)
        return

    extraction = _normalize_extraction_abilities(extraction, content, genre=genre)
    staging.status = StagingStatus.VALIDATED

    # 合并到正式表
    try:
        async with db.begin_nested():
            if force_reextract:
                await _clear_stale_structured_results_for_chapter(
                    db, job, chapter, extraction, genre, canon_level, origin,
                )
            await _merge_extraction(db, job, chapter, extraction, canon_level, origin)
        staging.status = StagingStatus.MERGED
    except Exception as e:
        logger.exception("merge 失败 chapter=%s", chapter.chapter_no)
        staging.status = StagingStatus.MERGE_FAILED
        staging.error_message = f"合并失败: {e}"[:1000]
        # merge 失败也走重试上限，防止稳定 bug 导致 job 永远 RUNNING
        await _handle_retry_or_fail(db, staging)


async def _save_staging(
    db: AsyncSession, job: ExtractionJob, chapter: ProjectSourceChapter,
    genre: str, *, raw_output: str, status: str, error_message: str | None = None,
) -> ExtractionStaging:
    """保存/更新 staging 记录。

    对同一 job + chapter 复用已有记录（retry 时不新建），让 retry_count 真实累积。
    首次处理时创建；重试时更新同一条记录的 raw_output/status/error_message。
    """
    # 查找该 job + chapter 已有的 staging（按最新一条）
    existing = None
    if chapter.id:
        result = await db.execute(
            select(ExtractionStaging)
            .where(ExtractionStaging.job_id == str(job.id))
            .where(ExtractionStaging.chapter_id == str(chapter.id))
            .order_by(ExtractionStaging.created_at.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()

    if existing is not None:
        # 复用：更新本次的 raw_output/status，保留 retry_count
        existing.raw_output = raw_output
        existing.raw_json = None
        existing.status = status
        existing.error_message = error_message
        await db.flush()
        return existing

    staging = ExtractionStaging(
        job_id=str(job.id),
        project_id=str(job.project_id),
        source_id=str(job.source_id),
        chapter_id=str(chapter.id) if chapter.id else None,
        chapter_no=chapter.chapter_no,
        chapter_title=chapter.chapter_title,
        genre=genre,
        template_name=TEMPLATE_NAME,
        schema_version=SCHEMA_VERSION,
        raw_output=raw_output,
        raw_json=None,
        status=status,
        error_message=error_message,
        provider=getattr(job, "provider", None) or "mock",
    )
    db.add(staging)
    await db.flush()
    return staging


def _extraction_keys(extraction: ChapterExtraction) -> dict[str, set]:
    """Build merge keys from a validated extraction snapshot."""
    return {
        "characters": {c.name for c in extraction.characters if c.name},
        "abilities": {
            (a.character, a.ability_type.value, a.ability_name)
            for a in extraction.abilities
            if a.character and a.ability_name
        },
        "world_rules": {
            (r.category, r.rule_text)
            for r in extraction.world_rules
            if r.category and r.rule_text
        },
    }


def _keys_from_staging_raw(raw_json: dict | None, chapter_content: str, genre: str) -> dict[str, set]:
    """Recover structured merge keys from a historical staging raw_json row."""
    empty = {"characters": set(), "abilities": set(), "world_rules": set()}
    if not raw_json:
        return empty
    try:
        extraction = ChapterExtraction.model_validate(raw_json)
    except ValidationError:
        return empty

    raw_keys = _extraction_keys(extraction)
    try:
        normalized = _normalize_extraction_abilities(extraction, chapter_content, genre=genre)
    except Exception:
        return raw_keys

    normalized_keys = _extraction_keys(normalized)
    return {
        key: raw_keys[key] | normalized_keys[key]
        for key in raw_keys
    }


async def _staging_keys_for_source(
    db: AsyncSession,
    *,
    source_id: str,
    genre: str,
    exclude_chapter_no: int | None = None,
    include_chapter_no: int | None = None,
) -> dict[str, set]:
    stmt = (
        select(ExtractionStaging.raw_json, ProjectSourceChapter.content)
        .join(
            ProjectSourceChapter,
            (ProjectSourceChapter.source_id == ExtractionStaging.source_id)
            & (ProjectSourceChapter.chapter_no == ExtractionStaging.chapter_no),
            isouter=True,
        )
        .where(ExtractionStaging.source_id == source_id)
        .where(ExtractionStaging.status == StagingStatus.MERGED)
    )
    if include_chapter_no is not None:
        stmt = stmt.where(ExtractionStaging.chapter_no == include_chapter_no)
    if exclude_chapter_no is not None:
        stmt = stmt.where(ExtractionStaging.chapter_no != exclude_chapter_no)

    result = await db.execute(stmt)
    keys = {"characters": set(), "abilities": set(), "world_rules": set()}
    for raw_json, content in result.all():
        row_keys = _keys_from_staging_raw(raw_json, content or "", genre)
        for key in keys:
            keys[key].update(row_keys[key])
    return keys


async def _clear_stale_structured_results_for_chapter(
    db: AsyncSession,
    job: ExtractionJob,
    chapter: ProjectSourceChapter,
    extraction: ChapterExtraction,
    genre: str,
    canon_level: str,
    origin: str,
) -> None:
    """Remove prior structured rows for a forced re-extraction target.

    The cleanup is intentionally narrow:
    - only the same project/source/origin/canon_level;
    - only rows tied to this chapter, or exact keys previously emitted by this
      chapter's merged staging snapshot;
    - keys that also appear in other chapters are kept unless the fresh
      extraction is about to replace them.
    """
    pid = str(job.project_id)
    sid = str(job.source_id)
    chapter_no = chapter.chapter_no
    current_keys = _extraction_keys(extraction)
    target_keys = await _staging_keys_for_source(
        db,
        source_id=sid,
        genre=genre,
        include_chapter_no=chapter_no,
    )
    outside_keys = await _staging_keys_for_source(
        db,
        source_id=sid,
        genre=genre,
        exclude_chapter_no=chapter_no,
    )

    def _base_stmt(model):
        return (
            delete(model)
            .where(model.project_id == pid)
            .where(model.source_id == sid)
            .where(model.canon_level == canon_level)
            .where(model.origin == origin)
        )

    # Events are intrinsically chapter-scoped and are appended on every merge.
    event_stmt = _base_stmt(EventTimeline).where(EventTimeline.chapter_no == chapter_no)
    if chapter.id:
        event_stmt = event_stmt.where(or_(
            EventTimeline.chapter_id == str(chapter.id),
            EventTimeline.chapter_id.is_(None),
        ))
    await db.execute(event_stmt)

    for name in target_keys["characters"]:
        if name in current_keys["characters"] or name in outside_keys["characters"]:
            continue
        await db.execute(_base_stmt(CharacterProfile).where(CharacterProfile.name == name))

    ability_keys = {
        key for key in target_keys["abilities"]
        if key not in outside_keys["abilities"]
    }
    for character_name, ability_type, ability_name in ability_keys:
        await db.execute(
            _base_stmt(AbilityProfile)
            .where(AbilityProfile.character_name == character_name)
            .where(AbilityProfile.ability_type == ability_type)
            .where(AbilityProfile.ability_name == ability_name)
            .where(AbilityProfile.first_seen_chapter == chapter_no)
        )

    world_rule_keys = {
        key for key in target_keys["world_rules"]
        if key not in outside_keys["world_rules"]
    }
    for category, rule_text in world_rule_keys:
        world_rule_stmt = (
            _base_stmt(WorldRule)
            .where(WorldRule.category == category)
            .where(WorldRule.rule_text == rule_text)
            .where(WorldRule.chapter_no == chapter_no)
        )
        if chapter.id:
            world_rule_stmt = world_rule_stmt.where(or_(
                WorldRule.chapter_id == str(chapter.id),
                WorldRule.chapter_id.is_(None),
            ))
        await db.execute(world_rule_stmt)


def _try_parse_json(text: str) -> dict | None:
    """尝试 parse JSON，容错去除 markdown 代码块包裹和前后解释文本。"""
    if not text or not text.strip():
        return None
    text = text.strip()
    # 去除可能的 ```json ... ``` 包裹
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    candidates = [text]
    first_obj = text.find("{")
    last_obj = text.rfind("}")
    if first_obj >= 0 and last_obj > first_obj:
        candidates.append(text[first_obj:last_obj + 1])

    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            pass
        if candidate.startswith("{"):
            try:
                parsed, _ = decoder.raw_decode(candidate)
                return parsed if isinstance(parsed, dict) else None
            except (json.JSONDecodeError, TypeError):
                pass
    return None


def _normalize_extraction_abilities(
    extraction: ChapterExtraction, chapter_content: str, *, genre: str = "magic_fantasy",
) -> ChapterExtraction:
    """补齐模型容易漏归一化的明确能力线索。

    真实抽取中模型可能把“电流 + 紫色弧线”识别成 unknown 异象，而不是雷系。
    这里只处理证据很窄、可解释的场景，避免把愿望/旁观描述误归因。
    """
    if genre != "magic_fantasy":
        return extraction

    abilities = []
    existing = {
        (a.character, a.ability_type.value, a.ability_name)
        for a in extraction.abilities
    }

    for ability in extraction.abilities:
        if _is_unbound_mentioned_ability(ability):
            continue
        normalized_name = _normalize_ability_name(ability.ability_name)
        if ability.ability_type == AbilityType.magic_element and normalized_name != ability.ability_name:
            original_key = (ability.character, ability.ability_type.value, ability.ability_name)
            normalized_key = (ability.character, ability.ability_type.value, normalized_name)
            # Drop the noisy original key so 雷霆系魔法 + 雷系 collapses to one canonical row.
            existing.discard(original_key)
            if normalized_key in existing:
                continue
            existing.add(normalized_key)
            abilities.append(ability.model_copy(update={"ability_name": normalized_name}))
        else:
            abilities.append(ability)

    def _add(character: str, ability_name: str, evidence: str) -> None:
        key = (character, AbilityType.magic_element.value, ability_name)
        if key in existing:
            return
        existing.add(key)
        abilities.append(AbilityItem(
            character=character,
            ability_type=AbilityType.magic_element,
            ability_name=ability_name,
            level="觉醒",
            status=AbilityStatus.new,
            importance=5,
            confidence=0.85,
            evidence=evidence,
        ))

    has_lightning_cue = "电流" in chapter_content and ("紫色" in chapter_content or "弧线" in chapter_content)
    if has_lightning_cue:
        for ability in extraction.abilities:
            cue_text = f"{ability.ability_name} {ability.evidence}"
            if ability.ability_type == AbilityType.unknown and ("紫色" in cue_text or "弧线" in cue_text):
                evidence = _extract_evidence_excerpt(chapter_content, ("电流", "紫色", "弧线"))
                _add(ability.character, "雷系", evidence)

    for ability in extraction.abilities:
        if _is_unbound_mentioned_ability(ability):
            continue
        cue_text = f"{ability.ability_name} {ability.evidence}"
        system_name = _system_from_skill_alias(cue_text)
        if system_name and ability.ability_type in (AbilityType.spell, AbilityType.skill, AbilityType.unknown):
            evidence = _extract_evidence_excerpt(chapter_content, tuple(
                alias for alias in SKILL_ALIAS_TO_SYSTEM if alias in cue_text
            ))
            _add(ability.character, system_name, evidence or ability.evidence)

    if abilities == list(extraction.abilities):
        return extraction
    return extraction.model_copy(update={"abilities": abilities})


def _normalize_ability_name(name: str) -> str:
    """Normalize obvious system label variants without changing non-system abilities."""
    return normalize_magic_system_label(name)


def _system_from_skill_alias(text: str) -> str | None:
    for alias, system_name in SKILL_ALIAS_TO_SYSTEM.items():
        if alias in text:
            return system_name
    return None


def _is_unbound_mentioned_ability(ability: AbilityItem) -> bool:
    """Drop generic skill mentions that the model incorrectly attaches to a character."""
    if ability.status != AbilityStatus.mentioned:
        return False
    if ability.ability_type not in {AbilityType.spell, AbilityType.skill, AbilityType.martial_art}:
        return False
    evidence = ability.evidence or ""
    return ability.character not in evidence


def _extract_evidence_excerpt(text: str, terms: tuple[str, ...], *, radius: int = 80) -> str:
    positions = [text.find(term) for term in terms if text.find(term) >= 0]
    if not positions:
        return text.strip()[:240]
    start = max(0, min(positions) - radius)
    end = min(len(text), max(positions) + radius)
    return " ".join(text[start:end].strip().split())[:300]


async def _handle_retry_or_fail(db: AsyncSession, staging: ExtractionStaging) -> None:
    """retry_count < MAX_RETRIES → RETRYING；否则 FAILED。

    语义：允许重试 MAX_RETRIES 次（retry_count 从 0 开始，<2 时 RETRYING，
    达到 2 后仍失败则 FAILED）。
    """
    if staging.retry_count < MAX_RETRIES:
        staging.status = StagingStatus.RETRYING
        staging.retry_count = staging.retry_count + 1
    else:
        staging.status = StagingStatus.FAILED
        staging.retry_count = staging.retry_count + 1


# ── Merge 到正式表（文档 §12） ──────────────────────────

async def _merge_extraction(
    db: AsyncSession, job: ExtractionJob, chapter: ProjectSourceChapter,
    extraction: ChapterExtraction, canon_level: str, origin: str,
) -> None:
    """将校验通过的抽取结果合并到 4 张正式表。"""
    pid = str(job.project_id)
    sid = str(job.source_id)
    chapter_no = chapter.chapter_no
    source_priority = CANON_PRIORITY.get(canon_level, 60)

    # 1. character_profile
    for char in extraction.characters:
        await _merge_character(db, pid, sid, char, canon_level, origin, source_priority)

    # 2. ability_profile
    for ability in extraction.abilities:
        await _merge_ability(db, pid, sid, chapter_no, ability, canon_level, origin, source_priority)

    # 3. event_timeline（MVP 不去重，直接新增）
    for event in extraction.events:
        db.add(EventTimeline(
            project_id=pid, source_id=sid,
            chapter_id=str(chapter.id) if chapter.id else None,
            chapter_no=chapter_no,
            event_title=event.event_title,
            event_desc=event.event_desc,
            characters=event.characters,
            location_desc=event.location or None,
            cause_desc=event.cause or None,
            effect_desc=event.effect or None,
            importance=event.importance,
            canon_level=canon_level, origin=origin,
            source_priority=source_priority, confidence=event.confidence,
            evidence=[event.evidence],
        ))

    # 4. world_rule（查重键 project_id + category + rule_text）
    for rule in extraction.world_rules:
        await _merge_world_rule(db, pid, sid, chapter_no, rule, canon_level, origin, source_priority)


async def _merge_character(
    db: AsyncSession, pid: str, sid: str, char, canon_level: str, origin: str, priority: int,
) -> None:
    """合并人物：UNIQUE(project_id, name)。
    存在则追加 aliases/evidence，identity 为空才填充，confidence 取较高。
    """
    result = await db.execute(
        select(CharacterProfile)
        .where(CharacterProfile.project_id == pid)
        .where(CharacterProfile.name == char.name)
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        db.add(CharacterProfile(
            project_id=pid, source_id=sid, name=char.name,
            aliases=list(char.aliases) if char.aliases else [],
            identity_desc=char.identity or None,
            status_desc=char.status or None,
            canon_level=canon_level, origin=origin,
            source_priority=priority, confidence=char.confidence,
            evidence=[char.evidence],
        ))
    else:
        # 优先级判断：低优先级来源不覆盖高优先级已有字段（manual > fanfic > original）
        new_higher = priority > existing.source_priority
        same_or_higher = priority >= existing.source_priority
        # aliases 总是追加去重（补充信息不冲突）
        if char.aliases:
            existing_aliases = set(existing.aliases or [])
            existing.aliases = list(existing_aliases | set(char.aliases))
        # identity：为空才填充，或新来源优先级更高才覆盖
        if char.identity:
            if not existing.identity_desc or new_higher:
                existing.identity_desc = char.identity
        # status：新来源优先级更高才覆盖；同优先级追加最新状态摘要
        if char.status:
            if new_higher:
                existing.status_desc = char.status
            elif same_or_higher and existing.status_desc != char.status:
                existing.status_desc = char.status
        # evidence 追加去重（所有来源都补充证据）
        if char.evidence and char.evidence not in (existing.evidence or []):
            existing.evidence = (existing.evidence or []) + [char.evidence]
        # 更新来源元数据（若新优先级更高）
        if new_higher:
            existing.canon_level = canon_level
            existing.origin = origin
            existing.source_priority = priority
        # confidence 取较高
        existing.confidence = max(existing.confidence, char.confidence)


async def _merge_ability(
    db: AsyncSession, pid: str, sid: str, chapter_no: int,
    ability, canon_level: str, origin: str, priority: int,
) -> None:
    """合并能力：UNIQUE(project_id, character_name, ability_type, ability_name)。
    存在则 first_seen 取更早、evidence 追加、status 按优先级更新、confidence 取较高。
    """
    result = await db.execute(
        select(AbilityProfile)
        .where(AbilityProfile.project_id == pid)
        .where(AbilityProfile.character_name == ability.character)
        .where(AbilityProfile.ability_type == ability.ability_type.value)
        .where(AbilityProfile.ability_name == ability.ability_name)
    )
    existing = result.scalar_one_or_none()
    status_priority = {"new": 5, "upgraded": 4, "used": 3, "mentioned": 2, "lost": 1, "unknown": 0}

    if existing is None:
        db.add(AbilityProfile(
            project_id=pid, source_id=sid,
            character_name=ability.character,
            ability_type=ability.ability_type.value,
            ability_name=ability.ability_name,
            level_desc=ability.level or None,
            status=ability.status.value,
            first_seen_chapter=chapter_no,
            canon_level=canon_level, origin=origin,
            source_priority=priority, confidence=ability.confidence,
            evidence=[ability.evidence],
        ))
    else:
        # 优先级判断：低优先级来源不覆盖高优先级已有字段
        new_higher = priority > existing.source_priority
        same_or_higher = priority >= existing.source_priority
        # first_seen 取更早（与优先级无关，是事实信息）
        if existing.first_seen_chapter is None or chapter_no < existing.first_seen_chapter:
            existing.first_seen_chapter = chapter_no
        # level：新优先级更高才覆盖；为空才填充
        if ability.level:
            if not existing.level_desc or new_higher:
                existing.level_desc = ability.level
        # status：新优先级更高才允许覆盖；同优先级按状态优先级更新
        if ability.status.value:
            if new_higher:
                existing.status = ability.status.value
            elif same_or_higher:
                if status_priority.get(ability.status.value, 0) > status_priority.get(existing.status, 0):
                    existing.status = ability.status.value
        # evidence 追加去重
        if ability.evidence and ability.evidence not in (existing.evidence or []):
            existing.evidence = (existing.evidence or []) + [ability.evidence]
        # 更新来源元数据（若新优先级更高）
        if new_higher:
            existing.canon_level = canon_level
            existing.origin = origin
            existing.source_priority = priority
        # confidence 取较高
        existing.confidence = max(existing.confidence, ability.confidence)


async def _merge_world_rule(
    db: AsyncSession, pid: str, sid: str, chapter_no: int,
    rule, canon_level: str, origin: str, priority: int,
) -> None:
    """合并世界规则：UNIQUE(project_id, category, rule_text)。
    完全匹配则追加 evidence，否则新增。
    """
    result = await db.execute(
        select(WorldRule)
        .where(WorldRule.project_id == pid)
        .where(WorldRule.category == rule.category)
        .where(WorldRule.rule_text == rule.rule_text)
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        db.add(WorldRule(
            project_id=pid, source_id=sid, chapter_no=chapter_no,
            category=rule.category, rule_text=rule.rule_text,
            priority=rule.priority.value,
            canon_level=canon_level, origin=origin,
            source_priority=priority, confidence=rule.confidence,
            evidence=[rule.evidence],
        ))
    else:
        if rule.evidence and rule.evidence not in (existing.evidence or []):
            existing.evidence = (existing.evidence or []) + [rule.evidence]


# ── Job 状态查询 ─────────────────────────────────────────

async def get_latest_job_status(db: AsyncSession, project_id: str, source_id: str) -> dict | None:
    """查询 source 最新 job 状态，含旧状态自愈。

    自愈：旧版 bug 可能把"未全书完成"的 job 标成 COMPLETED（如 20/3130）。
    读到这类 job 时自动修正为 BATCH_DONE，让前端显示"本批完成"并允许继续抽取。
    """
    pid = str(project_id)
    result = await db.execute(
        select(ExtractionJob)
        .where(ExtractionJob.project_id == pid)
        .where(ExtractionJob.source_id == str(source_id))
        .order_by(ExtractionJob.created_at.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if not job:
        return None

    # 旧状态自愈：COMPLETED 但未全书处理完 → BATCH_DONE
    if (job.status == JobStatus.COMPLETED
            and job.total_chapters > 0
            and job.extracted_count < job.total_chapters):
        job.status = JobStatus.BATCH_DONE
        job.finished_at = None
        await db.commit()
        logger.info(
            "自愈: job %s COMPLETED→BATCH_DONE (extracted=%d/%d)",
            job.id, job.extracted_count, job.total_chapters,
        )

    return _job_to_dict(job)


async def reset_extraction(db: AsyncSession, project_id: str, source_id: str) -> dict:
    """重置抽取：清除 staging/job/4 张结构化表，保留原文和 chunks。

    只删当前 project_id + source_id 的数据，不影响其它资料。
    """
    from sqlalchemy import delete, func
    pid = str(project_id)
    sid = str(source_id)

    async def _count(model) -> int:
        return (await db.execute(
            select(func.count(model.id))
            .where(model.project_id == pid)
            .where(model.source_id == sid)
        )).scalar() or 0

    deleted_staging = await _count(ExtractionStaging)
    deleted_jobs = await _count(ExtractionJob)
    deleted_chars = await _count(CharacterProfile)
    deleted_abilities = await _count(AbilityProfile)
    deleted_events = await _count(EventTimeline)
    deleted_rules = await _count(WorldRule)

    # 按依赖顺序删除（限定 project_id + source_id，避免跨项目误删）
    await db.execute(delete(ExtractionStaging)
                     .where(ExtractionStaging.project_id == pid)
                     .where(ExtractionStaging.source_id == sid))
    await db.execute(delete(ExtractionJob)
                     .where(ExtractionJob.project_id == pid)
                     .where(ExtractionJob.source_id == sid))
    await db.execute(delete(CharacterProfile)
                     .where(CharacterProfile.project_id == pid)
                     .where(CharacterProfile.source_id == sid))
    await db.execute(delete(AbilityProfile)
                     .where(AbilityProfile.project_id == pid)
                     .where(AbilityProfile.source_id == sid))
    await db.execute(delete(EventTimeline)
                     .where(EventTimeline.project_id == pid)
                     .where(EventTimeline.source_id == sid))
    await db.execute(delete(WorldRule)
                     .where(WorldRule.project_id == pid)
                     .where(WorldRule.source_id == sid))
    await db.commit()

    logger.info(
        "reset_extraction: source=%s jobs=%d staging=%d chars=%d abilities=%d events=%d rules=%d",
        sid, deleted_jobs, deleted_staging, deleted_chars, deleted_abilities,
        deleted_events, deleted_rules,
    )
    return {
        "status": "reset",
        "deleted_jobs": deleted_jobs,
        "deleted_staging": deleted_staging,
        "deleted_characters": deleted_chars,
        "deleted_abilities": deleted_abilities,
        "deleted_events": deleted_events,
        "deleted_world_rules": deleted_rules,
    }


async def _refresh_job_counts(db: AsyncSession, job: ExtractionJob) -> None:
    """从 staging 表重新统计 job 计数。

    extracted_count：已拿到 LLM 输出的章节（含中间态和终态，不含 RETRYING ——
      RETRYING 表示待重试，下次推进会重新处理，不应算作"已处理"）。
    failed_count：处于失败终态的章节（VALIDATION_FAILED / MERGE_FAILED / FAILED）。
    注意：VALIDATION_FAILED 在 retry 未超限时是 RETRYING，超限才转 FAILED；
      只有真正 FAILED 的才计入 failed_count，避免重试中的章节被误判为失败。
    """
    result = await db.execute(
        select(ExtractionStaging.status, func.count(ExtractionStaging.id))
        .where(ExtractionStaging.job_id == str(job.id))
        .group_by(ExtractionStaging.status)
    )
    counts = {row[0]: row[1] for row in result.all()}
    # extracted_count：所有非 RETRYING 的 staging（已尝试并拿到结果的）
    job.extracted_count = sum(
        counts.get(s, 0) for s in
        (StagingStatus.EXTRACTED, StagingStatus.VALIDATED, StagingStatus.MERGED,
         StagingStatus.VALIDATION_FAILED, StagingStatus.MERGE_FAILED, StagingStatus.FAILED)
    )
    job.validated_count = counts.get(StagingStatus.VALIDATED, 0) + counts.get(StagingStatus.MERGED, 0)
    job.merged_count = counts.get(StagingStatus.MERGED, 0)
    # failed_count：只有真正 FAILED（重试超限）才计，RETRYING/VALIDATION_FAILED 不计
    job.failed_count = counts.get(StagingStatus.FAILED, 0)


async def _count_terminal_staging(db: AsyncSession, job: ExtractionJob) -> int:
    """统计处于终态的 staging 数（MERGED 或 FAILED）。

    job 完成的判断依据：范围内章节要么 MERGED 要么 FAILED，
    没有 RETRYING / EXTRACTED / VALIDATED 等悬空态。
    """
    result = await db.execute(
        select(func.count(ExtractionStaging.id))
        .where(ExtractionStaging.job_id == str(job.id))
        .where(ExtractionStaging.status.in_([StagingStatus.MERGED, StagingStatus.FAILED]))
    )
    return result.scalar() or 0


async def _count_pending_staging(db: AsyncSession, job: ExtractionJob) -> int:
    """统计处于非终态的 staging 数（RETRYING / EXTRACTED / VALIDATED / VALIDATION_FAILED / MERGE_FAILED）。

    非 0 表示还有章节未到终态，job 不应标记完成。
    """
    result = await db.execute(
        select(func.count(ExtractionStaging.id))
        .where(ExtractionStaging.job_id == str(job.id))
        .where(ExtractionStaging.status.in_([
            StagingStatus.RETRYING, StagingStatus.EXTRACTED,
            StagingStatus.VALIDATED, StagingStatus.VALIDATION_FAILED,
            StagingStatus.MERGE_FAILED,
        ]))
    )
    return result.scalar() or 0


def _compute_last_run_outcome(merged: int, failed: int, extracted: int) -> str:
    """基于 job 计数推断本批实际抽取结果，用于区分'配置了真实模型但没成功'。

    返回：
      none    —— 未跑过（无任何 staging 落库）
      success —— 有合并成功且无失败
      partial —— 有成功也有失败
      failed  —— 全部失败
    """
    if extracted == 0:
        return "none"
    if merged > 0 and failed == 0:
        return "success"
    if merged > 0 and failed > 0:
        return "partial"
    # merged == 0 且 failed > 0
    return "failed"


def _job_to_dict(job: ExtractionJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "provider": getattr(job, "provider", None) or "mock",
        "last_run_outcome": _compute_last_run_outcome(
            job.merged_count, job.failed_count, job.extracted_count,
        ),
        "total_chapters": job.total_chapters,
        "extracted_count": job.extracted_count,
        "validated_count": job.validated_count,
        "merged_count": job.merged_count,
        "failed_count": job.failed_count,
        "error_message": job.error_message,
    }
