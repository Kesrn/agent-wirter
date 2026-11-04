"""可插拔文本 embedding provider（阶段二：向量检索层）。

按 ``settings.EMBEDDING_PROVIDER`` 分发，对外统一异步接口::

    async def get_embedding(text: str) -> list[float]   # 模块级便捷函数（单例 provider）
    class EmbeddingProvider: async def embed(self, text) -> list[float]

provider 取值
------------
- ``openai``：走 openai SDK（AsyncOpenAI），模型/维度走 settings
  （EMBEDDING_MODEL / EMBEDDING_DIMENSION）。EMBEDDING_API_KEY 存在时使用；
  无 key（或调用抛异常）时降级本地方案并 ``logger.warning``（与既有
  rag/context_loader 的降级行为一致，保证无 key 环境不崩溃）。
- ``local``（本阶段默认评测模式）：本地可复现 embedding，无任何外部依赖
  （不需要 API key、不需要 torch / sentence-transformers）。任何非 openai 的
  provider 值（含历史遗留的 ``mock`` / ``hash``）都走本地方案，仅记 info 日志。

LocalEmbeddingProvider 算法（docstring 内完整说明，可复现、可替换）
------------------------------------------------------------------
真模型接入点已留：实现/替换 ``embed`` 的 provider 类即可（如 BGE、
text-embedding-3-small），检索层（vector_retriever）只依赖本模块暴露的接口，
不感知具体算法。

``local`` 是"伪语义"替代：不做语义建模，只保证 **词面重叠可量化**——
中文小说评测场景下实体名/能力名（2~4 字专名）是核心检索信号，字符 n-gram
重叠足以撑起可复现的向量通道消融对比；有真模型时换 provider 即可，
无需改动检索层。

线程安全说明：set_corpus 应在索引前一次性调用（索引侧完成）；之后 embed
只读 n-gram / IDF 统计，在 CPython 下是 GIL 安全的。
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading

import numpy as np

from config.settings import settings

logger = logging.getLogger(__name__)

# 本地 embedding 固定维度上限：feature hashing 桶数。
# 维度取 min(settings.EMBEDDING_DIMENSION, LOCAL_MAX_DIMENSION)，且不低于 64。
# 512 桶在 7k 级 chunk 库上重叠量化足够，内存/存储（SQLite JSON 列）也更省；
# 如需更大维度，显式设置 EMBEDDING_DIMENSION（<= 512 时按设置值使用）。
LOCAL_MAX_DIMENSION = 512
LOCAL_MIN_DIMENSION = 64

# 字符 n-gram 阶数（1-gram 字符 + 2-gram + 3-gram，中文专名多为 2~4 字）
NGRAM_ORDERS = (1, 2, 3)


def _hash_bucket(gram: str, dim: int) -> int:
    """把 n-gram 确定性地哈希到 [0, dim) 桶（feature hashing）。

    SHA-256 保证跨进程/跨版本完全一致（同文本同向量）。
    """
    digest = hashlib.sha256(gram.encode("utf-8")).digest()
    # 取前 8 字节按大端解析成 64 位整数，再取模（比直接吃 4 字节碰撞更少）
    return int.from_bytes(digest[:8], "big") % dim


def extract_ngrams(text: str) -> list[str]:
    """提取字符 n-gram（1/2/3-gram），过滤空白与纯标点窗口。

    文本先按空白切块（中文通常无空白，英文按词切分），每块内逐字符滑窗。
    单字符 gram 仅保留中文字符/字母/数字（过滤标点），2/3-gram 同理
    （窗口内任一字符非字母数字时整窗丢弃，避免「的、」「——」这类噪声窗）。
    """
    if not text:
        return []
    grams: list[str] = []
    for piece in str(text).split():
        chars = list(piece)
        if not chars:
            continue
        for order in NGRAM_ORDERS:
            for i in range(len(chars) - order + 1):
                gram = "".join(chars[i : i + order])
                if any(ch.isalnum() for ch in gram):
                    grams.append(gram)
    return grams


class LocalEmbeddingProvider:
    """本地可复现 embedding：字符 n-gram 词袋 + IDF 加权 + feature hashing。

    算法（完全确定，无外部依赖）::

        1. 文本逐字符提取 unigram/bigram/trigram（见 extract_ngrams）；
        2. 每个 n-gram 用 SHA-256 哈希映射到固定维度桶（feature hashing，
           维度 = min(settings.EMBEDDING_DIMENSION, 512)，默认 512）；
        3. 桶权重 = 桶内 n-gram 在本文本的出现次数（TF） × 桶 IDF；
           IDF 来自 set_corpus() 提供的语料（默认空语料 → idf=1，即纯 TF）::

               idf(bucket) = log((N + 1) / (df(bucket) + 1)) + 1

           N = 语料文档数；df(bucket) = 包含该桶 n-gram 的文档数。
           语料即项目自身的 retrieval_chunks 内容（不依赖任何外部语料），
           索引侧调用 set_corpus 后，查询侧用同一实例 embed 即可一致；
        4. L2 归一化（单位向量，余弦相似度退化为点积）。

    确定性：同一文本 + 同一语料 → 同一向量（哈希与 IDF 均确定）；
    不同文本的词面重叠越大 → 余弦越大。

    说明：这是"伪语义"替代（只量化词面 n-gram 重叠，不做语义建模）。
    真模型接入点 = EmbeddingProvider.openai 或任何实现 ``embed(text)`` 的类；
    本类仅用于无 API key 时的可复现评测与离线场景。
    """

    def __init__(self, dimension: int | None = None):
        raw = int(dimension if dimension is not None else settings.EMBEDDING_DIMENSION)
        self.dimension = max(LOCAL_MIN_DIMENSION, min(raw, LOCAL_MAX_DIMENSION))
        # 语料统计（IDF 用）；set_corpus 一次性设置
        self._n_docs: int = 0
        self._df: dict[int, int] = {}
        self._lock = threading.RLock()

    # ── 语料（IDF） ────────────────────────────────────────

    def set_corpus(self, texts: list[str]) -> None:
        """用一份语料计算桶级 IDF（语料 = 项目自身的 chunk 内容）。

        同文档含多个相同 n-gram 只计一次 df；语料顺序不影响结果。
        空语料 → 重置为纯 TF 模式（idf=1）。
        """
        with self._lock:
            df: dict[int, int] = {}
            for t in texts:
                if not (t or "").strip():
                    continue
                seen: set[int] = set()
                for gram in extract_ngrams(t):
                    bucket = _hash_bucket(gram, self.dimension)
                    if bucket not in seen:
                        seen.add(bucket)
                        df[bucket] = df.get(bucket, 0) + 1
            self._df = df
            self._n_docs = len([t for t in texts if (t or "").strip()])

    def _idf(self, bucket: int) -> float:
        n = self._n_docs
        if n <= 0:
            return 1.0
        df = self._df.get(bucket, 0)
        return math.log((n + 1) / (df + 1)) + 1.0

    # ── 向量化 ────────────────────────────────────────────

    async def embed(self, text: str) -> list[float]:
        """文本 → 单位向量（维度 = self.dimension）。

        空文本返回全零向量（调用方应自行判空；检索层对空 query 提前返回 []）。
        """
        vec = np.zeros(self.dimension, dtype=np.float64)
        text = str(text or "")
        with self._lock:
            for gram in extract_ngrams(text):
                bucket = _hash_bucket(gram, self.dimension)
                vec[bucket] += self._idf(bucket)  # TF 计次（同桶多个 gram 累加）
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return [float(x) for x in vec]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（与 EmbeddingProvider.embed_many 同构接口）。

        本地实现逐条 embed，无并发/批量收益，但保证调用方（vector_retriever
        的批量索引路径）在 mock/local provider 下同样可用。
        """
        return [await self.embed(t) for t in texts]


