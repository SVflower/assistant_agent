"""M8b 摘要压缩测试：分轮、Compactor、双历史、降级、持久化、开关回归。"""

from __future__ import annotations

from assistant_agent.agent.compaction import Compactor, group_turns
from assistant_agent.agent.context import Conversation
from assistant_agent.llm.client import StreamEvent


class _SummaryClient:
    """假 client：把请求压成固定摘要，可选带 usage 或模拟失败。"""

    def __init__(self, summary: str = "摘要要点", usage=None, fail: bool = False) -> None:
        self._summary = summary
        self._usage = usage or {}
        self._fail = fail
        self.calls = 0

    def complete_stream(self, messages, tools=None):
        self.calls += 1
        if self._fail:
            yield StreamEvent(kind="error", text="boom")
            return
        yield StreamEvent(kind="content", text=self._summary)
        if self._usage:
            yield StreamEvent(kind="usage", usage=self._usage)


def _turn(user: str) -> list[dict]:
    return [{"role": "user", "content": user}, {"role": "assistant", "content": "ok"}]


def test_group_turns_splits_on_user():
    msgs = [*_turn("a"), *_turn("b"), {"role": "user", "content": "c"}]
    turns = group_turns(msgs)
    assert len(turns) == 3
    assert turns[0][0]["content"] == "a" and turns[2][0]["content"] == "c"


def test_group_turns_orphan_head_joins_first():
    msgs = [{"role": "tool", "content": "x"}, {"role": "user", "content": "a"}]
    turns = group_turns(msgs)
    assert len(turns) == 2 and turns[0][0]["role"] == "tool"


# ---- Compactor ----
def test_compact_summarizes_oldest_keeps_recent():
    tail = [m for i in range(6) for m in _turn(f"t{i}")]  # 6 轮
    c = Compactor(_SummaryClient("SUM"), keep_recent_turns=2)
    result = c.compact(tail, base_covered=0, prev_summary="")
    assert result is not None
    assert result.summary == "SUM"
    assert result.covered_upto == 8  # 前 4 轮 ×2 条 = 8 条被压


def test_compact_below_keep_returns_none():
    tail = [m for i in range(2) for m in _turn(f"t{i}")]  # 2 轮
    c = Compactor(_SummaryClient(), keep_recent_turns=4)
    assert c.compact(tail, 0, "") is None  # 不足保护窗，不压


def test_compact_failure_returns_none():
    tail = [m for i in range(6) for m in _turn(f"t{i}")]
    c = Compactor(_SummaryClient(fail=True), keep_recent_turns=2)
    assert c.compact(tail, 0, "") is None  # 摘要失败→降级


def test_compact_reports_usage():
    tail = [m for i in range(6) for m in _turn(f"t{i}")]
    c = Compactor(_SummaryClient("S", usage={"total_tokens": 42}), keep_recent_turns=2)
    result = c.compact(tail, 0, "")
    assert result.usage == {"total_tokens": 42}  # 摘要 token 供 loop 上报


def test_compact_merges_prev_summary():
    tail = [m for i in range(6) for m in _turn(f"t{i}")]
    client = _SummaryClient("NEW")
    Compactor(client, keep_recent_turns=2).compact(tail, base_covered=10, prev_summary="OLD")
    # base_covered 偏移正确传递
    result = Compactor(client, 2).compact(tail, base_covered=10, prev_summary="OLD")
    assert result.covered_upto == 18  # 10 + 8


# ---- 双历史（Conversation + checkpoint）----
def test_checkpoint_none_is_byte_identical():
    a = Conversation(system_prompt="S", max_context_tokens=8000)
    b = Conversation(system_prompt="S", max_context_tokens=8000)
    for conv in (a, b):
        conv.add_user("hi")
        conv.add_assistant("yo")
    b.load_checkpoint(None)  # 显式设 None
    assert a.messages() == b.messages()  # 无 checkpoint 逐字节一致


def test_checkpoint_prepends_summary_and_drops_covered():
    c = Conversation(system_prompt="S", max_context_tokens=8000)
    for i in range(4):
        c.add_user(f"q{i}")
        c.add_assistant(f"a{i}")
    c.set_checkpoint("早前要点", covered_upto=4)  # 压掉前 2 轮（4 条）
    msgs = c.messages()
    assert msgs[0]["role"] == "system"
    assert "早前要点" in msgs[1]["content"]  # 摘要在 system 之后
    # 被覆盖的 q0/q1 不再出现在原文里
    bodies = [m["content"] for m in msgs[2:]]
    assert "q0" not in bodies and "q3" in bodies


