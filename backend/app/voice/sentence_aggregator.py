"""把文字切成"可合成单元"——两种切法都在这里。

同一个关切（怎么切才适合送 TTS）的两个方向：

- :class:`SentenceAggregator` —— **增量**方向。模型逐 Token 吐字，Token 攒到
  出现标点才交出去。
- :func:`split_into_sentences` —— **一次切完**方向。手上一整段文本（比如订单
  分支的固定话术）时用。

两个函数放在一起是因为它们共用同一套"什么算句末、什么算空碎片"的判断——
分开两个文件，这两条规则迟早会漂移，而漂移的表现是"流式那段听起来正常、
整段那段听起来怪"，极难查。

**为什么流式那份把逗号也算句末，而整段那份不算**：不是疏忽，是两种输入的差别。
整段切分时逗号是句内停顿，切开反而把一句话拆散；逐 Token 聚合时，等一个句号
可能要多等十几个 Token，而一个逗号就能让第一帧音频提前几百毫秒——这里
要的就是那几百毫秒。

这个类**有意做成同步的纯对象**，不碰 asyncio——聚合逻辑和"从哪拿 Token"是两件事，
混在一起就没法单独测边界了。文字流版本的入口是 :func:`aggregate`。
"""

import re
from collections.abc import AsyncIterator

#: 流式聚合认定的句末标点。
#:
#: **没有分号、顿号、冒号**。顿号（``、``）尤其不能算：
#: "跑鞋、篮球鞋、足球鞋" 会被切成三段，每段都要起一次 TTS，得不偿失。
SENTENCE_ENDS = "。！？，"

#: 整段切分认定的句末标点——**只有句号、叹号、问号，没有逗号**。
SENTENCE_SPLIT = re.compile(r"(?<=[。！？])")

#: 去掉标点 / 空白后还剩内容的才算真句子
_HAS_CONTENT = re.compile(r"[^\s。！？，、；：,.!?;:]")


def split_into_sentences(text: str) -> list[str]:
    """把一整段文本按句末标点切开，供逐句合成。

    **为什么要切片送 TTS**——CosyVoice 对短句响应快，长句一次性合成首字延迟高。
    按自然句子切能让第一句音频更早到达用户。

    只含标点的碎片（连续标点切出来的 ``"。"``）会被丢掉——送进去只会合成一段
    无意义的停顿，纯属浪费一次 TTS 调用；连续标点还会切出空串，这里一并过滤。
    """
    return [
        part for part in SENTENCE_SPLIT.split(text or "") if _HAS_CONTENT.search(part)
    ]


class SentenceAggregator:
    """有状态的聚合缓冲。

    **必须每个流一个实例**（``self._buffer`` 是流级别的状态），
    多个流共用会把缓冲区串在一起。

    用法::

        agg = SentenceAggregator()
        async for token in tokens:
            for unit in agg.push(token):
                await speak(unit)
        for unit in agg.flush():     # 收尾，别忘了
            await speak(unit)
    """

    def __init__(self) -> None:
        self._buffer = ""

    @property
    def buffered(self) -> str:
        """当前还没交出去的内容（观测 / 测试用）。"""
        return self._buffer

    def push(self, token: str) -> list[str]:
        """喂一个 Token，返回这次可以交出去的单元（通常 0 个，最多 1 个）。

        做法：找出缓冲区里**最后一个**句末标点，把 ``[0, lastEnd]``
        整段吐出去。所以一个 Token 如果一口气带进来 "A。B。"，出去的是**一整块**
        "A。B。"，不会被拆成两句——**这是原版行为，不是疏漏**：逐 Token 流式下
        一个 Token 极少同时含两个标点，为几乎不会发生的输入加一层拆分不划算。

        内层 ``while`` 保留是因为**理论上**切完一次还可能剩着新的完整单元
        （比如缓冲区里本来就压着东西），留着循环才不用去证明它不会发生。

        只含标点的碎片（连续标点切出来的 ``"。"``）会被丢掉：送去合成只会得到一段
        无意义的停顿，白花一次 TTS 调用。
        """
        if not token:
            return []

        self._buffer += token
        units: list[str] = []

        while True:
            index = _last_end(self._buffer)
            if index < 0:
                break
            unit, self._buffer = self._buffer[: index + 1], self._buffer[index + 1 :]
            if _HAS_CONTENT.search(unit):
                units.append(unit)

        return units

    def flush(self) -> list[str]:
        """流结束时把残留吐出来。

        **模型最后一句很可能没有标点**（"…再聊聊"），不 flush 就永远卡在缓冲里，
        用户听不到结尾，所以流结束时必须调用 :meth:`flush`。
        """
        tail, self._buffer = self._buffer, ""
        if _HAS_CONTENT.search(tail):
            return [tail.strip()]
        return []


def _last_end(text: str) -> int:
    """最后一个句末标点的下标；一个都没有则 ``-1``。

    对每个句末标点各做一次 ``rfind``：``rfind`` 是 C 层实现，四次扫描对一个
    几十字的缓冲区来说完全是噪声级开销，比塞个正则值更直接。
    """
    return max(text.rfind(char) for char in SENTENCE_ENDS)


async def aggregate(tokens: AsyncIterator[str]) -> AsyncIterator[str]:
    """把 Token 流转成"可合成单元"流。"""
    aggregator = SentenceAggregator()
    async for token in tokens:
        for unit in aggregator.push(token):
            yield unit
    for unit in aggregator.flush():
        yield unit
