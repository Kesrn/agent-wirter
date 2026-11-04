"""知识问答固定回归集（用户验收场景）。

覆盖链路演进后的关键保证：
1. 王三胖和赵坤三躲在哪里 → 命中第 613 章铁笼
2. 许昭霆雷电实力如何 → 召回原文实战表现
3. 张英璐结局 → 召回"被杀"结局句（有证据就答）
4. 张小侯家庭 → 召回家庭信息句，不得拿职业经历代替
5. 否定证据按句子语义判断（"资料没有写许昭霆…"不被误当正向证据）
6. 模型空输出 / 格式错误 / 临时 500 → 真正重试一次，永不保存空消息

检索层断言走 V1 规则 planner（确定性，不依赖 LLM）；重试断言 mock provider。
"""

import asyncio
import uuid

import pytest

# 确保知识库模型注册到 Base.metadata（必须在 setup_db 建表之前）
from models.project_source import ProjectSource          # noqa: F401
from models.project_source_chunk import ProjectSourceChunk  # noqa: F401
from models.structured_knowledge import CharacterAliasCluster, CharacterProfile  # noqa: F401
from models.knowledge_qa_session import KnowledgeQaSession  # noqa: F401
from models.knowledge_qa_message import KnowledgeQaMessage  # noqa: F401

from test_smoke import client, _auth_headers, test_session_factory  # noqa: F401
from services.knowledge_query_plan import build_knowledge_query_plan
from services.knowledge_source import chunk_text, search_project_knowledge_with_plan
from services.retrieval_indexer import (
    clear_indexer_cache,
    ensure_fts_table,
    index_source_chunks,
)
from services.retrieval_tokenizer import clear_tokenizer_cache
from services.knowledge_retrieval import (
    _classify_evidence,
    check_evidence_sufficiency,
    build_supplemental_queries,
    retrieve_and_classify,
)


@pytest.fixture(scope="session", autouse=True)
def _setup_db():
    """建测试表（含 FTS5；与生产检索链路一致，本文件单独收集时也建表）。

    测试环境 SQLite 内存库共享，任何测试文件建表后其它文件均可复用。
    """
    import asyncio
    from db.session import set_engine
    from models.base import Base
    from test_smoke import test_engine

    set_engine(test_engine)

    async def _create():
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await ensure_fts_table(conn)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_create())
    loop.close()
    yield


@pytest.fixture(autouse=True)
def _hybrid_mode_clean_caches():
    """检索层用例走 hybrid（与生产一致：jieba 词法 + FTS + 实体通道），
    前后清理分词/索引缓存避免串扰，结束后恢复默认 ilike。"""
    from config.settings import settings

    object.__setattr__(settings, "RETRIEVAL_MODE", "hybrid")
    clear_tokenizer_cache()
    clear_indexer_cache()
    yield
    clear_tokenizer_cache()
    clear_indexer_cache()
    object.__setattr__(settings, "RETRIEVAL_MODE", "ilike")


# ── Helpers ──

def _create_project(title="回归测试项目"):
    headers = _auth_headers("regression_tester")
    resp = client.post("/api/projects", json={"title": title}, headers=headers)
    assert resp.status_code == 200, f"create project failed: {resp.text}"
    return resp.json()["id"], headers


async def _seed_source(db, project_id, title, content, vocab: dict[str, list[str]]):
    """种子资料：词表（实体通道）+ 切片 + FTS 索引（与生产上传链路等价）。

    vocab: 人物名 → 别名簇，供 lexical 实体通道精确召回人名。
    返回 source_id。
    """
    from sqlalchemy import select

    pid = str(project_id)
    for name, aliases in vocab.items():
        db.add(CharacterProfile(project_id=pid, name=name,
                                identity_desc="角色", origin="manual", source_priority=100))
        db.add(CharacterAliasCluster(project_id=pid, canonical_name=name,
                                     aliases=aliases, source="manual"))
    source = ProjectSource(project_id=pid, title=title, content=content)
    db.add(source)
    await db.flush()
    sid = str(source.id)
    for idx, text in enumerate(chunk_text(content)):
        db.add(ProjectSourceChunk(project_id=pid, source_id=sid, chunk_index=idx,
                                  content=text, token_count=len(text)))
    await db.commit()
    gid = await index_source_chunks(db, pid, sid)
    # index_source_chunks 不 commit：必须显式提交，否则检索会话（另一连接）
    # 看不到未提交的 retrieval_chunks 写入（SQLite 未提交事务对其他连接不可见）
    await db.commit()
    assert gid is not None, "索引写入失败"
    return sid


