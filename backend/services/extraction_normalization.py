"""归一化工具：人物名、世界规则 category、能力 evidence 绑定校验。

只处理真实验证暴露出的常见异体写法，不做大而泛的 alias 系统。
merge 阶段调用这些函数，QA 层可复用同一函数，保证口径一致。
"""

from __future__ import annotations

from services.magic_systems import canonical_magic_system_name


# ── 人物名归一 ─────────────────────────────────────────────

# 真实抽取暴露出的异体簇：{任意异体: (规范名, 全部异体集合)}
_CHARACTER_ALIAS_CLUSTERS: list[tuple[frozenset[str], str]] = [
    (frozenset({"张侯", "张候", "张小候", "张小侯"}), "张小侯"),
]


def normalize_character_name(name: str) -> tuple[str, list[str]]:
    """归一人物名，返回 (规范名, 全部异体列表)。

    本轮只处理真实验证暴露的异体簇。无匹配时返回原名、空 aliases。
    不硬编码大量小说角色；后续通用 alias 审核另做。
    """
    for cluster, canonical in _CHARACTER_ALIAS_CLUSTERS:
        if name in cluster:
            aliases = sorted(cluster - {canonical})
            return canonical, aliases
    return name, []


# ── 身份/状态描述归一（用于判断是否"实质变化"） ─────────────

import re as _re

# 身份描述里常见的前缀/修饰噪声：班级编号、强调性短语
_DESC_NOISE_PATTERNS = [
    _re.compile(r"[0-9一二三四五六七八九十百]+班"),
    _re.compile(r"，[^，]*重要性"),
    _re.compile(r"，?强调[^，]*"),
    _re.compile(r"，?仅提及"),
    _re.compile(r"，?曾想[^，]*"),
]


def core_desc_token(desc: str | None) -> str:
    """把身份/状态描述归一为"核心 token"，用于判断是否实质变化。

    去掉班级编号、强调性后缀等噪声，只保留核心角色词。
    例如："8班班主任" / "班主任，强调冥修重要性" / "班主任" → "班主任"
    这样同一身份的不同重述不会被判成"身份变化"，避免 evidence 膨胀。
    """
    if not desc:
        return ""
    s = desc.strip()
    for pat in _DESC_NOISE_PATTERNS:
        s = pat.sub("", s)
    # 去掉分隔符和空白，取剩余核心
    s = s.strip("，,。.、 ")
    return s


# ── 能力 evidence 绑定校验 ─────────────────────────────────

# 明确的拥有/使用动作词：evidence 中出现 [人物]+[动作] 才算绑定
_BIND_ACTIONS = (
    "觉醒", "拥有", "释放", "使用", "掌握", "施展", "施放",
    "学会", "习得", "控", "凝聚", "召唤", "激活", "爆发", "击出",
    "打出", "动用", "祭出", "轰出", "轰", "挥", "斩", "射",
)

# 明确的"非绑定"语境词：仅提及/听说/介绍，不算人物拥有能力
_UNBOUND_CONTEXTS = (
    "听说", "听闻", "询问", "介绍", "提到", "提及", "据说",
    "知道", "了解", "是什么", "就是", "是指", "被称为", "被称为",
    "书中", "故事", "传说", "记载",
)


# 第一人称/自称词：LLM 经常以主角视角写 evidence（"自己觉醒了火系"），需视作人物引用
_SELF_REFERENCES = ("自己", "我", "他的", "她的")


def is_ability_bound_to_character(character: str, ability_name: str, evidence: str) -> bool:
    """判断 evidence 是否明确把 ability 绑定到 character。

    返回 False 时该 ability 不应进入正式 ability_profile。
    规则：
      1. evidence 中出现人物名 或 第一人称自称 + 拥有/使用动作 → 绑定
      2. evidence 是介绍性/旁听性语境 → 不绑定
      3. evidence 中有人物名/自称且无"非绑定"语境，且有动作词 → 绑定
      4. evidence 中完全无人物名/自称 → 不绑定
    """
    if not evidence:
        return False
    text = evidence
    has_character = character in text
    has_self_ref = any(ref in text for ref in _SELF_REFERENCES)

    # 介绍性语境：即使有人物名，也只是旁听
    if any(ctx in text for ctx in _UNBOUND_CONTEXTS) and not _has_bind_action(text):
        return False

    if not has_character and not has_self_ref:
        return False

    # 有人物名/自称 + 绑定动作 → 明确绑定
    if _has_bind_action(text):
        return True

    # 有人物名/自称但无动作词：法系/血统等"拥有态"evidence（如"莫凡的雷系星尘"）允许 weak 绑定
    # 用"的"字连接人物与能力名，或自称+能力名共现，视为 weak 绑定
    if (ability_name in text or _system_name_in_text(ability_name, text)):
        if has_character and "的" in text:
            return True
        if has_self_ref:
            return True

    return False


def _has_bind_action(text: str) -> bool:
    return any(action in text for action in _BIND_ACTIONS)


def _system_name_in_text(ability_name: str, text: str) -> bool:
    """法系名可能在 evidence 中以变体出现（雷系星尘/雷霆系魔法）。"""
    canonical = canonical_magic_system_name(ability_name)
    if canonical and canonical in text:
        return True
    return ability_name in text


# ── world_rule category 归一 ───────────────────────────────

_CATEGORY_WHITELIST = (
    "魔法体系", "魔法觉醒", "魔法修炼", "魔法技能",
    "势力组织", "教育体系", "社会规则", "地理设定",
    "妖魔体系", "道具物品", "其他",
)

# 同义 category → 白名单的映射规则（关键词匹配，顺序敏感）
_CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("觉醒",), "魔法觉醒"),
    (("修炼", "修炼体系", "修炼机制", "星子", "冥修", "精神力"), "魔法修炼"),
    (("技能", "法术", "咒语"), "魔法技能"),
    (("世家", "家族", "势力", "穆氏", "组织", "帮派"), "势力组织"),
    (("学府", "学校", "高中", "教育", "考核", "班级"), "教育体系"),
    (("社会", "阶层", "阶级", "城", "市", "世界背景", "历史"), "社会规则"),
    (("地理", "地图", "地点", "场所", "区域"), "地理设定"),
    (("妖魔", "奴仆", "战将", "魔兽", "怪物"), "妖魔体系"),
    (("道具", "物品", "魔器", "装备", "坠子"), "道具物品"),
    (("等级", "境界", "阶", "天赋", "等阶", "品阶"), "魔法体系"),
    (("元素", "系别", "魔法体系", "魔法系统", "魔法分类"), "魔法体系"),
]


def normalize_world_rule_category(category: str, rule_text: str = "") -> str:
    """归一 world_rule.category 到白名单。

    顺序敏感：先匹配更具体的规则（觉醒/修炼/技能），再回退到魔法体系/其他。
    """
    if not category:
        return "其他"

    cat = category.strip()

    # 已是白名单则直接返回
    if cat in _CATEGORY_WHITELIST:
        return cat

    # 按规则顺序匹配关键词
    for keywords, target in _CATEGORY_RULES:
        for kw in keywords:
            if kw in cat:
                return target

    return "其他"
