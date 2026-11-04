"""对话意图路由 v2.4 —— 严格结构化模型（LLM 输出解析与校验）。

- `StrictModel`：`extra="forbid"` + `strict=True`——LLM 多输出任何字段（如
  requires_confirmation）一律校验失败回退规则，杜绝静默丢弃。
- `Operation` 白名单枚举：拒绝任意字符串，防止 LLM 自由发挥 operation。
- `IntentPlan`（内部模型）：1-based 步骤 + 依赖下标存在性 / from!=to / 无环校验。
- `to_conversation_plan()`：内部 IntentPlan → 对外 ConversationPlan
  （兼容现有前端契约：target 统一 {"chapter_no": N} 对象，多前置依赖按 to_step 聚合）。

方案文档：docs/对话意图路由重构v2方案.md（v2.4）
"""

from __future__ import annotations

import itertools
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

# 五类意图（与方案 §2.1 / 前端 ConversationIntent 一致）
IntentName = Literal[
    "knowledge_query", "discussion", "rewrite", "data_operation", "flow_control"
]
INTENTS: tuple[str, ...] = (
    "knowledge_query", "discussion", "rewrite", "data_operation", "flow_control",
)


class StrictModel(BaseModel):
    """严格模式基类：额外字段直接校验失败（LLM 多输出 requires_confirmation 等 → 拒绝）。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class Operation(str, Enum):
    """细粒度操作白名单（拒绝任意字符串）。"""

    STRENGTHEN_CHARACTER_PORTRAYAL = "strengthen_character_portrayal"
    REWRITE_CHAPTER = "rewrite_chapter"
    COMPRESS_CHAPTER = "compress_chapter"
    EXPAND_CHAPTER = "expand_chapter"
    POLISH_CHAPTER = "polish_chapter"
    CONTINUE_CHAPTER = "continue_chapter"


class Dependency(StrictModel):
    from_step: int  # 1-based
    to_step: int  # 1-based；from_step != to_step
    kind: Literal["depends_on"] = "depends_on"


class IntentStep(StrictModel):
    intent: IntentName
    raw: str = Field(max_length=200)
    target_chapter_no: int | None = Field(default=None, ge=1)


class IntentPlan(StrictModel):
    """内部模型：仅供 LLM 解析与校验，不直接出网。"""

    steps: list[IntentStep] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_dependencies(self) -> "IntentPlan":
        n = len(self.steps)
        for dep in self.dependencies:
            if not (1 <= dep.from_step <= n and 1 <= dep.to_step <= n):
                raise ValueError(
                    f"依赖下标越界: from_step={dep.from_step}, to_step={dep.to_step}, steps={n}"
                )
            if dep.from_step == dep.to_step:
                raise ValueError(f"依赖 from_step 不能等于 to_step: {dep.from_step}")
        # 有向无环（DFS 拓扑校验）
        adj: dict[int, list[int]] = {i: [] for i in range(1, n + 1)}
        for dep in self.dependencies:
            adj[dep.from_step].append(dep.to_step)
        state: dict[int, int] = {}  # 0=未访问 1=访问中 2=完成

        def dfs(node: int) -> None:
            state[node] = 1
            for nxt in adj[node]:
                if state.get(nxt, 0) == 1:
                    raise ValueError(f"依赖图存在环: {node} -> {nxt}")
                if state.get(nxt, 0) == 0:
                    dfs(nxt)
            state[node] = 2

        for node in range(1, n + 1):
            if state.get(node, 0) == 0:
                dfs(node)
        return self


class LLMIntentOutput(StrictModel):
    """LLM 意图识别输出（主路径）。

    注意：**无 requires_confirmation 字段**——后端按 intent + operation +
    risk_level 计算（方案补锁 1）；needs_clarification 仅作建议。
    """

    intent: IntentName
    operation: Operation | None = None
    target_scope: Literal[
        "current_chapter", "explicit_chapter", "project_level", "none"
    ] = "none"
    target_chapter_no: int | None = Field(default=None, ge=1)
    mentioned_entities: list[Annotated[str, StringConstraints(max_length=50)]] = Field(
        default_factory=list, max_length=20
    )
    requirements: list[Annotated[str, StringConstraints(max_length=120)]] = Field(
        default_factory=list, max_length=10
    )
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=200)
    plan: IntentPlan | None = None  # 复合任务完整计划（单一权威）；单步为 None


def to_conversation_plan(llm_plan: IntentPlan) -> dict[str, Any]:
    """内部 IntentPlan → 对外 ConversationPlan（兼容现有前端契约）。

    前端契约（frontend/src/api/chat.ts）：
      {plan_id, steps: [{intent, target?, query?, based_on?}],
       dependencies: [{step: number, depends_on: number[]}]}

    - target 统一为 {"chapter_no": N} 对象（前端类型 string | Record | null，不传裸 int）
    - 多前置依赖按 to_step 聚合为单条 {step, depends_on: [1, 3]}
    """
    return {
        "plan_id": uuid.uuid4().hex,
        "steps": [
            {
                "intent": s.intent,
                "target": {"chapter_no": s.target_chapter_no}
                if s.target_chapter_no is not None
                else None,
                "query": s.raw if s.intent == "knowledge_query" else None,
                "based_on": None,
            }
            for s in llm_plan.steps
        ],
        "dependencies": [
            {
                "step": to_step,
                "depends_on": sorted({d.from_step for d in deps}),
            }
            for to_step, deps in itertools.groupby(
                sorted(llm_plan.dependencies, key=lambda d: d.to_step),
                key=lambda d: d.to_step,
            )
        ],
    }