def _retrieve_qa(project_id: str, question: str, limit: int = 24):
    """资料问答检索层（与 ask 同路径）：规则 plan → retrieve_and_classify，
    include_structured=False 走多查询融合（hybrid 模式，与生产一致）。"""
    plan = build_knowledge_query_plan(question, project_id=project_id)

    async def _run():
        async with test_session_factory() as session:
            structured, chunks, grouped = await retrieve_and_classify(
                session, project_id, plan,
                intent=plan.intent,
                entities=plan.entities,
                attributes=[],
                include_structured=False,
                limit=limit,
            )
            return structured, chunks, grouped

    structured, chunks, grouped = asyncio.run(_run())
    return plan, structured, chunks, grouped


def _positive_snippets(grouped) -> list[str]:
    return [ev.snippet for ev in grouped.positive_evidence()]


# ── 1. 王三胖和赵坤三 → 第 613 章铁笼 ─────────────────────

def test_regression_wang_sanpang_cage_613():
    """"王三胖和赵坤三躲在哪里"应召回第 613 章铁笼片段。"""
    pid, headers = _create_project()
    content = (
        "第613章 铁笼\n"
        "铁笼很大，接近一个房间，里面铺着干草。王三胖和赵坤三两个人就在里面，"
        "张小侯也被关在旁边。三人透过铁栏看着外面的黑兽潮。"
    )

    async def _run():
        async with test_session_factory() as session:
            await _seed_source(
                session, pid, "全职法师 613", content,
                vocab={"王三胖": ["王三胖", "三胖"], "赵坤三": ["赵坤三", "坤三"]},
            )

    asyncio.run(_run())

    plan, _structured, _chunks, grouped = _retrieve_qa(pid, "王三胖和赵坤三躲在哪里")

    snippets = _positive_snippets(grouped)
    assert snippets, "应有正向证据"

    cage_hit = [
        s for s in snippets
        if "铁笼" in s and ("王三胖" in s or "赵坤三" in s)
    ]
    assert cage_hit, f"证据应含铁笼+人物句，实际 snippets: {snippets[:3]}"

    # 充分性：查询实体（王三胖/赵坤三）与核心词（铁笼）都应被覆盖
    report = check_evidence_sufficiency(plan, grouped)
    assert not report.missing_entities, f"实体未覆盖: {report.missing_entities}"
    assert "铁笼" not in [a.lower() for a in report.missing_attributes], (
        f"铁笼未被覆盖: {report.missing_attributes}"
    )


# ── 2. 许昭霆雷电实力 → 原文实战表现 ───────────────────────

def test_regression_xu_zhaoting_lightning_strength():
    """"许昭霆雷电实力如何"应召回原文实战表现句（而非缺失声明）。"""
    pid, headers = _create_project()
    content = (
        "第301章 疾风\n"
        "许昭霆觉醒雷电系后实力大涨，与魔法兽激战时释放雷弧，"
        "将冲来的魔法兽劈成焦炭，周围学员无不惊叹。"
    )

    async def _run():
        async with test_session_factory() as session:
            await _seed_source(
                session, pid, "全职法师 301", content,
                vocab={"许昭霆": ["许昭霆", "昭霆"]},
            )

    asyncio.run(_run())

    plan, _structured, _chunks, grouped = _retrieve_qa(pid, "许昭霆雷电实力如何")

    snippets = _positive_snippets(grouped)
    combat_hit = [
        s for s in snippets
        if "许昭霆" in s and any(t in s for t in ("雷弧", "劈", "雷电", "雷"))
    ]
    assert combat_hit, f"实战表现句应被召回，实际 snippets: {snippets[:3]}"

    # 缺失声明（资料没写）不应混进正向证据池
    assert not any("没有说明" in s or "无法确定" in s for s in snippets), (
        f"缺失声明不应为正向证据: {snippets}"
    )


# ── 3. 张英璐结局 → 有证据就召回结局句 ─────────────────────

