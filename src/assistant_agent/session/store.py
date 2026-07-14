"""会话持久化：把对话历史存成 JSON，支持列出/恢复/删除。

Agent Memory 的中期层——跨会话存活。纯存储，不依赖 agent/llm，
主流程（main）负责在 AgentLoop 与本模块之间搬运历史。

存储：每会话一个 JSON，默认位于项目下 ./.assistant_agent/sessions/。
不存 system 消息（含日期/环境，恢复时按当前重建）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(".assistant_agent") / "sessions"
_PREVIEW_LEN = 40
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass
class Session:
    """一次会话：元信息 + 对话历史（不含 system）。"""

    id: str
    created_at: str
    updated_at: str
    provider: str = ""
    model: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    # M8b：摘要 checkpoint（{summary, covered_upto}）；None=未压缩。持久化后 resume 不重复摘要。
    compaction_checkpoint: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provider": self.provider,
            "model": self.model,
            "messages": self.messages,
            "compaction_checkpoint": self.compaction_checkpoint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=data["id"],
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            provider=data.get("provider", ""),
            model=data.get("model", ""),
            messages=data.get("messages", []),
            compaction_checkpoint=data.get("compaction_checkpoint"),
        )

    @property
    def preview(self) -> str:
        """首条用户消息预览，供列表展示。"""
        for m in self.messages:
            if m.get("role") == "user":
                text = str(m.get("content") or "").strip().replace("\n", " ")
                return text[:_PREVIEW_LEN] + ("…" if len(text) > _PREVIEW_LEN else "")
        return "（空会话）"


@dataclass
class SessionMeta:
    """列表展示用的轻量元信息。"""

    id: str
    updated_at: str
    message_count: int
    preview: str


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_session_id() -> str:
    """时间戳 + 短随机后缀：可读、可排序、不撞。"""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class SessionStore:
    """会话文件的存取（save/load/list/delete）。"""

    def __init__(self, base_dir: str | Path = DEFAULT_DIR) -> None:
        self._dir = Path(base_dir)

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("非法会话 ID")
        root = self._dir.resolve()
        path = (root / f"{session_id}.json").resolve()
        if path.parent != root:
            raise ValueError("会话路径超出存储目录")
        return path

    def new_session(self, provider: str = "", model: str = "") -> Session:
        now = _now_iso()
        return Session(
            id=new_session_id(),
            created_at=now,
            updated_at=now,
            provider=provider,
            model=model,
        )

    def save(self, session: Session, messages: list[dict[str, Any]] | None = None) -> None:
        """保存会话（覆盖写整个文件）。可传入最新 messages 一并更新。"""
        if messages is not None:
            session.messages = messages
        session.updated_at = _now_iso()
        self._dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)
        # 用 errors="replace" 编码：模型输出/错误串偶尔含孤代理等无法编码的字符，
        # 保存会话绝不能因此崩溃并带崩整个程序。坏字符替换为占位即可。
        target = self._path(session.id)
        payload = text.encode("utf-8", errors="replace")
        fd, temp_name = tempfile.mkstemp(prefix=f".{session.id}-", suffix=".tmp", dir=self._dir)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def load(self, session_id: str) -> Session:
        """载入指定会话。文件不存在或损坏时抛异常。"""
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"会话不存在：{session_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        session = Session.from_dict(data)
        if session.id != session_id:
            raise ValueError("会话文件 ID 与请求 ID 不一致")
        return session

    def list(self) -> list[SessionMeta]:
        """列出所有会话，按更新时间倒序（最近在前）。"""
        if not self._dir.is_dir():
            return []
        metas: list[SessionMeta] = []
        for path in self._dir.glob("*.json"):
            try:
                session = Session.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, KeyError, OSError):
                continue  # 跳过损坏文件，不让列表整体失败
            metas.append(
                SessionMeta(
                    id=session.id,
                    updated_at=session.updated_at,
                    message_count=len(session.messages),
                    preview=session.preview,
                )
            )
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas

    def delete(self, session_id: str) -> bool:
        """删除指定会话，返回是否删除成功（不存在返回 False）。"""
        path = self._path(session_id)
        if not path.is_file():
            return False
        path.unlink()
        return True
