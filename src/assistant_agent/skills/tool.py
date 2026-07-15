"""LoadSkillTool：渐进披露 Level 2。

模型在系统提示词里看到技能的 name/description（Level 1），判断相关后调用
本工具按名加载正文。正文里若指向脚本/参考文件（Level 3），模型用现有
read_file / run_shell 读或执行——本工具不代跑，天然沿用既有确认门。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from assistant_agent.tools.base import Tool, ToolContext, ToolResult
from assistant_agent.tools.permissions import Capability, PermissionRequest

if TYPE_CHECKING:
    from assistant_agent.skills.store import SkillStore


class LoadSkillTool(Tool):
    name = "load_skill"
    description = (
        "加载一个技能的完整说明。当某个技能的描述与当前任务相关时调用，"
        "拿到它的分步指示后照做（其中可能让你运行脚本或读取参考文件）。"
    )

    def __init__(self, store: SkillStore) -> None:
        self._store = store

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "技能名（系统提示词'可用技能'里列出的 name）",
                },
            },
            "required": ["name"],
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return ToolResult.error("缺少参数 name")
        # 只按已发现的技能名查，不接受路径，杜绝路径穿越。
        body = self._store.get_body(name.strip())
        if body is None:
            available = ", ".join(m.name for m in self._store.list()) or "（无）"
            return ToolResult.error(f"未知技能：{name}。可用技能：{available}")
        return ToolResult.ok(body)

    def permission_requests(
        self, args: dict[str, Any], ctx: ToolContext
    ) -> list[PermissionRequest]:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return []
        meta = self._store.get_meta(name.strip())
        if meta is None:
            return []
        return [
            PermissionRequest(
                self.name,
                Capability.SKILL_LOAD,
                f"{meta.source}/{meta.name}",
                "Skill 内容会作为不可信指示进入模型上下文",
                metadata={"source": meta.source, "trusted": meta.trusted},
            )
        ]
