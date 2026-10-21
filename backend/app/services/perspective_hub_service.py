"""多视角点评团：把 Top3 商品扔进 MsgHub 开一场三人短讨论。

**这是旁路，不是主链路**。主链路（意图 → 澄清 → 推荐 → 情感）是强顺序状态机，
要求每一步结果可预测；点评团是锦上添花，失败了就返回空串，绝不影响推荐本身。

三个角色在同一 Hub 里**依次**发言，但每个人的发言会自动广播进其他人的 memory——
所以后发言的"入门买家"能接上"专业跑者"刚说的话，产生真实讨论感。这正是 MsgHub
相对 Service 顺序调用的核心差异。
"""

import logging
from collections.abc import Sequence

from agentscope.message import Msg
from agentscope.pipeline import MsgHub

from app.agent.agent_factory import AgentFactory
from app.schemas.recommend import RecommendedItem

logger = logging.getLogger(__name__)

#: 角色名 → 输出纪要里的中文标签
_ROLE_LABELS: tuple[tuple[str, str], ...] = (
    ("price_advisor", "价格顾问"),
    ("pro_runner", "专业跑者"),
    ("beginner_buyer", "入门买家"),
)


def render_items(items: Sequence[RecommendedItem]) -> str:
    """把商品列表渲染成主持人公告里的清单。

    输出格式：``- 名称 / ¥价格 / 理由``。
    """
    return "\n".join(
        f"- {item.name} / ¥{item.price} / {item.reason}" for item in items
    )


def build_announcement(utterance: str, items: Sequence[RecommendedItem]) -> str:
    """主持人开场白：用户原话 + 待点评商品 + 发言要求。"""
    return (
        f"用户原话：{utterance}\n\n"
        f"待点评的 Top3 商品：\n{render_items(items)}\n\n"
        "请各位依次发言，每人 30 字以内。"
    )


def _text(msg: Msg | None) -> str:
    """取 Msg 的文本内容，``None`` 视为空串。"""
    if msg is None:
        return ""
    return msg.get_text_content() or ""


class PerspectiveHubService:
    """多视角点评团 Service。

    Args:
        factory: Agent 工厂。点评团从这里取——与四个主链路 Agent 的管理路径
            统一，唯一区别是 :meth:`AgentFactory.new_perspective_team`
            **每次都新建、不进缓存**。
    """

    def __init__(self, factory: AgentFactory) -> None:
        self._factory = factory

    async def discuss(
        self,
        session_id: str,
        utterance: str,
        items: Sequence[RecommendedItem] | None,
    ) -> str:
        """开一场三人讨论，返回纪要文本；任何失败都降级为空串。

        Args:
            session_id: 会话 ID（只用于 Hub 命名和日志）。
            utterance: 用户原话，进主持人公告。
            items: 待点评的商品。为空直接返回 ``""``，连 Hub 都不开。

        Returns:
            形如 ``"价格顾问:...\\n专业跑者:...\\n入门买家:..."`` 的纪要；
            失败或商品为空时为 ``""``。
        """
        if not items:
            return ""

        try:
            # 把 ``new_perspective_team()`` 放进 try —— 构造 Agent 也可能
            # 失败（Prompt 文件缺失、API Key 没配），而点评团是旁路，**任何环节都不能
            # 把异常漏给主链路**。这与"每个 Worker 都要有降级"是同一条原则。
            team = self._factory.new_perspective_team()
            announcement = Msg("host", build_announcement(utterance, items), "user")

            # 注意：announcement 由 MsgHub.__aenter__ 自动广播，这里**不需要**
            # 再手动 broadcast 一次。
            #
            # async with 出块时会自动退订。
            # 三个 Agent 实例随之失去引用，InMemoryMemory 里的讨论上下文一起释放，
            # 所以下一轮点评是**完全干净的一次新讨论**。
            async with MsgHub(
                participants=[team.price_advisor, team.pro_runner, team.beginner_buyer],
                announcement=announcement,
                enable_auto_broadcast=True,
                name=f"perspective_{session_id}",
            ):
                replies = [
                    await team.price_advisor(),
                    await team.pro_runner(),
                    await team.beginner_buyer(),
                ]
        except Exception:
            logger.warning(
                "视角点评团讨论失败，降级为空文案 sessionId=%s", session_id, exc_info=True
            )
            return ""

        return "\n".join(
            f"{label}:{_text(reply)}"
            for (_, label), reply in zip(_ROLE_LABELS, replies, strict=True)
        )
