"""最小版合规兜底。

两道处理，覆盖电商场景里 80% 的低悬果实：

1. **绝对化用语**（``最好`` / ``第一`` / ``保证`` / ``绝对``）→ 替换成柔化说法；
2. **敏感词**（词表文件）→ 替换成**等长**的 ``*``。

上线前再按公司合规要求扩词表 / 接外部审核 API。

**这是 TTS 前的最后一道闸门，不能降级**——原则三：
不管前面几个 Agent 怎么降级，TTS 之前必须过一遍合规；合规只能过或不过，
不存在"合规服务挂了就先放行"。
"""

import logging

from app.config import Settings, get_settings
from app.schemas.emotion import EmotionResult

logger = logging.getLogger(__name__)

#: 绝对化用语 → 柔化说法
SOFTEN_MAP: dict[str, str] = {
    "最好": "比较合适",
    "第一": "常用",
    "保证": "通常",
    "绝对": "基本",
}


def soften(word: str) -> str:
    """把绝对化用语换成柔化说法；不在表里的原样返回。

    **匹配不到就原样返回**，这也是为什么配置里新增的绝对化词
    不会让文案变成空字符串。
    """
    return SOFTEN_MAP.get(word, word)


class ComplianceChecker:
    """敏感词 / 绝对化用语过滤。

    Args:
        sensitive_words: 敏感词集合。为空时只做绝对化用语处理。
        absolute_patterns: 绝对化用语列表。
    """

    def __init__(
        self,
        sensitive_words: set[str] | None = None,
        absolute_patterns: list[str] | None = None,
    ) -> None:
        self._sensitive_words = sensitive_words or set()
        self._absolute_patterns = absolute_patterns or []

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "ComplianceChecker":
        """按配置构造并加载词表。

        词表在构造时一次性读入，**进程启动时加载一次**，
        改词表要重启（试点阶段够用，要热更新得接配置中心）。
        """
        settings = settings or get_settings()
        return cls(
            sensitive_words=settings.sensitive_words,
            absolute_patterns=settings.absolute_claim_patterns,
        )

    @property
    def sensitive_word_count(self) -> int:
        """已加载的敏感词数量（观测 / 测试用）。"""
        return len(self._sensitive_words)

    def clean_text(self, text: str) -> str:
        """对一段文本做合规改写，返回改写后的文本。

        为流式场景抽出来的**文本级入口**：流式下发的是一句一句的文本，手上根本没有
        完整的 :class:`~app.schemas.emotion.EmotionResult`，而
        :meth:`ensure_compliant` 要的就是那个对象。两条路都收敛到这里，
        规则只有一份——否则"流式版忘了过滤敏感词"这种事迟早发生。
        """
        cleaned = text

        # 顺序固定：先绝对化用语，再敏感词
        for absolute in self._absolute_patterns:
            cleaned = cleaned.replace(absolute, soften(absolute))
        for word in self._sensitive_words:
            if word in cleaned:
                cleaned = cleaned.replace(word, "*" * len(word))
        return cleaned

    def ensure_compliant(
        self, session_id: str, user_id: int, result: EmotionResult | None
    ) -> EmotionResult | None:
        """对 ``speechText`` 做合规改写，``displayBlocks`` 原样透出。

        Args:
            session_id: 仅用于日志。
            user_id: 仅用于日志，实现里没用到。
            result: 情感 Agent 的输出。``None`` 或 ``speechText`` 为 ``None`` 时原样返回。

        Returns:
            改写后的新 :class:`EmotionResult`；未命中任何规则时**返回等价的新对象**，
            不做原地修改。
        """
        if result is None or result.speech_text is None:
            return result

        text = result.speech_text
        cleaned = self.clean_text(text)

        if cleaned != text:
            logger.warning("[Compliance] session=%s userId=%s 触发合规改写", session_id, user_id)

        return EmotionResult(speech_text=cleaned, display_blocks=result.display_blocks)
