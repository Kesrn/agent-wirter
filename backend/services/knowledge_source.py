"""知识源服务 —— 切片、搜索、重索引

设计原则：
- 切片是纯文本处理，不调用 LLM。
- 搜索返回命中结果，不生成回答。
- 摘要/事实/约束等 AI 字段在 Commit 3 的 summarize_project_source 中填充。
- 检索当前走关键词匹配（ILIKE + 打分），向量检索尚未接入：
  project_source_chunks.embedding 字段已就位但暂不填充，_mock_embedding 已停用。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy import String as SAString, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    import uuid

from models.project_source import ProjectSource
from models.project_source_chunk import ProjectSourceChunk
from models.knowledge_qa_session import KnowledgeQaSession
from models.knowledge_qa_message import KnowledgeQaMessage

logger = logging.getLogger(__name__)

# ── 切片配置 ──────────────────────────────────────────

DEFAULT_CHUNK_SIZE = 1200       # 字符数
DEFAULT_CHUNK_OVERLAP = 200     # 重叠字符数
MIN_CHUNK_SIZE = 30             # 最小切片长度（同人规则/短禁忌可能很短）

SECTION_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+.+|第[0-9零一二三四五六七八九十百千万两]+[章节卷回篇].*|正文卷.*|作品相关.*)$"
)
TABLE_ROW_RE = re.compile(r"\|")

QUESTION_STOP_WORDS = (
    "的个人资料", "个人资料", "个人信息", "人物资料", "角色资料", "人物信息",
    "的情况", "情况", "说一下", "介绍一下", "讲一下", "是什么", "是谁",
    "怎么样", "如何", "一下", "请问", "关于", "简介", "资料", "信息",
    "有哪些技能", "有什么技能", "有哪些魔法", "有什么魔法", "有哪些能力", "有什么能力",
    "有哪些系", "有什么系", "拥有哪些系", "几个系", "多少个系", "什么系",
)

MAGIC_SYSTEM_TERMS = (
    "冰系", "寒冰系", "火系", "雷系", "水系", "风系", "土系", "光系",
    "暗影系", "召唤系", "空间系", "植物系", "心灵系", "诅咒系",
    "毒系", "治愈系", "音系", "亡灵系", "混沌系",
)
MAGIC_SYSTEM_RE = "|".join(re.escape(term) for term in MAGIC_SYSTEM_TERMS)

RELATIONSHIP_STATUS_TERMS = (
    "伴侣", "恋人", "情侣", "夫妻", "夫人", "妻子", "丈夫", "男友", "女友",
    "男朋友", "女朋友", "喜欢", "爱慕", "暧昧", "独处", "同居", "约会",
    "妹妹", "哥哥", "兄妹", "家人", "亲人", "青梅竹马", "照顾", "陪伴",
    "雪雪", "心夏", "家里", "越界", "正宫",
)

ABILITY_TABLE_TERMS = (
    "技能", "基础技能", "一阶变体", "二阶变体", "三阶变体",
    "初阶", "中阶", "高阶", "超阶", "禁咒", "阶位",
)


def _like_escape(term: str) -> str:
    """转义 SQL LIKE/ILIKE 通配符 % 与 _，避免用户输入污染匹配语义。

    反斜杠本身也需先转义。配合 ilike 时 SQLAlchemy 默认不启用 ESCAPE 子句，
    这里用反斜杠转义并依赖各后端的默认 escape 行为（SQLite/Postgres 均支持
    反斜杠作为 LIKE escape 字符）。
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ilike_contains(column, term: str):
    """构造 column ILIKE '%term%'，term 中的 % / _ 已转义为字面量。"""
    return column.ilike(f"%{_like_escape(term)}%", escape="\\")


def _is_section_heading(line: str) -> bool:
    return bool(SECTION_HEADING_RE.match(line.strip()))


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.count("|") >= 2 or bool(TABLE_ROW_RE.search(stripped) and re.match(r"^\s*\S+\s+\|\s+\S+", stripped))


def _split_sentences(text: str) -> list[str]:
    sentences = [s.strip() for s in re.split(r"(?<=[。！？.!?])\s*", text) if s.strip()]
    if sentences:
        return sentences
    return [text.strip()] if text.strip() else []


def _tail_overlap(parts: list[str], overlap: int) -> str:
    if not parts or overlap <= 0:
        return ""
    tail = "\n".join(parts)
    return tail[-overlap:].strip()


def _split_long_block(block: str, chunk_size: int, overlap: int) -> list[str]:
    """按句子拆长正文块，避免硬切在句子中间。"""
    sentences = _split_sentences(block)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        if current and current_len + len(sentence) > chunk_size:
            chunks.append("".join(current).strip())
            overlap_text = _tail_overlap(current, overlap)
            current = [overlap_text] if overlap_text else []
            current_len = len(overlap_text)
        if len(sentence) > chunk_size:
            if current:
                chunks.append("".join(current).strip())
                current = []
                current_len = 0
            for start in range(0, len(sentence), max(1, chunk_size - overlap)):
                piece = sentence[start:start + chunk_size].strip()
                if piece:
                    chunks.append(piece)
            continue
        current.append(sentence)
        current_len += len(sentence)
    if current:
        chunks.append("".join(current).strip())
    return [c for c in chunks if c]


def _chunk_table_block(block: str, chunk_size: int) -> list[str]:
    """表格按行切片，重复标题/表头，保证检索命中的行仍有上下文。"""
    lines = [line.rstrip() for line in block.splitlines() if line.strip()]
    if not lines:
        return []
    if len(block) <= chunk_size:
        return [block.strip()]

    prefix: list[str] = []
    data_start = 0
    if lines and not _is_table_row(lines[0]):
        prefix.append(lines[0])
        data_start = 1
    if data_start < len(lines):
        prefix.append(lines[data_start])
        data_start += 1

    chunks: list[str] = []
    current = list(prefix)
    current_len = sum(len(line) + 1 for line in current)
    for line in lines[data_start:]:
        if current_len + len(line) + 1 > chunk_size and len(current) > len(prefix):
            chunks.append("\n".join(current).strip())
            current = list(prefix)
            current_len = sum(len(item) + 1 for item in current)
        current.append(line)
        current_len += len(line) + 1
    if len(current) > len(prefix) or not chunks:
        chunks.append("\n".join(current).strip())
    return chunks


def _split_structural_blocks(text: str) -> list[tuple[str, str]]:
    """把文本拆成 heading/prose/table 三类结构块。"""
    lines = text.splitlines()
    blocks: list[tuple[str, str]] = []
    current: list[str] = []
    current_kind = "prose"

    def flush() -> None:
        nonlocal current
        block = "\n".join(current).strip()
        if block:
            blocks.append((current_kind, block))
        current = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            current_kind = "prose"
            continue
        kind = "table" if _is_table_row(stripped) else "prose"
        if _is_section_heading(stripped):
            kind = "heading"
        if kind == "heading":
            flush()
            current_kind = "heading"
            current = [stripped]
            continue
        if current_kind == "table" and kind != "table":
            flush()
        elif current_kind not in ("table", "heading") and kind == "table":
            flush()
        if kind == "table":
            current_kind = "table"
        elif current_kind == "heading":
            current_kind = "prose"
        current.append(line.rstrip())
    flush()
    return blocks


