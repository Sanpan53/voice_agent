"""ASR 服务：把 Paraformer 实时识别封装成「开始 / 送一帧音频 / 结束」三步。

用一个 ``AsrSession`` 对象承接「开始 / 送帧 / 结束」的生命周期，
把结果通过回调实时吐出去。
"""

import asyncio
import logging
from collections.abc import Callable

from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

from app.config import get_settings
from app.voice.client import apply_api_key

logger = logging.getLogger(__name__)

# 实时回调：(文本, 是否一句话说完)
PartialCallback = Callable[[str, bool], None]


def _create_recognition(
    model: str,
    callback: RecognitionCallback,
    audio_format: str,
    sample_rate: int,
) -> Recognition:
    """构造 ``Recognition``（单独抽出，便于测试替换成假实现）。"""
    return Recognition(
        model=model,
        callback=callback,
        format=audio_format,
        sample_rate=sample_rate,
    )


class _AsrCallback(RecognitionCallback):
    """把 SDK 的回调转成 ``AsrSession`` 能处理的形状。"""

    def __init__(self, session: "AsrSession") -> None:
        self._session = session

    def on_event(self, result: RecognitionResult) -> None:
        self._session._handle_result(result)  # noqa: SLF001 — 内部协作类

    def on_error(self, result: RecognitionResult) -> None:
        logger.error("ASR 错误: %s", result)
        self._session._mark_error(RuntimeError(f"ASR 识别失败: {result}"))  # noqa: SLF001

    def on_complete(self) -> None:
        self._session._mark_done()  # noqa: SLF001

    def on_close(self) -> None:
        self._session._mark_done()  # noqa: SLF001


class AsrService:
    """无状态，按会话创建 ``AsrSession``。"""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        audio_format: str = "pcm",
        sample_rate: int = 16000,
    ) -> None:
        settings = get_settings()
        self._model = model or settings.asr_model
        self._api_key = api_key or settings.dashscope_api_key
        self._format = audio_format
        self._sample_rate = sample_rate

    def create_session(self, on_partial: PartialCallback) -> "AsrSession":
        """开一次识别会话。

        Args:
            on_partial: 实时回调 ``(文本, 是否最终结果)``。``is_sentence_end``
                为 True 意味着「一句话说完了」——这是关键节点，Orchestrator
                会在这个事件触发时把文字塞给意图 Agent。
        """
        return AsrSession(
            model=self._model,
            api_key=self._api_key,
            audio_format=self._format,
            sample_rate=self._sample_rate,
            on_partial=on_partial,
        )


class AsrSession:
    """一次语音识别会话（音频为 PCM 16k 16bit 单声道）。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        audio_format: str,
        sample_rate: int,
        on_partial: PartialCallback,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._format = audio_format
        self._sample_rate = sample_rate
        self._on_partial = on_partial
        self._final_parts: list[str] = []
        self._recognition: Recognition | None = None
        self._done = asyncio.Event()
        self._error: Exception | None = None

    @property
    def final_text(self) -> str:
        """已经识别完成的句子拼接结果。"""
        return "".join(self._final_parts)

    @property
    def is_done(self) -> bool:
        return self._done.is_set()

    async def start(self) -> None:
        """建连并开始识别（握手是阻塞网络调用，放到线程池）。"""
        apply_api_key(self._api_key)
        self._recognition = _create_recognition(
            self._model, _AsrCallback(self), self._format, self._sample_rate
        )
        await asyncio.to_thread(self._recognition.start)

    async def send_frame(self, frame: bytes) -> None:
        """送一帧音频；空帧直接丢弃。"""
        if not frame or self._recognition is None:
            return
        await asyncio.to_thread(self._recognition.send_audio_frame, frame)

    async def stop(self) -> str:
        """结束识别并返回最终文本。"""
        if self._recognition is not None:
            await asyncio.to_thread(self._recognition.stop)
        return self.final_text

    async def wait_done(self) -> str:
        """等待 SDK 报告完成 / 出错（供需要「等最后一次 final」的调用方使用）。"""
        await self._done.wait()
        if self._error is not None:
            raise self._error
        return self.final_text

    # ---------------------------------------------------------- SDK 回调入口

    def _handle_result(self, result: RecognitionResult) -> None:
        sentence = result.get_sentence()
        # 非最终结果时 SDK 也会回调，但可能没有 text 字段（纯静音帧）。
        if not isinstance(sentence, dict) or "text" not in sentence:
            return

        text = sentence["text"] or ""
        is_end = RecognitionResult.is_sentence_end(sentence)
        if is_end:
            self._final_parts.append(text)
        self._on_partial(text, is_end)

    def _mark_done(self) -> None:
        self._done.set()

    def _mark_error(self, error: Exception) -> None:
        self._error = error
        self._done.set()
