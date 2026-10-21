"""DashScope 实时语音 SDK 的凭证注入。

``dashscope.audio.asr.Recognition`` 与 ``dashscope.audio.tts_v2.SpeechSynthesizer``
都从模块级全局 ``dashscope.api_key`` 读取凭证（``get_default_api_key()``），
构造器不接受 ``api_key`` 参数。所以这里统一做一次显式注入，避免依赖
``.env`` 之外的隐式环境状态。
"""

import dashscope


def apply_api_key(api_key: str) -> None:
    """把配置里的 API Key 注入 dashscope 全局，缺配置时直接报错。"""
    if not api_key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法使用语音服务")
    dashscope.api_key = api_key