def test_export_history_stays_full_after_checkpoint():
    c = Conversation(system_prompt="S")
    for i in range(4):
        c.add_user(f"q{i}")
    c.set_checkpoint("摘要", covered_upto=2)
    assert len(c.export_history()) == 4  # 存档仍是完整原文


# ---- loop 集成 + 持久化 + 兜底 ----
from assistant_agent.agent.loop import AgentLoop  # noqa: E402
from assistant_agent.config.schema import AppConfig  # noqa: E402
from assistant_agent.session.store import SessionStore  # noqa: E402
from assistant_agent.tools.base import ToolContext  # noqa: E402
from assistant_agent.tools.registry import ToolRegistry  # noqa: E402


class _TurnClient:
    """任务轮直接给最终答复（无工具→结束循环）；摘要调用（tools=None）给摘要。"""

    def complete_stream(self, messages, tools=None):
        if tools is None:  # 摘要调用
            yield StreamEvent(kind="content", text="早前要点摘要")
        else:  # 任务轮
            yield StreamEvent(kind="content", text="答复")


def _loop(enabled: bool, threshold: float = 0.01) -> AgentLoop:
    cfg = AppConfig.model_validate({
        "active": "c", "providers": {"c": {"model": "openai/x"}},
        "agent": {"max_context_tokens": 2000,
                  "compaction": {"enabled": enabled, "threshold": threshold,
                                 "keep_recent_turns": 1}},
    })
    return AgentLoop(cfg, _TurnClient(), ToolRegistry(), ToolContext())


def _preload(loop: AgentLoop, n_turns: int) -> None:
    hist = []
    for i in range(n_turns):
        hist.append({"role": "user", "content": f"问题{i} " + "x" * 80})
        hist.append({"role": "assistant", "content": f"回答{i} " + "y" * 80})
    loop.load_history(hist)


def test_loop_triggers_compaction_over_threshold():
    loop = _loop(enabled=True, threshold=0.05)
    _preload(loop, 10)  # 大量历史，超阈值
    events = list(loop.run("新问题"))
    assert any(e.kind == "notice" for e in events)  # 触发了压缩提示
    assert loop.export_checkpoint() is not None  # checkpoint 已写


def test_loop_disabled_never_compacts():
    loop = _loop(enabled=False)
    _preload(loop, 10)
    events = list(loop.run("新问题"))
    assert not any(e.kind == "notice" for e in events)
    assert loop.export_checkpoint() is None  # 关闭→绝不压


def test_loop_summary_failure_degrades_gracefully():
    cfg = AppConfig.model_validate({
        "active": "c", "providers": {"c": {"model": "openai/x"}},
        "agent": {"max_context_tokens": 2000,
                  "compaction": {"enabled": True, "threshold": 0.05, "keep_recent_turns": 1}},
    })
    loop = AgentLoop(cfg, _SummaryFailClient(), ToolRegistry(), ToolContext())
    _preload(loop, 10)
    events = list(loop.run("新问题"))  # 摘要失败但不崩
    assert loop.export_checkpoint() is None  # 降级：checkpoint 未写
    assert any(e.kind in ("content_delta", "final") for e in events)


class _SummaryFailClient:
    def complete_stream(self, messages, tools=None):
        if tools is None:
            yield StreamEvent(kind="error", text="摘要炸了")
        else:
            yield StreamEvent(kind="content", text="答复")


def test_session_persists_checkpoint_roundtrip(tmp_path):
    store = SessionStore(base_dir=tmp_path / "s")
    sess = store.new_session(provider="c", model="m")
    sess.compaction_checkpoint = {"summary": "要点", "covered_upto": 6}
    store.save(sess, [{"role": "user", "content": "hi"}])
    loaded = store.load(sess.id)
    assert loaded.compaction_checkpoint == {"summary": "要点", "covered_upto": 6}


def test_session_checkpoint_defaults_none(tmp_path):
    store = SessionStore(base_dir=tmp_path / "s")
    sess = store.new_session()
    store.save(sess, [])
    assert store.load(sess.id).compaction_checkpoint is None


def test_huge_summary_does_not_overflow():
    c = Conversation(system_prompt="S", max_context_tokens=200)
    for i in range(4):
        c.add_user(f"q{i}")
    c.set_checkpoint("巨" * 500, covered_upto=2)  # 摘要远超预算
    msgs = c.messages()  # 不崩、不无限，至少含 system+summary
    assert msgs[0]["role"] == "system" and len(msgs) >= 2
