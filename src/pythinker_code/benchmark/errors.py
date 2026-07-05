from __future__ import annotations


class BenchmarkError(Exception):
    """Base class for benchmark command failures."""


class BenchmarkSyntaxError(BenchmarkError):
    """Raised when benchmark slash-command arguments are invalid."""


class UnknownBenchmarkModelError(BenchmarkError):
    """Raised when a requested model key is not configured."""


class UnknownBenchmarkTaskError(BenchmarkError):
    """Raised when a benchmark task id is unknown."""


class UnknownBenchmarkSuiteError(BenchmarkError):
    """Raised when a benchmark suite name is unknown."""


class MalformedBenchmarkTaskError(BenchmarkError):
    """Raised when a task definition is invalid."""


class MalformedBenchmarkSuiteError(BenchmarkError):
    """Raised when a suite definition is invalid."""


class BenchmarkProviderError(BenchmarkError):
    """Raised when the configured model provider fails."""


class BenchmarkRuntimeError(BenchmarkError):
    """Raised when the Pythinker runtime fails during a benchmark."""


class BenchmarkTimeoutError(BenchmarkError):
    """Raised when a benchmark exceeds its configured timeout."""


class BenchmarkCancelledError(BenchmarkError):
    """Raised when a benchmark run is cancelled."""


class BenchmarkVerificationError(BenchmarkError):
    """Raised when deterministic verification fails."""


class BenchmarkInternalError(BenchmarkError):
    """Raised when benchmark harness code fails."""