def test_regression_zhang_yinglu_fate():
    """"张英璐结局"应召回被杀结局句（有明确证据时）。"""
    pid, headers = _create_project()
    content = (
        "第101章 博城灾难\n"
        "张英璐在博城灾难中被巨眼猩鼠杀死，王三胖向白阳教官汇报时"
        "明确说\"张英璐被杀死了\"。"
    )

    async def _run():
        async with test_session_factory() as session:
            await _seed_source(
                session, pid, "全职法师 101", content,
                vocab={"张英璐": ["张英璐", "英璐"], "王三胖": ["王三胖", "三胖"]},
            )

    asyncio.run(_run())

    plan, _structured, _chunks, grouped = _retrieve_qa(pid, "张英璐结局")

    snippets = _positive_snippets(grouped)
    fate_hit = [
        s for s in snippets
        if "张英璐" in s and ("杀死" in s or "死" in s)
    ]
    assert fate_hit, f"结局句应被召回，实际 snippets: {snippets[:3]}"


# ── 4. 张小侯家庭 → 家庭信息句，不得拿职业经历代替 ──────────

def test_regression_zhang_xiaohou_family():
    """"张小侯的家庭情况"应召回家庭信息句（家庭句存在时）。"""
    pid, headers = _create_project()
    content = (
        "张小侯出身猎人世家，父亲是猎王，母亲早年去世。\n"
        "张小侯是魔法学院的精英学员，多次参加边境任务，屡立战功。"
    )

    async def _run():
        async with test_session_factory() as session:
            await _seed_source(
                session, pid, "张小侯资料", content,
                vocab={"张小侯": ["张小侯", "小侯"]},
            )

    asyncio.run(_run())

    plan, _structured, _chunks, grouped = _retrieve_qa(pid, "张小侯的家庭情况")

    snippets = _positive_snippets(grouped)
    family_hit = [
        s for s in snippets
        if any(t in s for t in ("父亲", "母亲", "猎人世家", "家"))
    ]
    assert family_hit, f"家庭信息句应被召回，实际 snippets: {snippets[:3]}"


# ── 5. 否定证据按句子语义判断 ──────────────────────────────

def test_regression_negative_by_sentence_semantics():
    """含查询实体但整句是缺失声明的片段应判 negative_evidence。

    "资料中没有说明许昭霆的雷系表现，无法确定他的雷电实力"——
    即使含"许昭霆"，句子语义是"资料没写"，不能当正向证据。
    """
    result = {
        "snippet": "资料中没有说明许昭霆的雷系表现，无法确定他的雷电实力。",
        "title": "资料说明",
        "source_type": "upload",
    }
    ev_type = _classify_evidence(
        result, "character_ability", ["许昭霆"], ["雷电"],
    )
    assert ev_type == "negative_evidence", f"缺失声明应判 negative，得到 {ev_type}"


def test_regression_positive_entity_clause_not_negative():
    """实体所在子句本身是正向事实时，不因其它子句的否定词误判 negative。

    "张英璐被巨眼猩鼠杀死，没有留下遗言"——"被杀死"是正向事实，
    否定词"没有"只在第二子句，不应整块判 negative。
    """
    result = {
        "snippet": "张英璐被巨眼猩鼠杀死，没有留下遗言。",
        "title": "博城灾难",
        "source_type": "upload",
    }
    ev_type = _classify_evidence(
        result, "character_profile", ["张英璐"], [],
    )
    assert ev_type != "negative_evidence", f"正向实体句不应判 negative，得到 {ev_type}"


def test_regression_plot_prose_not_worldbuilding_or_table():
    """剧情正文（含"灾难/初阶"等词）不得判为世界观设定或技能表。

    "莫凡在博城篇魔法达到了什么水平"的正文证据若被误判 worldbuilding_entry/
    ability_table，剧情片段会被挤出 prompt，回答变成设定罗列/时间线错乱。
    """
    result = {
        "snippet": "博城灾难发生后，莫凡还只是初阶魔法师，火雷双系，唐月老师评价他初阶水平。",
        "title": "全职法师笔趣阁txt",
        "source_type": "novel",
    }
    ev_type = _classify_evidence(
        result, "plot_event", ["莫凡"], [],
    )
    assert ev_type != "worldbuilding_entry", f"剧情正文不应判世界观，得到 {ev_type}"
    assert ev_type != "ability_table", f"剧情正文不应判技能表，得到 {ev_type}"
    assert ev_type == "timeline_event", f"剧情正文应判时间线/事件，得到 {ev_type}"


