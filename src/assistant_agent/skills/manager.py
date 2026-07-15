"""Skill 的 user/project 安装与安全卸载。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from assistant_agent.config.paths import project_skills_dir, user_skills_dir
from assistant_agent.skills.store import _parse_skill_file

SkillScope = Literal["user", "project"]
_MANIFEST = ".assistant-agent-install.json"
_MAX_FILES = 1000
_MAX_BYTES = 10_000_000


class SkillInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillInstallResult:
    name: str
    scope: SkillScope
    path: Path
    changed: bool


class SkillManager:
    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()

    def root(self, scope: SkillScope) -> Path:
        return user_skills_dir() if scope == "user" else project_skills_dir(self.workspace_root)

    def install(self, source: Path, scope: SkillScope = "user") -> SkillInstallResult:
        source = source.expanduser().resolve()
        meta = _parse_skill_file(source / "SKILL.md", source="configured")
        if meta is None:
            raise SkillInstallError("来源目录缺少合法 SKILL.md（需要 name 和 description）")
        digest, files, total = _tree_digest(source)
        if len(files) > _MAX_FILES or total > _MAX_BYTES:
            raise SkillInstallError(f"Skill 超过安装上限：{len(files)} files / {total} bytes")
        root = self.root(scope)
        target = (root / meta.name).resolve()
        if target.parent != root.resolve():
            raise SkillInstallError("Skill 安装路径逃逸")
        existing = _read_manifest(target)
        if target.exists():
            if existing and existing.get("digest") == digest:
                return SkillInstallResult(meta.name, scope, target, False)
            raise SkillInstallError(f"目标已存在且不是同一受管版本：{target}")

        root.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=f".{meta.name}-", dir=root))
        try:
            for relative in files:
                src = source / relative
                dst = temp / relative
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            manifest = {
                "version": 1,
                "name": meta.name,
                "scope": scope,
                "source": str(source),
                "digest": digest,
            }
            (temp / _MANIFEST).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp, target)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        return SkillInstallResult(meta.name, scope, target, True)

    def uninstall(self, name: str, scope: SkillScope = "user") -> bool:
        root = self.root(scope).resolve()
        target = (root / name).resolve()
        if target.parent != root:
            raise SkillInstallError("Skill 卸载路径逃逸")
        manifest = _read_manifest(target)
        if manifest is None or manifest.get("name") != name or manifest.get("scope") != scope:
            raise SkillInstallError("拒绝删除非受管 Skill 目录")
        shutil.rmtree(target)
        return True


def _tree_digest(root: Path) -> tuple[str, list[Path], int]:
    digest = hashlib.sha256()
    files: list[Path] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SkillInstallError(f"Skill 不允许包含符号链接：{path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        data = path.read_bytes()
        total += len(data)
        files.append(relative)
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest(), files, total


def _read_manifest(target: Path) -> dict[str, object] | None:
    try:
        data = json.loads((target / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
