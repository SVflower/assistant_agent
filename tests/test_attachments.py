from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from assistant_agent.config.schema import AttachmentsConfig, ProviderConfig
from assistant_agent.contracts.attachments import (
    AttachmentPartV1,
    AttachmentUploadV1,
    MessageContentV1,
    TextPartV1,
    UserMessageInputV1,
)
from assistant_agent.contracts.errors import (
    AttachmentInvalidError,
    AttachmentTooLargeError,
    AttachmentUnavailableError,
    UnsupportedInputModalityError,
)
from assistant_agent.persistence.attachments import AttachmentStore
from assistant_agent.providers.content_codec import (
    AttachmentContentCodec,
    ProviderInputCapabilities,
    resolve_input_capabilities,
)


def _store(tmp_path, **changes) -> AttachmentStore:
    config = AttachmentsConfig(**changes)
    return AttachmentStore(tmp_path / "attachments", config)


def _png(*, width: int = 3, height: int = 2) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


def test_text_ingest_normalizes_bom_and_never_exposes_path(tmp_path):
    store = _store(tmp_path)
    summary = store.ingest(
        "session-1",
        [AttachmentUploadV1("hello\r\n世界".encode("utf-16"), "notes.txt", "text/plain")],
    )[0]

    ref = summary.attachment
    assert ref.kind == "text"
    assert ref.encoding == "utf-8"
    assert ref.char_count == len("hello\r\n世界")
    assert summary.approximate_tokens is not None
    assert "path" not in ref.model_dump(mode="json")
    assert store.get(ref).data == "hello\r\n世界".encode()


@pytest.mark.parametrize(
    ("data", "name", "media_type"),
    [
        (b"abc\x00def", "bad.txt", "text/plain"),
        (b"not an image", "bad.png", "image/png"),
        (b"\xff\xfeA", "bad.txt", "text/plain"),
    ],
)
def test_binary_mime_and_encoding_spoof_fail_closed(tmp_path, data, name, media_type):
    with pytest.raises(AttachmentInvalidError):
        _store(tmp_path).ingest("session-1", [AttachmentUploadV1(data, name, media_type)])


def test_image_is_decoded_reencoded_and_integrity_checked(tmp_path):
    store = _store(tmp_path)
    ref = store.ingest("session-1", [AttachmentUploadV1(_png(), "chart.png", "image/png")])[
        0
    ].attachment

    assert (ref.kind, ref.width, ref.height) == ("image", 3, 2)
    item_dir = tmp_path / "attachments" / "session-1" / ref.attachment_id
    (item_dir / "payload.bin").write_bytes(b"tampered")
    with pytest.raises(AttachmentUnavailableError):
        store.get(ref)


def test_limits_and_batch_failure_leave_no_partial_publish(tmp_path, monkeypatch):
    store = _store(tmp_path, max_text_bytes=1024)
    with pytest.raises(AttachmentTooLargeError) as error:
        store.ingest("session-1", [AttachmentUploadV1(b"1" * 1025, "big.txt")])
    assert error.value.resource == "text_bytes"

    normal_store = _store(tmp_path / "atomic")
    original = normal_store._normalize
    calls = 0

    def fail_second(session_id, upload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AttachmentInvalidError("broken")
        return original(session_id, upload)

    monkeypatch.setattr(normal_store, "_normalize", fail_second)
    with pytest.raises(AttachmentInvalidError):
        normal_store.ingest(
            "session-1",
            [AttachmentUploadV1(b"one", "one.txt"), AttachmentUploadV1(b"two", "two.txt")],
        )
    session_dir = tmp_path / "atomic" / "attachments" / "session-1"
    assert not session_dir.exists() or not list(session_dir.iterdir())


def test_binding_unbound_delete_session_and_fork_are_isolated(tmp_path):
    store = _store(tmp_path)
    refs = tuple(
        item.attachment
        for item in store.ingest(
            "source",
            [AttachmentUploadV1(b"keep", "keep.txt"), AttachmentUploadV1(b"drop", "drop.txt")],
        )
    )
    store.bind("source", [refs[0].attachment_id])
    assert store.delete_unbound("source", [item.attachment_id for item in refs]) == 1

    mapping = store.fork("source", "target", [refs[0]])
    cloned = mapping[refs[0].attachment_id]
    assert cloned.attachment_id != refs[0].attachment_id
    assert cloned.session_id == "target"
    assert store.get(cloned).data == store.get(refs[0]).data
    store.delete_session("target")
    assert store.get(refs[0]).data == b"keep"


def test_provider_codec_materializes_transient_text_and_image(tmp_path):
    store = _store(tmp_path)
    text_ref, image_ref = (
        item.attachment
        for item in store.ingest(
            "session-1",
            [
                AttachmentUploadV1(b"alpha", "a.txt"),
                AttachmentUploadV1(_png(), "a.png"),
            ],
        )
    )
    content = MessageContentV1(
        parts=(
            TextPartV1(text="inspect"),
            AttachmentPartV1(attachment=text_ref),
            AttachmentPartV1(attachment=image_ref),
        )
    )
    checkpoint = {"role": "user", "content": content.model_dump(mode="json")}
    assert "base64" not in json.dumps(checkpoint)

    codec = AttachmentContentCodec(store, ProviderInputCapabilities(("text", "image"), "config"))
    materialized = codec.materialize([checkpoint])[0]["content"]
    assert materialized[1]["text"].endswith("---\n")
    assert materialized[2]["image_url"]["url"].startswith("data:image/png;base64,")

    text_only = AttachmentContentCodec(store, ProviderInputCapabilities(("text",), "config"))
    with pytest.raises(UnsupportedInputModalityError):
        text_only.materialize([checkpoint])


def test_capability_resolution_is_explicit_and_unknown_fails_closed(monkeypatch):
    enabled = ProviderConfig(model="x", image_input="enabled")
    assert resolve_input_capabilities(enabled).image is True
    assert resolve_input_capabilities(enabled, policy_allows_image=False).image is False

    import litellm

    monkeypatch.setattr(litellm, "supports_vision", lambda **_kwargs: None)
    unknown = ProviderConfig(model="unknown", image_input="auto")
    assert resolve_input_capabilities(unknown).modalities == ("text",)


def test_user_message_input_has_versioned_content_parts():
    value = UserMessageInputV1.from_text("hello")
    assert value.model_dump(mode="json") == {
        "schema_version": 1,
        "content": {
            "schema_version": 1,
            "parts": [{"type": "text", "text": "hello"}],
        },
    }
