"""Provider-boundary materialization of versioned message content parts."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.application.ports import AttachmentRepository
from assistant_agent.config.schema import ProviderConfig
from assistant_agent.contracts.attachments import AttachmentPartV1, parse_message_content
from assistant_agent.contracts.errors import UnsupportedInputModalityError


@dataclass(frozen=True)
class ProviderInputCapabilities:
    modalities: tuple[Literal["text", "image"], ...]
    source: Literal["policy", "config", "metadata", "unknown"]

    @property
    def image(self) -> bool:
        return "image" in self.modalities


def resolve_input_capabilities(
    provider: ProviderConfig, *, policy_allows_image: bool = True
) -> ProviderInputCapabilities:
    """Resolve without probing endpoints or guessing from model names."""
    if not policy_allows_image:
        return ProviderInputCapabilities(("text",), "policy")
    if provider.image_input == "enabled":
        return ProviderInputCapabilities(("text", "image"), "config")
    if provider.image_input == "disabled":
        return ProviderInputCapabilities(("text",), "config")
    try:
        import litellm

        supports_vision = getattr(litellm, "supports_vision", None)
        if callable(supports_vision) and supports_vision(model=provider.model) is True:
            return ProviderInputCapabilities(("text", "image"), "metadata")
    except Exception:  # noqa: BLE001 - unreliable metadata fails closed
        pass
    return ProviderInputCapabilities(("text",), "unknown")


class AttachmentContentCodec:
    """Turns opaque refs into transient OpenAI-compatible parts for one provider call."""

    def __init__(
        self,
        repository: AttachmentRepository,
        capabilities: ProviderInputCapabilities,
    ) -> None:
        self._repository = repository
        self.capabilities = capabilities

    def materialize(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, dict):
                result.append(dict(message))
                continue
            parsed = parse_message_content(content)
            parts: list[dict[str, Any]] = []
            for part in parsed.parts:
                if part.type == "text":
                    parts.append({"type": "text", "text": part.text})
                    continue
                parts.extend(self._materialize_attachment(part))
            rendered = dict(message)
            rendered["content"] = parts
            result.append(rendered)
        return result

    def _materialize_attachment(self, part: AttachmentPartV1) -> list[dict[str, Any]]:
        payload = self._repository.get(part.attachment)
        ref = payload.ref
        if ref.kind == "image":
            if not self.capabilities.image:
                raise UnsupportedInputModalityError("当前模型不支持图片输入")
            encoded = base64.b64encode(payload.data).decode("ascii")
            return [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{ref.media_type};base64,{encoded}",
                        "detail": part.image_detail,
                    },
                }
            ]
        text = payload.data.decode("utf-8")
        boundary = f"attachment:{ref.attachment_id}:{ref.display_name}"
        return [
            {
                "type": "text",
                "text": f"\n--- BEGIN {boundary} ---\n{text}\n--- END {boundary} ---\n",
            }
        ]
