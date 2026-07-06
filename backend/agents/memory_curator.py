"""Memory Curator — 把 Story Record 转为 writing_memory_staging 候选记忆。

接收 story-recorder 产出的 Story Record，将各维度（events/character_state_changes/...）
转为 staging 格式的 facts 列表（memory_type/title/payload/evidence），
交给 create_staging_from_extraction 写入 writing_memory_staging。

不直接写正式设定表。PLOT_FACT 不硬塞 CharacterEvent。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def story_record_to_facts(record: dict[str, Any], chapter_seq: int | None = None) -> list[dict[str, Any]]:
    """把 Story Record 转为 staging facts 列表。

    映射规则：
    - events → PLOT_FACT（每条事件一个 fact）
    - character_state_changes → CHARACTER（角色状态变化）
    - relationship_changes → PLOT_FACT（关系变化是剧情事实）
    - ability_changes → EVENT（能力变化是角色事件）
    - foreshadowing_new → FORESHADOWING
    - foreshadowing_resolved → PLOT_FACT（伏笔回收是剧情事实）
    - knowledge_state_changes → PLOT_FACT（信息揭示是剧情事实）

    Args:
        record: parse_story_record 返回的 Story Record dict
        chapter_seq: 章节序号（用于 payload）

    Returns:
        facts 列表，每条含 memory_type/title/payload/evidence
    """
    if not record or record.get("parse_error"):
        return []

    facts: list[dict[str, Any]] = []
    seq_str = str(chapter_seq) if chapter_seq else ""

    # events → PLOT_FACT
    for event in record.get("events", []):
        title = event.get("title", "")
        if not title:
            continue
        facts.append({
            "memory_type": "PLOT_FACT",
            "title": title,
            "payload": {
                "description": event.get("description", title),
                "chapter": seq_str,
                "character_names": event.get("character_names", []),
            },
            "evidence": event.get("evidence"),
        })

    # character_state_changes → CHARACTER
    for change in record.get("character_state_changes", []):
        name = change.get("character_name", "")
        if not name:
            continue
        facts.append({
            "memory_type": "CHARACTER",
            "title": name,
            "payload": {
                "name": name,
                "profile": change.get("change", ""),
            },
            "evidence": change.get("evidence"),
        })

    # relationship_changes → PLOT_FACT
    for rel in record.get("relationship_changes", []):
        chars = rel.get("characters", [])
        title = f"{'与'.join(chars)}关系变化" if chars else "关系变化"
        facts.append({
            "memory_type": "PLOT_FACT",
            "title": title,
            "payload": {
                "description": rel.get("change", ""),
                "chapter": seq_str,
            },
            "evidence": rel.get("evidence"),
        })

    # ability_changes → EVENT
    for ability in record.get("ability_changes", []):
        name = ability.get("character_name", "")
        ability_name = ability.get("ability", "")
        if not name:
            continue
        title = f"{name}{ability.get('change', '能力变化')}：{ability_name}" if ability_name else f"{name}能力变化"
        facts.append({
            "memory_type": "EVENT",
            "title": title,
            "payload": {
                "character_name": name,
                "event_summary": f"{ability.get('change', '')}：{ability_name}",
                "state_change": ability.get("change", ""),
            },
            "evidence": ability.get("evidence"),
        })

    # foreshadowing_new → FORESHADOWING + status_delta=PLANTED
    for foreshadow in record.get("foreshadowing_new", []):
        title = foreshadow.get("title", "")
        if not title:
            continue
        facts.append({
            "memory_type": "FORESHADOWING",
            "title": title,
            "payload": {
                "description": foreshadow.get("description", ""),
                "status_delta": "PLANTED",
                "chapter_nums": [chapter_seq] if chapter_seq else [],
            },
            "evidence": foreshadow.get("evidence"),
        })

    # K-3: foreshadowing_resolved → FORESHADOWING + status_delta=RESOLVED
    # 优先用 thread_name 匹配原伏笔，fallback 到 title
    for resolved in record.get("foreshadowing_resolved", []):
        title = (
            resolved.get("thread_name")
            or resolved.get("name")
            or resolved.get("title", "")
        )
        if not title:
            continue
        facts.append({
            "memory_type": "FORESHADOWING",
            "title": title,
            "payload": {
                "description": resolved.get("description", ""),
                "status_delta": "RESOLVED",
                "chapter_nums": [chapter_seq] if chapter_seq else [],
            },
            "evidence": resolved.get("evidence"),
        })

    # knowledge_state_changes → PLOT_FACT
    for knowledge in record.get("knowledge_state_changes", []):
        desc = knowledge.get("description", "")
        if not desc:
            continue
        facts.append({
            "memory_type": "PLOT_FACT",
            "title": desc[:100],  # 截断作为 title
            "payload": {
                "description": desc,
                "chapter": seq_str,
            },
            "evidence": knowledge.get("evidence"),
        })

    logger.info("memory_curator: story_record → %d facts (events=%d, char=%d, ability=%d, foreshadow=%d)",
                len(facts),
                len(record.get("events", [])),
                len(record.get("character_state_changes", [])),
                len(record.get("ability_changes", [])),
                len(record.get("foreshadowing_new", [])))
    return facts
