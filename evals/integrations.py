"""Eval 专用 Skill/MCP fixture 与配置 Skill 发现。"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from assistant_agent.config.schema import AppConfig
from assistant_agent.integrations.mcp.tool import MCPTool
from assistant_agent.integrations.skills import LoadSkillTool, SkillMeta, SkillSource, SkillStore
from assistant_agent.tools.registry import ToolRegistry
from evals.schema import EvalCase


def register_case_mocks(case: EvalCase, root: Path, registry: ToolRegistry) -> list[SkillMeta]:
    metas: dict[str, SkillMeta] = {}
    for skill in case.mocks.skills:
        path = root / ".eval-skills" / skill.name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n{skill.body}\n",
            encoding="utf-8",
        )
        metas[skill.name] = SkillMeta(
            skill.name, skill.description, path, skill.source, skill.trusted
        )
    if metas:
        registry.register(LoadSkillTool(SkillStore(metas)))

    for mock in case.mocks.mcp_tools:
        registered = (
            "mcp__"
            + re.sub(r"[^A-Za-z0-9_]", "_", mock.server)
            + "__"
            + re.sub(r"[^A-Za-z0-9_]", "_", mock.tool)
        )

        def caller(*_args: Any, result: str = mock.result) -> Any:
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=result)], isError=False
            )

        registry.register(
            MCPTool(
                server=mock.server,
                registered_name=registered,
                raw_tool=mock.tool,
                description="eval MCP mock",
                input_schema={"type": "object", "properties": {}},
                caller=caller,
                timeout=5,
                auto_approve=mock.trusted,
            )
        )
    return sorted(metas.values(), key=lambda meta: meta.name)


def discover_configured_skills(config: AppConfig) -> SkillStore:
    if not config.skills.enabled:
        return SkillStore({})
    if config.skills.dirs:
        dirs = [Path(value).expanduser() for value in config.skills.dirs]
        sources: list[SkillSource] = ["configured"] * len(dirs)
    else:
        dirs = [
            Path.cwd() / ".assistant_agent" / "skills",
            Path.home() / ".assistant_agent" / "skills",
        ]
        sources = ["project", "personal"]
    return SkillStore.discover(
        dirs,
        sources=sources,
        trusted_names=set(config.skills.trusted_project_skills),
    )
