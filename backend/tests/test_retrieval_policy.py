"""retrieval_policy 集中映射 与 定稿索引接入降级骨架 的单元测试。

覆盖范围：
1. get_retrieval_policy 的策略映射语义：full_pipeline 严格模式（不允许旧章节正文）、
   qa/query 放开（允许章节正文 + 全部来源）、V1/V2 Query Planner 与 structured_qa
   的问答类 intent 映射到 qa 策略（entity_first=True，阶段二落地）、
   continue/enhance 允许章节正文、未知 intent 回退 generate、字段默认值、
   大小写/空白归一化。
2. finalize_chapter_content 接入点的"延迟导入降级"契约（对应 D2）：
   - indexer 模块缺失（ImportError）→ 不抛异常，定稿正常完成，仅告警；
   - indexer 存在 → 以 (db, project_id, version_id) 正确调用一次；
   - indexer 运行期抛异常 → 不阻断定稿。

不依赖真实索引实现（services/retrieval_indexer 由并行任务提供），
全部使用最小 fake db / fake chapter，无需数据库。
"""

import sys
import types
import uuid

import pytest

from services.retrieval_policy import (
    ALL_SOURCE_KINDS,
    RetrievalPolicy,
    SOURCE_KIND_CHAPTER,
    get_retrieval_policy,
)


# ── get_retrieval_policy 纯函数测试 ──────────────────


def test_full_pipeline_rejects_chapter_body():
    """full_pipeline 严格模式：默认不检索旧章节正文、实体优先、无结果返回空。"""
    policy = get_retrieval_policy("full_pipeline")
    assert policy.allow_chapter_body is False
    assert SOURCE_KIND_CHAPTER not in policy.source_kinds
    assert policy.entity_first is True
    assert policy.on_no_result == "empty"


def test_full_pipeline_whitelist_only_structured_worldview_character_ability_timeline():
    """full_pipeline 来源白名单 = 世界观/角色/能力/时间线，不含正文与资料库。"""
    policy = get_retrieval_policy("full_pipeline")
    assert set(policy.source_kinds) == {
        "world_entry",             # 世界观
        "character",               # 角色
        "character_event",         # 时间线（章节事件）
        "hidden_thread",           # 时间线（暗线埋设/回收）
        "project_knowledge_fact",  # 能力/事实
    }
    # 资料库正文 / 旧章节正文 / 联网检索 / 大纲 都不允许进入严格模式
    for excluded in ("project_source_chunk", "chapter", "web_search", "outline"):
        assert excluded not in policy.source_kinds


def test_qa_and_query_isolate_source_chunks():
    """qa/query/ask：只检索上传资料切片（资料域与写作域隔离），实体优先。"""
    for intent in ("qa", "query", "ask"):
        policy = get_retrieval_policy(intent)
        assert policy.source_kinds == ["source_chunk"]
        assert policy.allow_chapter_body is False
        assert policy.entity_first is True
        assert policy.on_no_result == "log"


def test_query_planner_intents_map_to_qa_policy():
    """V1/V2 Query Planner 与 structured_qa 的问答类 intent 全部映射到 qa 策略。

    阶段二落地：此前这些 intent 未登记，按 D5 回退 generate（entity_first=False），
    QA 的 lexical 路径走不到实体优先通道。现在必须全部返回 qa 策略——
    只检索资料切片（领域隔离）、实体优先。
    """
    qa_intents = [
        # V1 规则 planner（services/knowledge_query_plan.py）
        "character_profile",
        "character_by_ability",
        "relationship",
        "ability",
        "timeline",
        "worldbuilding",
        "plot",
        "general",
        # V2 LLM planner（services/knowledge_query_planner.py VALID_INTENTS）
        "character_ability",
        "plot_event",
        "source_lookup",
        # 结构化问答（services/structured_qa.py）
        "event_query",
        "world_rule_query",
    ]
    qa_policy = get_retrieval_policy("qa")
    for intent in qa_intents:
        policy = get_retrieval_policy(intent)
        assert policy == qa_policy, f"{intent} 应映射到 qa 策略"
        assert policy.source_kinds == ["source_chunk"], intent  # 领域隔离
        assert policy.allow_chapter_body is False, intent
        assert policy.entity_first is True, intent
        assert policy.on_no_result == "log", intent
    # 未登记的结构化问答 unknown 仍回退 generate（区别于已登记的问答类 intent）
    assert get_retrieval_policy("unknown") == get_retrieval_policy("generate")


