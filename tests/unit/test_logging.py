from __future__ import annotations

from mediahub.logging import REDACTED, redact_secrets


def test_redacts_nested_sensitive_keys() -> None:
    out = redact_secrets(
        None,
        "info",
        {
            "event": "request",
            "headers": {"X-Api-Key": "abc", "Accept": "json"},
            "items": [{"token": "t"}, {"name": "ok"}],
            "password": "pw",
            "url": "http://emby:8096",
        },
    )
    assert out["headers"] == {"X-Api-Key": REDACTED, "Accept": "json"}
    assert out["items"] == [{"token": REDACTED}, {"name": "ok"}]
    assert out["password"] == REDACTED
    assert out["url"] == "http://emby:8096"
