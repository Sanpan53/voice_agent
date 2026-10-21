"""语句指纹：把用户原话归一化后算一个稳定哈希。

存在的意义只有一条：

    用户说"想买双跑鞋"和"想买双跑鞋。"是**同一句话**，
    不该因为多打一个句号就多烧一次 LLM 的钱。

归一化规则——去掉中英文标点和所有空白、转小写。

**标点表里刻意没有"、"**。它虽然也是停顿标点，但加进来会改变语义粒度
（"跑鞋、篮球鞋" 与 "跑鞋篮球鞋" 归一化到同一个 key），收益远小于风险。
:class:`~app.voice.sentence_aggregator.SentenceAggregator` 同样不切"、"，
两处口径一致。

.. note::

   本模块与 :mod:`app.services.common_confirmer` 共用同一个 :func:`normalize`。
   若各写一份正则，一旦有人改了一处，confirmer 认得"好的。"、指纹却不认，
   **不会有任何报错**。这里把"什么算噪音"收敛到这一个函数里。
"""

import hashlib
import re

#: 归一化时剔除的字符：中英文句读 + 所有空白。
#: 字符类 ``[，。！？,.!?\\s]`` 覆盖中英文句读与所有空白，
#: 加 ``+`` 只是让连续标点一次替换完。
_NOISE = re.compile(r"[，。！？,.!?\s]+")

#: 指纹长度。md5 是 32 位十六进制，取前 16 位（64 bit）。
#: 取值域是"单个会话内的几百句话"，64 bit 的碰撞概率可以忽略；
#: 但**别把它当全局唯一 ID 用**——真要全局唯一就取满 32 位。
FINGERPRINT_LENGTH = 16


def normalize(utterance: str | None) -> str:
    """归一化：去标点、去空白、转小写。

    Args:
        utterance: 用户原话，允许 ``None``（归一化成空串）。

    Returns:
        归一化后的字符串。**只有标点 / 空白的输入会得到空串**，
        调用方要自己决定空串算不算一次有效命中。
    """
    if not utterance:
        return ""
    return _NOISE.sub("", utterance).lower()


def compute(utterance: str | None) -> str:
    """算指纹：``md5(normalize(utterance))`` 的前 :data:`FINGERPRINT_LENGTH` 位。

    .. note::

       **为什么不用 Python 内置的 ``hash()``。** Python 的 ``hash()`` 对 str 带进程级随机化
       （``PYTHONHASHSEED``），**同一个字符串在两个进程里哈希值不同**。
       拿它当 Redis key，等于"每次重启服务，缓存全部失效"，而且不报错——
       表现只是"缓存命中率莫名其妙很低"。所以这里用 md5。
    """
    digest = hashlib.md5(normalize(utterance).encode("utf-8")).hexdigest()  # noqa: S324
    return digest[:FINGERPRINT_LENGTH]
