"""Provider-safe media attachment limits (task 7.5)."""

from __future__ import annotations

# Conservative defaults aligned with common multimodal provider caps.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_PDF_BYTES = 32 * 1024 * 1024


def format_byte_limit(limit_bytes: int) -> str:
    """Human-readable size for user-facing errors."""
    if limit_bytes >= 1024 * 1024:
        return f"{limit_bytes // (1024 * 1024)} MB"
    return f"{limit_bytes // 1024} KB"
