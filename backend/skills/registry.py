"""Skill Registry — 扫描 skills/ 目录，加载 SKILL.md 元信息，提供按专家角色查找 skill 的能力"""

import logging
import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent

# 专家 role_type → skill 目录名 映射
EXPERT_SKILL_MAP: dict[str, str] = {
    "writer": "creative-master",
    "critic": "brutal-critic",
    "editor": "professional-editor",
    "renderer": "sensory-renderer",
    "twister": "plot-twister",
    "summarizer": "summarizer",
    # consistency_checker 暂不映射到 crazy-writer（内容过长且非专精），
    # 保持使用 DEFAULT_CONSISTENCY_PROMPT 即可
}

# 工作流节点名 → 专家 role_type 映射
NODE_ROLE_MAP: dict[str, str] = {
    "writer": "writer",
    "critic": "critic",
    "editor": "editor",
    "consistency_checker": "consistency_checker",
}


@dataclass
class SkillInfo:
    name: str
    dir_name: str
    description: str = ""
    skill_md_path: Path = field(default_factory=Path)
    _content_cache: str | None = field(default=None, repr=False)

    def load_content(self) -> str:
        if self._content_cache is not None:
            return self._content_cache
        if self.skill_md_path.exists():
            self._content_cache = self.skill_md_path.read_text(encoding="utf-8")
        else:
            self._content_cache = ""
        return self._content_cache


def _parse_frontmatter(text: str) -> dict[str, str]:
    """解析 SKILL.md 的 YAML frontmatter（简单实现，不引入 pyyaml 依赖）"""
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    fm = text[3:end]
    result: dict[str, str] = {}
    current_key = None
    for line in fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- ") or line.startswith(">"):
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                result[key] = val
            current_key = key
        elif current_key and line:
            result[current_key] = result.get(current_key, "") + " " + line
    return result


def scan_skills() -> dict[str, SkillInfo]:
    """扫描 skills/ 目录，返回 {dir_name: SkillInfo}"""
    skills: dict[str, SkillInfo] = {}
    if not SKILLS_DIR.is_dir():
        logger.warning(f"Skills directory not found: {SKILLS_DIR}")
        return skills
    for child in sorted(SKILLS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name.startswith("_"):
            continue
        md_path = child / "SKILL.md"
        if not md_path.exists():
            continue
        text = md_path.read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)
        name = fm.get("name", child.name.replace("-", "_"))
        desc = fm.get("description", "")
        skills[child.name] = SkillInfo(
            name=name,
            dir_name=child.name,
            description=desc,
            skill_md_path=md_path,
        )
    logger.info(f"Scanned {len(skills)} skills: {list(skills.keys())}")
    return skills


# 模块级单例
_registry: dict[str, SkillInfo] | None = None


def get_registry() -> dict[str, SkillInfo]:
    global _registry
    if _registry is None:
        _registry = scan_skills()
    return _registry


def get_skill_for_role(role_type: str) -> Optional[SkillInfo]:
    """根据专家 role_type 查找对应 skill"""
    dir_name = EXPERT_SKILL_MAP.get(role_type)
    if not dir_name:
        return None
    return get_registry().get(dir_name)


def get_skill_for_node(node_name: str) -> Optional[SkillInfo]:
    """根据工作流节点名查找对应 skill

    支持内置节点名（writer, critic 等）和动态节点名（pre_writer_0, post_critic_1 等）。
    动态节点名会提取中间的 role_type 部分进行匹配。
    """
    # 先尝试直接匹配
    role_type = NODE_ROLE_MAP.get(node_name)
    if role_type:
        return get_skill_for_role(role_type)
    # 动态节点名：pre_writer_0, post_critic_1 → 提取 writer, critic
    stripped = re.sub(r'_\d+$', '', node_name)
    parts = stripped.split("_")
    for i in range(len(parts)):
        candidate = "_".join(parts[i:])
        role_type = NODE_ROLE_MAP.get(candidate)
        if role_type:
            return get_skill_for_role(role_type)
    return None


def get_skill_prompt(role_type: str) -> str:
    """获取专家 role_type 对应 skill 的 SKILL.md 全文内容，用于注入 prompt"""
    skill = get_skill_for_role(role_type)
    if not skill:
        return ""
    content = skill.load_content()
    if not content:
        return ""
    # 去掉 frontmatter，只返回正文
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            return content[end + 3:].strip()
    return content
