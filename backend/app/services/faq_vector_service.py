"""FAQ 向量服务：写入向量 + 阈值内最优命中。"""

from dataclasses import dataclass

from app.repositories.faq_repository import FaqRepository
from app.services.embedding_service import EmbeddingService


@dataclass(frozen=True)
class FaqMatch:
    """FAQ 命中结果。"""

    faq_id: int
    similarity: float


class FaqVectorService:
    """FAQ 向量检索。"""

    def __init__(
        self,
        repository: FaqRepository,
        embedding: EmbeddingService,
    ) -> None:
        self._repository = repository
        self._embedding = embedding

    async def upsert_embedding(self, faq_id: int, question: str) -> None:
        """为单条 FAQ 生成并回写向量。"""
        vector = await self._embedding.embed(question)
        await self._repository.update_embedding(faq_id, vector)

    async def search_best(
        self,
        question: str,
        merchant_id: int,
        threshold: float = 0.80,
    ) -> FaqMatch | None:
        """检索最相似 FAQ，相似度低于阈值返回 None。

        cosine 相似度 = 1 - cosine_distance，越接近 1 越相似。

        **顺序是这里的关键：先过阈值，再比优先级。** 仓储给回的候选已经按
        "商家自有在前、平台在后"排好，直接取第一条过阈值的即可——
        商家自有 FAQ 只在**同样够像**的前提下才优先，而不是无条件压掉平台 FAQ。
        反过来的话（先比优先级、后判阈值），商家库里随便一条不相关的 FAQ
        都能把精确命中的平台 FAQ 顶成 ``None``。
        """
        # 传进来的是用户当前问的这一句，属于 query 路径 —— 同样不进缓存。
        # 注意 :meth:`upsert_embedding` 走的是 embed（FAQ 库里的固定问题文本），
        # 那一条**该**缓存，两者不能一起改。
        vector = await self._embedding.embed_query(question)
        candidates = await self._repository.search_candidates(vector, merchant_id)
        for faq_id, distance in candidates:
            similarity = 1.0 - distance
            if similarity >= threshold:
                return FaqMatch(faq_id=faq_id, similarity=similarity)
        return None
