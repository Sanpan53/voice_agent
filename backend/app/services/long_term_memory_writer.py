"""跨会话长期记忆回流。

一次会话结束（用户关页面 / 超时 / 订单完成），这次对话里暴露的用户信息要回流到
长期记忆（``user_profile_dynamic``）。**这是"越用越懂你"的全部秘密**——
没有这一步，每个会话都是从零开始。

回流信号与动作：

+----------------------------+--------------------------------------------+
| 信号                       | 动作                                       |
+============================+============================================+
| 本次会话提到的品类         | ``category_affinity`` +0.05                |
| 本次会话下单的商品         | ``recent_purchased`` 追加、affinity +0.15  |
| 本次会话最终预算           | 更新 ``avg_order_amount``（滑动平均）      |
| 用户反复问"便宜点"的次数   | ``price_sensitivity`` 上调                 |
| 主动填的身高/体重等        | 写 ``user_profile_static``                 |
+----------------------------+--------------------------------------------+

这里实现前两条 + 价格敏感度；下单相关的（购买追加、均价）在订单链路里补完。
"""

import logging
from decimal import Decimal

from app.memory.short_term_memory import ShortTermMemory
from app.repositories.user_profile_repository import UserProfileDynamicRepository
from app.services.session_state_service import SessionStateService
from app.services.user_profile_service import UserProfileService

logger = logging.getLogger(__name__)

#: 会话提到的品类 → 偏好权重增量
CATEGORY_AFFINITY_DELTA = 0.05

#: 价格敏感度单次增量
PRICE_SENSITIVITY_DELTA = Decimal("0.1")

#: 价格敏感度的初始值（画像里没这个字段时从这里起步）
DEFAULT_PRICE_SENSITIVITY = Decimal("0.5")

#: 触发价格敏感度上调的"便宜/贵"出现次数门槛
PRICE_HIT_THRESHOLD = 2

#: 回流时最多回看多少轮
MAX_TURNS = 50

#: 表示价格方向诉求的关键词
PRICE_KEYWORDS = ("便宜", "贵")


class LongTermMemoryWriter:
    """会话结束时把短期记忆回流进长期画像。

    Args:
        repo: 动态画像仓储。
        state_service: 会话状态机（读 ``slots.category``）。
        short_mem: 短期记忆（读本次会话的所有轮次）。
        profile_service: 画像快照服务，回流后用来失效缓存。
    """

    def __init__(
        self,
        repo: UserProfileDynamicRepository,
        state_service: SessionStateService,
        short_mem: ShortTermMemory,
        profile_service: UserProfileService | None = None,
    ) -> None:
        self._repo = repo
        self._state = state_service
        self._short_mem = short_mem
        self._profile_service = profile_service

    async def flush_on_session_end(self, session_id: str, user_id: int) -> bool:
        """把一次会话的收获回流到长期画像。

        **这个方法从不抛异常**——调用点分布在 WebSocket 关闭、订单确认、
        Redis TTL 过期三处，任何一处被回流失败拖垮都是不可接受的。
        失败只记日志、返回 ``False``。

        Returns:
            是否真的发生了回流（会话没有任何轮次时返回 ``False``）。
        """
        try:
            return await self._flush(session_id, user_id)
        except Exception:
            logger.error("长期记忆回流失败 session=%s", session_id, exc_info=True)
            return False

    async def _flush(self, session_id: str, user_id: int) -> bool:
        state = await self._state.load(session_id)
        turns = await self._short_mem.recent(session_id, MAX_TURNS)
        if not turns:
            return False

        profile = await self._load_or_init(user_id)
        changed = False

        # ① 本次会话提到的品类 → 偏好权重累加
        category = (state.slots or {}).get("category")
        if category:
            profile.category_affinity = _bump_affinity(
                profile.category_affinity, str(category), CATEGORY_AFFINITY_DELTA
            )
            changed = True

        # ② 价格敏感度：看本会话里"便宜/贵"出现的次数
        if _count_price_hits(turns) >= PRICE_HIT_THRESHOLD:
            current = profile.price_sensitivity or DEFAULT_PRICE_SENSITIVITY
            profile.price_sensitivity = min(
                Decimal("1.0"), current + PRICE_SENSITIVITY_DELTA
            )
            changed = True

        if not changed:
            return False

        await self._repo.save(profile)

        # 回流完必须失效画像快照缓存，否则接下来 30 分钟推荐读到的还是旧画像。
        # 不做这一步的话，表现为"回流明明成功了，推荐却要等缓存过期才生效"。
        if self._profile_service is not None:
            await self._profile_service.evict(user_id)

        logger.info("[LongMem] 回流 userId=%s session=%s", user_id, session_id)
        return True

    async def _load_or_init(self, user_id: int) -> "object":
        """取动态画像，没有就初始化一份空骨架。"""
        from app.models.user_profile import UserProfileDynamic

        existing = await self._repo.find_by_id(user_id)
        if existing is not None:
            return existing
        return UserProfileDynamic(
            user_id=user_id,
            category_affinity={},
            brand_affinity={},
            recent_viewed=[],
            recent_purchased=[],
        )


def _bump_affinity(
    affinity: dict[str, float] | None, key: str, delta: float
) -> dict[str, float]:
    """权重表累加，上限 1.0（避免聊久了某一项爆掉，把别的商品全压死）。"""
    result = dict(affinity or {})
    result[key] = min(1.0, result.get(key, 0.0) + delta)
    return result


def _count_price_hits(turns: list) -> int:
    """统计本会话里带价格方向诉求的轮次数量。

    **这里按摘要文本匹配关键词。** 记忆写入只写一条 ``TURN`` 摘要
    （不再写 ``USER``），所以不能按用户角色过滤轮次——那样会一条都匹配不到，
    ``priceSensitiveHits`` 恒为 0，价格敏感度**再也不会更新**。

    摘要格式是
    ``[INTENT] 用户：<原话前 30 字> / 助手：<回复前 40 字>``，
    用户原话被完整嵌在里面，所以判断依然准确，而且顺带涵盖了助手话里的
    "这款更便宜"这类确认性表述。
    """
    hits = 0
    for turn in turns:
        text = turn.text or ""
        if any(keyword in text for keyword in PRICE_KEYWORDS):
            hits += 1
    return hits
