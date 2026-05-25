"""文章/文案上下文格式化 — 将小说素材语义映射为文章概念

小说 context_loader 返回的素材使用"大纲/角色/世界观/暗线/前文"术语，
文章模式需要映射为"内容结构/受众画像/品牌产品资料/内容策略/参考文本"。

不做 DB 迁移，只做 prompt 层面的术语重映射。
"""

import re

# 映射规则：小说术语 → 文章术语（用于 section 标题替换）
_SECTION_REMAP = {
    "大纲": "内容结构",
    "角色资料": "受众画像与关键人物",
    "世界观设定": "品牌/产品资料与背景",
    "暗线设定": "内容策略与隐含线索",
    "前文内容": "参考文本",
}

# 映射规则：小说口吻关键词 → 文章口吻替代（用于行内替换）
_INLINE_REMAP = {
    "章节": "段落",
    "角色": "关键人物",
    "主角": "核心对象",
    "反派": "对立面",
    "配角": "辅助角色",
    "世界观": "品牌/产品背景",
    "剧情": "内容线",
    "续写": "扩展",
    "伏笔": "策略铺垫",
    "转折点": "关键节点",
    "情节": "叙述线",
    "暗线": "隐含策略",
    "登场": "引入",
    "叙事": "叙述",
}

# 需要完全删除的小说元信息段落（不会在文章中有意义）
_NOVEL_META_PATTERNS = [
    re.compile(r'###?\s*角色关系[^\n]*(?:\n(?!\n|#{1,3})[^\n]+)*', re.MULTILINE),
    re.compile(r'###?\s*阵营[^\n]*(?:\n(?!\n|#{1,3})[^\n]+)*', re.MULTILINE),
]


def remap_context_for_article(novel_context: str) -> str:
    """将小说 context_loader 的输出重映射为文章语义

    处理步骤：
    1. 删除纯小说元信息段落（角色关系、阵营）
    2. 替换 section 标题（大纲→内容结构 等）
    3. 替换行内小说术语（角色→关键人物 等）
    4. 添加文章上下文标识头
    """
    if not novel_context:
        return ""

    result = novel_context

    # 1. 删除纯小说元信息
    for pattern in _NOVEL_META_PATTERNS:
        result = pattern.sub('', result)

    # 2. 替换 section 标题
    for novel_term, article_term in _SECTION_REMAP.items():
        result = re.sub(
            rf'(##\s*){re.escape(novel_term)}',
            rf'\1{article_term}',
            result,
        )

    # 3. 替换行内术语
    for novel_term, article_term in _INLINE_REMAP.items():
        result = result.replace(novel_term, article_term)

    # 4. 清理多余空行
    result = re.sub(r'\n{3,}', '\n\n', result)

    return result.strip()
