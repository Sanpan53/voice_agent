"""意图理解 Agent 的构造器。

用轻量模型（``qwen-flash``）即可，意图识别不需要主模型。

**不挂 Toolkit**——意图识别是纯分类任务，不需要调工具。
"""

from agentscope.agent import ReActAgent
from agentscope.formatter import DashScopeChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.model import ChatModelBase

from app.agent.prompt_loader import PromptLoader

#: 意图 Agent 的名字，调试日志与 MsgHub 广播里会用到
AGENT_NAME = "intent_agent"

#: Prompt 文件路径（相对 app/resources/prompts/）
PROMPT_PATH = "prompts/intent.txt"


class IntentAgentBuilder:
    """构造意图理解 Agent。

    Args:
        light_chat_model: 轻量模型实例。
        prompts: Prompt 加载器。
    """

    def __init__(self, light_chat_model: ChatModelBase, prompts: PromptLoader) -> None:
        self._model = light_chat_model
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
