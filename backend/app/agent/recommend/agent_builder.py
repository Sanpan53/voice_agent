"""商品推荐 Agent 的构造器。

**必须用主模型**（``qwen-max``）——推荐理由是直接说给用户听的"门面文字"，
用户对它的语感最敏感，这里省模型钱是省错了地方。意图识别那种内部中间结果才用
轻量模型。

**不挂 Toolkit**——推荐理由生成是纯文本生成，商品数据由 Service 层以 JSON 直接
喂进消息体，不需要模型自己调工具查库。
"""

from agentscope.agent import ReActAgent
from agentscope.formatter import DashScopeChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.model import ChatModelBase

from app.agent.prompt_loader import PromptLoader

#: 推荐 Agent 的名字
AGENT_NAME = "recommend_agent"

#: Prompt 文件路径（相对 app/resources/prompts/）
PROMPT_PATH = "prompts/recommend-reason.txt"


class RecommendAgentBuilder:
    """构造商品推荐 Agent。

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
