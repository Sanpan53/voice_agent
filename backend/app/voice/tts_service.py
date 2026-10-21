"""TTS 服务：CosyVoice 流式合成，文字流进、PCM 音频帧出。

用 ``asyncio.Queue`` 把 SDK 的回调模型桥到异步生成器——
每来一帧音频 yield 一帧，消费者可以边收边播。
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from dashscope.audio.tts_v2 import AudioFormat, ResultCallback, SpeechSynthesizer

from app.config import get_settings
from app.voice.client import apply_api_key

logger = logging.getLogger(__name__)

# 结束哨兵：SDK 的 on_complete / on_close 触发时压入队列，告诉消费者没有更多帧了。
_SENTINEL = object()


def _create_synthesizer(
    model: str,
    voice: str,
    audio_format: AudioFormat,
    callback: ResultCallback,
) -> SpeechSynthesizer:
    """构造 ``SpeechSynthesizer``（单独抽出，便于测试替换成假实现）。

    ``format`` 用 ``AudioFormat`` 枚举，已经把「编码 + 采样率 + 位深」封装在一起，
    不需要再单独传 sample_rate，避免两处配置不一致。
    """
    return SpeechSynthesizer(
        model=model, voice=voice, format=audio_format, callback=callback
    )


async def single_text_stream(text: str) -> AsyncIterator[str]:
    """把单段文字包装成文字流。"""
    yield text


class TtsService:
    """TTS 服务。"""

    def __init__(
        self,
        model: str | None = None,
        voice: str | None = None,
        api_key: str | None = None,
        audio_format: AudioFormat | None = None,
    ) -> None:
        settings = get_settings()
        self._model = model or settings.tts_model
        self._voice = voice or settings.tts_voice
        self._api_key = api_key or settings.dashscope_api_key
        self._format = audio_format or AudioFormat.PCM_16000HZ_MONO_16BIT

    async def synthesize(self, text_stream: AsyncIterator[str]) -> AsyncIterator[bytes]:
        """一次流式合成：文字流来一段就合成一段，尾包在文字流结束时吐出。

        Yields:
            PCM 16k 16bit 单声道音频帧。
        """
        apply_api_key(self._api_key)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue()

        def _put(item: object) -> None:
            # SDK 回调在它自己的线程里触发，必须切回事件循环再入队。
            loop.call_soon_threadsafe(queue.put_nowait, item)

        class _Callback(ResultCallback):
            """SDK 吐音频帧和结束事件的唯一出口。"""

            def on_data(self, data: bytes) -> None:
                if data:
                    _put(data)

            def on_complete(self) -> None:
                _put(_SENTINEL)

            def on_close(self) -> None:
                _put(_SENTINEL)

            def on_error(self, message) -> None:  # noqa: ANN001 — SDK 签名
                logger.error("TTS 合成失败: %s", message)
                _put(RuntimeError(f"TTS 合成失败: {message}"))

        synthesizer = _create_synthesizer(
            self._model, self._voice, self._format, _Callback()
        )

        async def _pump_text() -> None:
            """文字流 -> streamingCall；流结束必须调 streamingComplete 触发尾包。"""
            try:
                async for chunk in text_stream:
                    if chunk:
                        await asyncio.to_thread(synthesizer.streaming_call, chunk)
                # 不调这一句，最后几十毫秒音频会卡在 SDK 内部永远出不来。
                await asyncio.to_thread(synthesizer.streaming_complete)
            except Exception as exc:  # noqa: BLE001 — 统一转交给消费者
                _put(exc)
                _put(_SENTINEL)

        pump = asyncio.create_task(_pump_text())
        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item  # type: ignore[misc] — 非哨兵非异常即音频帧
        finally:
            if not pump.done():
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump

    async def synthesize_text(self, text: str) -> bytes:
        """便捷方法：合成一段文字并返回完整音频（用于 smoke test 等短文本场景）。"""
        chunks = [frame async for frame in self.synthesize(single_text_stream(text))]
        return b"".join(chunks)
