"""M11a CLI 展示契约、模式和流式 Markdown。"""

from __future__ import annotations

import sys
from io import StringIO
from threading import Thread
from time import sleep

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output.base import Size
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console as RichConsole

from assistant_agent.agent.events import StepEvent
from assistant_agent.tools.display import call_display, result_display, safe_text
from assistant_agent.tools.result import ToolResult
from assistant_agent.ui.chat_prompt import ChatPrompt, SlashCommandCompleter
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
        self._model_label = "openai/deepseek-v4-pro"
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
    assert call.preview is not None and call.preview.kind == "code"
    assert call.preview.language == "text" and call.preview.total_lines == 2
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


def test_normal_write_renderer_shows_redacted_bounded_preview_and_not_result_body():
    owner = _Owner()
    args = {"path": "tasks.py", "content": "token = 'sk-abcdef123456'\nprint('safe')"}
    call = call_display("write_file", args)
    renderer = ToolRenderer(owner._console, "normal")
    renderer.call(StepEvent(kind="tool_call", tool_name="write_file", tool_args=args, display=call))
    before = owner.text()
    assert "写入 tasks.py" in before and "写入预览 · 2 行" in before
    assert "print('safe')" in before and "sk-abcdef" not in before and "REDACTED" in before
    renderer.result(
        StepEvent(
            kind="tool_result",
            tool_name="write_file",
            text="raw result body",
            display=result_display(
                "write_file", args, ToolResult.ok("raw result body", metadata={"chars": 41})
            ),
        )
    )
    output = owner.text()
    assert "已写入 41 字符" in output and "raw result body" not in output
    assert "content=" not in output


def test_normal_edit_renderer_shows_structured_diff():
    owner = _Owner()
    args = {
        "path": "index.html",
        "old_string": "<h1>Old</h1>\n<p>Keep</p>",
        "new_string": "<h1>New</h1>\n<p>Keep</p>\n<button>Save</button>",
    }
    call = call_display("edit_file", args)
    assert call.preview is not None and call.preview.kind == "diff"
    assert call.preview.added_lines == 2 and call.preview.removed_lines == 1

    renderer = ToolRenderer(owner._console, "normal")
    renderer.call(StepEvent(kind="tool_call", tool_name="edit_file", tool_args=args, display=call))
    html = owner._console.export_html(inline_styles=True, clear=False).lower()
    output = owner.text()
    assert "编辑 index.html" in output and "拟议变更 · +2 -1" in output
    assert "-<h1>Old</h1>" in output and "+<h1>New</h1>" in output
    assert "#183c2b" in html and "#4a2429" in html and "#16181d" in html


def test_normal_write_preview_uses_code_area_background():
    owner = _Owner()
    args = {"path": "demo.py", "content": "value = 1"}
    renderer = ToolRenderer(owner._console, "normal")
    renderer.call(
        StepEvent(
            kind="tool_call",
            tool_name="write_file",
            tool_args=args,
            display=call_display("write_file", args),
        )
    )
    assert "#16181d" in owner._console.export_html(inline_styles=True).lower()


def test_write_preview_is_limited_by_lines():
    preview = call_display(
        "write_file",
        {"path": "long.txt", "content": "\n".join(f"line {i}" for i in range(30))},
    ).preview
    assert preview is not None
    assert preview.total_lines == 30 and preview.shown_lines == 14
    assert "省略 16 行" in preview.content and "line 29" not in preview.content


def test_multi_edit_preview_aggregates_changed_lines_and_redacts_secrets():
    preview = call_display(
        "multi_edit",
        {
            "path": "config.py",
            "edits": [
                {"old_string": "mode = 'old'", "new_string": "mode = 'new'"},
                {
                    "old_string": "token = None",
                    "new_string": "token = 'sk-abcdef123456'",
                },
            ],
        },
    ).preview
    assert preview is not None and preview.kind == "diff"
    assert preview.added_lines == 2 and preview.removed_lines == 2
    assert "config.py#1" in preview.content and "config.py#2" in preview.content
    assert "sk-abcdef" not in preview.content and "REDACTED" in preview.content


def test_write_preview_precedes_permission_confirmation(monkeypatch):
    console = Console()
    console._console = RichConsole(record=True, width=80)
    monkeypatch.setattr(console, "input", lambda _prompt: "3")
    renderer = ToolRenderer(console._console, "normal")
    args = {"path": "outside/demo.py", "content": "print('review me')"}

    renderer.call(
        StepEvent(
            kind="tool_call",
            tool_name="write_file",
            tool_args=args,
            display=call_display("write_file", args),
        )
    )
    assert console.confirm("需要授权：\n- filesystem.write: outside/demo.py") == "deny"

    output = console._console.export_text()
    assert output.index("print('review me')") < output.index("确认执行")


