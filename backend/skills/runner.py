"""Expert Skill Runner — 按专家角色执行 skill，返回结构化结果注入工作流节点"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from skills.registry import get_skill_prompt, get_skill_for_role, EXPERT_SKILL_MAP

logger = logging.getLogger(__name__)

# 输出格式模板的截断标记：SKILL.md 中 ## 输出格式 及之后的内容不注入 system_prompt，
# 防止 LLM 模仿输出模板中的 📌本章要点、💡后续建议、【视觉】等标记
_OUTPUT_FORMAT_PATTERN = re.compile(r'^##\s*输出格式', re.MULTILINE)


@dataclass
class ExpertSkillResult:
    expert_role: str
    skill_name: str
    prompt_content: str
    sources: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(self.prompt_content)


def run_expert_skill(
    role_type: str,
    project_id: str = "",
    chapter_id: str = "",
    draft: str = "",
    context: str = "",
    mode: str = "",
) -> ExpertSkillResult:
    """根据专家 role_type 加载对应 skill 的 prompt 内容

    当前阶段是预注入模式：读取 SKILL.md 内容作为 prompt 增强，
    不执行真正的本地工具调用。后续阶段可扩展为执行 skill handler。

    Args:
        role_type: 专家角色类型 (writer/critic/editor/renderer/twister/summarizer/consistency_checker)
        project_id: 当前项目 ID（用于日志和来源记录）
        chapter_id: 当前章节 ID
        draft: 当前草稿文本
        context: 基础创作上下文
        mode: 生成模式

    Returns:
        ExpertSkillResult 包含 skill prompt 内容和元信息
    """
    skill = get_skill_for_role(role_type)
    if not skill:
        dir_name = EXPERT_SKILL_MAP.get(role_type, "unknown")
        return ExpertSkillResult(
            expert_role=role_type,
            skill_name="",
            prompt_content="",
            warnings=[f"No skill found for role_type={role_type} (mapped to {dir_name})"],
        )

    prompt_content = get_skill_prompt(role_type)
    if not prompt_content:
        return ExpertSkillResult(
            expert_role=role_type,
            skill_name=skill.name,
            prompt_content="",
            warnings=[f"Skill {skill.dir_name} has no content"],
        )

    sources = [{"type": "skill", "name": skill.name, "dir": skill.dir_name}]
    if project_id:
        sources.append({"type": "project", "id": project_id})
    if chapter_id:
        sources.append({"type": "chapter", "id": chapter_id})

    logger.info(
        f"ExpertSkillRunner: role={role_type}, skill={skill.dir_name}, "
        f"content_len={len(prompt_content)}, project={project_id}"
    )

    return ExpertSkillResult(
        expert_role=role_type,
        skill_name=skill.name,
        prompt_content=prompt_content,
        sources=sources,
    )


def _strip_output_template(skill_content: str) -> str:
    """剥离 SKILL.md 中的输出格式模板部分

    SKILL.md 中 ## 输出格式 之后的内容是给 LLM 参考的输出结构示例，
    包含 📌本章要点、💡后续建议、【视觉】等标记。注入 system_prompt 会导致
    LLM 在正文中模仿这些标记，因此只保留创作指导部分。
    """
    match = _OUTPUT_FORMAT_PATTERN.search(skill_content)
    if match:
        stripped = skill_content[:match.start()].rstrip()
        logger.info(
            f"Stripped output template from skill content: "
            f"{len(skill_content)} -> {len(stripped)} chars"
        )
        return stripped
    return skill_content


def build_expert_system_prompt(role_type: str, default_prompt: str) -> str:
    """构建专家的增强 system_prompt

    将 skill 内容追加到默认 system_prompt 之后，
    让远程模型同时遵守默认约束和 skill 专项指导。

    注意：skill 内容中的输出格式模板（## 输出格式 之后的部分）
    会被自动剥离，防止 LLM 在正文中输出标记性内容。

    Args:
        role_type: 专家角色类型
        default_prompt: 默认的 system_prompt（如 DEFAULT_WRITER_PROMPT）

    Returns:
        增强后的 system_prompt
    """
    result = run_expert_skill(role_type)
    if not result.has_content:
        return default_prompt

    clean_content = _strip_output_template(result.prompt_content)

    return f"""{default_prompt}

---

# 专家 Skill 指导

以下是当前专家的专项 Skill 指导，你必须严格遵守：

{clean_content}
"""
