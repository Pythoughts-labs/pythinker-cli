"""Tests for provider-safe media limits (task 7.5)."""

from __future__ import annotations

from pythinker_code.utils.media_limits import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_VIDEO_BYTES,
    format_byte_limit,
)


def test_byte_limits_are_positive_and_ordered() -> None:
    assert MAX_IMAGE_BYTES > 0
    assert MAX_VIDEO_BYTES >= MAX_IMAGE_BYTES
    assert MAX_IMAGE_PIXELS > 0


def test_format_byte_limit_uses_megabytes_for_large_values() -> None:
    assert format_byte_limit(MAX_IMAGE_BYTES) == "20 MB"
