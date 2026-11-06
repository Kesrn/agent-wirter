"""检索策略集中映射 — D5 决策实现（阶段一骨架）

单一策略入口
============
所有按 intent 决定以下参数的行为都必须通过 ``get_retrieval_policy(intent)``
获取，禁止在调用方散落 ``if intent == ...`` 分支（D5）：

    intent → { source_kinds 白名单, allow_chapter_body, max_per_kind,
               token_budget, entity_first, on_no_result }

intent 实际取值（grep agents/ api/ services/ 结果）
==================================================
- 章节生成流程：agents/workflow.py:322 ``intent=state.get("mode", "generate")``，
  mode 由 api/routes.py 的 generate 接口透传 ``req.mode``。实际取值：
  ``full_pipeline`` / ``continue`` / ``enhance`` / ``summarize`` / ``revise``，
  另有 ``rewrite`` 出现在 services/chapter_quality.py / critic_schema 的修订场景。
- 上下文 / 方向接口：api/routes.py:2012 显式传 ``intent="generate"``（默认值）。
- 检索查询规划（services/knowledge_query_plan.py 规则 V1 +
  services/knowledge_query_planner.py LLM V2）产出的是问答类 intent，
  属于问答场景，统一走 qa 策略（阶段二起全部取值登记在 ``_QA_ALIAS_INTENTS``
  并映射到 ``_QA_POLICY``，见下）：
  - V1 规则 planner 实际取值（``_detect_intent`` / ``INTENT_PRIORITY``）：
    ``character_profile`` / ``character_by_ability`` / ``relationship`` /
    ``ability`` / ``timeline`` / ``worldbuilding`` / ``plot`` / ``general``；
  - V2 LLM planner（``VALID_INTENTS``）：
    ``character_ability`` / ``plot_event`` / ``source_lookup``
    （与 V1 同名的取值共用同一别名）。
- 结构化问答（services/structured_qa.py）内部 intent：``character_ability`` /
  ``event_query`` / ``world_rule_query``，也归入 qa 语义（``unknown`` 未登记：
  结构化问答对未识别问题不触发检索，走 generate 兜底无害）。
- 章节问答接口：/knowledge/ask（api/routes.py:8786），本表以 ``ask`` 别名纳入 qa 策略。

source_kind 取值约定（与 services/knowledge_source.py 检索返回一致）
==================================================================
``project_source_chunk | character | character_event | outline | world_entry |
hidden_thread | chapter | web_search | project_knowledge_fact``（另含 structured_qa
输出的 ``structured_fact``）。其中 ``chapter`` 即"旧章节正文"，由
``allow_chapter_body`` 决定是否允许检索。

各策略设计依据（对照 D5 与 build_chapter_context 现有行为）
==========================================================
- full_pipeline：build_chapter_context 的严格模式（services/chapter_context.py:510-511）
  已硬性约束——不注入当前章旧稿片段、默认不加载最近三章前文摘要、不加载
  资料库正文与已确认记忆。检索策略对齐该约束：allow_chapter_body=False；
  来源白名单仅结构化可信数据（世界观 world_entry / 角色 character /
  能力 project_knowledge_fact / 时间线 character_event + hidden_thread）；
  entity_first=True（与 knowledge_source 角色名精确匹配 +10 加权的现有行为一致）；
  on_no_result="empty"——无结果时返回空，不回退到会把旧文带回 prompt 的宽松检索。
- generate：默认（兜底）策略。允许旧章节正文（保持跨章连贯，与
  build_chapter_context 对非 full_pipeline 注入前文摘要的行为一致），
  不设来源白名单，宽松匹配，无结果时回退。未知 intent 与
  build_chapter_context 的默认值 ``"generate"``（chapter_context.py:470）对齐。
- continue / enhance：在旧文基础上工作，必须能检索旧章节正文。
  ``revise`` / ``rewrite`` 是修订场景（workflow_definitions TaskType.REWRITE），
  语义与 enhance 一致，同表归入。
- summarize：摘要对象就是（当前/目标）章节正文，允许章节正文，但来源条数与
  token 预算最小（摘要不需要大上下文）；无结果时不扩大检索。
- qa / query / ask：面向读者提问，允许章节正文 + 全部来源，实体优先
  （问答更依赖实体精确命中），无结果时仅记录日志、返回空答案。
  知识问答类 intent 别名（V1/V2 Query Planner 与 structured_qa 的全部实际取值，
  见 ``_QA_ALIAS_INTENTS``）同样映射到本策略 —— 这是实体优先通道在 QA 的
  lexical 路径生效的关键（阶段二落地，评审缺口修复）。
- 未知 intent：回退 generate 策略（与 build_chapter_context 的 intent 默认值一致）。

定稿索引接入说明（阶段一骨架，对应 D2）
======================================
- 接入位置：backend/services/chapter_save.py::finalize_chapter_content
  （插入点约 116-130 行："检索索引接入"注释块，import 在 124 行、调用在 125 行）。
  该函数是唯一的 finalize 入口，PATCH 定稿 / POST /finalize /
  resume action=finalize 三条路径都经过它。
- 调用时机：不可变版本已创建且 flush（create_version 内 flush），同 session
  可读到新版本；事务由路由层在调用方 db.commit() 提交（本函数不负责提交）。
- 调用契约：``await index_chapter_version(db, project_id, version_id)``，
  模块 services/retrieval_indexer 由并行任务实现；SQLite 同步增量 / PG 异步
  由该模块决定。
- 降级行为：延迟导入 + try/except 全捕获，ImportError（模块未实现）与运行期
  异常均仅 logger.warning，绝不阻断定稿流程 —— 对应 D2"索引任务失败不回滚章节，
  索引可重建"。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── source_kind 约定（与 services/knowledge_source.py 检索返回一致）──

SOURCE_KIND_PROJECT_SOURCE_CHUNK = "project_source_chunk"  # 资料库正文 chunk
SOURCE_KIND_CHARACTER = "character"                        # 角色
SOURCE_KIND_CHARACTER_EVENT = "character_event"            # 章节事件（时间线）
SOURCE_KIND_OUTLINE = "outline"                            # 大纲
SOURCE_KIND_WORLD_ENTRY = "world_entry"                    # 世界观设定
SOURCE_KIND_HIDDEN_THREAD = "hidden_thread"                # 暗线（埋设/回收时间线）
SOURCE_KIND_CHAPTER = "chapter"                            # 旧章节正文
SOURCE_KIND_WEB_SEARCH = "web_search"                      # 联网检索
SOURCE_KIND_PROJECT_KNOWLEDGE_FACT = "project_knowledge_fact"  # 知识事实（含能力/时间线事实）
SOURCE_KIND_STRUCTURED_FACT = "structured_fact"            # 结构化问答输出

ALL_SOURCE_KINDS: tuple[str, ...] = (
    SOURCE_KIND_PROJECT_SOURCE_CHUNK,
    SOURCE_KIND_CHARACTER,
    SOURCE_KIND_CHARACTER_EVENT,
    SOURCE_KIND_OUTLINE,
    SOURCE_KIND_WORLD_ENTRY,
    SOURCE_KIND_HIDDEN_THREAD,
    SOURCE_KIND_CHAPTER,
    SOURCE_KIND_WEB_SEARCH,
    SOURCE_KIND_PROJECT_KNOWLEDGE_FACT,
    SOURCE_KIND_STRUCTURED_FACT,
)


@dataclass(frozen=True)
class RetrievalPolicy:
    """一次检索的策略参数（D5：集中映射，禁止散落 if intent == ...）。

    Attributes:
        source_kinds: 允许检索的来源白名单（source_kind 取值见模块常量）；
            None 表示全部来源。
        allow_chapter_body: 是否允许检索旧章节正文（source_kind == "chapter"）。
        max_per_kind: 每类来源的最大条数。
        token_budget: 本次检索的总 token 预算（供排序/截断使用）。
        entity_first: 精确实体匹配优先于关键词宽松匹配。
        on_no_result: 无结果时的处理 ——
            "fallback"：回退到更宽泛的检索（放宽阈值/扩展 query）；
            "empty"：返回空结果，不扩大检索（严格模式防旧文回流）；
            "log"：仅记录日志，返回空结果。
    """

    source_kinds: list[str] | None = None
    allow_chapter_body: bool = False
    max_per_kind: int = 5
    token_budget: int = 4000
    entity_first: bool = False
    on_no_result: str = "fallback"


# full_pipeline：严格模式——只允许结构化可信来源（世界观/角色/能力/时间线），
# 不含 chapter（旧章节正文）、project_source_chunk（资料库正文）、
# web_search（联网）、outline（大纲由 build_chapter_context 结构化加载，不走检索）。
_FULL_PIPELINE_POLICY = RetrievalPolicy(
    source_kinds=[
        SOURCE_KIND_WORLD_ENTRY,            # 世界观
        SOURCE_KIND_CHARACTER,              # 角色
        SOURCE_KIND_CHARACTER_EVENT,        # 时间线（按章节序号的事件）
        SOURCE_KIND_HIDDEN_THREAD,          # 时间线（暗线埋设/回收）
        SOURCE_KIND_PROJECT_KNOWLEDGE_FACT, # 能力/事实（含能力表数据）
    ],
    allow_chapter_body=False,
    max_per_kind=5,
    token_budget=4000,
    entity_first=True,
    on_no_result="empty",
)

# generate：默认（兜底）策略。允许旧章节正文保持跨章连贯，全部来源，宽松匹配。
_GENERATE_POLICY = RetrievalPolicy(
    source_kinds=None,
    allow_chapter_body=True,
    max_per_kind=5,
    token_budget=5000,
    entity_first=False,
    on_no_result="fallback",
)

# continue：续写必须参考旧章节正文，上下文需求较大。
_CONTINUE_POLICY = RetrievalPolicy(
    source_kinds=None,
    allow_chapter_body=True,
    max_per_kind=6,
    token_budget=6000,
    entity_first=False,
    on_no_result="fallback",
)

# enhance / revise / rewrite：基于（当前/目标）章节正文做增强与修订，允许章节正文。
_ENHANCE_POLICY = RetrievalPolicy(
    source_kinds=None,
    allow_chapter_body=True,
    max_per_kind=5,
    token_budget=4000,
    entity_first=False,
    on_no_result="fallback",
)

# summarize：摘要对象就是章节正文；条数与 token 预算最小，无结果时不扩大检索。
_SUMMARIZE_POLICY = RetrievalPolicy(
    source_kinds=None,
    allow_chapter_body=True,
    max_per_kind=3,
    token_budget=3000,
    entity_first=False,
    on_no_result="empty",
)

# qa / query / ask：资料库问答/检索（knowledge/ask、knowledge/search）。
# 只允许资料切片。注意过滤值用检索索引的投影命名 "source_chunk"
# （retrieval_chunks.source_kind 的物理值；"project_source_chunk" 是
# knowledge_source 输出侧的映射命名，用于过滤会匹配不到任何行）。
# 资料域与写作域隔离：写作侧数据（world_entry / character / timeline_event
# 等投影）只服务写作场景（generate/continue/enhance 等策略，见下），
# 不进入资料问答证据，避免"问原著角色却引用用户写作设定"的串域。
_QA_POLICY = RetrievalPolicy(
    source_kinds=["source_chunk"],
    allow_chapter_body=False,
    max_per_kind=12,
    token_budget=12000,
    entity_first=True,
    on_no_result="log",
)

# 知识问答类 intent（V1/V2 Query Planner 与 structured_qa 的全部实际取值）→ qa 策略。
# 来源清单（以代码实际枚举为准）：
# - V1 规则 planner（services/knowledge_query_plan.py，_detect_intent / INTENT_PRIORITY）：
#   character_profile / character_by_ability / relationship / ability / timeline /
#   worldbuilding / plot / general；
# - V2 LLM planner（services/knowledge_query_planner.py，VALID_INTENTS）：
#   character_ability / plot_event / source_lookup；
# - 结构化问答（services/structured_qa.py，_detect_structured_intent）：
#   event_query / world_rule_query。
# general 是 planner 对未匹配特定意图的问答问题的回退 intent，同属问答语义，
# 一并登记（结构化问答的 unknown 未登记：该分支不触发检索，走 generate 兜底无害）。
_QA_ALIAS_INTENTS: frozenset[str] = frozenset({
    # V1 规则 planner
    "character_profile",
    "character_by_ability",
    "relationship",
    "ability",
    "timeline",
    "worldbuilding",
    "plot",
    "general",
    # V2 LLM planner
    "character_ability",
    "plot_event",
    "source_lookup",
    # structured_qa
    "event_query",
    "world_rule_query",
})

# intent → 策略 集中映射表（D5：唯一权威来源）
_POLICIES: dict[str, RetrievalPolicy] = {
    "full_pipeline": _FULL_PIPELINE_POLICY,
    "generate": _GENERATE_POLICY,
    "continue": _CONTINUE_POLICY,
    "enhance": _ENHANCE_POLICY,
    "revise": _ENHANCE_POLICY,      # resume action=revise 修订（workflow_definitions TaskType.REWRITE）
    "rewrite": _ENHANCE_POLICY,     # 修订场景别名
    "summarize": _SUMMARIZE_POLICY,
    "qa": _QA_POLICY,
    "query": _QA_POLICY,
    "ask": _QA_POLICY,              # /knowledge/ask 章节问答接口
}
# 知识问答类 intent 别名（V1/V2 Query Planner + structured_qa）→ qa 策略。
# 阶段二落地：此前这些 intent 未登记，按 D5 回退 generate（entity_first=False），
# 导致 QA 的 lexical 路径走不到实体优先通道。
_POLICIES.update({_alias: _QA_POLICY for _alias in _QA_ALIAS_INTENTS})

# 未知 intent 的回退策略（与 build_chapter_context 的 intent 默认值 "generate" 一致）
_FALLBACK_POLICY = _GENERATE_POLICY


def get_retrieval_policy(intent: str | None) -> RetrievalPolicy:
    """按 intent 返回检索策略；未知 intent 回退 generate 策略。

    intent 归一化：小写 + 去首尾空白 + 连字符/空格转下划线，
    避免 "Full_Pipeline" / " full pipeline " 这类变体产生不同策略。
    """
    normalized = ((intent or "").strip().lower().replace("-", "_").replace(" ", "_"))
    return _POLICIES.get(normalized, _FALLBACK_POLICY)
