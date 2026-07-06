from __future__ import annotations

from pythinker_code.benchmark.redact import redact_text


def test_redact_text_covers_common_secret_shapes() -> None:
    text = "\n".join(
        [
            "password=hunter2",
            "aws=AKIAIOSFODNN7EXAMPLE",
            "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----",
        ]
    )

    redacted = redact_text(text)

    assert "hunter2" not in redacted
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted
    assert "abc123" not in redacted