def test_regression_real_table_still_ability_table():
    """真技能表（管道分隔表格行）仍应判 ability_table。"""
    result = {
        "snippet": "11 | 冰系 | 初阶 | 冰蔓 | 冰蔓·覆盖\n12 | 冰系 | 中阶 | 冰锁",
        "title": "法系通用技能表",
        "source_type": "reference",
    }
    ev_type = _classify_evidence(
        result, "character_ability", ["冰系"], ["冰系"],
    )
    assert ev_type == "ability_table", f"真表格应判技能表，得到 {ev_type}"


# ── 9. 结局时间线通道：实体在最后章节的状态 = 结局线索 ─────

def test_regression_timeline_tail_finds_latest_chapter():
    """结局类问题按 chunk_index 倒序打捞实体最后出现的片段。

    "穆白的结局是什么"——普通检索命中中早期顺带提及；时间线通道必须返回
    实体在资料收录末端（最大 chunk_index）的出现。
    """
    from services.knowledge_source import _search_timeline_tail

    pid, headers = _create_project()
    content_early = (
        "第50章 历练\n穆白在百草山谷历练，与幽狼兽对战。"
    )
    content_late = (
        "第3195章 堕落之翼\n穆白被堕落之翼提到血红长空，与莫凡配合作战。"
    )

    async def _seed():
        async with test_session_factory() as session:
            await _seed_source(
                session, pid, "全职法师", content_early + "\n\n" + content_late,
                vocab={"穆白": ["穆白", "慕白"]},
            )

    asyncio.run(_seed())

    async def _run():
        async with test_session_factory() as session:
            return await _search_timeline_tail(session, pid, "穆白", limit=5)

    tail = asyncio.run(_run())
    assert tail, "时间线通道应有结果"
    assert "3195" in tail[0]["snippet"] or "堕落之翼" in tail[0]["snippet"], (
        f"应返回实体在最后章节的片段，实际: {tail[0]['snippet'][:50]}"
    )


# ── 6. 证据充分性 + 有上限补查（规则型） ───────────────────

def test_sufficiency_report_missing_entity():
    """查询实体未被任何正向证据覆盖时，报告 insufficient 并列出缺失实体。"""
    from services.knowledge_retrieval import GroupedEvidence, ClassifiedEvidence

    plan = build_knowledge_query_plan("王三胖躲在哪里", project_id="p-reg")
    grouped = GroupedEvidence()
    grouped.generic.append(ClassifiedEvidence(
        evidence_type="generic_context", source_kind="source_chunk",
        source_id="s1", chunk_id="c1", title="资料", snippet="铁笼很大，里面铺着干草。",
        score=10.0,
    ))

    report = check_evidence_sufficiency(plan, grouped)
    assert not report.sufficient
    assert "王三胖" in report.missing_entities


def test_supplemental_queries_from_report():
    """补充查询基于缺失实体 + 核心词生成，最多 2 条且不重复。"""
    from services.knowledge_retrieval import GroupedEvidence

    plan = build_knowledge_query_plan("王三胖和赵坤三躲在哪里", project_id="p-reg")
    report = check_evidence_sufficiency(plan, GroupedEvidence())
    queries = build_supplemental_queries(plan, report)

    assert 1 <= len(queries) <= 2
    for q in queries:
        assert "王三胖" in q, f"补充查询应含缺失实体，实际: {queries}"


# ── 7. 空输出 / 格式错误 / 500 → 真正重试一次 ──────────────

