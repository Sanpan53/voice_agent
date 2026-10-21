"""指代消解：把"就要第二款"翻译成 ``product_id``。

**这是语音下单和点按钮下单最大的区别。** 点按钮时前端直接把 ``productId`` 传上来，
不存在歧义；语音里用户说的是"第二款"、"最后那个"、"开头那个"，服务端要拿
**上一轮实际推给他的列表**（``state.last_recommendations``）去对齐。

对齐失败时**不能猜**——猜错的后果是给用户下了一单他没要的东西。返回 ``None``，
让编排层去追问"你想要的是刚才推荐的哪一款"。
"""

import logging
import re

from app.models.session import SessionState

logger = logging.getLogger(__name__)

#: 序数词 → 零基下标。只认一到五，推荐结果本来就只有 3-5 款。
ORDINAL_INDEX = {
    "一": 0,
    "1": 0,
    "二": 1,
    "2": 1,
    "三": 2,
    "3": 2,
    "四": 3,
    "4": 3,
    "五": 4,
    "5": 4,
}

#: 序数词后面跟着的量词。用户说"第二个"、"第三件"、"第五号"都算。
#: 刻意**不含"双"**——"来一双"在语音里几乎总是数量而不是序号。
_MEASURE_WORDS = "款件个种号"

#: 序数词模式。
#:
#: 宽松的 ``第?([一二三四五1-5])款?`` 会让"第"和"款"都变成可选，
#: 于是正则在任何位置都能匹配到数字汉字。用户说
#: "便宜一点"（含"一"）就会被解析成"要第一款"，**直接生成一张预览单**。
#:
#: 因此这里收紧成两条路径，**必须**满足其一：
#:
#: + 有"第"前缀：``第二`` / ``第3``；
#: + 数字后面紧跟量词：``二款`` / ``3个``。
#:
#: 这样"便宜一点"（"一"后面是"点"）就不会误判。
_ORDINAL_PATTERN = re.compile(
    rf"第\s*([一二三四五1-5])|([一二三四五1-5])\s*[{_MEASURE_WORDS}]"
)

#: "开头 / 最后 / 中间"这类位置指代，与序数词互相独立
_FIRST_HINTS = ("开头", "头一个", "最先", "最前")
_LAST_HINTS = ("最后", "末尾", "倒数")
_MIDDLE_HINTS = ("中间",)

#: 位置指代换算用的哨兵：它们在拿到列表长度之前算不出下标
_FROM_END = -1
_FROM_MIDDLE = -2


class OrderReferenceResolver:
    """把自然语言里的指代解析成上一轮推荐列表中的某一项。

    纯函数式实现——不碰数据库、不碰 Redis，输入是会话状态 + 用户原话，
    输出是下标或商品 ID。这让它可以被穷举测试。
    """

    def resolve(self, state: SessionState, utterance: str) -> int | None:
        """解析用户想下单的商品 ID。

        Args:
            state: 会话状态（要用它的 ``last_recommendations``）。
            utterance: 用户这一轮的原话。

        Returns:
            商品 ID；解析不出来返回 ``None``（调用方应当追问，**不要猜**）。
        """
        last = list(state.last_recommendations or [])
        index = self.resolve_index(state, utterance)
        if index is None:
            return None
        return last[index]

    def resolve_index(self, state: SessionState, utterance: str) -> int | None:
        """解析用户说的是**第几款**（零基下标）。

        单独留一个返回下标的口子，一是因为位置指代（"最后" → ``len-1``）本来
        就要先算下标再取商品，二是因为测试里直接断言"用户说的第几款"比断言
        一个商品 ID 清楚得多。

        Returns:
            下标；解析不出来或越界返回 ``None``。
        """
        last = list(state.last_recommendations or [])
        if not last:
            # 还没推荐过任何东西，无从指代
            return None

        text = utterance or ""

        index = self._by_ordinal(text)
        if index is None:
            index = self._by_position(text)

        if index == _FROM_END:
            return len(last) - 1
        if index == _FROM_MIDDLE:
            return len(last) // 2
        if index is None or not 0 <= index < len(last):
            # 越界（说了"第五款"但只推了 3 款）算解析失败，**不夹到最后一个**——
            # 夹过去等于替用户改了主意。
            return None

        logger.info(
            "[Order] 指代解析成功 utterance=%r -> index=%d productId=%s",
            text,
            index,
            last[index],
        )
        return index

    @staticmethod
    def _by_ordinal(text: str) -> int | None:
        """"第二款" / "第3个" / "二款" → 零基下标。"""
        match = _ORDINAL_PATTERN.search(text)
        if match is None:
            return None
        # 两个分组二选一，取命中的那个
        return ORDINAL_INDEX.get(match.group(1) or match.group(2))

    @staticmethod
    def _by_position(text: str) -> int | None:
        """"开头那个" / "最后那个" / "中间" → 哨兵值。

        序数词分支已经覆盖了"第一"，这里只保留真正独立的三种位置指代。

        判断顺序是**最后 → 中间 → 开头**："最后中间那个"这种话没有实际意义，
        但真实语料里"中间"和"最后"可能同现（"最后那两个中间的"），
        按"最后"优先至少不会解析到一个用户没指的位置。
        """
        if any(hint in text for hint in _LAST_HINTS):
            return _FROM_END
        if any(hint in text for hint in _MIDDLE_HINTS):
            return _FROM_MIDDLE
        if any(hint in text for hint in _FIRST_HINTS):
            return 0
        return None


__all__ = ["ORDINAL_INDEX", "OrderReferenceResolver"]
