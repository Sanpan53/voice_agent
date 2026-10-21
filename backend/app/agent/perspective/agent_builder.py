"""多视角点评团的 Agent 构造器。

三个角色（价格顾问 / 专业跑者 / 入门买家）**共用一个 Builder**，靠 ``name`` 和
``prompt_path`` 两个参数区分——比写三个几乎一样的类清爽得多，这也是
``build(name, prompt_path)`` 这样设计的原因。

**必须用多 Agent Formatter**（``DashScopeMultiAgentFormatter``）：这三个 Agent
要在一个 MsgHub 里互相看到彼此的发言。若沿用单 Agent 的 ``DashScopeChatFormatter``，
所有人的发言会被扁平化成同一种 role 拼进 Prompt，Hub 的自动广播就白做了——
这是这套点评团实现的两条硬约束之一。
"""

from agentscope.agent import ReActAgent
from agentscope.formatter import DashScopeMultiAgentFormatter
from agentscope.memory import InMemoryMemory
from agentscope.model import ChatModelBase

from app.agent.prompt_loader import PromptLoader

#: 三个角色的名字（同时用于 Hub 内区分发言者）
PRICE_ADVISOR = "price_advisor"
PRO_RUNNER = "pro_runner"
BEGINNER_BUYER = "beginner_buyer"

#: 三个角色的 Prompt 文件（相对 app/resources/prompts/）
PRICE_ADVISOR_PROMPT = "prompts/perspective_price.txt"
PRO_RUNNER_PROMPT = "prompts/perspective_pro.txt"
BEGINNER_BUYER_PROMPT = "prompts/perspective_beginner.txt"


class PerspectiveAgentBuilder:
    """构造一个视角点评 Agent。

    Args:
        multi_agent_model: 多 Agent 模型实例（``qwen-plus``）。
        prompts: Prompt 加载器。
    """

    def __init__(self, multi_agent_model: ChatModelBase, prompts: PromptLoader) -> None:
        self._model = multi_agent_model
        self._prompts = prompts

    def build(self, name: str, prompt_path: str) -> ReActAgent:
        """按角色名和 Prompt 文件构造一个全新的点评 Agent。

        每次都返回新实例——点评团是"用完即抛"的，见
        :meth:`~app.agent.agent_factory.AgentFactory.new_perspective_team`。
        """
        return ReActAgent(
            name=name,
            model=self._model,
            sys_prompt=self._prompts.load(prompt_path),
            formatter=DashScopeMultiAgentFormatter(),
            memory=InMemoryMemory(),
        )
