"""小说资料章节切分。

输入：project_sources.content（用户上传的小说全文）
输出：project_source_chapters 记录

切分策略（文档 §5）：
1. 优先按章节标题正则切分（第X章 / Chapter N / N、标题）
2. 识别不到章节标记 → 按 5000-8000 字粗切，split_type=auto_length
3. split_type 记录切分方式，便于后续 evidence 章节定位
4. 接受脏输入：无标记、标记不统一、整本合一都能兜底

单章长度由抽取阶段 MAX_EXTRACT_CHARS 控制，切分阶段不做截断。
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.extraction_pipeline import ProjectSourceChapter
from models.project_source import ProjectSource

logger = logging.getLogger(__name__)

# 章节标题正则（按优先级）
# 1. 第X章 / 第X节（中文数字或阿拉伯数字）
# 2. Chapter N
# 3. 行首 数字、或 数字. 开头
_CHAPTER_PATTERNS = [
    re.compile(r"^[\s]*第[一二三四五六七八九十百千万0-9]+[章节回卷][\s:：．\.]*(.*)$", re.MULTILINE),
    re.compile(r"^[\s]*Chapter\s+(\d+)[\s:：．\.]*(.*)$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^[\s]*(\d+)[、.．]\s*(.*)$", re.MULTILINE),
]

# auto_length 兜底切分范围
AUTO_LENGTH_MIN = 5000
AUTO_LENGTH_MAX = 8000


@dataclass
class SplitResult:
    """切分结果。"""
    chapter_count: int
    split_type: str  # chapter_regex | auto_length | manual


async def split_source_chapters(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    source_id: str | uuid.UUID,
) -> SplitResult:
    """切分资料源为章节，写入 project_source_chapters。

    1. 删除该 source 旧章节
    2. 读取 source.content
    3. 按章节正则切分；识别不到按长度粗切
    4. 写入章节记录
    """
    pid = str(project_id)
    sid = str(source_id)

    # 删旧章节
    await db.execute(
        delete(ProjectSourceChapter).where(ProjectSourceChapter.source_id == sid)
    )

    # 读 source
    result = await db.execute(
        select(ProjectSource.content).where(ProjectSource.id == sid)
    )
    content = result.scalar()
    if not content:
        await db.commit()
        return SplitResult(chapter_count=0, split_type="chapter_regex")

    # 尝试按章节正则切分
    chapters = _split_by_regex(content)
    split_type = "chapter_regex"

    if not chapters:
        # 兜底：按长度粗切
        chapters = _split_by_length(content)
        split_type = "auto_length"

    # 写入
    for i, (title, body) in enumerate(chapters, start=1):
        char_count = len(body)
        warning = None
        if char_count == 0:
            warning = "章节内容为空"
        db.add(ProjectSourceChapter(
            project_id=pid,
            source_id=sid,
            chapter_no=i,
            chapter_title=title,
            content=body,
            split_type=split_type,
            char_count=char_count,
            warning=warning,
        ))

    await db.commit()
    logger.info("split_source_chapters: source=%s chapters=%d type=%s", sid, len(chapters), split_type)
    return SplitResult(chapter_count=len(chapters), split_type=split_type)


def _split_by_regex(content: str) -> list[tuple[str | None, str]]:
    """按章节标题正则切分。返回 [(title, body), ...]。

    找到所有章节标题位置，每个标题到下一个标题之间的文本作为该章正文。
    标题前的引导文本（如果有）作为第 0 章或并入第 1 章。
    """
    # 找第一个能匹配较多章节的模式
    best_matches: list[re.Match] = []
    for pattern in _CHAPTER_PATTERNS:
        matches = list(pattern.finditer(content))
        if len(matches) > len(best_matches):
            best_matches = matches

    # 至少要匹配到 2 个章节标题才算有效切分（否则可能是误匹配）
    if len(best_matches) < 2:
        return []

    chapters: list[tuple[str | None, str]] = []
    for i, m in enumerate(best_matches):
        title = _extract_title(m)
        body_start = m.end()
        body_end = best_matches[i + 1].start() if i + 1 < len(best_matches) else len(content)
        body = content[body_start:body_end].strip()
        chapters.append((title, body))

    return chapters


def _extract_title(match: re.Match) -> str | None:
    """从正则匹配中提取章节标题。"""
    # 各 pattern 的分组：第X章 → 标题在 group(1)；Chapter N → group(2)；N、 → group(2)
    groups = [g for g in match.groups() if g is not None]
    # 优先取最后一个非空组作为标题
    title = groups[-1].strip() if groups else None
    # 整行也作为标题候选
    full_line = match.group(0).strip()
    if not title:
        return full_line[:200] if full_line else None
    # 标题过长则截断
    return title[:200] if len(title) > 200 else title


def _split_by_length(content: str) -> list[tuple[str | None, str]]:
    """按长度粗切（5000-8000 字），尽量在段落边界切。"""
    content = content.strip()
    if not content:
        return []

    chapters: list[tuple[str | None, str]] = []
    target = (AUTO_LENGTH_MIN + AUTO_LENGTH_MAX) // 2  # 约 6500 字
    pos = 0
    chapter_no = 1

    while pos < len(content):
        # 取目标长度
        end = min(pos + target, len(content))
        if end < len(content):
            # 在 [pos+AUTO_LENGTH_MIN, pos+AUTO_LENGTH_MAX] 范围找最近的段落边界
            search_start = min(pos + AUTO_LENGTH_MIN, len(content))
            search_end = min(pos + AUTO_LENGTH_MAX, len(content))
            # 找换行符
            nl = content.rfind("\n", search_start, search_end)
            if nl > pos:
                end = nl
        body = content[pos:end].strip()
        if body:
            chapters.append((f"第{chapter_no}段（自动切分）", body))
            chapter_no += 1
        pos = end

    return chapters
