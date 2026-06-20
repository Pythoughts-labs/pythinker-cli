"""Guard that turns post-self-update archive corruption into a restart prompt.

A silent native update replaces the running bundle on disk; the next not-yet-loaded
lazy import then reads a stale archive and raises ``zlib.error``. The CLI boundary
must recognize that signature (frozen build + zlib error in the cause chain) and
surface a clear restart message instead of a fatal traceback — without re-importing
anything from the corrupted archive.
"""

import zlib

import pytest

from pythinker_code.cli import _is_post_update_bundle_corruption


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)


def test_detects_direct_zlib_error_on_frozen_build(frozen):
    assert _is_post_update_bundle_corruption(zlib.error("incorrect header check")) is True


def test_detects_zlib_error_in_cause_chain(frozen):
    try:
        try:
            raise zlib.error("incorrect header check")
        except zlib.error as inner:
            raise ImportError("cannot load module") from inner
    except ImportError as exc:
        assert _is_post_update_bundle_corruption(exc) is True


def test_detects_zlib_error_in_implicit_context(frozen):
    try:
        try:
            raise zlib.error("incorrect header check")
        except zlib.error:
            raise RuntimeError("boom")  # noqa: B904 - implicit __context__ is the point
    except RuntimeError as exc:
        assert _is_post_update_bundle_corruption(exc) is True


def test_ignores_zlib_error_when_not_frozen(monkeypatch):
    monkeypatch.delattr("sys.frozen", raising=False)
    assert _is_post_update_bundle_corruption(zlib.error("incorrect header check")) is False


def test_ignores_unrelated_error_on_frozen_build(frozen):
    assert _is_post_update_bundle_corruption(ValueError("nope")) is False


def test_ignores_zlib_error_with_unrelated_message(frozen):
    # A decompression failure that is NOT the bundle-corruption signature (e.g. a
    # bad gzip HTTP response) must not be masked behind a restart-only message.
    assert _is_post_update_bundle_corruption(zlib.error("invalid distance too far back")) is False


def test_handles_cyclic_cause_chain(frozen):
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a  # cycle: must terminate, not spin
    assert _is_post_update_bundle_corruption(a) is False
