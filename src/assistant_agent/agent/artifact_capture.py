"""原生 ArtifactWriter：把 provider 文本流写入受管草稿，不暴露存储协议。"""

from __future__ import annotations

from assistant_agent.agent.output_validation import (
    OutputValidationResult,
    validate_output_content,
)
from assistant_agent.agent.run.state import PendingOutputCaptureState
from assistant_agent.contracts.outputs import OutputArtifactV1, OutputLimitExceededError
from assistant_agent.tools.ports import OutputStorePort


class ArtifactCaptureWriter:
    def __init__(
        self,
        store: OutputStorePort,
        *,
        session_id: str,
        run_id: str,
        pending: PendingOutputCaptureState,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._run_id = run_id
        self._pending = pending
        self._buffer = ""
        self._chunk_index = 0
        self._size_bytes = 0
        self.validation_result: OutputValidationResult | None = None

    @property
    def size_bytes(self) -> int:
        return self._size_bytes + len(self._buffer.encode("utf-8"))

    def start(self) -> None:
        # 暂停、进程崩溃后的模型会从头生成正文；旧的半文件绝不能与新流拼接。
        self._store.reset_text_draft(
            session_id=self._session_id,
            run_id=self._run_id,
            draft_id=self._pending.draft_id,
        )

    def write(self, text: str) -> None:
        if not text:
            return
        self._buffer += text
        limit = self._pending.max_chunk_bytes
        while len(self._buffer.encode("utf-8")) > limit:
            split = self._largest_prefix(limit)
            if split == 0:
                raise OutputLimitExceededError("输出分块上限小于单个 UTF-8 字符")
            self._append(self._buffer[:split])
            self._buffer = self._buffer[split:]

    def finalize(self) -> OutputArtifactV1:
        if self._buffer:
            self._append(self._buffer)
            self._buffer = ""
        if self._size_bytes == 0:
            raise OutputLimitExceededError("模型未生成文件正文")
        content = self._store.read_text_draft(
            session_id=self._session_id,
            run_id=self._run_id,
            draft_id=self._pending.draft_id,
        )
        self.validation_result = validate_output_content(self._pending.media_type, content)
        return self._store.finalize_text_draft(
            session_id=self._session_id,
            run_id=self._run_id,
            draft_id=self._pending.draft_id,
        )

    def _largest_prefix(self, max_bytes: int) -> int:
        low, high = 0, len(self._buffer)
        while low < high:
            mid = (low + high + 1) // 2
            if len(self._buffer[:mid].encode("utf-8")) <= max_bytes:
                low = mid
            else:
                high = mid - 1
        return low

    def _append(self, content: str) -> None:
        self._size_bytes = self._store.append_text_draft(
            session_id=self._session_id,
            run_id=self._run_id,
            draft_id=self._pending.draft_id,
            chunk_index=self._chunk_index,
            content=content,
        )
        self._chunk_index += 1
