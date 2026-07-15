"""M11a CLI 展示契约、模式和流式 Markdown。"""

from __future__ import annotations

from rich.console import Console as RichConsole

from assistant_agent.agent.events import StepEvent
from assistant_agent.tools.display import call_display, result_display, safe_text
from assistant_agent.tools.result import ToolResult
from assistant_agent.ui.console import Console
from assistant_agent.ui.conversation_renderer import ConversationRenderer
from assistant_agent.ui.markdown_stream import StreamingMarkdownRenderer
from assistant_agent.ui.tool_renderer import ToolRenderer


class _Owner:
    def __init__(self, mode="normal", width=100) -> None:
        self._console = RichConsole(record=True, width=width)
        self._active_live = None
        self._at_line_start = True
        self._context_limit = 8000
        self.mode = mode

    def text(self) -> str:
        return self._console.export_text()


def test_write_display_uses_semantic_metadata():
    args = {"path": "demo/tasks.txt", "content": "a\nb\n"}
    call = call_display("write_file", args)
    display = result_display(
        "write_file", args, ToolResult.ok("raw body", metadata={"chars": 4}), call
    )
    assert call.action == "写入" and call.target == "demo/tasks.txt"
    assert display.summary == "已写入 4 字符，2 行"


def test_read_edit_and_list_summaries_use_metadata():
    read = result_display(
        "read_file",
        {"path": "a.py"},
        ToolResult.ok("body", metadata={"start_line": 2, "end_line": 7}),
    )
    edit = result_display(
        "edit_file",
        {"path": "a.py", "old_string": "x", "new_string": "y"},
        ToolResult.ok("ok", metadata={"replacements": 2}),
    )
    listing = result_display(
        "list_dir",
        {"path": "."},
        ToolResult.ok("files", metadata={"returned": 5, "truncated": True}),
    )
    assert read.summary == "已读取 6 行"
    assert edit.summary == "已替换 2 处"
    assert listing.summary == "发现 5 项（结果已截断）"


def test_safe_display_redacts_secrets_and_terminal_controls():
    text = safe_text("sk-abcdef123456\x1b[31m\nnext")
    assert "sk-abcdef" not in text
    assert "\x1b" not in text
    assert "\\n" in text


def test_unknown_tool_fallback_keeps_registered_name():
    display = call_display("custom_tool", {"value": "x"})
    assert display.action == "调用工具"
    assert display.target == "custom_tool"


def test_normal_tool_renderer_hides_raw_arguments_and_result_body():
    owner = _Owner()
    args = {"path": "tasks.txt", "content": "TOP-SECRET\nsecond"}
    call = call_display("write_file", args)
    renderer = ToolRenderer(owner._console, "normal")
    renderer.call(StepEvent(kind="tool_call", tool_name="write_file", tool_args=args, display=call))
    renderer.result(
        StepEvent(
            kind="tool_result",
            tool_name="write_file",
            text="TOP-SECRET\nsecond",
            display=result_display(
                "write_file", args, ToolResult.ok("TOP-SECRET", metadata={"chars": 17})
            ),
        )
    )
    output = owner.text()
    assert "写入 tasks.txt" in output
    assert "已写入 17 字符" in output
    assert "content=" not in output and "TOP-SECRET" not in output


def test_verbose_tool_renderer_is_detailed_but_redacted():
    owner = _Owner()
    args = {"api_key": "sk-abcdef123456", "value": "safe"}
    renderer = ToolRenderer(owner._console, "verbose")
    renderer.call(
        StepEvent(
            kind="tool_call",
            tool_name="external",
            tool_args=args,
            call_id="call-1234567890",
            display=call_display("external", args),
        )
    )
    output = owner.text()
    assert "external" in output and "call-123456" in output
    assert "safe" in output and "sk-abcdef" not in output
    assert "REDACTED" in output


def test_verbose_result_metadata_is_recursively_redacted():
    owner = _Owner()
    renderer = ToolRenderer(owner._console, "verbose")
    renderer.result(
        StepEvent(
            kind="tool_result",
            text="ok",
            display=result_display("external", {}, ToolResult.ok("ok")),
            result_code="ok",
            result_metadata={"nested": {"api_token": "raw-secret", "count": 2}},
        )
    )
    output = owner.text()
    assert "raw-secret" not in output and "REDACTED" in output


