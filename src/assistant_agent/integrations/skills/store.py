"""技能发现与加载。

SKILL.md = YAML frontmatter（name/description）+ Markdown 正文。
discover 只读 frontmatter（Level 1，便宜）；get_body 按需读正文（Level 2）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import yaml

SkillSource = Literal["project", "personal", "configured"]
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class SkillMeta:
    """技能元数据（Level 1）。正文按需经 SkillStore.get_body 读取。"""

    name: str
    description: str
    path: Path  # 该技能的 SKILL.md 路径
    source: SkillSource = "configured"
    trusted: bool = False


@dataclass(frozen=True)
class SkillDiscoveryReport:
    invalid: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """拆出 frontmatter dict 与正文。无合法 frontmatter 时返回 ({}, 全文)。"""
    if not text.startswith("---"):
        return {}, text
    # 首行 --- 之后找下一处独占一行的 ---
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, body


class SkillStore:
    """扫描技能目录，持有元数据，按需加载正文。

    发现在构造时一次完成（不热重载）。同名技能：先出现的目录优先
    （main 传入时把项目级放在个人级之前，实现"项目覆盖个人"）。
    """

    def __init__(
        self, metas: dict[str, SkillMeta], report: SkillDiscoveryReport | None = None
    ) -> None:
        self._metas = metas
        self.report = report or SkillDiscoveryReport()

    @classmethod
    def discover(
        cls,
        dirs: list[Path],
        *,
        sources: list[SkillSource] | None = None,
        trusted_names: set[str] | None = None,
    ) -> SkillStore:
        """扫描目录列表，每个 <dir>/<skill-name>/SKILL.md 解析为一条元数据。

        坏文件（缺 name/description、frontmatter 非法）跳过，绝不抛。
        目录不存在直接略过。同名保留先出现者。
        """
        metas: dict[str, SkillMeta] = {}
        if sources is not None and len(sources) != len(dirs):
            raise ValueError("sources 与 dirs 数量必须一致")
        trusted_names = trusted_names or set()
        invalid: list[str] = []
        conflicts: list[str] = []
        for index, base in enumerate(dirs):
            if not base.is_dir():
                continue
            source = sources[index] if sources is not None else "configured"
            for skill_md in sorted(base.glob("*/SKILL.md")):
                meta = _parse_skill_file(
                    skill_md,
                    source=source,
                    trusted=source == "personal",
                )
                if meta is None:
                    invalid.append(str(skill_md))
                    continue
                if meta.name in trusted_names:
                    meta = replace(meta, trusted=True)
                if meta.name in metas:
                    conflicts.append(meta.name)
                    continue
                metas[meta.name] = meta
        return cls(
            metas,
            SkillDiscoveryReport(tuple(sorted(invalid)), tuple(sorted(set(conflicts)))),
        )

    def list(self) -> list[SkillMeta]:
        """按名排序返回所有技能元数据。"""
        return sorted(self._metas.values(), key=lambda m: m.name)

    def get_body(self, name: str) -> str | None:
        """读取指定技能的正文（去 frontmatter）。未知名返回 None。"""
        meta = self._metas.get(name)
        if meta is None:
            return None
        try:
            text = meta.path.read_text(encoding="utf-8")
        except OSError:
            return None
        _, body = _split_frontmatter(text)
        return body.strip()

    def get_meta(self, name: str) -> SkillMeta | None:
        return self._metas.get(name)


def _parse_skill_file(
    path: Path, *, source: SkillSource = "configured", trusted: bool = False
) -> SkillMeta | None:
    """解析单个 SKILL.md 为元数据。缺 name/description 或读失败返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    data, _ = _split_frontmatter(text)
    name = data.get("name")
    description = data.get("description")
    if not isinstance(name, str) or not _VALID_NAME.fullmatch(name.strip()):
        return None
    if not isinstance(description, str) or not description.strip():
        return None
    clean_description = _CONTROL_CHARS.sub(" ", description).strip()[:256]
    if not clean_description:
        return None
    return SkillMeta(
        name=name.strip(),
        description=clean_description,
        path=path,
        source=source,
        trusted=trusted,
    )
