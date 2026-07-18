"""Run 恢复时的 provider/model/prompt/tool 定义兼容检查。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_agent.agent.run_state import RunState, canonical_hash


@dataclass(frozen=True)
class DefinitionDifference:
    field: str
    saved: str
    current: str


class DefinitionStateMixin:
    state: RunState

    def checkpoint(self) -> None:
        raise NotImplementedError

    def definition_differences(
        self,
        *,
        provider: str,
        model: str,
        system_prompt: str,
        tool_schemas: list[dict[str, Any]],
    ) -> list[DefinitionDifference]:
        current = {
            "provider": provider,
            "model": model,
            "system_prompt_hash": canonical_hash(system_prompt),
            "tool_schema_hash": canonical_hash(tool_schemas),
        }
        return [
            DefinitionDifference(field, str(getattr(self.state, field)), value)
            for field, value in current.items()
            if getattr(self.state, field) != value
        ]

    def accept_definitions(
        self,
        *,
        provider: str,
        model: str,
        system_prompt: str,
        tool_schemas: list[dict[str, Any]],
    ) -> None:
        self.state.provider = provider
        self.state.model = model
        self.state.system_prompt_hash = canonical_hash(system_prompt)
        self.state.tool_schema_hash = canonical_hash(tool_schemas)
        self.checkpoint()