def chunk_text(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """将资料按结构感知切片。

    优先保留章节标题、Markdown/Excel 表格行和普通正文段落的边界。
    """
    if not text or not text.strip():
        return []

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = _split_structural_blocks(text)
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    def flush_current() -> None:
        nonlocal current_parts, current_len
        if current_parts:
            chunks.append("\n\n".join(current_parts).strip())
            current_parts = []
            current_len = 0

    for kind, block in blocks:
        block_len = len(block)
        if kind == "heading":
            flush_current()
            current_parts = [block]
            current_len = block_len
            continue

        if kind == "table":
            flush_current()
            chunks.extend(_chunk_table_block(block, chunk_size))
            continue

        if _is_section_heading(block.splitlines()[0] if block.splitlines() else ""):
            flush_current()
            if block_len > chunk_size:
                lines = block.splitlines()
                heading = lines[0]
                body = "\n".join(lines[1:]).strip()
                for piece in _split_long_block(body, max(1, chunk_size - len(heading) - 2), overlap):
                    chunks.append(f"{heading}\n{piece}".strip())
            else:
                chunks.append(block)
            continue

        if block_len > chunk_size:
            flush_current()
            chunks.extend(_split_long_block(block, chunk_size, overlap))
            continue

        if current_parts and current_len + block_len + 2 > chunk_size:
            flush_current()

        current_parts.append(block)
        current_len += block_len + 2

    flush_current()

    # 过滤掉太短的切片，但如果只产出一个 chunk 且原文很短则保留（同人规则/短禁忌）
    result = [c for c in chunks if len(c) >= MIN_CHUNK_SIZE]
    if not result and text.strip() and len(text.strip()) >= 2:
        return [text.strip()]
    return result


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文约 1.5 字符/token，英文约 4 字符/token）。"""
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


def _extract_search_keywords(query: str) -> list[str]:
    """提取适合 LIKE 检索的关键词，兼容中文自然问法。

    用户问题通常是“莫凡有什么系”“介绍一下张小侯的个人资料”，直接拿整句
    ILIKE 命中率很低。这里会去掉问句套话，拆出更适合检索的实体/属性词。
    """
    raw_parts = [kw.strip() for kw in re.split(r"[,，\s]+", query) if kw.strip()]
    keywords: list[str] = []

    def add(value: str) -> None:
        value = value.strip(" 的，,。！？!?：:；;、\"'""''（）()[]【】")
        if len(value) >= 2 and value not in keywords:
            keywords.append(value)

    for part in raw_parts:
        simplified = part
        for stop in QUESTION_STOP_WORDS:
            simplified = simplified.replace(stop, " ")
        before_count = len(keywords)
        for token in re.split(r"\s+", simplified):
            add(token)
            for sub_token in re.split(r"[的是]", token):
                add(sub_token)
        if len(keywords) == before_count:
            add(part)

    return keywords


def _mock_embedding(text: str, dim: int = 384) -> list[float]:
    """[已停用] SHA256 哈希伪向量。

    语义上完全无效（同义不同字 → 不同向量），且 SHA256 hex 仅 64 字符只能产生
    32 个非零分量，维度声明 384 但后半全 0。资料库检索当前走关键词匹配，
    未调用此函数。保留仅作为将来接入真实 embedding 服务时的签名参考。
    真正接入前不应在 chunk_and_save 中生成 embedding。
    """
    import hashlib
    h = hashlib.sha256(text.encode()).hexdigest()
    values = [int(h[i:i + 2], 16) / 255.0 for i in range(0, min(len(h), dim * 2), 2)]
    # 归一化
    norm = sum(v ** 2 for v in values) ** 0.5
    if norm > 0:
        values = [v / norm for v in values]
    # 填充或截断到 dim
    if len(values) < dim:
        values.extend([0.0] * (dim - len(values)))
    return values[:dim]


# ── 核心服务函数 ──────────────────────────────────────


async def chunk_and_save(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    source_id: str | uuid.UUID,
) -> int:
    """将资料条目切片并保存到 project_source_chunks 表。

    返回切片数量。如果资料内容为空则返回 0。

    这个函数是资料库入库/重建索引的核心步骤：
    1. 找到 ProjectSource 原文；
    2. 删除旧 chunks，避免重复索引；
    3. 用 chunk_text 做结构感知切片；
    4. 写入 ProjectSourceChunk，并记录粗略 token_count。
    """
    pid = str(project_id)
    sid = str(source_id)

    result = await db.execute(
        select(ProjectSource).where(ProjectSource.id == sid, ProjectSource.project_id == pid)
    )
    source = result.scalar_one_or_none()
    if not source or not source.content.strip():
        return 0

    # 删除旧切片：重建索引时采用“先删后建”，保证 chunk_index 连续且旧内容不会残留。
    old_chunks = await db.execute(
        select(ProjectSourceChunk).where(
            ProjectSourceChunk.source_id == sid, ProjectSourceChunk.project_id == pid
        )
    )
    for c in old_chunks.scalars().all():
        await db.delete(c)

    # 切片：保留章节标题、表格和句子边界，后续检索命中时 snippet 更可读。
    raw_chunks = chunk_text(source.content)
    if not raw_chunks:
        source.chunk_count = 0
        await db.commit()
        return 0

    for i, chunk_content in enumerate(raw_chunks):
        chunk = ProjectSourceChunk(
            project_id=pid,
            source_id=sid,
            chunk_index=i,
            content=chunk_content,
            token_count=_estimate_tokens(chunk_content),
            # embedding 暂不生成：资料库 RAG 当前走关键词检索，向量检索尚未接入。
            # 真正接入 embedding 服务后再在此填充（见 _mock_embedding 上方说明）。
        )
        db.add(chunk)

    source.chunk_count = len(raw_chunks)
    await db.commit()
    logger.info("chunk_and_save: source=%s chunks=%d", sid, len(raw_chunks))
    return len(raw_chunks)


async def search_project_knowledge(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    query: str,
    *,
    source_type: str | None = None,
    limit: int = 10,
    required_terms: list[str] | None = None,
    boost_terms: list[str] | None = None,
    chapter_num: int | None = None,
    intent: str | None = None,
) -> list[dict]:
    """纯检索：在资料库中搜索匹配的内容片段。

    通过 required_terms / boost_terms 增强召回质量。
    不调用 LLM，只返回命中结果。
    
    Args:
        intent: 查询意图，如 "character_by_ability" 表示反向属性检索
    """
    pid = str(project_id)
    keywords = _extract_search_keywords(query)
    if not keywords and not required_terms:
        return []

    # 合并所有检索词。required_terms 来自 Query Planner，代表必须尽量命中的核心词；
    # keywords 来自自然问句拆词，负责扩大召回。
    all_terms = list(keywords)
    if required_terms:
        all_terms.extend(required_terms)

    # 搜索 chunks：正文、chunk 摘要、chunk keywords 都参与匹配。
    # 当前版本使用关键词检索；如果接入 pgvector，这里会增加向量距离排序。
    conditions = []
    for kw in all_terms:
        conditions.append(_ilike_contains(ProjectSourceChunk.content, kw))
        conditions.append(_ilike_contains(ProjectSourceChunk.summary, kw))
        # 也搜 chunk keywords 字段
        conditions.append(_ilike_contains(ProjectSourceChunk.keywords.cast(SAString), kw))

    # 搜 source 级别的 summary / key_facts / keywords。
    # 有些资料在 chunk 中未命中，但资料级摘要/事实字段已经提炼出关键词，也应召回。
    for kw in all_terms:
        conditions.append(_ilike_contains(ProjectSource.summary, kw))
        conditions.append(_ilike_contains(ProjectSource.key_facts.cast(SAString), kw))
        conditions.append(_ilike_contains(ProjectSource.keywords.cast(SAString), kw))

    if not conditions:
        return []

    stmt = (
        select(ProjectSourceChunk, ProjectSource.title, ProjectSource.always_inject, ProjectSource.source_type)
        .join(ProjectSource, ProjectSourceChunk.source_id == ProjectSource.id)
        .where(ProjectSourceChunk.project_id == pid)
    )
    if source_type:
        stmt = stmt.where(ProjectSource.source_type == source_type)
    for rt in required_terms or []:
        stmt = stmt.where(or_(
            _ilike_contains(ProjectSourceChunk.content, rt),
            _ilike_contains(ProjectSourceChunk.summary, rt),
            _ilike_contains(ProjectSource.summary, rt),
            _ilike_contains(ProjectSource.key_facts.cast(SAString), rt),
        ))
    stmt = stmt.where(or_(*conditions))
    # 候选上限：多 required_terms 时放宽候选池以便重排，单条件时收紧。
    candidate_limit = min(max(limit * 80, 400), 1500) if required_terms and len(required_terms) >= 2 else min(limit * 10, 200)
    stmt = stmt.limit(candidate_limit)

    result = await db.execute(stmt)
    rows = result.all()

    results: list[dict] = []
    seen_ids: set[str] = set()
    for chunk, title, always_inject, src_type in rows:
        cid = str(chunk.id)
        if cid in seen_ids:
            continue
        seen_ids.add(cid)

        searchable_text = "\n".join([
            title or "",
            chunk.summary or "",
            chunk.content or "",
        ]).lower()

        # ── 评分 ──
        score = 0.0

        # required_terms 命中：基础分 +10
        if required_terms:
            for rt in required_terms:
                if rt.lower() in searchable_text:
                    score += 10.0

        # query keywords 命中数
        matched_kw = sum(1 for kw in keywords if kw.lower() in searchable_text)
        score += matched_kw

        # boost_terms 命中数 * 0.3
        if boost_terms:
            matched_boost = sum(1 for bt in boost_terms if bt.lower() in searchable_text)
            score += matched_boost * 0.3

        # ── 反向属性检索特殊处理 ──
        CHARACTER_BY_ABILITY_TERMS = (
            "觉醒", "拥有", "修炼", "掌握", "主修", "辅修",
            "第一系", "第二系", "第三系", "天生", "法师", "人物", "角色",
        )
        CHARACTER_BY_ABILITY_NEGATION_TERMS = (
            "没有说明", "未说明", "没说明", "不能说明", "无法说明",
            "不是", "并非", "不属于", "没有明确", "未明确", "无法确定",
        )
        # 优先使用 intent 参数判断，如果没有则从 query 推断
        is_character_by_ability_query = (
            intent == "character_by_ability"
            or any(marker in query for marker in ("谁是", "谁有", "哪些人", "哪些角色", "人物有哪些", "角色有哪些", "有哪些人", "有哪些角色"))
        )
        character_by_ability_window: str | None = None
        if is_character_by_ability_query and required_terms:
            # 检查是否有"人名 + 觉醒/拥有/修炼/掌握 + 冰系"模式
            for rt in required_terms:
                if rt.endswith("系"):
                    # 模式1: 人名 + 觉醒/拥有/修炼/掌握 + 冰系
                    pattern1 = rf"([\u4e00-\u9fff]{{2,6}}).{{0,60}}(?:觉醒|拥有|修炼|掌握|主修|辅修|天生).{{0,60}}{re.escape(rt)}"
                    # 模式2: 人名 + 第X系 + 冰系
                    pattern2 = rf"([\u4e00-\u9fff]{{2,6}}).{{0,60}}(?:第一系|第二系|第三系|主修|辅修).{{0,60}}{re.escape(rt)}"
                    # 模式3: 人名 + 冰系法师/能力。不要使用“冰系 + 任意汉字”模式，
                    # 它会把“冰系技能/冰系包括”误判成人物证据。
                    pattern3 = rf"([\u4e00-\u9fff]{{2,6}}).{{0,40}}{re.escape(rt)}(?:法师|能力|魔法)"
                    
                    for pattern in [pattern1, pattern2, pattern3]:
                        match = re.search(pattern, chunk.content)
                        if match:
                            # 提取包含人名和系别的窗口
                            start = max(0, match.start() - 150)
                            end = min(len(chunk.content), match.end() + 150)
                            character_by_ability_window = chunk.content[start:end]
                            if any(term in character_by_ability_window for term in CHARACTER_BY_ABILITY_NEGATION_TERMS):
                                character_by_ability_window = None
                                continue
                            score += 18.0  # 高权重，优先于技能表
                            # 额外加权：如果窗口中包含人物相关词汇
                            if any(term in character_by_ability_window for term in CHARACTER_BY_ABILITY_TERMS):
                                score += 5.0
                            break
                    if character_by_ability_window:
                        break
        
        # 如果是反向属性查询但没有匹配到人物模式，降权技能表
        if is_character_by_ability_query and required_terms and not character_by_ability_window:
            # 检查是否是技能表（包含大量技能术语但没有人名）
            skill_table_indicators = sum(1 for term in ABILITY_TABLE_TERMS if term in chunk.content)
            if skill_table_indicators >= 1 or "技能表" in (title or ""):
                score *= 0.15  # 技能表在反向属性查询中强降权

        is_system_query = any(kw in {"系", "魔法", "能力", "技能"} or kw.endswith("系") for kw in keywords + (required_terms or []))
        direct_system_window: str | None = None
        if is_system_query and any(term in searchable_text for term in MAGIC_SYSTEM_TERMS):
            score += 1.0
            if not is_character_by_ability_query:
                for rt in required_terms or []:
                    direct_system_pattern = (
                        rf"{re.escape(rt)}.{{0,140}}(?:觉醒|学会|修炼|拥有|释放|掌握|天生双系|星尘|星辰|星云|星河).{{0,140}}(?:{MAGIC_SYSTEM_RE})"
                        rf"|{re.escape(rt)}.{{0,140}}(?:{MAGIC_SYSTEM_RE}).{{0,40}}(?:星尘|星辰|星云|星河)"
                        rf"|{re.escape(rt)}.{{0,220}}(?:基础技能|一阶变体|二阶变体|三阶变体|初阶|中阶|高阶|超阶|禁咒|技能)"
                    )
                    match = re.search(direct_system_pattern, chunk.content)
                    if match:
                        score += 16.0
                        system_rows = [
                            line.strip()
                            for line in re.split(r"[\n\r]+", chunk.content)
                            if rt in line
                        ]
                        if len(system_rows) >= 2:
                            direct_system_window = "\n".join(system_rows[:12])
                        else:
                            direct_system_window = chunk.content[max(0, match.start() - 120): min(len(chunk.content), match.end() + 180)]
                        break
            # 反向属性查询时，技能表加分也要降权
            for system_term in MAGIC_SYSTEM_TERMS:
                if system_term in (required_terms or []) and system_term in chunk.content:
                    table_hits = [term for term in ABILITY_TABLE_TERMS if term in chunk.content]
                    if table_hits:
                        if is_character_by_ability_query:
                            score += 2.0  # 反向属性查询时技能表只给低分
                        else:
                            score += 14.0 + min(len(table_hits), 5)
                        if not direct_system_window:
                            system_rows = [
                                line.strip()
                                for line in re.split(r"[\n\r]+", chunk.content)
                                if system_term in line
                            ]
                            if len(system_rows) >= 2:
                                direct_system_window = "\n".join(system_rows[:12])
                            else:
                                idx = chunk.content.find(system_term)
                                direct_system_window = chunk.content[max(0, idx - 220): min(len(chunk.content), idx + 520)]
                    break
        if is_system_query and required_terms and not direct_system_window and not character_by_ability_window:
            score *= 0.55

        is_relationship_query = any(kw in {"关系", "感情", "冲突", "伴侣", "恋人", "夫人", "男友", "女友", "独处"} for kw in keywords)
        relation_window: str | None = None
        if is_relationship_query:
            if required_terms and len(required_terms) >= 2:
                both_entities_hit = all(rt.lower() in searchable_text for rt in required_terms[:2])
                if both_entities_hit:
                    score += 8.0
                    first_positions = [m.start() for m in re.finditer(re.escape(required_terms[0]), chunk.content)]
                    second_positions = [m.start() for m in re.finditer(re.escape(required_terms[1]), chunk.content)]
                    best_pair: tuple[int, int] | None = None
                    best_distance: int | None = None
                    for p1 in first_positions:
                        for p2 in second_positions:
                            distance = abs(p1 - p2)
                            if best_distance is None or distance < best_distance:
                                best_pair = (p1, p2)
                                best_distance = distance
                    if best_pair and best_distance is not None and best_distance <= 900:
                        start = max(0, min(best_pair) - 220)
                        end = min(len(chunk.content), max(best_pair) + 420)
                        relation_window = chunk.content[start:end]
                        score += 6.0
                        relationship_hits = [term for term in RELATIONSHIP_STATUS_TERMS if term in relation_window]
                        if relationship_hits:
                            score += 12.0 + min(len(relationship_hits), 5) * 2.0
                    else:
                        relationship_hits = [term for term in RELATIONSHIP_STATUS_TERMS if term in chunk.content]
                        if relationship_hits:
                            score += min(len(relationship_hits), 3)
            else:
                relationship_hits = [term for term in RELATIONSHIP_STATUS_TERMS if term in chunk.content]
                if relationship_hits:
                    score += min(len(relationship_hits), 4) * 2.0

        # summary/keywords 命中加权
        if chunk.summary:
            for kw in all_terms[:5]:
                if kw.lower() in chunk.summary.lower():
                    score += 1.2
                    break

        # always_inject 加权
        if always_inject:
            score += 8.0

        # source_type 加权
        type_weight = {"fanfic_rule": 5, "reference": 3, "timeline": 2, "note": 1, "upload": 0}
        score += type_weight.get(src_type, 0)

        # required_terms 全部未命中则降分
        if required_terms:
            any_hit = any(rt.lower() in searchable_text for rt in required_terms)
            if not any_hit:
                score *= 0.2

        if is_character_by_ability_query and not character_by_ability_window:
            skill_table_indicators = sum(1 for term in ABILITY_TABLE_TERMS if term in chunk.content)
            if skill_table_indicators >= 1 or "技能表" in (title or ""):
                continue
            if any(term in chunk.content for term in CHARACTER_BY_ABILITY_NEGATION_TERMS):
                continue

        # 截取包含关键词的片段（前后各 100 字符）
        snippet = chunk.content[:300]
        snippet_terms = []
        if character_by_ability_window:
            snippet = character_by_ability_window
        elif relation_window:
            snippet = relation_window
        elif direct_system_window:
            snippet = direct_system_window
        has_special_window = bool(character_by_ability_window or relation_window or direct_system_window)
        if is_character_by_ability_query and not character_by_ability_window:
            snippet_terms.extend([term for term in CHARACTER_BY_ABILITY_TERMS if term in chunk.content])
        if is_system_query and not direct_system_window:
            snippet_terms.extend([term for term in MAGIC_SYSTEM_TERMS if term in chunk.content])
        if is_relationship_query and not relation_window:
            snippet_terms.extend([term for term in RELATIONSHIP_STATUS_TERMS if term in chunk.content])
        snippet_terms.extend(all_terms[:3])
        if not has_special_window:
            for kw in snippet_terms:
                idx = chunk.content.lower().find(kw.lower())
                if idx >= 0:
                    start = max(0, idx - 100)
                    end = min(len(chunk.content), idx + len(kw) + 100)
                    snippet = chunk.content[start:end]
                    break

        # 确定 matched_query
        matched_q = ""
        for kw in all_terms:
            if kw.lower() in searchable_text:
                matched_q = kw
                break

        results.append({
            "source_kind": "project_source_chunk",
            "source_id": str(chunk.source_id),
            "chunk_id": cid,
            "title": title,
            "snippet": snippet,
            "score": round(score, 2),
            "matched_query": matched_q,
        })

    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:limit]


async def _search_structured_knowledge(
    db: AsyncSession,
    project_id: str,
    plan,
    *,
    limit: int = 10,
) -> list[dict]:
    """检索结构化表（characters / character_events / outlines / world_entries / hidden_threads）。"""
    from models.character import Character
    from models.character_event import CharacterEvent
    from models.outline import Outline
    from models.world_entry import WorldEntry
    from models.hidden_thread import HiddenThread

    entities = plan.entities or plan.keywords[:2]
    all_terms = entities + (plan.keywords or [])
    if not all_terms:
        return []

    results: list[dict] = []

    # ── characters ──
    try:
        char_conds = []
        for t in all_terms[:5]:
            char_conds.append(_ilike_contains(Character.name, t))
            char_conds.append(_ilike_contains(Character.profile, t))
        if char_conds:
            chars_result = await db.execute(
                select(Character).where(
                    Character.project_id == project_id,
                    or_(*char_conds),
                ).limit(limit)
            )
            for c in chars_result.scalars().all():
                searchable = f"{c.name} {c.profile or ''} {c.faction or ''}".lower()
                score = 0.0
                for e in entities:
                    if e.lower() in searchable:
                        score += 10.0  # 角色名精确匹配
                matched = sum(1 for t in all_terms[:3] if t.lower() in searchable)
                score += matched

                results.append({
                    "source_kind": "character",
                    "source_id": str(c.id),
                    "chunk_id": None,
                    "title": f"角色：{c.name}（{c.role_type}）",
                    "snippet": (c.profile or "")[:500],
                    "score": round(score, 2),
                    "matched_query": next((t for t in entities if t.lower() in searchable), ""),
                })
    except Exception:
        pass

    # ── character_events ──
    try:
        ev_conds = []
        for t in all_terms[:5]:
            ev_conds.append(_ilike_contains(CharacterEvent.event_summary, t))
        if ev_conds:
            ev_result = await db.execute(
                select(CharacterEvent, Character.name).join(
                    Character, CharacterEvent.character_id == Character.id
                ).where(
                    CharacterEvent.project_id == project_id,
                    or_(*ev_conds),
                ).limit(limit)
            )
            for ev, char_name in ev_result.all():
                score = 0.0
                searchable = f"{char_name} {ev.event_summary or ''} {ev.state_change or ''}".lower()
                for e in entities:
                    if e.lower() in searchable:
                        score += 5.0
                matched = sum(1 for t in all_terms[:3] if t.lower() in searchable)
                score += matched

                results.append({
                    "source_kind": "character_event",
                    "source_id": str(ev.id),
                    "chunk_id": None,
                    "title": f"{char_name}·第{ev.chapter_sequence_number}章事件",
                    "snippet": ev.event_summary or "",
                    "score": round(score, 2),
                    "matched_query": "",
                })
    except Exception:
        pass

    # ── outlines ──
    try:
        ol_conds = []
        for t in all_terms[:5]:
            ol_conds.append(_ilike_contains(Outline.title, t))
            ol_conds.append(_ilike_contains(Outline.summary, t))
        if ol_conds:
            ol_result = await db.execute(
                select(Outline).where(
                    Outline.project_id == project_id,
                    or_(*ol_conds),
                ).limit(limit)
            )
            for o in ol_result.scalars().all():
                searchable = f"{o.title} {o.summary or ''} {o.turning_point or ''}".lower()
                score = 0.0
                matched = sum(1 for t in all_terms[:3] if t.lower() in searchable)
                score += matched * 1.5

                results.append({
                    "source_kind": "outline",
                    "source_id": str(o.id),
                    "chunk_id": None,
                    "title": f"大纲·第{o.sequence_number}章 {o.title}",
                    "snippet": (o.summary or "")[:500],
                    "score": round(score, 2),
                    "matched_query": "",
                })
    except Exception:
        pass

    # ── world_entries ──
    try:
        we_conds = []
        for t in all_terms[:5]:
            we_conds.append(_ilike_contains(WorldEntry.title, t))
            we_conds.append(_ilike_contains(WorldEntry.content, t))
        if we_conds:
            we_result = await db.execute(
                select(WorldEntry).where(
                    WorldEntry.project_id == project_id,
                    or_(*we_conds),
                ).limit(limit)
            )
            for w in we_result.scalars().all():
                searchable = f"{w.title} {w.content or ''}".lower()
                score = 0.0
                for e in entities:
                    if e.lower() in searchable:
                        score += 3.0
                matched = sum(1 for t in all_terms[:3] if t.lower() in searchable)
                score += matched

                results.append({
                    "source_kind": "world_entry",
                    "source_id": str(w.id),
                    "chunk_id": None,
                    "title": f"设定：{w.title}（{w.category}）",
                    "snippet": (w.content or "")[:500],
                    "score": round(score, 2),
                    "matched_query": next((t for t in entities if t.lower() in searchable), ""),
                })
    except Exception:
        pass

    # ── hidden_threads ──
    try:
        ht_conds = []
        for t in all_terms[:5]:
            ht_conds.append(_ilike_contains(HiddenThread.name, t))
            ht_conds.append(_ilike_contains(HiddenThread.description, t))
        if ht_conds:
            ht_result = await db.execute(
                select(HiddenThread).where(
                    HiddenThread.project_id == project_id,
                    or_(*ht_conds),
                ).limit(limit)
            )
            for h in ht_result.scalars().all():
                searchable = f"{h.name} {h.description or ''}".lower()
                score = 0.0
                matched = sum(1 for t in all_terms[:3] if t.lower() in searchable)
                score += matched * 1.5

                results.append({
                    "source_kind": "hidden_thread",
                    "source_id": str(h.id),
                    "chunk_id": None,
                    "title": f"暗线：{h.name}",
                    "snippet": (h.description or "")[:500],
                    "score": round(score, 2),
                    "matched_query": "",
                })
    except Exception:
        pass

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


async def search_project_knowledge_with_plan(
    db: AsyncSession,
    project_id: str,
    plan,
    *,
    limit: int = 12,
) -> tuple[list[dict], list[dict]]:
    """多路检索：结构化 + 非结构化联合召回，去重排序。

    返回 (structured_results, chunk_results)。

    plan.search_queries 可能有多条，例如“莫凡 雷系”“莫凡 能力”，每条都跑一次
    search_project_knowledge，最后按 chunk_id 去重并保留最高分结果。
    """
    # 1. 结构化表检索：角色表、事件表、世界观等“可信结构化数据”优先召回。
    structured = await _search_structured_knowledge(db, project_id, plan, limit=limit)

    # 2. 非结构化 chunk 检索（多 query 并集）。
    # 同一个 chunk 被多条 query 命中时，保留分数更高或 snippet 更完整的版本。
    chunk_results_by_id: dict[str, dict] = {}

    for sq in plan.search_queries:
        hits = await search_project_knowledge(
            db, project_id, sq,
            required_terms=plan.required_terms,
            boost_terms=plan.keywords,
            limit=limit,
            intent=plan.intent,
        )
        for h in hits:
            cid = h.get("chunk_id", "")
            if not cid:
                continue
            existing = chunk_results_by_id.get(cid)
            if (
                existing is None
                or h.get("score", 0) > existing.get("score", 0)
                or len(h.get("snippet", "")) > len(existing.get("snippet", ""))
            ):
                chunk_results_by_id[cid] = h
    chunk_results = list(chunk_results_by_id.values())

    # 3. 去重（结构化 + chunk 之间）。
    # 去重 key 包含 source_kind/source_id/chunk_id，避免同一证据在 prompt 中重复出现。
    all_ids: set[str] = set()
    deduped_structured = []
    for r in structured:
        key = f"{r['source_kind']}:{r['source_id']}:{r.get('chunk_id', '')}"
        if key not in all_ids:
            all_ids.add(key)
            deduped_structured.append(r)

    deduped_chunks = []
    for r in chunk_results:
        key = f"{r['source_kind']}:{r['source_id']}:{r.get('chunk_id', '')}"
        if key not in all_ids:
            all_ids.add(key)
            deduped_chunks.append(r)
    deduped_chunks.sort(key=lambda r: r.get("score", 0), reverse=True)

    return deduped_structured, deduped_chunks


async def reindex_project_sources(
    db: AsyncSession,
    project_id: str | uuid.UUID,
) -> dict[str, int]:
    """重新切片项目的所有资料条目。

    返回 {"total_sources": N, "total_chunks": M, "failed": K}。

    重建索引用于切片策略升级、资料导入修复或用户手动点击“重新索引”。
    单个 source 失败不会中断整个项目，最终通过 failed 计数反馈给前端。
    """
    pid = str(project_id)

    result = await db.execute(
        select(ProjectSource).where(ProjectSource.project_id == pid)
    )
    sources = result.scalars().all()

    total_chunks = 0
    failed = 0
    for source in sources:
        try:
            n = await chunk_and_save(db, pid, source.id)
            total_chunks += n
        except Exception:
            logger.exception("reindex failed for source %s", source.id)
            failed += 1

    return {
        "total_sources": len(sources),
        "total_chunks": total_chunks,
        "failed": failed,
    }


# ── AI 摘要 ──────────────────────────────────────────

SUMMARIZE_SOURCE_PROMPT = """\
你是一位资料分析专家。请对以下小说资料进行结构化分析。

要求：
1. summary: 200字以内的资料摘要
2. key_facts: 关键事实列表（每项一句话）
3. constraints: 约束/禁忌/规则列表
4. characters: 涉及的角色名称列表
5. keywords: 关键词列表（用于检索匹配）

只输出严格 JSON，不要其他内容。

```json
{
  "summary": "...",
  "key_facts": ["...", "..."],
  "constraints": ["...", "..."],
  "characters": ["...", "..."],
  "keywords": ["...", "..."]
}
```"""

SUMMARIZE_CHUNK_PROMPT = """\
你是一位资料分析专家。请对以下小说资料片段进行简要分析。

要求：
1. summary: 50字以内的片段摘要
2. facts: 该片段包含的关键事实列表
3. constraints: 该片段中的约束/禁忌（如有）
4. keywords: 该片段的关键词

只输出严格 JSON，不要其他内容。

```json
{
  "summary": "...",
  "facts": ["..."],
  "constraints": ["..."],
  "keywords": ["...", "..."]
}
```"""


def _safe_parse_json(text: str) -> dict:
    """从 LLM 响应中安全提取 JSON。

    摘要任务要求模型输出严格 JSON，但真实模型可能带 Markdown 代码块或解释文字。
    这里按“直接解析 -> 代码块 -> 第一个大括号范围”逐级降级。
    """
    import json
    # 尝试直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # 尝试从 ```json ... ``` 中提取
    import re
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass
    # 尝试找第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


async def summarize_project_source(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    source_id: str | uuid.UUID,
    user_id: str | uuid.UUID,
) -> dict:
    """使用 LLM 对资料条目生成摘要、事实、约束、关键词。

    原文永远保存。摘要字段失败时不丢原文，只返回空摘要。

    这些摘要/关键词用于提升后续关键词检索质量，不是唯一事实来源。
    因此失败时不能删除用户上传的原文，也不应阻断 chunk 检索。
    """
    pid = str(project_id)
    sid = str(source_id)

    result = await db.execute(
        select(ProjectSource).where(ProjectSource.id == sid, ProjectSource.project_id == pid)
    )
    source = result.scalar_one_or_none()
    if not source:
        return {"error": "资料不存在"}

    # 获取 LLM provider：使用用户自己的模型配置，使摘要和问答/生成保持同一模型上下文。
    from agents.llm_provider import get_llm_provider
    from api.llm_deps import get_user_llm_config

    llm_config_dict = await get_user_llm_config(str(user_id), db)
    provider = get_llm_provider(llm_config_dict)

    # 资料全文（截断到 8000 字符避免撑爆 context）。
    # 原文仍完整保存在 source.content，只是摘要调用不把超长文本一次性塞给模型。
    content_for_llm = source.content[:8000] if source.content else ""
    if not content_for_llm.strip():
        return {"chunk_count": 0, "message": "资料内容为空，跳过摘要"}

    # ── 1. 资料级摘要 ──
    # source.summary/key_facts/constraints 是“资料整体”层面的索引字段。
    try:
        result_text = await provider.generate(
            SUMMARIZE_SOURCE_PROMPT,
            f"## 资料标题\n{source.title}\n\n## 资料内容\n{content_for_llm}",
            temperature=0.2,
            max_tokens=1500,
        )
        parsed = _safe_parse_json(result_text)
        source.summary = parsed.get("summary", "")
        source.key_facts = parsed.get("key_facts", [])
        source.constraints = parsed.get("constraints", [])
        source.characters = parsed.get("characters", [])
        source.keywords = parsed.get("keywords", [])
    except Exception:
        logger.exception("资料摘要失败，保留原文不丢失")
        # 不清空已有摘要，直接跳过

    # ── 2. Chunk 级摘要 ──
    # chunk.summary/facts/keywords 是“片段级”索引字段，用于精确命中具体证据。
    chunks_result = await db.execute(
        select(ProjectSourceChunk)
        .where(ProjectSourceChunk.source_id == sid, ProjectSourceChunk.project_id == pid)
        .order_by(ProjectSourceChunk.chunk_index)
    )
    chunks = chunks_result.scalars().all()

    for chunk in chunks:
        if not chunk.content.strip():
            continue
        try:
            chunk_result_text = await provider.generate(
                SUMMARIZE_CHUNK_PROMPT,
                f"## 片段内容\n{chunk.content[:3000]}",
                temperature=0.2,
                max_tokens=500,
            )
            chunk_parsed = _safe_parse_json(chunk_result_text)
            chunk.summary = chunk_parsed.get("summary", "")
            chunk.facts = chunk_parsed.get("facts", [])
            chunk.constraints = chunk_parsed.get("constraints", [])
            chunk.keywords = chunk_parsed.get("keywords", [])
        except Exception:
            logger.exception("chunk %s 摘要失败", chunk.id)
            # chunk 摘要失败不阻断

    await db.commit()
    logger.info("summarize_project_source: source=%s chunks=%d", sid, len(chunks))

    return {
        "source_id": sid,
        "chunk_count": len(chunks),
        "summary": source.summary or "",
        "key_facts": source.key_facts or [],
        "constraints": source.constraints or [],
        "characters": source.characters or [],
        "keywords": source.keywords or [],
    }


# ── 资料问答 ──────────────────────────────────────────

QA_SYSTEM_PROMPT = """\
你是小说资料库的 AI 助手。用户会问关于小说资料的问题。
你必须基于提供的资料片段、结构化数据和可选联网查询结果回答。
回答必须附带引用来源。如果资料中没有相关信息，如实说明。
当用户询问"个人资料 / 是谁 / 情况 / 介绍一下"时，只要参考资料中出现该人物或对象，就应归纳资料中可见的身份、关系、能力、经历等信息；不要因为资料没有直接写成档案格式就回答"没有资料"。
当用户询问"有哪些系 / 能力 / 魔法系"时，只能列出资料片段中明确支持的系别；不要因为一个整理性标题或单个模糊片段就宣称"总共有 N 个系"。如果资料只显示阶段性觉醒或疑似来源，请分成"明确提到"和"仍需确认"。
当用户询问"谁是某系 / 哪些角色有某系 / 某系人物有哪些"时：
- 只列出资料片段中明确把人物与该系绑定的角色。
- 不要把技能表中的"冰系技能"误认为某个人物属于冰系。
- 如果只找到部分证据，要说明"当前资料中明确提到的是..."。
- 如果存在疑似但不明确的内容，放到"待确认"里。
当用户询问"某人的某系是什么样 / 某人的某系详情"时：
- 先找资料中是否明确写了该人物与该法系的个人绑定、觉醒、修炼或表现。
- 如果没有直接个人证据，但资料库提供了该法系的通用技能表或设定，可以回答"当前资料未明确该人物个人的该系表现；通用该系资料如下..."。
- 不要因为缺少"人物+法系"同句证据就忽略通用法系资料。
回答尽量用简洁 Markdown：短结论在前，后面用分行列表；不要把 1、2、3 全挤在同一段里。
引用来源只能来自提供的资料片段、结构化数据或联网查询结果，不能编造新的资料标题。
联网查询结果属于外部参考，回答时应和项目资料区分；不要把网页信息自动写成小说内既定设定。

输出格式：
```json
{
  "answer": "你的回答",
  "citations": [
    {
      "source_kind": "project_source_chunk | character | character_event | outline | world_entry | hidden_thread | chapter | web_search",
      "source_id": "ID",
      "chunk_id": "chunk ID 或 null",
      "title": "来源标题",
      "snippet": "引用片段"
    }
  ]
}
```"""

# 资料库检索上下文预算
KNOWLEDGE_QA_RECENT_MESSAGES = 6
KNOWLEDGE_QA_MAX_CONTEXT_CHARS = 24000      # 证据+history+web+policy 的总字符预算（硬上限）
# 每 source 最大 chunk 数：单一来源在 retrieval 层，此处仅引用，避免双常量漂移。
from services.knowledge_retrieval import MAX_CHUNKS_PER_SOURCE as KNOWLEDGE_QA_MAX_CHUNKS_PER_SOURCE  # noqa: E402


def _polish_qa_answer_format(answer: str) -> str:
    """把模型挤成一行的编号列表整理成可读 Markdown。"""
    text = (answer or "").strip()
    if not text:
        return text
    text = re.sub(r"\s+(\d+\.\s+)", r"\n\1", text)
    text = re.sub(r"(:|：)\n(1\.\s+)", r"\1\n\n\2", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_table_cell(value: str) -> str:
    value = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9·—\-（）()、，,：: ]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _format_magic_system_row(line: str, system: str) -> str | None:
    """把包含法系的表格行整理成可读技能行。"""
    if system not in line or "|" not in line:
        return None
    cells = [_clean_table_cell(cell) for cell in line.strip().strip("|").split("|")]
    cells = [cell for cell in cells if cell and cell not in {"序号", "法系", "法系大类", "备注"}]
    if len(cells) < 3:
        return None

    system_idx = next((i for i, cell in enumerate(cells) if system in cell), -1)
    if system_idx < 0:
        return None
    stage_idx = next(
        (i for i, cell in enumerate(cells) if re.search(r"(初阶|中阶|高阶|超阶|禁咒)", cell)),
        -1,
    )
    if stage_idx < 0:
        stage_idx = min(system_idx + 1, len(cells) - 1)
    stage = cells[stage_idx]

    skill_cells = [
        cell for i, cell in enumerate(cells)
        if i not in {system_idx, stage_idx}
        and not cell.isdigit()
        and cell not in {"—", "-", "无"}
        and system not in cell
    ]
    if not skill_cells:
        return None
    return f"- {stage}：{'；'.join(skill_cells[:6])}"


def _extract_magic_system_lines(system: str, evidence_items: list) -> list[str]:
    """从证据片段中抽取某个法系的技能/设定行。"""
    lines: list[str] = []
    seen: set[str] = set()
    for item in evidence_items:
        snippet = getattr(item, "snippet", "") or ""
        for raw_line in re.split(r"[\n\r]+", snippet):
            line = raw_line.strip()
            if system not in line:
                continue
            formatted = _format_magic_system_row(line, system)
            if not formatted:
                formatted = re.sub(r"\s+", " ", line)
                if len(formatted) > 180:
                    formatted = formatted[:180].rstrip() + "..."
                formatted = f"- {formatted}"
            if formatted not in seen:
                seen.add(formatted)
                lines.append(formatted)
            if len(lines) >= 12:
                return lines
    return lines


def _is_negative_text(text: str) -> bool:
    return any(term in text for term in (
        "没有说明", "未说明", "没有明确", "未明确", "无法确定", "没有提到", "未提到",
    ))


def _extract_characters_bound_to_system(system: str, evidence_items: list) -> list[str]:
    """从片段中抽取“人物-法系”绑定，避免把技能表误当人物证据。"""
    names: list[str] = []
    seen: set[str] = set()
    generic_names = {
        "技能包括", "法系通用", "基础技能", "一阶变体", "二阶变体", "三阶变体",
        "高阶技能", "中阶技能", "初阶技能", "超阶技能", "当前资料", "资料片段",
    }

    def add(name: str) -> None:
        name = name.strip(" ，,。；;：:")
        if not (2 <= len(name) <= 6):
            return
        if name in generic_names or name.endswith(("技能", "法系", "资料", "片段", "角色")):
            return
        if name not in seen:
            seen.add(name)
            names.append(name)

    patterns = (
        rf"([\u4e00-\u9fff]{{2,6}}).{{0,60}}(?:觉醒|拥有|修炼|掌握|主修|辅修|天生|第三系|第二系|第一系).{{0,60}}{re.escape(system)}",
        rf"([\u4e00-\u9fff]{{2,6}}).{{0,40}}{re.escape(system)}(?:法师|能力|魔法|天赋)",
        rf"([\u4e00-\u9fff]{{2,6}}).{{0,30}}(?:是|为).{{0,10}}{re.escape(system)}",
    )
    for item in evidence_items:
        snippet = getattr(item, "snippet", "") or ""
        if _is_negative_text(snippet):
            continue
        if getattr(item, "evidence_type", "") == "ability_table":
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, snippet):
                add(match.group(1))
    return names


_SYSTEM_NORMALIZE_MAP = {
    "寒冰系": "冰系",
    "雷霆系": "雷系",
    "火炎系": "火系",
    "凌水系": "水系",
    "圣光系": "光系",
}

_KNOWN_SYSTEM_NAMES = (
    "雷霆系", "寒冰系", "火炎系", "凌水系", "圣光系",
    "暗影系", "召唤系", "空间系", "混沌系", "治愈系", "亡灵系", "心灵系",
    "诅咒系", "植物系", "音系", "毒系", "土系", "火系", "雷系", "冰系",
    "水系", "风系", "光系",
)

_GENERIC_SYSTEM_CONTEXT_TERMS = (
    "七系", "元素魔法", "道题", "做出来", "代表着的是", "红色星尘", "青色星尘",
    "褐色星尘", "蓝色星尘", "金色星尘", "紫色星尘，代表", "技能表", "法系通用",
)

_CHARACTER_SYSTEM_BINDING_TERMS = (
    "觉醒", "拥有", "主修", "辅修", "修炼", "掌握", "星尘", "魔法", "天生双系",
    "第一系", "第二系", "第三系", "觉醒系",
)

_HYPOTHETICAL_SYSTEM_CONTEXT_TERMS = (
    "但愿", "希望", "最好", "选择", "适合", "缺少", "祝你", "等等觉醒",
    "不要灰心", "可能", "要是", "听说", "想要", "想学", "可以再觉醒",
)


def _normalize_system_name(system: str) -> str:
    system = system.strip(" ，,。；;：:（）()")
    return _SYSTEM_NORMALIZE_MAP.get(system, system)


def _extract_system_names(text: str) -> list[str]:
    """抽取法系名，过滤“法系/体系”这类泛词。"""
    names: list[str] = []
    for raw in _KNOWN_SYSTEM_NAMES:
        if raw not in text:
            continue
        name = _normalize_system_name(raw)
        if name not in names:
            names.append(name)
    return names


def _looks_like_generic_system_context(text: str) -> bool:
    return any(term in text for term in _GENERIC_SYSTEM_CONTEXT_TERMS)


def _extract_bound_systems_from_context(character: str, text: str,
                                       known_entities: tuple[str, ...] = ()) -> list[str]:
    if character not in text:
        return []
    if _looks_like_generic_system_context(text):
        return []
    if re.search(rf"(?:收拾|对付|嘲讽|看不起|鄙视|针对).{{0,12}}{re.escape(character)}", text):
        return []

    from services.knowledge_retrieval import _is_entity_attr_bound

    found: list[str] = []
    for raw in _KNOWN_SYSTEM_NAMES:
        if raw not in text:
            continue
        system = _normalize_system_name(raw)
        if re.search(rf"{re.escape(character)}.{{0,40}}(?:知道|了解|见到|听说|看见|觉得).{{0,100}}{re.escape(raw)}", text):
            continue

        strong_patterns = (
            rf"{re.escape(character)}.{{0,100}}(?:拥有了?|主修|辅修|修炼|掌握|觉醒(?:成|了|的是)?).{{0,30}}{re.escape(raw)}",
            rf"{re.escape(character)}.{{0,80}}(?:自己的?|他的|她的|{re.escape(character)}的).{{0,10}}{re.escape(raw)}(?:星尘|星辰|魔法|技能|能力|修为)",
            rf"{re.escape(character)}的{re.escape(raw)}(?:星尘|星辰|魔法|技能|能力|修为)",
        )
        if any(re.search(pattern, text) for pattern in strong_patterns):
            if raw not in found:
                found.append(raw)
            continue

        # 补充：就近绑定判定（含第三人拦截）。
        # 覆盖 strong_patterns 漏掉的反问/系动词句式，如"张小侯，你不是风系的吗"，
        # 同时靠 _is_entity_attr_bound 内部的第三人拦截排除"赵满延的光系保护张小侯"。
        # 注意：用 raw（原文出现的法系名，如"雷霆系"）做绑定判定，而非 normalize 后的"雷系"。
        if _is_entity_attr_bound(character, raw, text, known_entities=known_entities):
            if raw not in found:
                found.append(raw)
    return found


def _extract_character_systems(character: str, evidence_items: list,
                              known_entities: tuple[str, ...] = ()) -> list[str]:
    """从证据中抽取某人物明确绑定的法系。

    只接受人物附近出现的“觉醒/拥有/主修/星尘”等绑定语境，避免把通用法系表
    或“元素魔法七系”误当成人物个人能力。

    抽取后用就近绑定判定（_is_entity_attr_bound，含第三人拦截）复核：
    "赵满延的光系魔法保护张小侯"里光系属于赵满延，不会算到张小侯头上。
    known_entities 传入已知人物实体，用于精确识别第三人。
    """
    from services.knowledge_retrieval import _is_entity_attr_bound

    systems: list[str] = []
    seen: set[str] = set()

    def add(system: str) -> None:
        system = _normalize_system_name(system)
        if not system or system in seen:
            return
        seen.add(system)
        systems.append(system)

    def _bound_in_text(system: str, text: str) -> bool:
        """就近绑定复核：法系是否真的绑定到目标人物（而非第三人）。"""
        return _is_entity_attr_bound(character, system, text, known_entities=known_entities)

    for item in evidence_items:
        snippet = getattr(item, "snippet", "") or ""
        if character not in snippet or _is_negative_text(snippet):
            continue
        if getattr(item, "evidence_type", "") == "ability_table":
            continue

        # 强模式：人物和法系在同一短窗口，且窗口里有绑定词。
        for match in re.finditer(re.escape(character), snippet):
            start = max(0, match.start() - 80)
            end = min(len(snippet), match.end() + 140)
            window = snippet[start:end]
            if any(term in window for term in _HYPOTHETICAL_SYSTEM_CONTEXT_TERMS):
                continue
            for system in _extract_bound_systems_from_context(character, window, known_entities):
                # 就近绑定复核：防止旁人法系被误归因
                if not _bound_in_text(system, window):
                    continue
                add(system)

        # 句子模式：适配“穆宁雪早期明确觉醒冰系，后续掌握风系”这种资料句。
        for sentence in re.split(r"[。！？!?；;\n\r]+", snippet):
            sentence = sentence.strip()
            if character not in sentence:
                continue
            if any(term in sentence for term in _HYPOTHETICAL_SYSTEM_CONTEXT_TERMS):
                continue
            for system in _extract_bound_systems_from_context(character, sentence, known_entities):
                if not _bound_in_text(system, sentence):
                    continue
                add(system)

    return systems


async def _try_answer_from_facts(
    db: AsyncSession,
    project_id: str,
    v2_plan,
) -> tuple[str, list[dict]] | None:
    """优先查规则事实索引，命中则返回确定性答案 + 仅含 fact evidence 的 citations。

    处理两种意图：
    - character_ability（某人是什么系）：按 subject 查该人物法系
    - character_by_ability（谁是某系）：按 object 查某法系的人物
    未命中返回 None，由调用方回退到 RAG。
    """
    from services.knowledge_fact_index import query_character_system_facts

    intent = getattr(v2_plan, "intent", "general")
    entities = list(getattr(v2_plan, "entities", []) or [])
    attributes = list(getattr(v2_plan, "attributes", []) or [])
    # 干净人物名（剔除"X系"）
    character_entities = [x for x in entities if isinstance(x, str) and x and not x.endswith("系")]
    # 法系候选（attributes 或 entities 里以"系"结尾的）
    system_candidates = [x for x in attributes + entities if isinstance(x, str) and x.endswith("系")]

    # 正向：某人是什么系
    if intent == "character_ability" and character_entities:
        character = character_entities[0]
        facts = await query_character_system_facts(db, project_id, subject=character, limit=50)
        if not facts:
            return None
        systems = list(dict.fromkeys(f.object for f in facts))  # 保序去重
        lines = "\n".join(f"- {s}" for s in systems[:12])
        answer = f"根据资料索引，**{character}** 明确绑定的法系有：\n\n{lines}"
        # citations 只含参与回答的 fact evidence
        citations = [
            {
                "source_kind": "project_knowledge_fact",
                "source_id": str(f.source_id),
                "chunk_id": str(f.chunk_id) if f.chunk_id else None,
                "title": (f.metadata_ or {}).get("source_title", "") or "资料",
                "snippet": f.evidence_text[:300],
                "evidence_type": "character_system_fact",
                "matched_query": f"{f.subject} -> {f.object}",
                "score": 1.0,
            }
            for f in facts
        ]
        return answer, citations

    # 反向：谁是某系
    if intent == "character_by_ability" and system_candidates:
        system = _normalize_system_name(system_candidates[0])
        facts = await query_character_system_facts(db, project_id, system=system, limit=50)
        if not facts:
            return None
        names = list(dict.fromkeys(f.subject for f in facts))  # 保序去重
        lines = "\n".join(f"- {n}" for n in names[:12])
        answer = f"根据资料索引，明确绑定 **{system}** 的人物有：\n\n{lines}"
        citations = [
            {
                "source_kind": "project_knowledge_fact",
                "source_id": str(f.source_id),
                "chunk_id": str(f.chunk_id) if f.chunk_id else None,
                "title": (f.metadata_ or {}).get("source_title", "") or "资料",
                "snippet": f.evidence_text[:300],
                "evidence_type": "character_system_fact",
                "matched_query": f"{f.subject} -> {f.object}",
                "score": 1.0,
            }
            for f in facts
        ]
        return answer, citations

    return None


def _try_compile_local_qa_answer(question: str, v2_plan, evidence_grouped) -> str | None:
    """对高置信、可抽取的问题直接用本地证据生成答案，降低模型误读表格的概率。"""
    intent = getattr(v2_plan, "intent", "general")
    entities = list(getattr(v2_plan, "entities", []) or [])
    attributes = list(getattr(v2_plan, "attributes", []) or [])
    system = next((x for x in attributes + entities if isinstance(x, str) and x.endswith("系")), None)
    character_entities = [x for x in entities if isinstance(x, str) and x and not x.endswith("系")]

    positive_items = list(evidence_grouped.positive_evidence())
    ability_items = list(evidence_grouped.ability_table) + list(evidence_grouped.direct_character)

    if intent == "character_by_ability" and system:
        names = _extract_characters_bound_to_system(system, positive_items)
        if not names:
            return None
        lines = "\n".join(f"- {name}" for name in names[:12])
        return f"当前资料中明确和 **{system}** 绑定的人物有：\n\n{lines}"

    if intent == "character_ability" and system:
        skill_lines = _extract_magic_system_lines(system, ability_items)
        character = character_entities[0] if character_entities else None
        if character:
            direct_items = [
                item for item in evidence_grouped.direct_character
                if character in (getattr(item, "snippet", "") or "")
                and system in (getattr(item, "snippet", "") or "")
                and not _is_negative_text(getattr(item, "snippet", "") or "")
            ]
            parts: list[str] = []
            if direct_items:
                snippet = re.sub(r"\s+", " ", direct_items[0].snippet).strip()
                parts.append(f"资料中有 **{character}** 与 **{system}** 的直接证据：{snippet[:220]}")
            elif skill_lines:
                parts.append(f"当前资料没有明确写出 **{character}** 个人的 **{system}** 表现；但资料库里有 **{system}** 的通用设定/技能。")
            if skill_lines:
                parts.append(f"通用 **{system}** 资料如下：\n\n" + "\n".join(skill_lines[:8]))
            return "\n\n".join(parts) if parts else None

        if skill_lines:
            return f"根据当前资料，**{system}** 相关技能/设定包括：\n\n" + "\n".join(skill_lines[:10])

    if intent == "character_ability" and not system and character_entities:
        character = character_entities[0]
        # 传入已知实体（含其它人物），用于就近绑定时的第三人拦截
        systems = _extract_character_systems(character, positive_items, known_entities=tuple(character_entities))
        if systems:
            lines = "\n".join(f"- {name}" for name in systems[:12])
            return f"根据当前资料，**{character}** 明确绑定的法系有：\n\n{lines}"

    return None


def _build_structured_context(project_id: str, question: str) -> tuple[str, list[dict]]:
    """从结构化表构建上下文（同步版本，调用方需传入已查询的数据）。
    这个函数被 async ask_knowledge_question 调用。
    返回 (context_text, citations_list)。
    """
    # 这里只是占位，实际逻辑在 ask_knowledge_question 中直接实现
    return "", []


async def ask_knowledge_question(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    user_id: str | uuid.UUID,
    question: str,
    *,
    conversation_id: str | None = None,
    chapter_num: int | None = None,
    include_structured: bool = True,
    include_web: bool = False,
    web_provider: str | None = None,
    web_api_key: str | None = None,
    web_base_url: str | None = None,
) -> dict:
    """资料问答主入口 -- 基于 query_plan 的语义检索 + LLM 归纳。

    处理流程：
    1. 找到或创建 QA 会话，并加载最近对话；
    2. 用 Query Planner 把自然语言问题改写成检索计划；
    3. 检索并分类结构化/非结构化证据；
    4. 优先尝试本地规则事实回答，能短路就不调用 LLM；
    5. 组装带证据和回答策略的 prompt 调 LLM；
    6. 保存用户问题和助手回答，返回 citations。
    """
    pid = str(project_id)
    _ = (web_provider, web_api_key, web_base_url)

    # -- 1. 会话管理与最近对话 --
    # 会话用于支持追问，例如“那他还有什么能力？”需要从最近对话补全“他”是谁。
    session_obj = None
    if conversation_id:
        sess_result = await db.execute(
            select(KnowledgeQaSession).where(
                KnowledgeQaSession.id == conversation_id,
                KnowledgeQaSession.project_id == pid,
            )
        )
        session_obj = sess_result.scalar_one_or_none()
    if not session_obj:
        session_obj = KnowledgeQaSession(project_id=pid, title="资料问答")
        db.add(session_obj)
        await db.flush()

    msgs_result = await db.execute(
        select(KnowledgeQaMessage)
        .where(KnowledgeQaMessage.session_id == str(session_obj.id), KnowledgeQaMessage.project_id == pid)
        .order_by(KnowledgeQaMessage.created_at.desc())
        .limit(KNOWLEDGE_QA_RECENT_MESSAGES * 2)
    )
    recent_messages = list(reversed(msgs_result.scalars().all()))

    # -- 2. 构建查询计划（V2）--
    # Query Planner 会输出 intent/entities/attributes/sub_queries 等结构化检索指令。
    from services.knowledge_query_planner import build_knowledge_query_plan_v2

    recent_msg_dicts = [{"role": msg.role, "content": msg.content} for msg in recent_messages]
    v2_plan = await build_knowledge_query_plan_v2(
        question,
        recent_messages=recent_msg_dicts,
        user_id=str(user_id),
        db=db,
    )
    resolved_question = v2_plan.rewritten_question or question
    plan = v2_plan.to_v1()

    # -- 3. 证据检索（分类 + 重排 + 分组）--
    # retrieve_and_classify 会把证据分为人物直接证据、技能表、世界观、时间线等类型。
    from services.knowledge_retrieval import retrieve_and_classify

    structured_results, chunk_results, evidence_grouped = await retrieve_and_classify(
        db, pid, plan,
        intent=v2_plan.intent,
        entities=v2_plan.entities,
        attributes=v2_plan.attributes,
        include_structured=include_structured,
        limit=10,
    )

    # -- 3.5 可选联网检索 --
    # 默认关闭。创作资料问答一般应以用户资料库为准，联网结果只能作为补充材料。
    web_results: list[dict] = []
    if include_web:
        from services.web_search import search_web

        web_results = await search_web(resolved_question, limit=5)

    # -- 4. 对话历史 --
    # history 是低优先级上下文，预算不足时优先裁剪，避免挤掉当前问题和证据。
    conversation_history = ""
    if session_obj.summary:
        conversation_history += f"## 之前的对话摘要\n{session_obj.summary}\n\n"
    if recent_messages:
        history_lines = []
        for msg in recent_messages[-KNOWLEDGE_QA_RECENT_MESSAGES * 2:]:
            role_label = "用户" if msg.role == "user" else "助手"
            history_lines.append(f"{role_label}: {msg.content[:500]}")
        conversation_history += "## 最近对话\n" + "\n".join(history_lines)

    # -- 4.5 优先查规则事实索引（人物-法系），命中则短路 RAG/LLM。
    # 这类问题有明确结构化事实时，不需要让 LLM 再归纳，能降低幻觉。
    fact_answer: tuple[str, list[dict]] | None = await _try_answer_from_facts(db, pid, v2_plan)

    compiled_answer = _try_compile_local_qa_answer(question, v2_plan, evidence_grouped)

    # -- 5. 组装 prompt（按优先级分配预算，绝不裁剪用户问题）--
    # 优先级（高→低，低优先级先被裁）：用户问题 + 指代消解 > 证据 > answer_policy > web > history
    # 这样即便 history/web 极长，被裁的也是它们，用户问题永远完整保留。

    # 5.1 必保留部分（不可裁剪）
    must_keep_parts: list[str] = [f"## 用户问题\n{question}"]
    if resolved_question != question:
        must_keep_parts.append(f"## 指代消解\n原问题: {question}\n检索问题: {resolved_question}")
    must_keep_text = "\n\n".join(must_keep_parts)
    # 分隔符开销（每段间 "\n\n"）
    sep_len = 2
    must_keep_len = len(must_keep_text)

    # 5.2 可裁剪部分，按优先级从高到低（裁剪时从低优先级开始砍）
    answer_policy_text = f"answer_policy: {v2_plan.answer_policy}" if v2_plan.answer_policy else ""

    web_text = ""
    if web_results:
        web_lines = ["## 联网查询结果"]
        for item in web_results[:5]:
            url = item.get("url") or item.get("source_id") or ""
            web_lines.append(f"- [{item.get('title', '网页结果')}] {item.get('snippet', '')[:1000]}\n  URL: {url}")
        web_text = "\n".join(web_lines)

    history_text = conversation_history

    # 5.3 分层分配预算：先扣必保留，剩余按 evidence > policy > web > history 分配
    remaining = KNOWLEDGE_QA_MAX_CONTEXT_CHARS - must_keep_len
    # history 优先级最低，先给它最小承诺（总预算的 1/4，但不超过它自身长度）
    history_budget = min(len(history_text), max(0, remaining // 4)) if history_text else 0
    remaining_after_history = remaining - history_budget
    # web 次低
    web_budget = min(len(web_text), max(0, remaining_after_history // 3)) if web_text else 0
    remaining_after_web = remaining_after_history - web_budget
    # policy
    policy_budget = min(len(answer_policy_text), max(0, remaining_after_web // 4)) if answer_policy_text else 0
    remaining_after_policy = remaining_after_web - policy_budget
    # 证据拿剩下的全部
    evidence_budget = remaining_after_policy

    # 5.4 按预算裁剪各段（history/web 超预算从尾部截，保留开头更早的上下文）
    evidence_text = evidence_grouped.to_prompt_text(max_chars=evidence_budget)
    if answer_policy_text and len(answer_policy_text) > policy_budget:
        answer_policy_text = answer_policy_text[:policy_budget]
    if web_text and len(web_text) > web_budget:
        web_text = web_text[:web_budget]
    if history_text and len(history_text) > history_budget:
        history_text = history_text[:history_budget]

    # 5.5 按稳定顺序拼接（证据在前，问题在最后，确保问题不会被任何裁剪波及）
    user_prompt_parts: list[str] = []
    if evidence_text:
        user_prompt_parts.append(evidence_text)
    if web_text:
        user_prompt_parts.append(web_text)
    if answer_policy_text:
        user_prompt_parts.append(answer_policy_text)
    if history_text:
        user_prompt_parts.append(history_text)
    user_prompt_parts.append(must_keep_text)
    user_prompt = "\n\n".join(user_prompt_parts)

    # 硬上限兜底：理论上分层预算已保证不超，此处防御性收尾，且只裁可裁部分（非 must_keep）。
    if len(user_prompt) > KNOWLEDGE_QA_MAX_CONTEXT_CHARS:
        overflow = len(user_prompt) - KNOWLEDGE_QA_MAX_CONTEXT_CHARS
        # 只从 must_keep 之前的内容裁，保证用户问题完整
        head = "\n\n".join(user_prompt_parts[:-1])
        head = head[:max(0, len(head) - overflow)]
        user_prompt = (head + "\n\n" + must_keep_text) if head else must_keep_text

    # -- 7. LLM 调用（facts 命中时短路，不调 LLM）--
    provider = None
    if fact_answer is not None:
        # 规则事实索引命中：直接用确定性答案，citations 只含 fact evidence
        answer, all_citations = fact_answer
    else:
        answer = compiled_answer or ""
        llm_citations: list[dict] = []
        try:
            from agents.llm_provider import LLMConfigError, get_llm_provider
            from api.llm_deps import get_user_llm_config
            llm_config_dict = await get_user_llm_config(str(user_id), db)
            provider = get_llm_provider(llm_config_dict)
            if not compiled_answer:
                result_text = await provider.generate(QA_SYSTEM_PROMPT, user_prompt, temperature=0.3, max_tokens=2000)
                parsed = _safe_parse_json(result_text)
                answer = _polish_qa_answer_format(parsed.get("answer", result_text))
                llm_citations = parsed.get("citations", [])
        except LLMConfigError as exc:
            logger.warning("模型配置不可用: %s", exc)
            if not answer:
                answer = f"模型配置不可用: {exc}"
        except Exception:
            logger.exception("LLM 调用失败")
            if not answer:
                answer = "抱歉，AI 模型暂时无法响应，请稍后重试。"

        # -- 8. citations：用分组证据顺序（含 evidence_type），否定证据不返回引用
        all_citations = evidence_grouped.to_citations() + web_results

    # -- 9. 保存消息 --
    if session_obj.message_count == 0 and session_obj.title == "资料问答":
        session_obj.title = _make_qa_session_title(question)
    user_msg = KnowledgeQaMessage(session_id=str(session_obj.id), project_id=pid, role="user", content=question)
    db.add(user_msg)
    assistant_msg = KnowledgeQaMessage(session_id=str(session_obj.id), project_id=pid, role="assistant", content=answer, citations=all_citations[:10])
    db.add(assistant_msg)
    session_obj.message_count += 2
    await db.commit()

    # -- 10. 对话压缩 --
    summary_updated = False
    if provider is not None and session_obj.message_count > KNOWLEDGE_QA_RECENT_MESSAGES * 2:
        summary_updated = await _maybe_compress_session(db, session_obj, provider)

    return {
        "answer": answer,
        "citations": all_citations[:10],
        "conversation_id": str(session_obj.id),
        "conversation_summary_updated": summary_updated,
        "query_plan": {
            "intent": v2_plan.intent,
            "entities": v2_plan.entities,
            "attributes": v2_plan.attributes,
            "sub_queries": v2_plan.sub_queries[:8],
            "search_queries": plan.search_queries[:8],
            "required_terms": plan.required_terms,
            "confidence": v2_plan.confidence,
            "planner": v2_plan.planner,
            "answer_policy": v2_plan.answer_policy,
        },
        "retrieval_stats": {
            "structured_hits": len(structured_results),
            "chunk_hits": len(chunk_results),
            "web_hits": len(web_results),
        },
    }


def _make_qa_session_title(question: str) -> str:
    title = re.sub(r"\s+", " ", question).strip()
    if not title:
        return "资料问答"
    if len(title) > 28:
        title = title[:28].rstrip() + "..."
    return title

async def _maybe_compress_session(
    db: AsyncSession,
    session_obj: KnowledgeQaSession,
    provider,
) -> bool:
    """如果会话消息数超过阈值，压缩旧消息为摘要。"""
    if session_obj.message_count <= KNOWLEDGE_QA_RECENT_MESSAGES * 2:
        return False

    # 取所有消息
    msgs_result = await db.execute(
        select(KnowledgeQaMessage)
        .where(KnowledgeQaMessage.session_id == str(session_obj.id))
        .order_by(KnowledgeQaMessage.created_at)
    )
    all_messages = msgs_result.scalars().all()

    if len(all_messages) <= KNOWLEDGE_QA_RECENT_MESSAGES * 2:
        return False

    # 保留最近 N 轮，压缩更早的
    keep_count = KNOWLEDGE_QA_RECENT_MESSAGES * 2
    old_messages = all_messages[:-keep_count]

    old_history = []
    for msg in old_messages:
        role_label = "用户" if msg.role == "user" else "助手"
        old_history.append(f"{role_label}：{msg.content}")

    old_history_text = "\n".join(old_history)
    if len(old_history_text) > 8000:
        old_history_text = old_history_text[:8000]

    compress_prompt = (
        "请将以下问答对话压缩为一段简明摘要，保留关键信息、用户偏好和未解决的问题。\n\n"
        f"{old_history_text}\n\n"
        "输出 JSON：\n"
        '{"summary": "对话摘要", "user_preferences": "用户偏好", "open_questions": "未解决问题"}'
    )

    try:
        result = await provider.generate(
            "你是一位对话摘要专家。只输出 JSON。",
            compress_prompt,
            temperature=0.2,
            max_tokens=800,
        )
        parsed = _safe_parse_json(result)
        session_obj.summary = parsed.get("summary", session_obj.summary or "")
        session_obj.user_preferences = parsed.get("user_preferences", "")
        session_obj.open_questions = parsed.get("open_questions", "")
        await db.commit()
        logger.info("Session %s compressed, old messages: %d", session_obj.id, len(old_messages))
        return True
    except Exception:
        logger.exception("对话压缩失败")
        return False
