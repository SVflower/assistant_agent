"""M10a 有界进程捕获和 workspace artifact。"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from assistant_agent.persistence.artifacts import ArtifactStore
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.process import run_bounded_process
from assistant_agent.tools.shell import ShellTool


def _command(code: str) -> str:
    parts = [sys.executable, "-c", code]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def test_dual_stream_capture_is_bounded_and_does_not_deadlock():
    code = "import sys; sys.stdout.write('o'*200000); sys.stderr.write('e'*200000)"
    result = run_bounded_process(
        [sys.executable, "-c", code], shell=False, timeout=10, max_stream_chars=10_000
    )
    assert result.returncode == 0
    assert result.stdout.total_bytes == 200_000
    assert result.stderr.total_bytes == 200_000
    assert len(result.stdout.text) < 11_000
    assert len(result.stderr.text) < 11_000
    assert result.complete is False
    assert "省略" in result.stdout.text


def test_timeout_kills_and_waits_for_process():
    started = time.perf_counter()
    result = run_bounded_process(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        shell=False,
        timeout=0.1,
        max_stream_chars=1_000,
    )
    assert result.timed_out is True
    assert time.perf_counter() - started < 3


def test_shell_large_output_returns_bounded_preview_and_artifact(tmp_path):
    ctx = ToolContext(
        workspace_root=tmp_path,
        artifact_root=tmp_path / "state" / "artifacts" / "tools",
        max_output_chars=1_000,
        max_captured_output_chars=5_000,
        max_artifact_files=10,
    )
    result = ShellTool().run(
        {"command": _command("print('x'*20000)")},
        ctx,
    )
    assert result.code == "ok"
    assert len(result.output) <= 1_000
    assert result.artifacts
    ref = result.artifacts[0]
    assert result.output.startswith(f"[artifact: {ref.path},")
    assert "\\" not in ref.path
    artifact = Path(ref.path)
    assert artifact.is_file()
    assert artifact.resolve().is_relative_to((tmp_path / "state").resolve())
    assert ref.complete is False
    assert artifact.stat().st_size <= 5_100


def test_artifact_store_caps_content_and_prunes_old_files(tmp_path):
    root = tmp_path / "state" / "artifacts" / "tools"
    store = ArtifactStore(tmp_path, max_chars=100, max_files=2, root=root)
    refs = [store.write_text(str(index) * 200, prefix="test") for index in range(3)]
    files = list(root.glob("*.txt"))
    assert len(files) == 2
    assert not Path(refs[0].path).exists()
    assert refs[-1].complete is False
    assert len(Path(refs[-1].path).read_text(encoding="utf-8")) <= 100


def test_artifact_prefix_cannot_escape_workspace(tmp_path):
    store = ArtifactStore(
        tmp_path, max_chars=100, max_files=2, root=tmp_path / "state" / "artifacts"
    )
    ref = store.write_text("ok", prefix="../../outside")
    assert Path(ref.path).is_file()
    assert ".." not in Path(ref.path).parts
