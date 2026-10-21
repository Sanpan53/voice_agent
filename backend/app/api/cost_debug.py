"""成本控制调试接口。

三组调试入口（缓存指纹、闲聊模板匹配、TTS 缓存）合在一个文件里，
因为它们服务的是同一件事——
**把"省钱的那几层"单独拎出来验证**：这些层全都是"命中就绕过外部调用"，
一旦写歪，表现只是"账单没降下来"，不会有任何报错。

.. danger::

   **生产环境这三个接口要挂在内网管理域名下、加鉴权，别暴露到公网。**
   ``/tts-cache/preload`` 能直接触发外部计费调用（烧钱），
   ``/fingerprint`` 和 ``/chitchat/match`` 会把内部规则表吐给调用方。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_cached_tts_phrases
from app.services import chitchat_templates
from app.services.utterance_fingerprint import compute as compute_fingerprint
from app.voice.cached_tts_phrases import CachedTtsPhrases

router = APIRouter(prefix="/api/v1/debug", tags=["cost-debug"])


@router.get("/cache/fingerprint")
async def fingerprint(text: Annotated[str, Query()]) -> dict:
    """看一眼归一化后的指纹。

    用途是验证"标点 / 大小写差异不该浪费一次 LLM 调用"这条规则真的生效了：

    .. code-block:: bash

       curl "localhost:8080/api/v1/debug/cache/fingerprint?text=想买双跑鞋"
       curl "localhost:8080/api/v1/debug/cache/fingerprint?text=想买双跑鞋。"
       curl "localhost:8080/api/v1/debug/cache/fingerprint?text=想买双跑鞋！"

    三句应该吐出**同一个 fp**。
    """
    return {"text": text, "fp": compute_fingerprint(text)}


@router.get("/chitchat/match")
async def chitchat_match(text: Annotated[str, Query()]) -> dict:
    """闲聊模板匹配。

    ``hit=false`` 说明这一句会走 qwen-max——那正是要花钱的那条路。
    """
    reply = chitchat_templates.match(text)
    return {"text": text, "hit": reply is not None, "reply": reply or ""}


@router.get("/tts-cache/probe")
async def tts_cache_probe(
    phrases: Annotated[CachedTtsPhrases, Depends(get_cached_tts_phrases)],
    phrase: Annotated[str, Query()],
) -> dict:
    """查一条短语有没有预合成好。"""
    audio = phrases.get(phrase)
    return {
        "phrase": phrase,
        "hit": audio is not None,
        "bytes": 0 if audio is None else len(audio),
        "cached": phrases.size(),
    }


@router.post("/tts-cache/preload")
async def tts_cache_preload(
    phrases: Annotated[CachedTtsPhrases, Depends(get_cached_tts_phrases)],
) -> dict:
    """手工预热短语缓存。

    **这是手工预热入口的价值**：启动时的预热只跑一次，改词表后不重启也能补预热
    （幂等，已预热的会跳过）。
    """
    loaded = await phrases.preload()
    return {"loaded": loaded, "cached": phrases.size(), "phrases": list(phrases.phrases)}
