"""MCP client 层：连接外部 MCP server（stdio），把其工具适配成本地 Tool。

对外只暴露 MCPManager（生命周期 + 同步桥）与 MCPTool（Tool 适配器）。
本层实现 Tool 端口并依赖官方 mcp 包，不拥有 Agent/Application 用例。
"""

from assistant_agent.integrations.mcp.configure import MCPConfigureError, MCPProbeResult, MCPService
from assistant_agent.integrations.mcp.manager import MCPManager
from assistant_agent.integrations.mcp.status import MCPRequiredServerError, MCPServerStatus
from assistant_agent.integrations.mcp.tool import MCPDependencyUnavailable, MCPTool, extract_result

__all__ = [
    "MCPConfigureError",
    "MCPDependencyUnavailable",
    "MCPManager",
    "MCPProbeResult",
    "MCPRequiredServerError",
    "MCPServerStatus",
    "MCPService",
    "MCPTool",
    "extract_result",
]
