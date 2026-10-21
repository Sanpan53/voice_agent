"""TTS 过渡语的预合成缓存。

TTS **按字符收费**，而 Agent 的过渡语高度重复：

* "鸡哥给你挑了三款。"
* "好的，稍等。"
* "这就帮你找。"

这三句在 :class:`~app.voice.sentence_aggregator.SentenceAggregator` 切出来的
**第一句**里出现频率极高。命中一次省掉的不只是字符费，还有一次 DashScope 的
网络往返（300-500ms）——用户在等第一声，这几百毫秒是听得出来的。

.. important::

   **预合成失败绝不能拦住应用启动。** 预热会真的调 DashScope，
   **DashScope 抖一下就可能抛异常，不能让整个应用起不来。** 一个成本优化项
   不该有一票否决启动流程的权力。所以这里做了两处处理：

   1. 启动预热是**显式开关**（``tts_phrase_preload_on_startup``，默认关）；
   2. :meth:`preload` 自己吞掉所有异常，只记 warning——
      失败的结果就是缓存空着，一切照旧走真实的 TTS。

   另外补了一个手工预热入口 ``POST /api/v1/debug/tts-cache/preload``，
   解决"只有重启才预热"的节奏里最难做的一件事：**不重启就扩词表**。

.. note::

   缓存命中时返回的是**整段拼好的字节**，而未命中时是 SDK 吐出来的若干帧。
   也就是说命中路径下 WebSocket 发的是**一条大二进制消息**而不是 N 条小消息。
   对当前的 ``voice_ws``（逐帧转发、不看帧大小）没有影响，
   但如果将来要做打断（barge-in）按帧粒度丢弃，这里得改成存帧列表。
"""

import logging
from collections.abc import Iterable

from app.voice.tts_service import TtsService, single_text_stream

logger = logging.getLogger(__name__)

#: 预置短语表。
#:
#: 选这三条是因为它们高频出现在``SentenceAggregator`` 切出的**第一句**位置。
#: 要扩词表直接往这里加——改完后可以调
#: ``POST /api/v1/debug/tts-cache/preload`` 热加载，不必重启。
DEFAULT_PHRASES: tuple[str, ...] = (
    "鸡哥给你挑了三款。",
    "好的，稍等。",
    "这就帮你找。",
)


class CachedTtsPhrases:
    """预合成短语的内存缓存。

    Args:
        tts: TTS 服务。传 ``None`` 时 :meth:`preload` 直接跳过（缓存恒为空，
            所有句子都走真实合成）——对应"TTS 没装配"的降级形态。
        phrases: 要预热的短语，默认 :data:`DEFAULT_PHRASES`。

    Note:
        缓存放在**进程内存**而不是 Redis。理由：PCM 音频是几百 KB 级的二进制，
        塞 Redis 要走一次序列化 + 网络往返，省下的那点合成时间又赔回去了；
        而这三条短语是**全局共享**的、跟会话无关，每台机器各存一份正好。
    """

    def __init__(
        self,
        tts: TtsService | None = None,
        phrases: Iterable[str] = DEFAULT_PHRASES,
    ) -> None:
        self._tts = tts
        self._phrases = tuple(phrases)
        self._cache: dict[str, bytes] = {}

    @property
    def phrases(self) -> tuple[str, ...]:
        """当前配置的短语表。"""
        return self._phrases

    def get(self, phrase: str) -> bytes | None:
        """取预合成音频；没预热到就返回 ``None``，调用方走真实 TTS。

        **这是个纯查表，不做任何 IO**——
        它挂在每一句话的合成路径上，不能有 await、不能有网络。
        """
        return self._cache.get(phrase)

    def size(self) -> int:
        """已预热的条数（调试接口用）。"""
        return len(self._cache)

    def __contains__(self, phrase: object) -> bool:
        return phrase in self._cache

    async def preload(self) -> int:
        """逐条合成并缓存，返回**成功**的条数。

        幂等：已经预热过的短语会跳过（不重复烧 TTS 字符费）。

        Returns:
            本次新预热成功的条数。全失败时返回 0，**不抛异常**。
        """
        if self._tts is None:
            logger.info("[Cost] 未装配 TTS，跳过短语预热")
            return 0

        loaded = 0
        for phrase in self._phrases:
            if phrase in self._cache:
                continue
            try:
                frames = [
                    frame async for frame in self._tts.synthesize(single_text_stream(phrase))
                ]
            except Exception:  # noqa: BLE001 — 预热失败只影响省钱，不能影响启动
                logger.warning("[Cost] TTS 短语预热失败 phrase=%r", phrase, exc_info=True)
                continue
            audio = b"".join(frames)
            if not audio:
                # 合成"成功"但一个字都没出来——**别把空字节缓存进去**，
                # 否则这一句以后永远是静音，而且不会走真实 TTS 重试。
                logger.warning("[Cost] TTS 短语预热返回空音频 phrase=%r", phrase)
                continue
            self._cache[phrase] = audio
            loaded += 1

        logger.info(
            "[Cost] TTS 短语缓存预热完成，本次 %d 条 / 共 %d 条", loaded, len(self._cache)
        )
        return loaded

    def clear(self) -> None:
        """清空缓存（测试 / 手工重启用）。"""
        self._cache.clear()
