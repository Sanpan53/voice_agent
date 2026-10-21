"""AgentScope / DashScope 模型配置。

对应 ``AgentScopeConfig`` 中的 ``mainChatModel`` 与 ``lightChatModel``
两个模型档位：集中配置、按职责选择、进程内复用。

另外追加第三个模型档位：``multiAgentChatModel``。

**关于 Formatter 的一个重要约束**：AgentScope Python 1.x 里 Formatter 是
**Agent 的构造参数**——``DashScopeChatModel.__init__``
根本没有 ``formatter`` 形参（多余的关键字会被 ``**_kwargs`` 静默吞掉，不报错，
这是个很隐蔽的坑）。

所以本模块只负责"模型档位"，多 Agent Formatter 由
:class:`~app.agent.perspective.agent_builder.PerspectiveAgentBuilder` 在构造 Agent
时传入——**语义完全等价，只是配置的落点从 Model 挪到了 Agent**。
"""

from functools import lru_cache

from agentscope.model import DashScopeChatModel

from app.config import get_settings


def _create_chat_model(model_name: str) -> DashScopeChatModel:
    """创建 AgentScope 1.x 的 DashScope 模型实例。

    构造模型对象本身不会发送模型请求；只有后续 Agent 真正调用模型时
    才会发生网络访问和计费。
    """
    settings = get_settings()
    if not settings.dashscope_api_key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法创建 AgentScope 模型")

    return DashScopeChatModel(
        model_name=model_name,
        api_key=settings.dashscope_api_key,
        stream=True,
    )


@lru_cache(maxsize=1)
def get_main_chat_model() -> DashScopeChatModel:
    """主模型：推荐 Agent 与情感 Agent 使用。"""
    return _create_chat_model(get_settings().main_model)


@lru_cache(maxsize=1)
def get_light_chat_model() -> DashScopeChatModel:
    """轻量模型：意图识别、分类和槽位提取使用。"""
    return _create_chat_model(get_settings().light_model)


@lru_cache(maxsize=1)
def get_multi_agent_chat_model() -> DashScopeChatModel:
    """多 Agent 模型：MsgHub 点评团使用。

    档位选 ``qwen-plus``：点评团是旁路锦上添花，一次跑三次调用，用 ``qwen-max``
    性价比太低；用轻量模型（``qwen-flash``）又撑不起"多视角点评"需要的推理。

    注意这里**只决定模型档位**，多 Agent Formatter 在 Agent 侧传入——见模块
    docstring 的说明。
    """
    return _create_chat_model(get_settings().multi_agent_model)
