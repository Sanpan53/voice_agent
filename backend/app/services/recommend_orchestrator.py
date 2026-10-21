"""推荐链路编排。

把三步串起来：

    1. 检索   →  向量检索 + 属性过滤，Top 20
    2. 加权   →  结合用户画像打分
    3. Rerank →  Top 20 精排到 Top 3

**第四步"生成理由"已经被摘掉了。** 原先这里会调
:class:`~app.services.recommend_reason_service.RecommendReasonService`
让主模型为每款写一句理由（``qwen-max``，约 800ms），再由情感 Agent 把这些理由
编成一段口语（又一次 ``qwen-max``，约 600ms）。两次调用做的是同一件事的上下半段，
合成一次就够了：**情感 Agent 直接拿裸商品数据，自己看 ``attributes`` 编理由**。

所以本方法返回的 ``RecommendedItem.reason`` 是 ``None``——**这不是 bug，是故意的**。
理由由下游的情感 Agent 现场生成，谁要理由谁去找它。
"""

import logging
from typing import Any

from app.schemas.recommend import TONE_PROFESSIONAL, RecommendedItem, RecommendResult
from app.schemas.session import SessionScope
from app.services.profile_reranker import ProfileReranker
from app.services.recommend_candidates_service import RecommendCandidatesService
from app.services.scope_filter import resolve_scope
from app.services.session_scope_cache import SessionScopeCache
from app.services.user_profile_service import UserProfileService

logger = logging.getLogger(__name__)

#: 召回条数
CANDIDATE_TOP_N = 20

#: 精排后返回给用户的条数（语音场景说三款就到头了，再多用户记不住）
FINAL_TOP_K = 3

#: 空结果兜底：放宽品类时少召一点，避免把整个库都翻出来
RELAXED_TOP_N = 10

#: 空结果兜底：预算放宽倍数
BUDGET_RELAX_FACTOR = 1.3


class RecommendOrchestrator:
    """推荐 Agent 的入口。"""

    def __init__(
        self,
        candidates: RecommendCandidatesService,
        reranker: ProfileReranker,
        profile_service: UserProfileService,
        scope_cache: SessionScopeCache | None = None,
    ) -> None:
        self._candidates = candidates
        self._reranker = reranker
        self._profile_service = profile_service
        self._scope_cache = scope_cache

    async def recommend(
        self,
        session_id: str,
        user_id: int,
        utterance: str,
        slots: dict[str, Any] | None,
    ) -> RecommendResult:
        """跑完整条推荐链路。"""
        profile = await self._profile_service.load(user_id)
        query = build_query(utterance, slots)
        scope = await self.resolve_scope(session_id, user_id)

        top_n = await self._candidates.fetch_candidates(query, slots, CANDIDATE_TOP_N, scope)
        if not top_n:
            top_n = await self._relax_and_retry(query, slots, scope)
            if not top_n:
                # 三步放宽都空：交给情感 Agent 说"没找到合适的"，而不是硬推一款
                return RecommendResult.empty()

        reranked = self._reranker.rerank(top_n, profile, slots)
        top_k = reranked[:FINAL_TOP_K]

        # 到这里就返回——理由字段留空，交给情感 Agent（见模块 docstring）。
        return RecommendResult(items=top_k, explanation_tone=TONE_PROFESSIONAL)

    async def resolve_scope(self, session_id: str, user_id: int) -> SessionScope:
        """取本次会话的商家范围（降级方向见 :func:`~app.services.scope_filter.resolve_scope`）。"""
        return await resolve_scope(self._scope_cache, session_id, user_id)

    async def _relax_and_retry(
        self, query: str, slots: dict[str, Any] | None, scope: SessionScope | None = None
    ) -> list[RecommendedItem]:
        """空结果兜底：先放宽预算 +30%，再丢掉品类。

        过滤条件太严、品类罕见商品少的时候，向量检索返回 0 条是真的会发生。

        **放宽预算和品类，但绝不放宽商家范围**：前者是"找不到合适的"，
        放宽是体验问题；后者是权限，放宽就是事故。所以 ``scope`` 原样传下去。
        """
        relaxed = dict(slots or {})
        budget = relaxed.get("budget")
        if isinstance(budget, (int, float)) and not isinstance(budget, bool):
            relaxed["budget"] = int(budget * BUDGET_RELAX_FACTOR)

        candidates = await self._candidates.fetch_candidates(
            query, relaxed, CANDIDATE_TOP_N, scope
        )
        if candidates:
            logger.debug("放宽预算后命中 %d 条候选", len(candidates))
            return candidates

        # 丢掉二级分类再试一次
        relaxed.pop("category", None)
        candidates = await self._candidates.fetch_candidates(
            query, relaxed, RELAXED_TOP_N, scope
        )
        if candidates:
            logger.debug("放宽品类后命中 %d 条候选", len(candidates))
        return candidates


def build_query(utterance: str, slots: dict[str, Any] | None) -> str:
    """检索文本 = 用户原话 + 场景词。

    场景词单独补一遍是有意的：用户说"想买双跑鞋，膝盖不太好"，"膝盖"这个词在商品
    描述里未必出现，但"水泥路"这种场景词能直接拉高对应属性商品的相似度。
    """
    scenario = (slots or {}).get("scenario")
    if isinstance(scenario, str) and scenario:
        return f"{utterance} {scenario}"
    return utterance
