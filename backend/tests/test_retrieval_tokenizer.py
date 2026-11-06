"""项目级隔离分词器服务（retrieval_tokenizer）单元测试。

不依赖 dev-web.sqlite3：数据库用例使用内存 SQLite + 构造数据。
"""

import asyncio
import uuid

import jieba
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from models.base import Base
from models.structured_knowledge import AbilityProfile, CharacterAliasCluster, CharacterProfile
from models.world_entry import WorldEntry
from services.retrieval_tokenizer import (
    ProjectTokenizer,
    build_project_vocabulary,
    clear_tokenizer_cache,
    get_project_tokenizer,
    normalize_entities,
)


@pytest.fixture(autouse=True)
def _clean_tokenizer_cache():
    """每个用例前后清空缓存，避免测试间串扰。"""
    clear_tokenizer_cache()
    yield
    clear_tokenizer_cache()


# ── 多项目隔离 ───────────────────────────────────────────

def test_two_projects_tokenizers_isolated():
    tok_a = get_project_tokenizer("proj-iso-a")
    tok_b = get_project_tokenizer("proj-iso-b")

    assert tok_a is not tok_b
    assert tok_a._tokenizer is not tok_b._tokenizer
    assert tok_a._tokenizer is not jieba.dt  # 不触碰全局分词器

    tok_a.add_vocab("暮云澈")
    tok_b.add_vocab("林晚晴")

    # A 的词典只对 A 生效
    assert tok_a.tokenize("暮云澈") == ["暮云澈"]
    # B 的词典不含 A 的词：默认词典切不出整词（实测为 ['暮云', '澈']）
    assert "暮云澈" not in tok_b.tokenize("暮云澈")
    assert tok_b.tokenize("林晚晴") == ["林晚晴"]

    # 全局 jieba 未被污染
    jieba.dt.initialize()
    assert "暮云澈" not in jieba.dt.FREQ
    assert "林晚晴" not in jieba.dt.FREQ


# ── 版本变化重建 ─────────────────────────────────────────

def test_version_change_rebuilds_new_instance():
    t1 = get_project_tokenizer("proj-ver", "v1")
    t1.add_vocab("旧词条")
    assert t1.vocabulary_version == "v1"

    t2 = get_project_tokenizer("proj-ver", "v2")
    assert t2 is not t1
    assert t2.vocabulary_version == "v2"
    # 新实例不携带旧词典
    assert "旧词条" not in t2._tokenizer.FREQ
    assert t2.tokenize("旧词条") != ["旧词条"]

    # 同一版本命中缓存，返回同一实例
    assert get_project_tokenizer("proj-ver", "v2") is t2

    # 不指定版本时返回项目当前实例（不重建）
    assert get_project_tokenizer("proj-ver") is t2


# ── 中文分词 ─────────────────────────────────────────────

def test_tokenize_chinese_text():
    tok = get_project_tokenizer("proj-tok")
    tok.add_vocab("叶心夏")
    tok.add_vocab("火焰魔法")

    tokens = tok.tokenize("叶心夏使用火焰魔法，击败了敌人。")
    assert "叶心夏" in tokens
    assert "火焰魔法" in tokens
    # 空白与标点（含单字符）被过滤
    assert "，" not in tokens
    assert "。" not in tokens
    assert all(t.strip() for t in tokens)

    # 空输入
    assert tok.tokenize("") == []
    assert tok.tokenize(None) == []


# ── 实体优先通道（构造词汇表，不依赖数据库） ─────────────

def test_normalize_entities_recognizes_canonical_and_aliases():
    tok = get_project_tokenizer("proj-entity")
    tok.add_entity("叶心夏", ["心夏", "叶心夏妹妹"])
    tok.add_entity("穆宁雪", [])

    found, cleaned = normalize_entities("proj-entity", "心夏和穆宁雪一起去了雪山")
    assert found == ["叶心夏", "穆宁雪"]
    assert "心夏" not in cleaned
    assert "穆宁雪" not in cleaned
    assert "雪山" in cleaned

    # 正式名本身也能命中
    found2, cleaned2 = normalize_entities("proj-entity", "叶心夏使用火焰魔法")
    assert found2 == ["叶心夏"]
    assert "叶心夏" not in cleaned2
    assert "火焰魔法" in cleaned2


