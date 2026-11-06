"""项目级隔离分词器服务（阶段一：RAG 增强）。

核心决策
--------
- 每个项目持有独立的 ``jieba.Tokenizer`` 实例（不是全局 ``jieba``），
  映射 ``(project_id, vocabulary_version) -> ProjectTokenizer``。
  禁止使用全局 ``jieba.add_word``：多项目共享词典会串词、删除残留、
  测试顺序敏感，且多进程（如 gunicorn workers）各自状态不一致。
- 词典来源（``build_project_vocabulary``，只读 SELECT）：
    1. CharacterProfile.name（角色正式名）
    2. AbilityProfile.character_name + ability_name（能力名称）
    3. 已确认别名（character_alias_clusters 簇）
    4. WorldEntry.title（稳定世界观专名，scope_type 任意）
  词去重、长度过滤（>= 2 字）。
- 实体优先通道（``normalize_entities``）：别名精确命中发生在分词之前——
  查询 → 识别正式名/已确认别名 → 归一为 canonical_name → 精确实体检索 →
  再词法补充。

"已确认别名"的判定规则（读自 alias_service / character_alias_clusters）
-----------------------------------------------------------------------
- ``character_alias_clusters`` 表只写入"人工确认过"的簇：候选探测
  ``detect_alias_candidates`` 只读不写库，必须人工确认后才入表（见
  alias_service 模块 docstring："候选探测只给建议，必须人工确认后才入此表"）。
  因此表中每个簇的 canonical_name + aliases 都视为"已确认"。
- 簇的 ``source`` 字段：manual=人工确认；detected=候选探测建议但已人工确认；
  imported=外部导入——三种都经过确认，本服务不再按 source 过滤。
- ``CharacterProfile.aliases`` 是 LLM 抽取的"未确认"异体写法，不进词典、
  不进实体表（避免未确认别名污染实体检索），仅在 sources 统计中被记录为
  skipped_profile_aliases。
"""

from __future__ import annotations

import hashlib
import json
import threading

import jieba
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.structured_knowledge import AbilityProfile, CharacterProfile
from models.world_entry import WorldEntry
from services.alias_service import list_alias_clusters

__all__ = [
    "ProjectTokenizer",
    "get_project_tokenizer",
    "build_project_vocabulary",
    "normalize_entities",
    "clear_tokenizer_cache",
]

# 词典词条最小长度（按字符数计，中文 1 字 = 1 字符）
MIN_VOCAB_WORD_LEN = 2


def _clean_vocab_word(word: object) -> str | None:
    """清洗候选词：去空白、长度 >= 2、至少含一个字母/数字/汉字字符。

    返回 None 表示该词不符合词典词条标准，应被过滤。
    """
    text = str(word or "").strip()
    if len(text) < MIN_VOCAB_WORD_LEN:
        return None
    if not any(ch.isalnum() for ch in text):
        return None
    return text


class ProjectTokenizer:
    """单个项目的独立 jieba 分词器。

    每个实例持有一个独立的 ``jieba.Tokenizer()``，``add_word`` 只作用于该
    实例，不会影响全局 jieba 或其他项目。实例同时维护实体名录：

    - ``entity_map``: 别名（含正式名）-> canonical_name
    - ``canonical_names``: 正式名集合

    线程安全说明：构建（add_vocab/add_entity/load_vocabulary）应在服务启动
    或版本切换时一次性完成；此后只读调用（tokenize、entity_map）在 CPython
    下是 GIL 安全的。
    """

    def __init__(self, project_id: str, vocabulary_version: str | None = None):
        self.project_id = str(project_id)
        self.vocabulary_version = vocabulary_version
        self._tokenizer = jieba.Tokenizer()
        self.entity_map: dict[str, str] = {}
        self.canonical_names: set[str] = set()

    # ── 词典操作 ──────────────────────────────────────────

    def add_vocab(self, word: str) -> None:
        """往本实例词典加词（仅影响本实例，不影响全局 jieba / 其他项目）。"""
        clean = _clean_vocab_word(word)
        if clean is None:
            return
        self._tokenizer.add_word(clean)

    def add_entity(self, canonical_name: str, aliases: list[str] | None = None) -> None:
        """注册一个实体簇：正式名 + 已确认别名，全部加入词典并建立归一映射。

        正式名与每个别名都映射到 canonical_name（含自身映射），
        供 ``normalize_entities`` 精确命中。
        """
        canonical = _clean_vocab_word(canonical_name)
        if canonical is None:
            return
        self.add_vocab(canonical)
        self.canonical_names.add(canonical)
        self.entity_map[canonical] = canonical
        for raw_alias in aliases or ():
            alias = _clean_vocab_word(raw_alias)
            if alias is None or alias == canonical:
                continue
            self.add_vocab(alias)
            self.entity_map[alias] = canonical

    def load_vocabulary(
        self,
        vocabulary: list[str],
        entities: dict[str, list[str]] | None = None,
    ) -> None:
        """用整份词典数据填充实例（供 get_project_tokenizer 重建新实例时使用）。"""
        for word in vocabulary:
            self.add_vocab(word)
        for canonical, aliases in (entities or {}).items():
            self.add_entity(canonical, aliases)

    # ── 分词 ──────────────────────────────────────────────

    def tokenize(self, text: str) -> list[str]:
        """jieba.cut（精确模式）+ 过滤空白与纯标点，返回 token 列表。

        过滤规则：空白 token、单字符标点，以及纯标点连排（如 ——、！！！）。
        单个汉字/字母/数字 token 保留（如「火」「剑」是有意义的词素）。
        """
        if not text:
            return []
        tokens: list[str] = []
        for raw in self._tokenizer.lcut(str(text)):
            token = raw.strip()
            if not token:
                continue
            if not any(ch.isalnum() for ch in token):
                continue
            tokens.append(token)
        return tokens


