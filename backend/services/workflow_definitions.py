"""Workflow Definition — Expert System v2 结构化工作流模板。

第一版写死在 Python，不进数据库。定义每种 TaskType 对应的节点序列，
为 I-4 切 workflow_v2 准备审计链。I-3 阶段只定义不执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskType(str, Enum):
    """用户任务类型 — 决定走哪套 workflow。"""
    GENERATE_CHAPTER = "generate_chapter"    # novel full_pipeline 标准章节生成
    CONTINUE = "continue"                     # 续写（suggest / generate）
    ENHANCE_SCENE = "enhance_scene"           # 场景增强（suggest / apply）
    REVIEW = "review"                         # resume action=review 审稿
    REWRITE = "revise"                        # resume action=revise 修订
    SUMMARIZE = "summarize"                   # 摘要
    EXTRACT_MEMORY = "extract_memory"         # 预留：剧情记录 / 记忆抽取
    APPROVE = "approve"                       # resume action=approve
    REJECT = "reject"                         # resume action=reject


@dataclass(frozen=True)
class WorkflowStep:
    """工作流中的一个节点。"""
    node_key: str                              # 节点名，如 chapter_architect
    expert_key: str | None                     # 对应 v2 expert_key，如 chapter-writer
    step_order: int
    is_checkpoint: bool = False                # 是否是 Human Review 检查点
    checkpoint_name: str | None = None         # planning_review / final_review
    is_conditional: bool = False               # 是否是条件节点（如 clarification，可能跳过）
    routes: dict[str, str] | None = None       # 条件节点的路由映射（如 {"needs_clarification": "human_clarification", "ready": "chapter_architect"}）


@dataclass(frozen=True)
class WorkflowDefinition:
    """一套完整的工作流定义。"""
    workflow_key: str                          # 稳定标识，如 generate_chapter_standard
    version: str                               # 如 v2.0
    task_type: TaskType
    project_mode: str                          # novel / article
    steps: list[WorkflowStep] = field(default_factory=list)

    @property
    def checkpoints(self) -> list[WorkflowStep]:
        return [s for s in self.steps if s.is_checkpoint]

    def to_dict(self) -> dict:
        """可序列化 dict，用于写入 AiRun.workflow_snapshot。"""
        return {
            "workflow_key": self.workflow_key,
            "version": self.version,
            "task_type": self.task_type.value,
            "project_mode": self.project_mode,
            "steps": [
                {
                    "node_key": s.node_key,
                    "expert_key": s.expert_key,
                    "step_order": s.step_order,
                    "is_checkpoint": s.is_checkpoint,
                    "checkpoint_name": s.checkpoint_name,
                    "is_conditional": s.is_conditional,
                    "routes": s.routes,
                }
                for s in self.steps
            ],
        }


# ── Novel 工作流定义 ──────────────────────────────────

_NOVEL_GENERATE_STANDARD = WorkflowDefinition(
    workflow_key="generate_chapter_standard",
    version="v2.1",
    task_type=TaskType.GENERATE_CHAPTER,
    project_mode="novel",
    steps=[
        WorkflowStep("context_builder", None, 1),
        WorkflowStep(
            "clarification_planner", "clarification-planner", 2,
            is_conditional=True,
            routes={"needs_clarification": "human_clarification", "ready": "chapter_architect"},
        ),
        WorkflowStep("human_clarification", None, 3, is_checkpoint=True, checkpoint_name="clarification_review"),
        WorkflowStep("chapter_architect", "chapter-architect", 4),
        WorkflowStep("planning_review", None, 5, is_checkpoint=True, checkpoint_name="planning_review"),
        WorkflowStep("chapter_writer", "chapter-writer", 6),
        WorkflowStep("structural_critic", "structural-critic", 7),
        WorkflowStep("narrative_editor", "narrative-editor", 8),
        WorkflowStep("continuity_checker", "continuity-checker", 9),
        WorkflowStep("final_review", None, 10, is_checkpoint=True, checkpoint_name="final_review"),
    ],
)

_NOVEL_CONTINUE_FAST = WorkflowDefinition(
    workflow_key="continue_fast",
    version="v2.0",
    task_type=TaskType.CONTINUE,
    project_mode="novel",
    steps=[
        WorkflowStep("chapter_architect_lite", "chapter-architect", 1),
        WorkflowStep("chapter_writer", "chapter-writer", 2),
        WorkflowStep("continuity_checker", "continuity-checker", 3),
        WorkflowStep("final_review", None, 4, is_checkpoint=True, checkpoint_name="final_review"),
    ],
)

_NOVEL_ENHANCE = WorkflowDefinition(
    workflow_key="enhance_scene",
    version="v2.0",
    task_type=TaskType.ENHANCE_SCENE,
    project_mode="novel",
    steps=[
        WorkflowStep("scene_enhancer", "scene-enhancer", 1),
        WorkflowStep("narrative_editor", "narrative-editor", 2),
    ],
)

_NOVEL_SUMMARIZE = WorkflowDefinition(
    workflow_key="summarize",
    version="v2.0",
    task_type=TaskType.SUMMARIZE,
    project_mode="novel",
    steps=[
        WorkflowStep("story_recorder", "story-recorder", 1),
    ],
)

_NOVEL_REVIEW = WorkflowDefinition(
    workflow_key="review",
    version="v2.0",
    task_type=TaskType.REVIEW,
    project_mode="novel",
    steps=[
        WorkflowStep("structural_critic", "structural-critic", 1),
        WorkflowStep("continuity_checker", "continuity-checker", 2),
    ],
)

_NOVEL_REWRITE = WorkflowDefinition(
    workflow_key="rewrite",
    version="v2.0",
    task_type=TaskType.REWRITE,
    project_mode="novel",
    steps=[
        WorkflowStep("structural_critic", "structural-critic", 1),
        WorkflowStep("narrative_editor", "narrative-editor", 2),
        WorkflowStep("continuity_checker", "continuity-checker", 3),
    ],
)

_NOVEL_APPROVE = WorkflowDefinition(
    workflow_key="approve",
    version="v2.0",
    task_type=TaskType.APPROVE,
    project_mode="novel",
    steps=[
        WorkflowStep("final_review", None, 1, is_checkpoint=True, checkpoint_name="final_review"),
        WorkflowStep("story_recorder", "story-recorder", 2),
    ],
)

_NOVEL_REJECT = WorkflowDefinition(
    workflow_key="reject",
    version="v2.0",
    task_type=TaskType.REJECT,
    project_mode="novel",
    steps=[],
)

_NOVEL_EXTRACT_MEMORY = WorkflowDefinition(
    workflow_key="extract_memory",
    version="v2.0",
    task_type=TaskType.EXTRACT_MEMORY,
    project_mode="novel",
    steps=[
        WorkflowStep("story_recorder", "story-recorder", 1),
        WorkflowStep("memory_curator", "memory-curator", 2),
    ],
)


# ── Article 工作流定义 ────────────────────────────────

_ARTICLE_GENERATE = WorkflowDefinition(
    workflow_key="article_generate",
    version="v2.0",
    task_type=TaskType.GENERATE_CHAPTER,
    project_mode="article",
    steps=[
        WorkflowStep("article_writer", None, 1),
        WorkflowStep("article_editor", None, 2),
        WorkflowStep("final_review", None, 3, is_checkpoint=True, checkpoint_name="final_review"),
    ],
)

_ARTICLE_CONTINUE = WorkflowDefinition(
    workflow_key="article_continue",
    version="v2.0",
    task_type=TaskType.CONTINUE,
    project_mode="article",
    steps=[
        WorkflowStep("article_strategist", None, 1),
        WorkflowStep("article_writer", None, 2),
    ],
)

_ARTICLE_ENHANCE = WorkflowDefinition(
    workflow_key="article_enhance",
    version="v2.0",
    task_type=TaskType.ENHANCE_SCENE,
    project_mode="article",
    steps=[
        WorkflowStep("article_editor", None, 1),
    ],
)

_ARTICLE_SUMMARIZE = WorkflowDefinition(
    workflow_key="article_summarize",
    version="v2.0",
    task_type=TaskType.SUMMARIZE,
    project_mode="article",
    steps=[
        WorkflowStep("article_summarizer", None, 1),
    ],
)


# ── 全量 workflow 定义表 ──────────────────────────────
# key 格式: "{project_mode}:{mode}" 或 "{project_mode}:{mode}:{action}"

WORKFLOW_DEFINITIONS: dict[str, WorkflowDefinition] = {
    # ── Novel ──
    "novel:full_pipeline": _NOVEL_GENERATE_STANDARD,
    "novel:continue": _NOVEL_CONTINUE_FAST,
    "novel:enhance": _NOVEL_ENHANCE,
    "novel:summarize": _NOVEL_SUMMARIZE,
    "novel:review": _NOVEL_REVIEW,
    "novel:revise": _NOVEL_REWRITE,
    "novel:approve": _NOVEL_APPROVE,
    "novel:reject": _NOVEL_REJECT,
    "novel:extract_memory": _NOVEL_EXTRACT_MEMORY,
    # ── Article ──
    "article:full_pipeline": _ARTICLE_GENERATE,
    "article:continue": _ARTICLE_CONTINUE,
    "article:enhance": _ARTICLE_ENHANCE,
    "article:summarize": _ARTICLE_SUMMARIZE,
}
