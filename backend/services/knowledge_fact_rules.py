"""规则事实抽取 —— 人物 -> 法系绑定。

reindex 阶段扫描 chunk 全文，预计算"人物明确拥有某法系"的事实。
纯字符串/正则规则，零 LLM 成本，可解释、可测试。

设计要点：
- 法系来自 _KNOWN_SYSTEM_NAMES（通用，不含具体作品角色名）。
- 人物名从 chunk 文本中识别（2-4 汉字 + 绑定上下文），不预设角色表。
- 绑定判定复用 knowledge_retrieval._is_entity_attr_bound（含第三人拦截），
  避免"赵满延的光系魔法保护张小侯"被误归因为张小侯会光系。
- 排除假设性语境（希望/想要/可能觉醒）和通用法系罗列（元素魔法七系包括...）。
- 宁可不写 fact，也不写错 fact。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 法系常量从 knowledge_source 引入（该模块顶层不 import 本文件，无循环）
from services.knowledge_source import (
    _KNOWN_SYSTEM_NAMES,
    _normalize_system_name,
    _GENERIC_SYSTEM_CONTEXT_TERMS,
    _HYPOTHETICAL_SYSTEM_CONTEXT_TERMS,
)
# 就近绑定判定从 retrieval 引入（纯函数，无副作用）
from services.knowledge_retrieval import (
    _is_entity_attr_bound,
    _split_clauses,
    CHARACTER_NAME_RE,
    BINDING_TERMS,
)

# 非"拥有"关系的动词，出现这些词时法系不属于主语
_NON_POSSESSION_VERBS = (
    "看见", "看到", "听说", "了解到", "知道", "希望", "想要", "想学",
    "被攻击", "受到", "遭受", "抵挡", "挡下",
)

# 技能别名 -> 法系（第一版只用于 weak 线索）
_SKILL_ALIAS_TO_SYSTEM: dict[str, str] = {
    "风轨": "风系", "风刃": "风系",
    "火滋": "火系", "烈拳": "火系", "火浪": "火系",
    "雷印": "雷系", "霹雳": "雷系", "雷击": "雷系",
    "冰蔓": "冰系", "冰锁": "冰系", "冰封": "冰系",
    "地波": "土系", "岩障": "土系", "陨石": "土系",
    "遁影": "暗影系", "影遁": "暗影系",
}


@dataclass
class FactCandidate:
    """规则抽取的事实候选。"""
    subject: str
    object: str
    confidence: str          # "explicit" | "weak"
    evidence_text: str
    evidence_start: int | None = None
    evidence_end: int | None = None
    metadata: dict = field(default_factory=dict)


# 句子分隔符（含逗号，与 _split_clauses 一致）
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;\n\r]+")


def _sentence_has_non_possession(sentence: str) -> bool:
    """句子是否含"看见/听说/希望/被攻击"等非拥有关系词。"""
    return any(v in sentence for v in _NON_POSSESSION_VERBS)


def _find_character_names_near(text: str, system_raw: str) -> list[str]:
    """在含法系的文本里找候选人名（2-4 汉字），去重保序。

    用 CHARACTER_NAME_RE 非重叠匹配（避免"张小""小侯"拆碎），但对贪婪吞掉
    动词的 4 字匹配（如"莫凡觉醒"），回退尝试内部 2/3 字干净前缀。
    """
    verb_chars: set[str] = set()
    for v in BINDING_TERMS + _NON_POSSESSION_VERBS:
        verb_chars.update(v)
    # 属格"的"、否定"不"、常见虚词出现在人名中间是异常，一并过滤
    verb_chars.update("的了过在被向把将让给和与不也都很就还出来去起下上")
    bad_fragments = ("不是", "的吗", "你们", "我们", "他们", "这是", "那是", "就是",
                     "出来", "起来", "过来", "下去", "上去", "可以", "能够")

    def _is_clean_name(candidate: str) -> bool:
        if not candidate:
            return False
        if candidate == system_raw or system_raw in candidate or candidate in system_raw:
            return False
        if candidate.endswith("系"):
            return False
        if any(ch in verb_chars for ch in candidate):
            return False
        if any(bad in candidate for bad in bad_fragments):
            return False
        return True

    names: list[str] = []
    seen: set[str] = set()
    for m in CHARACTER_NAME_RE.finditer(text):
        name = m.group()
        if _is_clean_name(name):
            if name not in seen:
                seen.add(name)
                names.append(name)
            continue
        # 贪婪匹配吞了动词/虚词，在该区间内找更短的干净子片段
        # 尝试所有 2~3 字子串，优先长的（3 字先于 2 字）
        found_sub = False
        for sub_len in (3, 2):
            for start in range(len(name) - sub_len + 1):
                sub = name[start:start + sub_len]
                if _is_clean_name(sub) and sub not in seen:
                    seen.add(sub)
                    names.append(sub)
                    found_sub = True
                    break
            if found_sub:
                break
    return names


def extract_character_system_facts_from_text(
    text: str,
    *,
    source_title: str = "",
) -> list[FactCandidate]:
    """从一段文本抽取"人物 -> 法系"事实候选。

    只抽取明确绑定（explicit）和技能别名线索（weak）。
    误归因（旁人施法、看见/听说、假设性语境）一律不抽。
    """
    if not text or not text.strip():
        return []

    candidates: list[FactCandidate] = []
    # 用 (subject, object) 去重
    seen_pairs: set[tuple[str, str, str]] = set()

    def _add(subject: str, obj: str, confidence: str,
             evidence: str, start: int | None, end: int | None,
             metadata: dict | None = None) -> None:
        key = (subject, obj, confidence)
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        md = {"source_title": source_title} if source_title else {}
        if metadata:
            md.update(metadata)
        candidates.append(FactCandidate(
            subject=subject,
            object=obj,
            confidence=confidence,
            evidence_text=evidence[:400],
            evidence_start=start,
            evidence_end=end,
            metadata=md,
        ))

    # 按句子扫描（句末标点切分，保留逗号子句供绑定判定）
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]

    for sentence in sentences:
        # 排除假设性语境
        if any(t in sentence for t in _HYPOTHETICAL_SYSTEM_CONTEXT_TERMS):
            continue
        # 排除通用法系罗列（"元素魔法七系包括..."）
        if any(t in sentence for t in _GENERIC_SYSTEM_CONTEXT_TERMS):
            continue
        # 排除非拥有关系（看见/听说/希望/被攻击）
        if _sentence_has_non_possession(sentence):
            continue

        # 1. 显式法系名绑定
        for raw_system in _KNOWN_SYSTEM_NAMES:
            if raw_system not in sentence:
                continue
            normalized = _normalize_system_name(raw_system)
            # 在该句里找候选人名
            for name in _find_character_names_near(sentence, raw_system):
                # 就近绑定判定（无 known_entities 时退化为跳过含绑定词的疑似人名）
                if _is_entity_attr_bound(name, raw_system, sentence, known_entities=(name,)):
                    sent_start = text.find(sentence)
                    _add(
                        subject=name,
                        obj=normalized,
                        confidence="explicit",
                        evidence=sentence,
                        start=sent_start if sent_start >= 0 else None,
                        end=(sent_start + len(sentence)) if sent_start >= 0 else None,
                    )

        # 2. 技能别名线索（weak）
        for alias, system in _SKILL_ALIAS_TO_SYSTEM.items():
            if alias not in sentence:
                continue
            # 同句需有"释放/施展/使用/掌握"等动词才采信
            if not any(v in sentence for v in ("释放", "施展", "使用", "掌握", "释放出")):
                continue
            for name in _find_character_names_near(sentence, alias):
                # 别名场景：人物 + 动词 + 别名 就近
                if _is_entity_attr_bound(name, alias, sentence, known_entities=(name,)):
                    _add(
                        subject=name,
                        obj=system,
                        confidence="weak",
                        evidence=sentence,
                        start=text.find(sentence) if text.find(sentence) >= 0 else None,
                        end=None,
                        metadata={"via_alias": alias},
                    )

    return candidates
