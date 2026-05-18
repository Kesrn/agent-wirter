"""RAG 上下文加载服务

从数据库检索相关世界观、角色、前文章节。
支持 mock embedding fallback（无需 API key）。
向量搜索使用 Python 内存余弦相似度计算（无需 pgvector 扩展）。
"""

import logging
import math
import hashlib
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingService:
    """Embedding 服务：mock 或真实 API"""

    def __init__(self):
        self._provider = settings.EMBEDDING_PROVIDER

    async def embed_text(self, text: str) -> list[float]:
        """生成文本向量"""
        if self._provider == "mock":
            return self._mock_embed(text)
        return await self._api_embed(text)

    def _mock_embed(self, text: str) -> list[float]:
        """Mock embedding：基于文本哈希生成确定性伪向量"""
        dim = settings.EMBEDDING_DIMENSION
        h = hashlib.sha256(text.encode()).digest()
        vec = []
        for i in range(dim):
            byte_idx = i % len(h)
            vec.append((h[byte_idx] / 255.0 - 0.5) * 0.1)
        return vec

    async def _api_embed(self, text: str) -> list[float]:
        """真实 API embedding（OpenAI / HuggingFace）"""
        if not settings.EMBEDDING_API_KEY:
            logger.warning("EMBEDDING_API_KEY 未设置，降级为 mock embedding")
            return self._mock_embed(text)

        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.LLM_BASE_URL or 'https://api.openai.com/v1'}/embeddings",
                headers={"Authorization": f"Bearer {settings.EMBEDDING_API_KEY}"},
                json={"model": settings.EMBEDDING_MODEL, "input": text},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]


