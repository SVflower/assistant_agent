"""Shell/Git 共用的进程结果格式化。"""

from __future__ import annotations

from typing import Any

from assistant_agent.tools.ports import ProcessResultPort


def format_process_result(
    result: ProcessResultPort,
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
        "termination_reason": result.termination_reason.value,
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


def _bounded_preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "\n[…输出预览已省略中间内容…]\n"
    if limit <= len(marker):
        return marker[:limit]
    keep = limit - len(marker)
    head = keep // 2
    return value[:head] + marker + value[-(keep - head) :]


__all__ = ["format_process_result"]