# ── 缓存管理 ─────────────────────────────────────────────

_cache_lock = threading.RLock()
_tokenizer_cache: dict[tuple[str, str | None], ProjectTokenizer] = {}
_project_vocab_cache: dict[str, dict] = {}


def get_project_tokenizer(
    project_id: str,
    vocabulary_version: str | None = None,
) -> ProjectTokenizer:
    """获取项目分词器（进程内缓存，线程安全，多项目隔离）。

    - 缓存键 ``(project_id, vocabulary_version)``；vocabulary_version 变化时
      重建全新实例并丢弃同项目旧实例（不就地修改旧实例）。
    - ``vocabulary_version=None`` 时返回该项目当前缓存实例（不重建），
      适用于 normalize_entities 这类不关心版本的同步访问。
    - 新实例会用该项目最近一次 ``build_project_vocabulary`` 的结果自动填充；
      若从未构建过，则返回空词典实例（可后续 add_vocab/add_entity）。
    """
    pid = str(project_id)
    with _cache_lock:
        if vocabulary_version is None:
            existing = _tokenizer_cache.get((pid, None))
            if existing is None:
                for key, tok in _tokenizer_cache.items():
                    if key[0] == pid:
                        existing = tok
                        break
            if existing is not None:
                _tokenizer_cache[(pid, None)] = existing
                return existing

        key = (pid, vocabulary_version)
        cached = _tokenizer_cache.get(key)
        if cached is not None:
            return cached

        # 重建：丢弃同项目所有旧实例
        for old_key in [k for k in _tokenizer_cache if k[0] == pid]:
            del _tokenizer_cache[old_key]
        tokenizer = ProjectTokenizer(pid, vocabulary_version=vocabulary_version)
        vocab_data = _project_vocab_cache.get(pid)
        if vocab_data is not None:
            tokenizer.load_vocabulary(
                vocab_data["vocabulary"], vocab_data.get("entities")
            )
        _tokenizer_cache[key] = tokenizer
        return tokenizer


def clear_tokenizer_cache() -> None:
    """清空全部缓存（测试隔离 / 热重载用）。"""
    with _cache_lock:
        _tokenizer_cache.clear()
        _project_vocab_cache.clear()


# ── 词典构建（只读，不写库） ─────────────────────────────

