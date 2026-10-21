"""高频简短确认词的进程内短路。

用户在一轮会话里说"嗯""好""继续"的重复率很高，
这类词的意图基本固定，**连 Redis 都不用查**：进程内一个常量集合，命中直接返回，
单次开销 < 1ms。日活 5000、每会话 3-4 轮里只要有 1 轮是确认词，
一天就省下几百次 Intent LLM 调用。

.. important::

   **词表按语义拆成两组，各返回各的常量。** 确认类（"嗯""好""可以"）落到
   ``ORDER_CONFIRM``，继续类（"继续""下一个""换一个"）落到
   ``PRODUCT_COMPARE``。两组语义不同，**不能塞进同一个集合**统一返回订单确认。
   如果混在一起，用户说"继续"会被送进订单分支：``OrchestratorService`` 的
   ``_handle_order_confirm`` 发现没有待确认的预览单，就去解析"继续"指的是哪一款，
   解析不出来只好回一句"你想下单的是哪一款？可以说第几款，或者说说商品名。"——
   用户只是想让推荐继续，系统却问他要下哪一单。

   这类错误之所以隐蔽，是因为**订单阶段的短路排在意图识别之前**
   （``state.phase == ORDER_CONFIRM`` 时压根不走 ``classify``），所以只有
   "不在下单阶段却说继续词"这一条路径能触发，而那条路径恰好就是最常见的用法。

.. note::

   确认词**不写回** ``IntentCache``——它本来就是常量返回，
   写缓存只是白占 Redis 一个 key。
"""

from app.schemas.intent import IntentResult, IntentType
from app.services.utterance_fingerprint import normalize

#: 确认类：回答"是 / 好 / 可以"，落到订单确认分支。
CONFIRM_WORDS: frozenset[str] = frozenset({"嗯", "好", "好的", "对", "是", "行", "可以"})

#: 继续类：要求"再来一批 / 换一批"，落到对比分支（重跑推荐）。
#:
#: 为什么是 ``PRODUCT_COMPARE`` 而不是 ``PRODUCT_RECOMMENDATION``：
#: 对比分支会带上上一轮的推荐结果（排除已推过的款、按 priceDirection 调整价格区间）
#: 再推一批，这正是用户说"继续 / 换一个"想要的；而推荐分支会把上一轮的上下文丢掉，
#: 变成一次全新的检索。
CONTINUE_WORDS: frozenset[str] = frozenset({"继续", "下一个", "再来", "换一个"})

#: 判定为确认词的最大长度（归一化之后）。
#: 长度上限为 3，含义是"短到不可能承载完整需求"——
#: "便宜点的跑鞋"（6 字）不该被短路，"下一个"（3 字）该。
#:
#: .. note::
#:
#:    **这道闸门目前是冗余的，说清楚免得被人当"测不到的代码"删掉。**
#:    词表里最长的词是 3 字的"下一个"，而判定用的是**整串集合成员**，
#:    所以任何超过 3 字的输入本来就进不了任何一个集合——把闸门摘掉，
#:    行为一个字节都不会变（变异测试当场证明了这一点：摘掉闸门，
#:    1192 条测试全绿）。
#:
#:    留着它是因为**词表会变**：哪天有人往 ``CONTINUE_WORDS`` 里加了
#:    "再给我来一个"，闸门就是唯一还会提醒他那句话"长到不该被短路"的东西。
#:    为了让它不至于退化成一个纯粹的摆设，
#:    ``test_every_confirmer_word_fits_within_the_length_gate``
#:    把"词表里的词都不超过这个水位"钉成了一条会变红的断言——
#:    改词表的人会立刻看到 `MAX_LENGTH` 的存在，而不是在几个月后
#:    困惑为什么用户说"再给我来一个便宜点的跑鞋"会被当成确认词。
MAX_LENGTH = 3

#: 命中确认类时返回的常量结果。
_CONFIRM_INTENT = IntentResult(
    intent=IntentType.ORDER_CONFIRM,
    slots={"confirmer": True},
    confidence=0.95,
)

#: 命中继续类时返回的常量结果。
_CONTINUE_INTENT = IntentResult(
    intent=IntentType.PRODUCT_COMPARE,
    slots={"confirmer": True},
    confidence=0.95,
)


def match(utterance: str | None) -> IntentResult | None:
    """命中确认词则返回意图，未命中返回 ``None``。

    Args:
        utterance: 用户原话。

    Returns:
        ``IntentType.ORDER_CONFIRM``（确认类）或 ``IntentType.PRODUCT_COMPARE``
        （继续类）的结果；都不是则 ``None``，调用方继续走缓存 / LLM。

    .. warning::

       **返回的是一份深拷贝，不是共享常量。** ``IntentResult`` 是 pydantic
       模型、``slots`` 是**普通 dict**——
       返回共享实例意味着任何一个调用方顺手 ``result.slots["x"] = 1``
       就会把常量污染掉，而且此后**每一次都返回被污染的那份**，不报错。
       一次深拷贝是几个微秒，相对于它替代掉的那次 LLM 调用可以忽略。
    """
    normalized = normalize(utterance)
    if not normalized or len(normalized) > MAX_LENGTH:
        return None
    if normalized in CONFIRM_WORDS:
        return _CONFIRM_INTENT.model_copy(deep=True)
    if normalized in CONTINUE_WORDS:
        return _CONTINUE_INTENT.model_copy(deep=True)
    return None


def is_common_confirmer(utterance: str | None) -> bool:
    """是否是确认词（不关心是哪一类）。"""
    return match(utterance) is not None
