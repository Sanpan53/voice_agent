"""并行版推荐链路：画像查询 ∥ 候选检索。

与 :class:`~app.services.recommend_orchestrator.RecommendOrchestrator`
是**同一套业务步骤**，只有一处差别：把互相独立的两步（读画像、召回候选）从顺序
执行改成并发执行，用 ``asyncio.gather`` 合流。

**为什么这里可以用 asyncio 而不是线程**：两条路都是网络 IO（Redis / PostgreSQL
+ DashScope），``await`` 处会让出事件循环，等待时间真正重叠。如果哪天换成阻塞式
同步调用，必须用 ``asyncio.to_thread`` 包一层，否则会卡死整个事件循环。

与顺序版同步摘掉了"生成理由"这一步（见
:mod:`app.services.recommend_orchestrator` 的模块 docstring）——
**并行版也必须摘**，否则两条链路会有 800ms 的延迟差，而那正是这里要消掉的东西。
"""

import asyncio
import logging
from typing import Any

from app.schemas.recommend import TONE_EMPTY, TONE_PROFESSIONAL, RecommendResult
from app.services.profile_reranker import ProfileReranker
from app.services.recommend_candidates_service import RecommendCandidatesService
from app.services.recommend_orchestrator import build_query
from app.services.scope_filter import resolve_scope
from app.services.session_scope_cache import SessionScopeCache
from app.services.user_profile_service import UserProfileService

logger = logging.getLogger(__name__)

#: 召回条数（与顺序版保持一致）
CANDIDATE_TOP_N = 20

#: 精排后说给用户的条数
FINAL_TOP_K = 3


class ParallelRecommendService:
    """推荐链路的并行版。

    Args:
        profile_service: 用户画像服务（第一路）。
        candidates: 候选召回服务（第二路）。
        reranker: 画像加权重排器。
    """

    def __init__(
        self,
        profile_service: UserProfileService,
        candidates: RecommendCandidatesService,
        reranker: ProfileReranker,
        scope_cache: SessionScopeCache | None = None,
    ) -> None:
        self._profile_service = profile_service
        self._candidates = candidates
        self._reranker = reranker
        self._scope_cache = scope_cache

    async def recommend(
        self,
        session_id: str,
        user_id: int,
        utterance: str,
        slots: dict[str, Any] | None,
    ) -> RecommendResult:
        """并发跑「读画像」和「召回候选」，合流后照常重排 + 生成理由。"""
        query = build_query(utterance, slots)

        # scope 必须在 gather **之前**取好：它是一次独立的 Redis 读，
        # 但候选召回依赖它的结果，没法并进那两路里去。
        scope = await resolve_scope(self._scope_cache, session_id, user_id)

        # 两路并发，等两边都完成后合流。
        profile, top_n = await asyncio.gather(
            self._profile_service.load(user_id),
            self._candidates.fetch_candidates(query, slots, CANDIDATE_TOP_N, scope),
        )

        if not top_n:
            return RecommendResult(items=[], explanation_tone=TONE_EMPTY)

        reranked = self._reranker.rerank(top_n, profile, slots)
        top_k = reranked[:FINAL_TOP_K]

        # 理由字段留空，交给情感 Agent（与顺序版一致）
        return RecommendResult(items=top_k, explanation_tone=TONE_PROFESSIONAL)
