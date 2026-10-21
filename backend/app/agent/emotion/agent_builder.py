"""情感应答 Agent 的构造器。

**必须用主模型**（``qwen-max``）——这个 Agent 是整条链路里离用户最近的一个，它的
产出直接进 TTS 变成声音，质量好坏直接决定体验是"像在听一个懂行的朋友"还是
"像在听机器念说明书"。

**不挂 Toolkit**——包装口语是纯生成任务，商品数据以 JSON 直接喂进消息体。

加载 :data:`PROMPT_PATH` 指向的 **``emotion-merged.txt``**：推荐理由
生成被合并进这个 Agent（它现在拿裸商品数据自己编理由），输出契约也从
``{"speechText": ...}`` 换成了**纯文本**。旧的 ``emotion.txt`` 保留在
``app/resources/prompts/`` 下作为回滚资产，但生产链路上不再加载。
"""

from agentscope.agent import ReActAgent
from agentscope.formatter import DashScopeChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.model import ChatModelBase

from app.agent.prompt_loader import PromptLoader

#: 情感 Agent 的名字
AGENT_NAME = "emotion_agent"

#: Prompt 文件路径（相对 app/resources/prompts/）。
#: 由 ``prompts/emotion.txt`` 换成合并版。
PROMPT_PATH = "prompts/emotion-merged.txt"


class EmotionAgentBuilder:
    """构造情感应答 Agent。

    Args:
        main_chat_model: 主模型实例（``qwen-max``）。
        prompts: Prompt 加载器。
    """

    def __init__(self, main_chat_model: ChatModelBase, prompts: PromptLoader) -> None:
        self._model = main_chat_model
        self._prompts = prompts

    def build(self) -> ReActAgent:
        """每次调用返回一个全新的 Agent 实例（每会话一个）。"""
        return ReActAgent(
            name=AGENT_NAME,
            model=self._model,
            sys_prompt=self._prompts.load(PROMPT_PATH),
            formatter=DashScopeChatFormatter(),
            memory=InMemoryMemory(),
        )
