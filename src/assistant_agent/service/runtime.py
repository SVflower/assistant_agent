"""兼容导入；Runtime 生命周期与装配已分离。"""

from assistant_agent.application.runtime import AgentRuntime
from assistant_agent.bootstrap.runtime import create_runtime
from assistant_agent.contracts.capabilities import RuntimeNotice

__all__ = ["AgentRuntime", "RuntimeNotice", "create_runtime"]
