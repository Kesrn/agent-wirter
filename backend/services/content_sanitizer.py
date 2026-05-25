"""章节内容清洗 — 去除 LLM 输出中混入的提示词标记和 meta 段落

作为兜底安全网，在 skill runner 的输出模板隔离之后，
仍可能有少量标记性内容漏入正文，此处统一清洗。
"""

import re

# --- 感官标签：【视觉】【听觉】【嗅觉】【味觉】【触觉】【心理】等 ---
_SENSORY_TAG_RE = re.compile(r'【[^】]*?(?:视觉|听觉|嗅觉|味觉|触觉|心理|氛围|意象)[^】]*?】\s*')

# --- meta 段落标题：📌本章要点、💡后续建议、📝写作笔记 等 ---
_META_PREFIX = r'[ \t]*(?:#{1,6}\s*)?(?:📌|💡|📝|⚠️|🎯|✨|🔍|📖)?\s*'
_META_TITLE = r'(?:本章要点|后续建议|下章预告|写作笔记|注意事项|创作目标|亮点|改进|总结|关键|核心|建议|方向|伏笔|提示)'

# --- meta 段落块：从标题到下一个空行或下一个 ## 标题 ---
_META_BLOCK_RE = re.compile(
    r'(?:^|\n)'                            # 行首
    + _META_PREFIX
    + _META_TITLE
    + r'[^\n]*'                            # 标题行
    + r'(?:\n(?![ \t]*\n)(?![ \t]*#{1,6}\s*)[^\n]+)*',
    re.MULTILINE,
)

# --- 残留 markdown 标记：## 输出格式、## 创作方向 等 ---
_LEFTOVER_SECTION_RE = re.compile(
    r'(?:^|\n)##\s*(?:输出格式|创作方向|写作方向|改进方向|审校意见|编辑建议)[^\n]*'
    r'(?:\n(?![ \t]*\n)(?!##)[^\n]+)*',
    re.MULTILINE,
)

_MARKDOWN_EMPHASIS_RE = re.compile(r'(\*\*|__)(.*?)\1')
_HORIZONTAL_RULE_RE = re.compile(r'(?:^|\n)[ \t]*-{3,}[ \t]*(?=\n|$)')

# --- 尾部空行清理 ---
_TRAILING_BLANKS_RE = re.compile(r'\n{3,}$')


def sanitize_chapter_content(content: str) -> str:
    """清洗章节正文内容，去除提示词标记和 meta 段落

    处理顺序：
    1. 去除感官标签（【视觉】等）
    2. 去除 meta 段落块（📌本章要点 等，含后续内容行）
    3. 去除残留 section 标题（## 输出格式 等，含后续内容行）
    4. 清理多余空行
    """
    if not content:
        return content

    # 1. 去除感官标签
    result = _SENSORY_TAG_RE.sub('', content)

    # 2. 去除 meta 段落块（整块删除，含后续内容行）
    result = _META_BLOCK_RE.sub('', result)

    # 3. 去除残留 section
    result = _LEFTOVER_SECTION_RE.sub('', result)

    # 4. 去除正文中常见的提示词强调残留
    result = _MARKDOWN_EMPHASIS_RE.sub(r'\2', result)
    result = _HORIZONTAL_RULE_RE.sub('', result)

    # 5. 清理多余空行
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = _TRAILING_BLANKS_RE.sub('\n\n', result)
    result = result.strip()

    return result
