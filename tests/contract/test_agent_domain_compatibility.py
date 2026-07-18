"""M19d Agent 领域迁移的旧路径兼容快照。"""

from assistant_agent.agent.compaction import Compactor as LegacyCompactor
from assistant_agent.agent.context.compaction import Compactor
from assistant_agent.agent.continuation import ContinuationController as LegacyContinuation
from assistant_agent.agent.execution import LoopCursor as LegacyLoopCursor
from assistant_agent.agent.recovery import RunCoordinator as LegacyCoordinator
from assistant_agent.agent.run.budgets import ContinuationController
from assistant_agent.agent.run.coordinator import RunCoordinator
from assistant_agent.agent.run.ports import ControlState
from assistant_agent.agent.run.state import RunState
from assistant_agent.agent.run_state import RunState as LegacyRunState
from assistant_agent.agent.tool_batch import LoopCursor
from assistant_agent.execution import ControlState as RuntimeControlState


def test_agent_legacy_paths_keep_type_identity():
    assert LegacyCompactor is Compactor
    assert LegacyContinuation is ContinuationController
    assert LegacyLoopCursor is LoopCursor
    assert LegacyCoordinator is RunCoordinator
    assert LegacyRunState is RunState
    assert RuntimeControlState is ControlState
