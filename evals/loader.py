"""Eval case 发现、校验与 fixture workspace 安全构造。"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml
from pydantic import ValidationError

from evals.schema import EvalCase


class EvalLoadError(ValueError):
    pass


def safe_relative_path(value: str) -> Path:
    normalized = value.replace("\\", "/")
    raw_parts = normalized.split("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise EvalLoadError(f"eval 路径必须是无 '.'/'..' 的相对路径：{value!r}")
    return Path(*path.parts)


def confined_path(root: Path, value: str, *, label: str = "eval") -> Path:
    """解析受限相对路径，并拒绝解析后逃出 root（包括符号链接）。"""
    resolved_root = root.resolve()
    target = (resolved_root / safe_relative_path(value)).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise EvalLoadError(f"{label} 路径逃逸：{value}")
    return target


def _documents(path: Path) -> list[object]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EvalLoadError(f"无法读取 eval case {path}：{exc}") from exc
    if isinstance(loaded, list):
        return loaded
    return [loaded]


def load_cases(path: str | Path) -> list[EvalCase]:
    root = Path(path)
    files = sorted(root.glob("*.yaml")) if root.is_dir() else [root]
    if not files or any(not file.is_file() for file in files):
        raise EvalLoadError(f"没有可读取的 eval case：{root}")
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for file in files:
        for index, raw in enumerate(_documents(file), start=1):
            try:
                case = EvalCase.model_validate(raw)
            except ValidationError as exc:
                raise EvalLoadError(f"case 校验失败 {file}#{index}：\n{exc}") from exc
            if case.id in seen:
                raise EvalLoadError(f"重复 case id：{case.id}")
            seen.add(case.id)
            for fixture_path in case.fixture.files:
                safe_relative_path(fixture_path)
            for expected_path in case.expect.files:
                safe_relative_path(expected_path)
            cases.append(case)
    return cases


@contextmanager
def fixture_workspace(case: EvalCase) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=f"assistant-agent-eval-{case.id}-") as raw_root:
        root = Path(raw_root).resolve()
        for relative, content in case.fixture.files.items():
            target = confined_path(root, relative, label="fixture")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        yield root