class _FakeProvider:
    """可编程 mock provider：按调用次数依次返回结果。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.prompts = []

    async def generate(self, system_prompt, user_prompt, **kwargs):
        self.calls += 1
        self.prompts.append(user_prompt)
        item = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def test_retry_empty_then_success():
    """第一次空输出 → 带提示重试 → 第二次成功，返回成功结果。"""
    from services.knowledge_source import _generate_qa_answer_with_retry

    provider = _FakeProvider([
        "   ",
        '{"answer": "许昭霆实力强大，曾用雷弧劈碎魔法兽。", "citations": []}',
    ])
    answer, citations, ok = asyncio.run(
        _generate_qa_answer_with_retry(provider, "许昭霆雷电实力如何")
    )
    assert ok
    assert provider.calls == 2
    assert "许昭霆" in answer
    assert "重试" not in provider.prompts[0]
    assert "必须直接以 JSON 格式回答" in provider.prompts[1]


def test_retry_empty_twice_returns_fallback_signal():
    """两次都空输出 → 返回 (\"\", [], False)，调用方兜底，绝不保存空消息。"""
    from services.knowledge_source import _generate_qa_answer_with_retry

    provider = _FakeProvider(["", ""])
    answer, _citations, ok = asyncio.run(
        _generate_qa_answer_with_retry(provider, "张英璐结局")
    )
    assert not ok
    assert answer == ""


def test_retry_format_error_then_success():
    """第一次格式错误（无 answer 字段）→ 重试 → 第二次成功。"""
    from services.knowledge_source import _generate_qa_answer_with_retry

    provider = _FakeProvider([
        "这是纯文本解释，没有 JSON。",
        '{"answer": "根据资料，张英璐被巨眼猩鼠杀死。", "citations": []}',
    ])
    answer, _citations, ok = asyncio.run(
        _generate_qa_answer_with_retry(provider, "张英璐结局")
    )
    assert ok
    assert provider.calls == 2
    assert "张英璐被巨眼猩鼠杀死" in answer


def test_retry_500_then_success():
    """第一次临时异常（500 类）→ 重试 → 第二次成功。"""
    from services.knowledge_source import _generate_qa_answer_with_retry

    provider = _FakeProvider([
        RuntimeError("upstream 500"),
        '{"answer": "王三胖和赵坤三躲在铁笼里。", "citations": []}',
    ])
    answer, _citations, ok = asyncio.run(
        _generate_qa_answer_with_retry(provider, "王三胖和赵坤三躲在哪里")
    )
    assert ok
    assert provider.calls == 2
    assert "铁笼" in answer


def test_retry_500_twice_raises():
    """两次都临时异常 → 最后一次异常向上抛（由调用方按 500 兜底）。"""
    from services.knowledge_source import _generate_qa_answer_with_retry

    provider = _FakeProvider([
        RuntimeError("upstream 500"),
        RuntimeError("upstream 500 again"),
    ])
    with pytest.raises(RuntimeError):
        asyncio.run(_generate_qa_answer_with_retry(provider, "任意问题"))


# ── 8. ask 端到端：重试后仍空 → 保存的消息永不为空 ─────────

@pytest.mark.asyncio
async def test_ask_never_saves_empty_message(monkeypatch):
    """LLM 两次空输出 → ask 返回兜底文案，且 DB 里 assistant 消息非空。"""
    from sqlalchemy import select
    from services.knowledge_source import ask_knowledge_question

    pid, headers = _create_project()
    content = "许昭霆觉醒了雷电系，实战中释放雷弧劈碎魔法兽。"

    async def _seed():
        async with test_session_factory() as session:
            await _seed_source(
                session, pid, "许昭霆", content,
                vocab={"许昭霆": ["许昭霆", "昭霆"]},
            )

    await _seed()

    class _EmptyProvider:
        async def generate(self, *args, **kwargs):
            return ""

    async def _no_llm_plan(*args, **kwargs):
        return None

    async def _fake_llm_config(*args, **kwargs):
        return {"provider": "openai", "api_key": "test", "base_url": "http://x", "model": "m"}

    # 强制走 LLM 生成路径：规则 fallback planner + 本地编译答案关闭
    monkeypatch.setattr("services.knowledge_query_planner._build_llm_plan", _no_llm_plan)
    monkeypatch.setattr("services.knowledge_source._try_compile_local_qa_answer",
                        lambda q, plan, grouped: None)
    # LLM provider 恒返回空串（两次重试都空）
    monkeypatch.setattr("agents.llm_provider.get_llm_provider", lambda cfg: _EmptyProvider())
    monkeypatch.setattr("api.llm_deps.get_user_llm_config", _fake_llm_config)

    user_id = _user_id()
    async with test_session_factory() as db:
        result = await ask_knowledge_question(
            db, pid, user_id, "许昭霆雷电实力如何",
            conversation_id=None,
        )
        answer = result["answer"]
        assert answer and answer.strip(), "answer 不应为空字符串"

        msgs = (
            await db.execute(
                select(KnowledgeQaMessage)
                .where(KnowledgeQaMessage.session_id == result["conversation_id"])
            )
        ).scalars().all()
        assistant = [m for m in msgs if m.role == "assistant"]
        assert assistant, "应保存 assistant 消息"
        for m in assistant:
            assert m.content and m.content.strip(), "永不保存空消息"


def _user_id():
    """从 auth headers 解析当前测试用户 id。"""
    resp = client.get("/api/auth/me", headers=_auth_headers("regression_tester"))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]