def test_unknown_extension_preview_renders_in_narrow_terminal():
    owner = _Owner(width=40)
    args = {"path": "data.unknown", "content": "a very long value that exceeds the terminal width"}
    renderer = ToolRenderer(owner._console, "normal")
    renderer.call(
        StepEvent(
            kind="tool_call",
            tool_name="write_file",
            tool_args=args,
            display=call_display("write_file", args),
        )
    )
    output = owner.text()
    assert "写入 data.unknown" in output and "a very long value" in output


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
            StepEvent(kind="content_delta", text="接着做一次验证。"),
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
            StepEvent(kind="usage", usage={"prompt_tokens": 1200, "completion_tokens": 80}),
            StepEvent(kind="content_delta", text="最终完成。"),
            StepEvent(kind="final", text="最终完成。"),
        ]
    )
    ConversationRenderer(owner, "normal", False).render(events)
    output = owner.text()
    assert "我先读取文件" in output
    assert "接着做一次验证" not in output
    assert "$ Assistant" in output and "回答" not in output
    assert output.count("最终完成") == 1
    assert "token ↑1200 ↓80 共 1280" in output
    assert "上下文 1200/8000（15%）" in output


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
    console._console = RichConsole(record=True, width=80)
    monkeypatch.setattr(console, "input", lambda _prompt: "")

    choice = console.confirm(
        '需要授权：\n- process.execute: del "demo.html"\n风险：进程可能修改文件'
    )

    output = console._console.export_text()
    assert choice == "deny"
    assert "确认执行" in output
    assert "本会话允许" in output and "拒绝（默认）" in output
    assert "⚠" not in output

    console.user_echo("检查项目")
    assert console.chat_input() == ""
    output = console._console.export_text()
    assert "› 检查项目" in output
    assert "你:" not in output and "你：" not in output
    rules = [line for line in output.splitlines() if line and set(line) == {"─"}]
    assert len(rules) >= 3
    assert all(len(line) == 79 for line in rules[-3:])


def test_scoped_confirmation_exposes_tool_and_server_session_choices(monkeypatch):
    console = Console()
    console._console = RichConsole(record=True, width=100)
    monkeypatch.setattr(console, "input", lambda _prompt: "3")
    choice = console.confirm_scoped(
        "需要授权：\n- mcp.call: playwright/click", "本会话信任 playwright"
    )
    output = console._console.export_text()
    assert choice == "broader"
    assert "本会话允许此工具" in output
    assert "本会话信任 playwright" in output


def test_chat_input_uses_compact_prompt_in_tty(monkeypatch):
    class FakeChatPrompt:
        def read(self):
            return "hello"

    console = Console()
    console._console = RichConsole(record=True, width=64)
    console._chat_prompt = FakeChatPrompt()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert console.chat_input() == "hello"
    output = console._console.export_text()
    assert "› hello" in output
    assert "查看命令" not in output and "Ctrl+C 退出" not in output


def test_chat_prompt_renders_compact_four_line_layout_and_reads_input():
    stdout = StringIO()
    output = Vt100_Output(
        stdout,
        lambda: Size(rows=24, columns=80),
        enable_cpr=False,
    )
    with create_pipe_input() as pipe:
        prompt = ChatPrompt(input=pipe, output=output)
        pipe.send_text("hello\r")
        assert prompt.read() == "hello"

    rendered = stdout.getvalue()
    assert "─" in rendered
    assert "› " in rendered
    assert "? / 查看命令" in rendered
    assert "Ctrl+C 退出" in rendered


def test_chat_prompt_requires_second_ctrl_c_and_shows_exit_hint():
    stdout = StringIO()
    output = Vt100_Output(
        stdout,
        lambda: Size(rows=24, columns=80),
        enable_cpr=False,
    )
    with create_pipe_input() as pipe:

        def press_twice():
            pipe.send_text("\x03")
            sleep(0.05)
            pipe.send_text("\x03")

        sender = Thread(target=press_twice)
        sender.start()
        with pytest.raises(KeyboardInterrupt):
            ChatPrompt(input=pipe, output=output).read()
        sender.join()

    assert stdout.getvalue().count("Ctrl+C") >= 2


def test_chat_prompt_first_ctrl_c_clears_existing_input():
    stdout = StringIO()
    output = Vt100_Output(
        stdout,
        lambda: Size(rows=24, columns=80),
        enable_cpr=False,
    )
    with create_pipe_input() as pipe:
        pipe.send_text("draft\x03hello\r")
        assert ChatPrompt(input=pipe, output=output).read() == "hello"


def test_slash_command_completer_matches_only_leading_command_prefix():
    completer = SlashCommandCompleter(
        [("/clear", "新会话"), ("/context", "查看上下文"), ("/model", "切换模型")]
    )

    def complete(text):
        document = Document(text, cursor_position=len(text))
        return list(completer.get_completions(document, CompleteEvent(text_inserted=True)))

    assert [(item.text, item.display_meta_text) for item in complete("/c")] == [
        ("/clear", "新会话"),
        ("/context", "查看上下文"),
    ]
    assert complete("") == []
    assert complete("hello /") == []
    assert complete("/clear now") == []


def test_chat_prompt_shows_command_menu_and_enter_chooses_current_item():
    stdout = StringIO()
    output = Vt100_Output(
        stdout,
        lambda: Size(rows=24, columns=100),
        enable_cpr=False,
    )
    with create_pipe_input() as pipe:

        def type_command():
            pipe.send_text("/cl")
            sleep(0.05)
            pipe.send_text("\r")

        sender = Thread(target=type_command)
        sender.start()
        result = ChatPrompt(
            [("/clear", "Clear current session"), ("/context", "Show context usage")],
            input=pipe,
            output=output,
        ).read()
        sender.join()

    rendered = stdout.getvalue()
    assert result == "/clear"
    assert "/clear" in rendered and "Clear current session" in rendered


def test_chat_prompt_arrow_key_selects_another_command():
    stdout = StringIO()
    output = Vt100_Output(
        stdout,
        lambda: Size(rows=24, columns=100),
        enable_cpr=False,
    )
    with create_pipe_input() as pipe:

        def select_second_command():
            pipe.send_text("/")
            sleep(0.05)
            pipe.send_text("\x1b[B")
            sleep(0.02)
            pipe.send_text("\r")

        sender = Thread(target=select_second_command)
        sender.start()
        result = ChatPrompt(
            [("/clear", "Clear current session"), ("/context", "Show context usage")],
            input=pipe,
            output=output,
        ).read()
        sender.join()

    assert result == "/context"