def test_normalize_entities_longest_alias_wins():
    """长别名优先，避免「心夏」先命中把「叶心夏妹妹」的错误前缀替换掉。"""
    tok = get_project_tokenizer("proj-entity-len")
    tok.add_entity("叶心夏", ["心夏", "叶心夏妹妹"])

    found, cleaned = normalize_entities("proj-entity-len", "叶心夏妹妹出门了")
    assert found == ["叶心夏"]
    assert "叶心夏妹妹" not in cleaned
    assert "出门" in cleaned


def test_normalize_entities_dedup_and_empty_vocab():
    tok = get_project_tokenizer("proj-entity-dedup")
    tok.add_entity("叶心夏", ["心夏"])
    found, cleaned = normalize_entities("proj-entity-dedup", "心夏是叶心夏")
    assert found == ["叶心夏"]  # 别名 + 正式名去重
    assert cleaned == "是"

    # 从未构建词汇表的项目：原样返回
    assert normalize_entities("proj-entity-empty", "心夏在做什么") == ([], "心夏在做什么")
    assert normalize_entities("proj-entity-empty", "") == ([], "")


# ── build_project_vocabulary（内存 SQLite 构造数据） ──────

@pytest.mark.asyncio
async def test_build_project_vocabulary_from_db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    pid = str(uuid.uuid4())
    other_pid = str(uuid.uuid4())

    try:
        async with factory() as db:
            db.add_all([
                # 本项目的角色：正式名 + LLM 抽取的未确认别名（不纳入词典）
                CharacterProfile(project_id=pid, name="叶心夏", aliases=["心夏"],
                                 origin="manual", source_priority=100, confidence=1.0),
                CharacterProfile(project_id=pid, name="莫凡",
                                 origin="manual", source_priority=100, confidence=1.0),
                # 其他项目的角色：必须被隔离
                CharacterProfile(project_id=other_pid, name="别家角色",
                                 origin="manual", source_priority=100, confidence=1.0),
                # 能力：人物名 + 能力名
                AbilityProfile(project_id=pid, character_name="莫凡",
                               ability_type="spell", ability_name="火焰魔法",
                               origin="manual", source_priority=100, confidence=1.0),
                # 已确认别名簇
                CharacterAliasCluster(project_id=pid, canonical_name="叶心夏",
                                      aliases=["心夏", "叶心夏妹妹"], source="manual"),
                # 世界观专名
                WorldEntry(project_id=pid, title="圣城", scope_type="global",
                           category="location", content="圣城是世界的中心。"),
            ])
            await db.commit()

        async with factory() as db:
            data = await build_project_vocabulary(db, pid)

        assert data["version"]
        vocab = data["vocabulary"]
        assert "叶心夏" in vocab
        assert "莫凡" in vocab
        assert "火焰魔法" in vocab
        assert "圣城" in vocab
        assert "心夏" in vocab            # 已确认别名入词表
        assert "叶心夏妹妹" in vocab
        assert "别家角色" not in vocab    # 项目隔离
        assert len(vocab) == len(set(vocab))

        # 实体映射：已确认别名 -> canonical
        assert data["entities"]["叶心夏"] == ["心夏", "叶心夏妹妹"]

        # 来源统计
        assert data["sources"]["characters"] == 2
        assert data["sources"]["abilities"] == 2      # 莫凡 + 火焰魔法
        assert data["sources"]["alias_clusters"] == 1
        assert data["sources"]["alias_names"] == 2    # 心夏 + 叶心夏妹妹
        assert data["sources"]["world_entries"] == 1
        assert data["sources"]["skipped_profile_aliases"] == 1  # profile 的未确认别名

        # 版本确定性：相同数据两次构建版本一致
        async with factory() as db:
            data2 = await build_project_vocabulary(db, pid)
        assert data2["version"] == data["version"]

        # 构建后 tokenizer 自动填充，实体优先通道可用
        tok = get_project_tokenizer(pid, data["version"])
        assert tok.vocabulary_version == data["version"]
        assert tok.tokenize("莫凡施展火焰魔法") == ["莫凡", "施展", "火焰魔法"]

        found, cleaned = normalize_entities(pid, "心夏学会了火焰魔法")
        assert found == ["叶心夏"]
        assert "火焰魔法" in cleaned
    finally:
        await engine.dispose()
