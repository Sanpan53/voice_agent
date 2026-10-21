"""闲聊模板匹配。

闲聊走的是 **qwen-max**（
``OrchestratorService._handle_chitchat`` → ``EmotionService.wrap``），
而"你是谁""你会不会推荐""今天天气怎么样"这类问题占了闲聊的绝大多数——
用一条正则挡掉，命中就**完全不调 LLM**，成本从 0.09 元直接掉到 0。

没命中的（"今天股票涨了吗"）照旧走 LLM，所以这是一层**只会省钱、不会变差**的优化：
模板命中时用户拿到的是一句写死的、比模型自由发挥更可控的话术。

三处刻意的实现选择：

1. **模板顺序确定。** 模板用**元组**存储，声明顺序就是匹配顺序，第一条命中即返回；
   若改用无序映射，两条正则同时命中时哪条生效将不可复现。
2. **大小写不敏感。** 模板里写了 ``ai``，要能匹配"你是AI吗"——而这恰恰是最常见的
   问法，所以正则加 ``re.IGNORECASE``。
3. **首尾空白。** 匹配前先 ``strip()``，避免用户多打空格就漏掉模板。
"""

import re

#: 模板表：``(正则, 回复)``。**顺序即优先级**，第一条 ``fullmatch`` 成功的胜出。
#:
#: 正则写成整串匹配的形态（前后用 ``.*`` 兜住），配合 ``re.fullmatch``
#: 要求**整个字符串**匹配，而不是"包含子串"。
TEMPLATES: tuple[tuple[str, str], ...] = (
    (
        r"你是(谁|什么|ai|机器人).*",
        "我是鸡哥家的语音导购，帮你挑好商品。你想看点啥？",
    ),
    (
        r".*(会不会|能不能).*(推荐|挑).*",
        "当然能！你说个需求我就帮你挑。",
    ),
    (
        r".*(天气|冷|热).*",
        "天气就不聊了哈，我主业是帮你选东西。想看点啥？",
    ),
)

#: 预编译后模板表是模块级常量，进程内复用，避免每次匹配都重新编译正则。
_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reply) for pattern, reply in TEMPLATES
)


def match(text: str | None) -> str | None:
    """返回命中的模板话术；没命中返回 ``None``（调用方继续走 LLM）。

    Args:
        text: 用户原话。

    Returns:
        写死的话术，或 ``None``。
    """
    if not text:
        return None
    stripped = text.strip()
    for pattern, reply in _COMPILED:
        if pattern.fullmatch(stripped):
            return reply
    return None


def hit(text: str | None) -> bool:
    """是否命中模板（调试接口与测试用）。"""
    return match(text) is not None