def test_streaming_markdown_renders_fragmented_markup():
    owner = _Owner(width=60)
    renderer = StreamingMarkdownRenderer(owner._console, lambda _live: None)
    for chunk in ("## 标", "题\n\n- **粗", "体**\n- `code`"):
        renderer.append(chunk)
    renderer.finish()
    output = owner.text()
    assert "标题" in output and "粗体" in output and "code" in output
    assert "##" not in output and "**" not in output


def test_streaming_markdown_sanitizes_split_secret_and_ansi():
    owner = _Owner()
    renderer = StreamingMarkdownRenderer(owner._console, lambda _live: None)
    renderer.append("value sk-abc")
    renderer.append("def123456\x1b[31m")
    renderer.finish()
    output = owner.text()
    assert "sk-abcdef" not in output and "REDACTED" in output and "\x1b" not in output


def test_streaming_markdown_can_discard_non_final_segment():
    owner = _Owner()
    renderer = StreamingMarkdownRenderer(
        owner._console,
        lambda _live: None,
        transient=True,
    )
    renderer.append("准备调用工具")
    renderer.finish(commit=False)
    assert owner.text() == ""


def test_conversation_normal_avoids_duplicate_tool_body():
    owner = _Owner()
    args = {"path": "notes.txt"}
    result = ToolResult.ok("文件完整正文", metadata={"start_line": 1, "end_line": 3})
    events = iter(
        [
            StepEvent(
                kind="tool_call",
                tool_name="read_file",
                tool_args=args,
                display=call_display("read_file", args),
            ),
            StepEvent(
                kind="tool_result",
                tool_name="read_file",
                text=result.output,
                display=result_display("read_file", args, result),
            ),
            StepEvent(kind="content_delta", text="**完成**"),
            StepEvent(kind="final", text="**完成**"),
        ]
    )
    ConversationRenderer(owner, "normal", False).render(events)
    output = owner.text()
    assert "读取 notes.txt" in output and "已读取 3 行" in output
    assert "文件完整正文" not in output
    assert "完成" in output and "**" not in output


def test_conversation_normal_discards_tool_progress_but_commits_final_once():
    owner = _Owner()
    args = {"path": "notes.txt"}
    events = iter(
        [
            StepEvent(kind="content_delta", text="我先读取文件。"),
            StepEvent(
                kind="tool_call",
                tool_name="read_file",
                tool_args=args,
                display=call_display("read_file", args),
            ),
            StepEvent(
                kind="tool_result",
                tool_name="read_file",
                display=result_display(
                    "read_file",
                    args,
                    ToolResult.ok("body", metadata={"start_line": 1, "end_line": 1}),
                ),
            ),
            StepEvent(kind="content_delta", text="最终完成。"),
            StepEvent(kind="final", text="最终完成。"),
        ]
    )
    ConversationRenderer(owner, "normal", False).render(events)
    output = owner.text()
    assert "我先读取文件" not in output
    assert "回答" in output
    assert output.count("最终完成") == 1


def test_conversation_verbose_keeps_tool_progress_and_usage():
    owner = _Owner(mode="verbose")
    events = iter(
        [
            StepEvent(kind="content_delta", text="我先读取文件。"),
            StepEvent(kind="tool_call", tool_name="read_file", tool_args={"path": "a.txt"}),
            StepEvent(kind="usage", usage={"prompt_tokens": 10, "completion_tokens": 2}),
            StepEvent(kind="final", text="完成。"),
        ]
    )
    ConversationRenderer(owner, "verbose", False).render(events)
    output = owner.text()
    assert "我先读取文件" in output
    assert "token" in output and "上下文" in output


def test_conversation_quiet_only_prints_final_answer():
    owner = _Owner(mode="quiet")
    events = iter(
        [
            StepEvent(kind="content_delta", text="过程"),
            StepEvent(kind="tool_call", tool_name="read_file", tool_args={"path": "a"}),
            StepEvent(kind="tool_result", text="body"),
            StepEvent(kind="final", text="最终答案"),
        ]
    )
    ConversationRenderer(owner, "quiet", False).render(events)
    assert owner.text().strip() == "最终答案"


def test_confirmation_prompt_is_compact_and_defaults_to_deny(monkeypatch):
    console = Console()
    console._console = RichConsole(record=True, width=120)
    monkeypatch.setattr(console, "input", lambda _prompt: "")

    choice = console.confirm(
        '需要授权：\n- process.execute: del "demo.html"\n风险：进程可能修改文件'
    )

    output = console._console.export_text()
    assert choice == "deny"
    assert "确认执行" in output
    assert "本会话允许" in output and "拒绝（默认）" in output
    assert "⚠" not in output
