"""分轮摘要：把一轮完整对话压成一句话再进短期记忆。

**为什么需要这一层**——如果把用户原话和 Agent 回复都原样塞进 Redis List，
下一轮意图 Agent 拿到的"最近 3 轮"里一半是没用的复述，还白占 Token。
摘要层保证内存里永远只有简短、信息密度高的一行。

摘要格式：::

    [PRODUCT_RECOMMENDATION] 用户：想买双跑鞋 / 助手：给你挑了三款轻量跑鞋…

意图名放在最前面，是为了让意图 Agent 一眼看到"上一轮聊到哪个环节了"。
"""

import re

#: 连续空白（含换行）——摘要必须是**单行**，否则 ``redis-cli LRANGE`` 里
#: 一条记忆看起来像好几条，排查问题时会数错轮次。
_WHITESPACE = re.compile(r"\s+")

#: 用户原话截断长度——够表达诉求，又不会把摘要撑长
UTTERANCE_MAX = 30

#: 助手回复截断长度——比用户话宽松些，因为助手话里常带商品名
REPLY_MAX = 40

#: 截断后补的省略号（单个字符，不是三个点——语音场景下长度敏感）
ELLIPSIS = "…"


class TurnSummarizer:
    """把一轮用户原话 + Agent 回复 + 意图压成一句话。"""

    def summarize(self, user_utterance: str, intent: str, agent_reply: str) -> str:
        """生成一条轮次摘要。

        Args:
            user_utterance: 用户这一轮的原话。
            intent: 路由到的意图名（如 ``PRODUCT_RECOMMENDATION``）。
            agent_reply: 助手这一轮的口播文案。

        Returns:
            形如 ``[PRODUCT_COMPARE] 用户：再便宜点 / 助手：这款基本划算…`` 的一行摘要。
        """
        user_part = self.truncate(one_line(user_utterance), UTTERANCE_MAX)
        reply_part = self.truncate(one_line(agent_reply), REPLY_MAX)
        return f"[{intent}] 用户：{user_part} / 助手：{reply_part}"

    @staticmethod
    def truncate(text: str | None, max_length: int) -> str:
        """超长截断并补省略号；``None`` 当空串处理，摘要里不会出现 ``None`` 字面量。"""
        if text is None:
            return ""
        if len(text) > max_length:
            return text[:max_length] + ELLIPSIS
        return text


def one_line(text: str | None) -> str | None:
    """把连续空白（含换行、制表）压成单个空格。

    语音链路不会产生换行（ASR 输出是连续文本），但 ``POST /api/v1/chat``
    这个文本调试入口会——用户从别处粘一段多行文本进来，摘要就散成好几行了。
    ``None`` 原样透传，交给 :meth:`TurnSummarizer.truncate` 转空串。
    """
    if text is None:
        return None
    return _WHITESPACE.sub(" ", text).strip()