class EmbeddingProvider:
    """按 settings.EMBEDDING_PROVIDER 分发的 provider 门面。

    - ``openai``：AsyncOpenAI 调 embeddings API（EMBEDDING_API_KEY 存在时）；
      无 key / 调用失败 → 降级 LocalEmbeddingProvider（logger.warning）。
    - 其他取值（含 ``local`` / 历史遗留 ``mock`` / ``hash``）→ 本地方案。
    """

    def __init__(
        self,
        provider: str | None = None,
        *,
        api_key: str | None = None,
        model: str | None = None,
        dimension: int | None = None,
    ):
        self.provider = (provider or settings.EMBEDDING_PROVIDER).lower()
        self.api_key = api_key if api_key is not None else settings.EMBEDDING_API_KEY
        self.model = model or settings.EMBEDDING_MODEL
        self._local = LocalEmbeddingProvider(dimension=dimension)
        self._client = None  # AsyncOpenAI 客户端（懒加载缓存）

    def _openai_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            # embedding 端点优先独立配置（EMBEDDING_BASE_URL），
            # 其次回退 LLM 对话端点，最后 OpenAI 官方默认。
            base_url = settings.EMBEDDING_BASE_URL or settings.LLM_BASE_URL or None
            self._client = AsyncOpenAI(api_key=self.api_key, base_url=base_url)
        return self._client

    def set_corpus(self, texts: list[str]) -> None:
        """设置本地 provider 的 IDF 语料（透传到 LocalEmbeddingProvider）。

        检索侧（vector_retriever.index_project_vectors）在索引前调用；
        openai 通道是真实语义模型，不依赖语料，此调用仅影响本地降级实例。
        """
        self._local.set_corpus(texts)

    async def _embed_openai(self, text: str) -> list[float]:
        """openai SDK 调用。文本按既有约定截断到 8000 字符。

        部分端点（如 bge-m3 的 8192 token 上限）对超长中文输入返回 400：
        按 8000 → 4000 → 2000 逐级缩短重试（仅 400 触发，其余错误直接抛出，
        由上层统一降级）。
        """
        for limit in (8000, 4000, 2000):
            truncated = text[:limit]
            try:
                resp = await self._openai_client().embeddings.create(
                    model=self.model, input=truncated
                )
                return list(resp.data[0].embedding)
            except Exception as exc:  # noqa: BLE001 — 仅 400（输入超长）继续缩短重试
                if limit == 2000 or getattr(exc, "status_code", None) != 400:
                    raise
                logger.warning(
                    "embedding 输入超长被拒（limit=%d），缩短重试", limit
                )
        raise RuntimeError("unreachable")  # pragma: no cover — 循环必然 return/raise

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding（一次 API 请求多条，OpenAI 兼容端点支持 input 数组）。

        - 空列表返回 []；
        - 批量调用失败（限流 / 批内单条超长）时**二分拆批**递归重试：
          成功侧保持批量，仅失败侧继续拆，最坏退化为单条 embed
          （含 400 截断重试）——不整体降级逐条，索引大库时省大量请求；
        - 非 openai / 无 key 时走本地方案逐条生成。
        """
        if not texts:
            return []
        if self.provider != "openai" or not self.api_key:
            return [await self._local.embed(t) for t in texts]
        try:
            resp = await self._openai_client().embeddings.create(
                model=self.model,
                input=[str(t or "")[:8000] for t in texts],
            )
            by_index = {d.index: d.embedding for d in resp.data}
            return [list(by_index[i]) for i in range(len(texts))]
        except Exception:  # noqa: BLE001 — 失败侧二分重试（定位问题条）
            if len(texts) <= 1:
                return [await self.embed(texts[0])]
            mid = len(texts) // 2
            left = await self.embed_many(texts[:mid])
            right = await self.embed_many(texts[mid:])
            return left + right

    async def embed(self, text: str) -> list[float]:
        if self.provider == "openai":
            if not self.api_key:
                logger.warning(
                    "EMBEDDING_API_KEY 未配置，openai provider 降级为本地伪语义 "
                    "embedding（评测请显式设置 EMBEDDING_PROVIDER=local）"
                )
                return await self._local.embed(text)
            try:
                return await self._embed_openai(text)
            except Exception as exc:  # noqa: BLE001 — 网络/限流/维度错误统一降级
                logger.warning("openai embedding 调用失败（%s），降级本地 embedding", exc)
                return await self._local.embed(text)
        if self.provider in ("mock", "hash"):
            logger.info("provider=%s 视为 local 伪语义 embedding（历史遗留命名）", self.provider)
        return await self._local.embed(text)


# ── 进程内单例（检索层默认使用；同一实例保证索引/查询的 IDF 一致） ──

_provider_lock = threading.RLock()
_provider_singletons: dict[str, EmbeddingProvider] = {}


def get_provider(provider: str | None = None) -> EmbeddingProvider:
    """获取 provider 单例（同一 provider 名共享同一实例）。

    注意：本地 provider 的 IDF 语料挂在实例上，索引项目向量前应调用
    ``provider.set_corpus(...)``（vector_retriever.index_project_vectors 内部会做），
    查询侧务必用同一实例（VectorRetriever 构造时默认取此单例）。
    """
    name = (provider or settings.EMBEDDING_PROVIDER).lower()
    with _provider_lock:
        inst = _provider_singletons.get(name)
        if inst is None:
            inst = EmbeddingProvider(provider=name)
            _provider_singletons[name] = inst
        return inst


def clear_provider_cache() -> None:
    """清空 provider 单例缓存（测试隔离用）。"""
    with _provider_lock:
        _provider_singletons.clear()


async def get_embedding(text: str) -> list[float]:
    """模块级便捷入口：按 settings.EMBEDDING_PROVIDER 生成文本向量。"""
    return await get_provider().embed(text)


__all__ = [
    "EmbeddingProvider",
    "LocalEmbeddingProvider",
    "get_provider",
    "get_embedding",
    "clear_provider_cache",
    "extract_ngrams",
]
