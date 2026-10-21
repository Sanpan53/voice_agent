"""会话情绪识别。

情绪识别**轻量级单独走**——不能每次让主模型一边识别一边生成，主模型贵，情绪识别
用规则 + 轻量模型单独出。这里的实现是**纯规则 + 记忆回溯**，一次模型调用都不发。
"""

import re
from enum import StrEnum

from app.memory.short_term_memory import ShortTermMemory


class SessionMood(StrEnum):
    """会话情绪。"""

    NEUTRAL = "neutral"
    HESITANT = "hesitant"
    IMPATIENT = "impatient"
    POSITIVE = "positive"
    NEGATIVE = "negative"


#: 规则命中顺序即优先级：不耐烦 > 负面 > 正面。
#: ``行`` 在 ``不行`` 里也会命中，靠 NEGATIVE 先判来消歧。
IMPATIENT_PATTERN = re.compile(r"(算了|不要|别说了|快点|到底|跳过)")
POSITIVE_PATTERN = re.compile(r"(好的|可以|不错|挺好|行|ok)")
NEGATIVE_PATTERN = re.compile(r"(贵|太贵|不喜欢|丑|不行|不对)")

#: 反问句尾——用于判断"最近是不是在反复被追问"
QUESTION_MARKS = ("？", "?")

#: 回溯轮数
RECENT_TURNS = 4

#: 连续反问到这个数就判"犹豫"
HESITANT_ASK_THRESHOLD = 2


class SessionMoodDetector:
    """从用户原话 + 最近几轮记忆里判断当前会话情绪。"""

    def __init__(self, memory: ShortTermMemory) -> None:
        self._memory = memory

    async def detect(self, session_id: str, current_utterance: str) -> SessionMood:
        """返回当前会话情绪，默认 ``neutral``。

        Note:
            这里必须是 ``async``——``ShortTermMemory`` 走 Redis，
            读写天然是异步的。
        """
        # 先走规则，覆盖 80% 场景
        rule = _by_rule(current_utterance)
        if rule is not None:
            return rule

        # 再看最近几轮是不是在反复追问（犹豫）。
        # 对齐 AgentScope 的 ``Msg.role`` 约定用小写 ``"assistant"``——写进记忆的
        # 也是小写，见 ``Msg(msg.name, text, "assistant")``。
        recent = await self._memory.recent(session_id, RECENT_TURNS)
        ask_turns = sum(
            1
            for turn in recent
            if turn.role == "assistant" and turn.text and turn.text.endswith(QUESTION_MARKS)
        )
        if ask_turns >= HESITANT_ASK_THRESHOLD:
            return SessionMood.HESITANT

        return SessionMood.NEUTRAL


def _by_rule(text: str | None) -> SessionMood | None:
    """规则层——命中即返回，都不命中返回 ``None`` 交给下一层。

    用 ``re.search`` 做子串匹配，不是整串匹配。
    """
    if not text:
        return None
    if IMPATIENT_PATTERN.search(text):
        return SessionMood.IMPATIENT
    if NEGATIVE_PATTERN.search(text):
        return SessionMood.NEGATIVE
    if POSITIVE_PATTERN.search(text):
        return SessionMood.POSITIVE
    return None
