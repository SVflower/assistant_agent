"""Shell/Git 共用的双流并发 drain 与有界进程结果。"""

from __future__ import annotations

import locale
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import IO, Any


@dataclass(frozen=True)
class CapturedStream:
    text: str
    total_bytes: int
    complete: bool


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: CapturedStream
    stderr: CapturedStream
    timed_out: bool = False

    @property
    def complete(self) -> bool:
        return self.stdout.complete and self.stderr.complete


class _Collector:
    def __init__(self, limit: int) -> None:
        self.limit = max(limit, 1)
        self.head_limit = self.limit // 2
        self.tail_limit = self.limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        remaining_head = self.head_limit - len(self.head)
        if remaining_head > 0:
            self.head.extend(chunk[:remaining_head])
            chunk = chunk[remaining_head:]
        if chunk and self.tail_limit > 0:
            self.tail.extend(chunk)
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    def finish(self) -> CapturedStream:
        complete = self.total <= self.limit
        if complete:
            raw = bytes(self.head + self.tail)
        else:
            omitted = self.total - len(self.head) - len(self.tail)
            marker = f"\n[…省略 {omitted} bytes…]\n".encode()
            raw = bytes(self.head) + marker + bytes(self.tail)
        return CapturedStream(_decode(raw), self.total, complete)


def run_bounded_process(
    command: str | list[str],
    *,
    shell: bool,
    timeout: float,
    max_stream_chars: int,
    cwd: str | None = None,
) -> BoundedProcessResult:
    process = subprocess.Popen(
        command,
        shell=shell,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout = _Collector(max_stream_chars)
    stderr = _Collector(max_stream_chars)
    threads = [
        threading.Thread(target=_drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        returncode = process.wait()
    finally:
        for thread in threads:
            thread.join()
    return BoundedProcessResult(returncode, stdout.finish(), stderr.finish(), timed_out)


def format_process_result(
    result: BoundedProcessResult,
    *,
    artifact_writer: Any,
    artifact_prefix: str,
    inline_limit: int,
) -> tuple[str, list[Any], dict[str, Any]]:
    parts = [f"退出码：{result.returncode}"]
    if result.stdout.text:
        parts.append(f"stdout:\n{result.stdout.text.rstrip()}")
    if result.stderr.text:
        parts.append(f"stderr:\n{result.stderr.text.rstrip()}")
    full = "\n".join(parts)
    source_complete = result.complete
    needs_artifact = not source_complete or (inline_limit > 0 and len(full) > inline_limit)
    artifacts: list[Any] = []
    metadata = {
        "returncode": result.returncode,
        "stdout_bytes": result.stdout.total_bytes,
        "stderr_bytes": result.stderr.total_bytes,
        "source_complete": source_complete,
        "timed_out": result.timed_out,
    }
    if not needs_artifact:
        return full, artifacts, metadata
    artifact = artifact_writer(full, prefix=artifact_prefix, complete=source_complete)
    artifacts.append(artifact)
    reference = (
        f"[artifact: {artifact.path}, chars={artifact.size_chars}, "
        f"complete={str(artifact.complete).lower()}]"
    )
    if inline_limit > 0:
        preview_limit = max(inline_limit - len(reference) - 1, 0)
        preview = _bounded_preview(full, preview_limit)
        output = f"{reference}\n{preview}" if preview else reference[:inline_limit]
    else:
        output = f"{reference}\n{full}"
    metadata["artifact_complete"] = artifact.complete
    return output, artifacts, metadata


def _drain(stream: IO[bytes], collector: _Collector) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            collector.add(chunk)
    finally:
        stream.close()


def _bounded_preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "\n[…输出预览已省略中间内容…]\n"
    if limit <= len(marker):
        return marker[:limit]
    keep = limit - len(marker)
    head = keep // 2
    return value[:head] + marker + value[-(keep - head) :]


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    encodings = ["utf-8"]
    if sys.platform == "win32":
        encodings.append(locale.getpreferredencoding(False))
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(encodings[-1], errors="replace")
