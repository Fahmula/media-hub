"""Signed, opaque image references handed to the browser.

A reference names an image on one instance (e.g. an Emby item id + image tag) and is signed
with the keyring's HMAC key, so ``/img/...`` can only proxy images MediaHub itself linked to:
the browser never sees an API key and can't make MediaHub fetch arbitrary URLs.
"""

from __future__ import annotations

import re

from mediahub.db.crypto import get_keyring

_PART = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _payload(instance_id: str, item_id: str, tag: str) -> str:
    return f"img/v1/{instance_id}/{item_id}/{tag}"


def valid_part(value: str) -> bool:
    return bool(_PART.fullmatch(value))


def image_url(instance_id: str, item_id: str, tag: str) -> str | None:
    if not (valid_part(instance_id) and valid_part(item_id) and valid_part(tag)):
        return None
    signature = get_keyring().sign(_payload(instance_id, item_id, tag))
    return f"/img/{instance_id}/{item_id}/{tag}/{signature}"


def verify(instance_id: str, item_id: str, tag: str, signature: str) -> bool:
    parts = (instance_id, item_id, tag, signature)
    if not all(valid_part(p) for p in parts):
        return False
    return get_keyring().verify(_payload(instance_id, item_id, tag), signature)
