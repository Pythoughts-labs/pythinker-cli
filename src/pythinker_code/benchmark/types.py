from __future__ import annotations

from dataclasses import dataclass

JsonObject = dict[str, object]


@dataclass(frozen=True, slots=True)
class BenchmarkReportRow:
    run: JsonObject
    summary: JsonObject
