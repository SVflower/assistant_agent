"""紧凑的 Claude 风格聊天输入区。"""

from __future__ import annotations

from typing import Any

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_completions
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

CommandDescription = tuple[str, str]


class SlashCommandCompleter(Completer):
    """仅在输入以 / 开头且仍处于命令名阶段时提供候选。"""

    def __init__(self, commands: list[CommandDescription]) -> None:
        self._commands = commands

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Any:
        text = document.text_before_cursor
        if not text.startswith("/") or any(char.isspace() for char in document.text):
            return
        query = text.casefold()
        for name, description in self._commands:
            if name.casefold().startswith(query):
                yield Completion(
                    name,
                    start_position=-len(text),
                    display=name,
                    display_meta=description,
                    style="class:completion.command",
                    selected_style="class:completion.command.selected",
                )


class ChatPrompt:
    """随对话位置渲染的四行输入组件，不占用终端底部工具栏。"""

    def __init__(
        self,
        commands: list[CommandDescription] | None = None,
        *,
        input: Any = None,
        output: Any = None,
    ) -> None:
        self._history = InMemoryHistory()
        self._commands = list(commands or [])
        self._input = input
        self._output = output

    def set_commands(self, commands: list[CommandDescription]) -> None:
        self._commands = list(commands)

    def read(self) -> str:
        exit_armed = False

        def accept(buffer: Any) -> bool:
            state = buffer.complete_state
            if state is not None and state.current_completion is not None:
                buffer.apply_completion(state.current_completion)
            get_app().exit(result=buffer.text)
            return True

        def footer_hint() -> list[tuple[str, str]]:
            text = "  再次按 Ctrl+C 退出" if exit_armed else "  ? / 查看命令"
            style = "class:hint.exit" if exit_armed else "class:hint"
            return [(style, text)]

        completer = SlashCommandCompleter(self._commands)
        field = TextArea(
            multiline=False,
            height=1,
            prompt=[("class:prompt", "› ")],
            accept_handler=accept,
            history=self._history,
            completer=completer,
            complete_while_typing=True,
        )
        footer = VSplit(
            [
                _label(footer_hint),
                Window(height=1),
                _label("Ctrl+C 退出  "),
            ],
            height=1,
        )
        menu = ConditionalContainer(
            CompletionsMenu(
                max_height=min(max(len(self._commands), 1), 10),
                scroll_offset=1,
                display_arrows=True,
            ),
            filter=has_completions,
        )
        footer_container = ConditionalContainer(footer, filter=~has_completions)
        root = HSplit([_rule(), field, _rule(), menu, footer_container])
        bindings = KeyBindings()

        @bindings.add("c-c")
        def interrupt(event: Any) -> None:
            nonlocal exit_armed
            if event.current_buffer.text:
                event.current_buffer.text = ""
                exit_armed = False
            elif exit_armed:
                event.app.exit(exception=KeyboardInterrupt)
            else:
                exit_armed = True
                event.app.invalidate()

        @bindings.add("c-d")
        def eof(event: Any) -> None:
            event.app.exit(exception=EOFError)

        @bindings.add("enter", filter=has_completions, eager=True)
        def choose_completion(event: Any) -> None:
            state = event.current_buffer.complete_state
            completion = state.current_completion if state is not None else None
            if completion is None:
                document = event.current_buffer.document
                completion = next(
                    completer.get_completions(document, CompleteEvent(completion_requested=True)),
                    None,
                )
            if completion is not None:
                event.current_buffer.apply_completion(completion)
            event.app.exit(result=event.current_buffer.text)

        @bindings.add("down", filter=has_completions, eager=True)
        def next_completion(event: Any) -> None:
            had_selection = (
                event.current_buffer.complete_state is not None
                and event.current_buffer.complete_state.current_completion is not None
            )
            event.current_buffer.complete_next()
            if not had_selection:
                event.current_buffer.complete_next()

        @bindings.add("up", filter=has_completions, eager=True)
        def previous_completion(event: Any) -> None:
            event.current_buffer.complete_previous()

        app: Application[str] = Application(
            layout=Layout(root, focused_element=field),
            style=Style.from_dict(
                {
                    "rule": "#666666",
                    "prompt": "bold #dddddd",
                    "hint": "#777777",
                    "hint.exit": "#aaaaaa",
                    "completion-menu": "bg:default #999999",
                    "completion-menu.completion.current": "bg:#3a3a3a #ffffff",
                    "completion-menu.meta.completion.current": "bg:#3a3a3a #dddddd",
                    "completion.command": "#aaaaaa",
                    "completion.command.selected": "#c0c8ff",
                }
            ),
            full_screen=False,
            erase_when_done=True,
            key_bindings=bindings,
            input=self._input,
            output=self._output,
        )
        return app.run()


def _rule() -> Window:
    return Window(height=1, char="─", style="class:rule")


def _label(text: Any) -> Window:
    content = text if callable(text) else [("class:hint", text)]
    return Window(
        FormattedTextControl(content),
        height=1,
        dont_extend_width=True,
    )
