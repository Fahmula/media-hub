from __future__ import annotations

import os
from pathlib import Path

from mediahub.integrations import image_refs
from mediahub.web.image_cache import ImageCache


def test_image_refs_sign_and_verify() -> None:
    url = image_refs.image_url("inst-1", "123", "abc_456")
    assert url is not None
    *_, signature = url.split("/")
    assert image_refs.verify("inst-1", "123", "abc_456", signature)
    assert not image_refs.verify("inst-2", "123", "abc_456", signature)
    assert not image_refs.verify("inst-1", "124", "abc_456", signature)


def test_image_refs_reject_unsafe_parts() -> None:
    assert image_refs.image_url("inst", "../etc", "tag") is None
    assert image_refs.image_url("inst", "1", "a/b") is None
    assert not image_refs.verify("inst", "1", "tag", "../sig")


def test_image_cache_round_trip_and_eviction(tmp_path: Path) -> None:
    cache = ImageCache(tmp_path, max_bytes=250)
    cache.put("a", b"x" * 100, "image/jpeg")
    assert cache.get("a") == (b"x" * 100, "image/jpeg")
    assert cache.get("missing") is None
    old = cache._path("a")
    os.utime(old, (1, 1))  # make "a" the least recently used
    cache.put("b", b"y" * 100, "image/png")
    cache.put("c", b"z" * 100, "image/png")
    assert cache.get("a") is None
    assert cache.get("b") is not None and cache.get("c") is not None


def test_image_cache_ignores_corrupt_files(tmp_path: Path) -> None:
    cache = ImageCache(tmp_path)
    tmp_path.joinpath(cache._path("k").name).write_bytes(b"text/html\n<script>")
    assert cache.get("k") is None