def test_qa_alias_intents_cover_actual_planner_enums():
    """枚举漂移防护：代码实际产出的问答类 intent 必须全部映射到 qa 策略。

    从各 planner 的真实枚举动态收集（不依赖手写清单，避免清单与代码脱节）：
    - V1 规则 planner：INTENT_PRIORITY + SEARCH_QUERY_TEMPLATES 的全部取值；
    - V2 LLM planner：VALID_INTENTS 的全部取值；
    - 结构化问答：_detect_structured_intent 的已登记分支
      （character_ability / event_query / world_rule_query）。
    日后任一枚举新增 intent 而未登记到 _QA_ALIAS_INTENTS 时，本用例立即失败，
    保证 QA 的 lexical 路径始终走实体优先通道。
    """
    from services.knowledge_query_plan import (
        INTENT_PRIORITY,
        SEARCH_QUERY_TEMPLATES,
    )
    from services.knowledge_query_planner import VALID_INTENTS
    from services.structured_qa import _detect_structured_intent

    # unknown 分支按设计不登记（不触发检索，走 generate 兜底无害），排除在外
    structured_intents = {
        _detect_structured_intent(q)
        for q in ("莫凡有什么系", "发生了什么", "世界规则是什么", "叶心夏是谁")
    } - {"unknown"}
    code_intents = (
        set(INTENT_PRIORITY)
        | set(SEARCH_QUERY_TEMPLATES)
        | set(VALID_INTENTS)
        | structured_intents
    )
    qa_policy = get_retrieval_policy("qa")
    for intent in sorted(code_intents):
        policy = get_retrieval_policy(intent)
        assert policy == qa_policy, f"代码枚举 intent {intent} 未映射到 qa 策略"
        assert policy.entity_first is True, intent
        assert policy.allow_chapter_body is False, intent  # 领域隔离：不含章节正文
        assert policy.source_kinds == ["source_chunk"], intent


def test_continue_and_enhance_allow_chapter_body():
    """continue/enhance（及修订别名 revise/rewrite）允许旧章节正文。"""
    for intent in ("continue", "enhance", "revise", "rewrite"):
        policy = get_retrieval_policy(intent)
        assert policy.allow_chapter_body is True


def test_generate_allows_chapter_body_with_all_sources():
    """generate：默认策略，允许章节正文，全部来源，宽松匹配。"""
    policy = get_retrieval_policy("generate")
    assert policy.allow_chapter_body is True
    assert policy.source_kinds is None
    assert policy.entity_first is False
    assert policy.on_no_result == "fallback"


def test_summarize_keeps_small_budget():
    """summarize：允许章节正文，但条数与 token 预算最小。"""
    policy = get_retrieval_policy("summarize")
    assert policy.allow_chapter_body is True
    assert policy.token_budget < get_retrieval_policy("generate").token_budget
    assert policy.max_per_kind < get_retrieval_policy("generate").max_per_kind
    assert policy.on_no_result == "empty"


def test_unknown_intent_falls_back_to_generate():
    """未知 intent 回退 generate 策略。

    阶段二起问答类 intent（ability / character_ability / event_query 等）已
    登记到 qa 策略，不再回退；未登记的 intent（含结构化问答的 unknown）与
    None/空串仍安全回退 generate。
    """
    fallback = get_retrieval_policy("generate")
    assert get_retrieval_policy("no_such_intent") == fallback
    assert get_retrieval_policy("") == fallback
    assert get_retrieval_policy(None) == fallback
    # 结构化问答的 unknown（未识别问题）未登记，同样安全回退
    assert get_retrieval_policy("unknown") == fallback


def test_intent_matching_is_case_and_whitespace_insensitive():
    assert get_retrieval_policy("FULL_PIPELINE") == get_retrieval_policy("full_pipeline")
    assert get_retrieval_policy("  Full Pipeline  ") == get_retrieval_policy("full_pipeline")
    assert get_retrieval_policy("Continue") == get_retrieval_policy("continue")
    assert get_retrieval_policy("FULL-PIPELINE") == get_retrieval_policy("full_pipeline")


