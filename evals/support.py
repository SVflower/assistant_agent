"""Eval 专用的 ToolContext 装配。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from assistant_agent.execution import HostWorkspace, ProcessSupervisor, RunControl
from assistant_agent.observability import NullLogger
from assistant_agent.observability.redaction import sanitize_for_display
from assistant_agent.persistence.artifacts import ArtifactStore
from assistant_agent.tools.context import ConfirmChoice
from assistant_agent.tools.context import ToolContext as RuntimeToolContext
from assistant_agent.tools.models import ToolBudget, ToolResult
from assistant_agent.tools.ports import ToolTelemetry
from assistant_agent.tools.tool import Tool


class EvalToolContext(RuntimeToolContext):
    """为 Eval 装配独立的宿主执行资源。"""

    def __init__(self, **kwargs: Any) -> None:
        root = Path(kwargs.get("workspace_root", Path.cwd())).expanduser().resolve()
        workspace = kwargs.pop("workspace", None)
        control = kwargs.pop("run_control", None) or RunControl()
        supervisor = kwargs.pop("process_supervisor", None) or ProcessSupervisor()
        workspace = workspace or HostWorkspace(root, supervisor=supervisor, control=control)
        logger = cast(ToolTelemetry, kwargs.pop("logger", None) or NullLogger())
        kwargs.setdefault("sanitize_for_display", sanitize_for_display)
        artifact_store = kwargs.pop("artifact_store", None) or ArtifactStore(
            workspace.root,
            max_chars=int(kwargs.get("max_captured_output_chars", 1_000_000)),
            max_files=int(kwargs.get("max_artifact_files", 100)),
            root=kwargs.get("artifact_root"),
        )
        self.process_supervisor = supervisor
        super().__init__(
            workspace=workspace,
            run_control=control,
            logger=logger,
            artifact_store=artifact_store,
            **kwargs,
        )


__all__ = ["ConfirmChoice", "EvalToolContext", "Tool", "ToolBudget", "ToolResult"]