class ContextLoader:
    """上下文加载器：从数据库检索相关上下文，优先使用向量相似度搜索"""

    def __init__(self):
        self.embedding = EmbeddingService()

    async def index_text(self, project_id: str, text: str, metadata: dict) -> None:
        """将文本索引到数据库 embedding 字段"""
        source_type = metadata.get("source_type", "")
        source_id = metadata.get("source_id", "")

        logger.info(f"[RAG] 索引请求: project={project_id}, type={source_type}, id={source_id}")

        try:
            vec = await self.embedding.embed_text(text)
            from db.session import async_session
            from sqlalchemy import update

            async with async_session() as session:
                if source_type == "world_entry":
                    from models.world_entry import WorldEntry
                    await session.execute(
                        update(WorldEntry)
                        .where(WorldEntry.id == source_id, WorldEntry.project_id == project_id)
                        .values(embedding=vec)
                    )
                elif source_type == "character":
                    from models.character import Character
                    await session.execute(
                        update(Character)
                        .where(Character.id == source_id, Character.project_id == project_id)
                        .values(embedding=vec)
                    )
                else:
                    logger.warning(f"[RAG] 未知的 source_type: {source_type}")
                    return

                await session.commit()
                logger.info(f"[RAG] 索引完成: type={source_type}, id={source_id}")

        except Exception as e:
            logger.warning(f"[RAG] 索引失败: {e}")

    async def search_similar(self, project_id: str, query_text: str, top_k: int = 5) -> list[dict]:
        """使用 Python 内存余弦相似度搜索

        从数据库加载所有有 embedding 的条目，在内存中计算余弦相似度并排序。
        """
        try:
            query_vec = await self.embedding.embed_text(query_text)
            from db.session import async_session
            from models.world_entry import WorldEntry
            from models.character import Character
            from sqlalchemy import select

            results = []

            async with async_session() as session:
                # 加载世界观条目
                we_result = await session.execute(
                    select(WorldEntry).where(
                        WorldEntry.project_id == project_id,
                        WorldEntry.embedding.isnot(None),
                    )
                )
                for entry in we_result.scalars().all():
                    sim = _cosine_similarity(query_vec, entry.embedding)
                    results.append({
                        "source_type": "world_entry",
                        "source_id": str(entry.id),
                        "title": entry.title,
                        "content": entry.content,
                        "category": entry.category,
                        "distance": 1 - sim,
                    })

                # 加载角色
                ch_result = await session.execute(
                    select(Character).where(
                        Character.project_id == project_id,
                        Character.embedding.isnot(None),
                    )
                )
                for char in ch_result.scalars().all():
                    sim = _cosine_similarity(query_vec, char.embedding)
                    results.append({
                        "source_type": "character",
                        "source_id": str(char.id),
                        "name": char.name,
                        "profile": char.profile,
                        "role_type": char.role_type,
                        "distance": 1 - sim,
                    })

            results.sort(key=lambda x: x["distance"])
            return results[:top_k]

        except Exception as e:
            logger.warning(f"[RAG] 向量搜索失败: {e}")
            return []

    async def load_context(
        self,
        project_id: str,
        chapter_id: Optional[str] = None,
        include_world: bool = True,
        include_characters: bool = True,
        include_previous_chapters: int = 3,
        include_outline: bool = True,
    ) -> str:
        """加载创作上下文"""
        parts = []

        try:
            from db.session import async_session
            from models.world_entry import WorldEntry
            from models.character import Character
            from models.chapter import Chapter
            from sqlalchemy import select

            query_text_parts = []
            if chapter_id:
                async with async_session() as session:
                    ch_result = await session.execute(
                        select(Chapter).where(Chapter.id == chapter_id)
                    )
                    chapter = ch_result.scalar_one_or_none()
                    if chapter and chapter.outline:
                        query_text_parts.append(chapter.outline)
                    if chapter and chapter.content:
                        query_text_parts.append(chapter.content[:500])

            query_text = " ".join(query_text_parts) if query_text_parts else ""

            vector_results = []
            if query_text:
                vector_results = await self.search_similar(project_id, query_text, top_k=10)

            if vector_results:
                if include_world:
                    we_results = [r for r in vector_results if r["source_type"] == "world_entry"]
                    if we_results:
                        world_text = "\n".join(
                            f"- [{r.get('category', 'general')}] {r.get('title', '')}: {r.get('content', '')[:200]}"
                            for r in we_results[:20]
                        )
                        parts.append(f"## 世界观设定\n{world_text}")

                if include_characters:
                    ch_results = [r for r in vector_results if r["source_type"] == "character"]
                    if ch_results:
                        char_text = "\n".join(
                            f"- {r.get('name', '')}({r.get('role_type', '')}): {r.get('profile', '') or '暂无简介'}"
                            for r in ch_results[:20]
                        )
                        parts.append(f"## 角色资料\n{char_text}")
            else:
                async with async_session() as session:
                    if include_world:
                        result = await session.execute(
                            select(WorldEntry).where(WorldEntry.project_id == project_id)
                        )
                        entries = result.scalars().all()
                        if entries:
                            world_text = "\n".join(
                                f"- [{e.category}] {e.title}: {e.content[:200]}" for e in entries[:20]
                            )
                            parts.append(f"## 世界观设定\n{world_text}")

                    if include_characters:
                        result = await session.execute(
                            select(Character).where(Character.project_id == project_id)
                        )
                        chars = result.scalars().all()
                        if chars:
                            char_text = "\n".join(
                                f"- {c.name}({c.role_type}): {c.profile or '暂无简介'}" for c in chars[:20]
                            )
                            parts.append(f"## 角色资料\n{char_text}")

            if include_previous_chapters > 0 and chapter_id:
                async with async_session() as session:
                    result = await session.execute(
                        select(Chapter)
                        .where(Chapter.project_id == project_id)
                        .order_by(Chapter.sequence_number.desc())
                        .limit(include_previous_chapters)
                    )
                    chapters = result.scalars().all()
                    if chapters:
                        ch_text = "\n\n".join(
                            f"### {c.title}\n{c.content[:500] or '(空)'}" for c in reversed(chapters)
                        )
                        parts.append(f"## 前文内容\n{ch_text}")

        except Exception as e:
            logger.warning(f"上下文加载失败，使用空上下文: {e}")
            parts.append("(上下文加载失败，请检查数据库连接)")

        return "\n\n".join(parts) if parts else "(暂无上下文)"