def test_policy_field_defaults():
    """RetrievalPolicy 字段默认值：全部来源、不允许章节正文、宽松匹配。"""
    policy = RetrievalPolicy()
    assert policy.source_kinds is None
    assert policy.allow_chapter_body is False
    assert policy.max_per_kind == 5
    assert policy.token_budget == 4000
    assert policy.entity_first is False
    assert policy.on_no_result == "fallback"


def test_source_kind_constants_cover_retrieval_conventions():
    """source_kind 常量与 knowledge_source.py 检索约定一致。"""
    assert "chapter" in ALL_SOURCE_KINDS
    assert "project_source_chunk" in ALL_SOURCE_KINDS
    assert "project_knowledge_fact" in ALL_SOURCE_KINDS
    assert SOURCE_KIND_CHAPTER == "chapter"


# ── finalize 定稿接入骨架：延迟导入降级（D2）───────────


class _FakeResult:
    """模拟 execute 返回值：scalar() → None，all() → []。"""

    def __init__(self, value=None):
        self._value = value

    def scalar(self):
        return self._value

    def all(self):
        return []


class _FakeDB:
    """仅覆盖 finalize_chapter_content 路径所需的最小 AsyncSession 接口。"""

    def __init__(self):
        self.added = []

    async def get(self, model, ident):
        return None

    async def execute(self, stmt):
        return _FakeResult()

    def add(self, obj):
        # 模拟 UUIDMixin 的 Python 侧 id 默认值（真实 SQLAlchemy 在 flush 时赋值）
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self):
        pass


class _FakeChapter:
    def __init__(self, content: str = "测试正文"):
        self.id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.content = content
        self.word_count = 0
        self.status = "draft"
        self.final_version_id = None


async def _finalize_once(content: str = "测试正文") -> tuple[_FakeDB, _FakeChapter]:
    """走一遍 finalize_chapter_content（fake db / fake chapter，无数据库）。"""
    from services.chapter_save import finalize_chapter_content

    db = _FakeDB()
    chapter = _FakeChapter(content=content)
    result = await finalize_chapter_content(db, chapter, content)
    return db, result


def _install_indexer_module(monkeypatch, *, fn=None) -> None:
    """把 fake services.retrieval_indexer 模块装入 sys.modules。"""
    fake_module = types.ModuleType("services.retrieval_indexer")
    if fn is not None:
        # finalize 接入点已从 index_chapter_version 换为 request_chapter_index（P0 拆分）
        fake_module.request_chapter_index = fn
        fake_module.index_chapter_version = fn  # 兼容旧名
    monkeypatch.setitem(sys.modules, "services.retrieval_indexer", fake_module)


@pytest.mark.asyncio
async def test_finalize_degrades_when_indexer_module_missing(monkeypatch, caplog):
    """indexer 模块未实现（ImportError）时，定稿不抛异常，仅告警。"""
    # sys.modules 中置 None 会使 `from services.retrieval_indexer import ...`
    # 抛 ImportError（Python 导入机制的标准行为），模拟并行任务尚未实现。
    monkeypatch.setitem(sys.modules, "services.retrieval_indexer", None)

    db, result = await _finalize_once()

    assert result.final_version_id is not None  # 定稿正常完成
    assert result.status == "final"
    assert result.content == "测试正文"
    assert "检索索引" in caplog.text  # 降级告警


@pytest.mark.asyncio
async def test_finalize_calls_index_chapter_version(monkeypatch):
    """indexer 存在时，以 (db, project_id, version_id) 调用一次，version_id 与定稿版本一致。"""
    calls: list[tuple] = []

    async def fake_index(db, project_id, version_id):
        calls.append((db, project_id, version_id))

    _install_indexer_module(monkeypatch, fn=fake_index)

    db, result = await _finalize_once()

    assert len(calls) == 1
    called_db, called_pid, called_vid = calls[0]
    assert called_db is db
    assert str(called_pid) == str(result.project_id)
    assert str(called_vid) == str(result.final_version_id)
    assert result.final_version_id is not None


@pytest.mark.asyncio
async def test_finalize_swallows_indexer_runtime_error(monkeypatch):
    """indexer 运行期抛异常也不阻断定稿（D2：索引失败不回滚章节）。"""

    async def broken_index(db, project_id, version_id):
        raise RuntimeError("indexer boom")

    _install_indexer_module(monkeypatch, fn=broken_index)

    db, result = await _finalize_once()

    assert result.final_version_id is not None
    assert result.status == "final"
    assert result.content == "测试正文"