async def build_project_vocabulary(db: AsyncSession, project_id: str) -> dict:
    """从数据库构建项目词典（只读 SELECT，不写库）。

    返回::

        {
            "vocabulary": [...],          # 去重、排序、>=2 字的词表
            "version": "<sha1 前 12 位>", # 内容哈希，内容不变则版本不变
            "entities": {canonical: [aliases...]},   # 正式名 -> 已确认别名
            "sources": {..., "alias_rule": ...},     # 各来源词条统计
        }

    词表来源与统计：
    - characters: CharacterProfile.name（去重后计）
    - abilities: AbilityProfile.character_name + ability_name（去重后计）
    - alias_clusters / alias_names: character_alias_clusters 簇数 / 已确认别名数
    - world_entries: WorldEntry.title（scope_type 任意）
    - skipped_profile_aliases: CharacterProfile.aliases 中未纳入的 LLM 抽取别名数

    调用方拿到结果后通过 ``get_project_tokenizer(project_id, data["version"])``
    获得已填充的隔离分词器。
    """
    pid = str(project_id)

    vocabulary: set[str] = set()
    entities: dict[str, list[str]] = {}
    sources: dict[str, int] = {
        "characters": 0,
        "abilities": 0,
        "alias_clusters": 0,
        "alias_names": 0,
        "world_entries": 0,
        "skipped_profile_aliases": 0,
    }

    # 1) 角色正式名（同时取 aliases 仅用于统计，不纳入词典）
    profile_rows = (await db.execute(
        select(CharacterProfile.name, CharacterProfile.aliases)
        .where(CharacterProfile.project_id == pid)
    )).all()
    for name, aliases in profile_rows:
        clean = _clean_vocab_word(name)
        if clean is not None:
            vocabulary.add(clean)
            sources["characters"] += 1
        if aliases:
            sources["skipped_profile_aliases"] += len(
                [a for a in aliases if str(a or "").strip()]
            )

    # 2) 能力名称（人物名 + 能力名）
    ability_rows = (await db.execute(
        select(AbilityProfile.character_name, AbilityProfile.ability_name)
        .where(AbilityProfile.project_id == pid)
    )).all()
    seen_ability_words: set[str] = set()
    for char_name, ability_name in ability_rows:
        for raw in (char_name, ability_name):
            clean = _clean_vocab_word(raw)
            if clean is None or clean in seen_ability_words:
                continue
            seen_ability_words.add(clean)
            vocabulary.add(clean)
            sources["abilities"] += 1

    # 3) 已确认别名（character_alias_clusters 表内簇均经过人工确认，见模块 docstring）
    clusters = await list_alias_clusters(db, pid)
    for cluster in clusters:
        sources["alias_clusters"] += 1
        canonical = _clean_vocab_word(cluster.get("canonical_name"))
        if canonical is None:
            continue
        confirmed_aliases: list[str] = []
        for raw_alias in cluster.get("aliases") or []:
            clean_alias = _clean_vocab_word(raw_alias)
            if clean_alias is None or clean_alias == canonical:
                continue
            confirmed_aliases.append(clean_alias)
            vocabulary.add(clean_alias)
            sources["alias_names"] += 1
        vocabulary.add(canonical)
        entities[canonical] = confirmed_aliases

    # 4) 稳定世界观专名（scope_type 任意）
    entry_titles = (await db.execute(
        select(WorldEntry.title).where(WorldEntry.project_id == pid)
    )).scalars().all()
    for raw in entry_titles:
        clean = _clean_vocab_word(raw)
        if clean is not None:
            vocabulary.add(clean)
            sources["world_entries"] += 1

    ordered = sorted(vocabulary)
    version = hashlib.sha1(
        json.dumps(
            {
                "vocabulary": ordered,
                "entities": {
                    canonical: sorted(aliases)
                    for canonical, aliases in sorted(entities.items())
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]

    result = {
        "vocabulary": ordered,
        "version": version,
        "entities": entities,
        "sources": sources,
        "alias_rule": (
            "已确认别名 = character_alias_clusters 表中全部簇的 canonical_name + "
            "aliases；该表只接受人工确认的簇（detect_alias_candidates 只读不写库），"
            "故不再按 source 过滤；CharacterProfile.aliases（LLM 抽取未确认）不纳入。"
        ),
    }
    with _cache_lock:
        _project_vocab_cache[pid] = result
    return result


# ── 实体优先通道（分词之前） ─────────────────────────────

def normalize_entities(project_id: str, query: str) -> tuple[list[str], str]:
    """实体优先通道：识别查询中的正式名/已确认别名，归一到 canonical_name。

    返回 ``(canonical_names, cleaned_query)``：
    - canonical_names：命中实体的正式名列表（按查询中首次出现顺序，去重）
    - cleaned_query：移除实体提及后的查询文本（供精确实体检索 / 词法补充）

    需要先对该项目执行过 ``build_project_vocabulary``（或手动 add_entity），
    否则词汇表为空，返回 ``([], query)``。

    最长别名优先匹配，避免短别名（如「心夏」）先于长正式名（如「叶心夏」）
    命中导致错误归一；同一实体去重（别名与正式名同时出现只记一次）。
    """
    tokenizer = get_project_tokenizer(project_id)
    query_text = str(query or "")
    if not query_text:
        return [], query_text

    cleaned = query_text
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    for alias in sorted(tokenizer.entity_map, key=len, reverse=True):
        pos = cleaned.find(alias)
        if pos < 0:
            continue
        canonical = tokenizer.entity_map[alias]
        cleaned = cleaned.replace(alias, " ")
        if canonical not in seen:
            seen.add(canonical)
            hits.append((pos, canonical))

    # 词表无命中时：取 jieba 分词的 3 字+ 词作实体候选（中文专有名词多为
    # 3 字人名/地名，如"张英璐/许昭霆"；2 字高频词"死了/结局/什么"自然
    # 排除）。让"张英璐死了吗"这类查询走短语精确匹配，避免实体片段被
    # 查询词里的高频噪声（如"死"）在 bm25 排序中淹没。
    if not hits:
        for word in tokenizer.tokenize(query_text):
            w = str(word).strip()
            if len(w) >= 3:
                pos = query_text.find(w)
                if pos >= 0 and w not in seen:
                    seen.add(w)
                    hits.append((pos, w))
                    break  # 只取第一个 3 字+ 词（通常为查询主实体）

    hits.sort(key=lambda item: item[0])
    return [canonical for _, canonical in hits], " ".join(cleaned.split())
