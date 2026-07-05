from __future__ import annotations

import html
import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from html.parser import HTMLParser

SOURCE_URLS = {
    "terminal-bench": "https://www.tbench.ai/",
    "swe-bench": "https://www.swebench.com/",
    "codeclash": "https://codeclash.ai/",
    "deepswe": "https://deepswe.datacurve.ai/",
}

_DIFFICULTY_LABELED_SOURCES = {"terminal-bench"}
_TITLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_ ./:'()&+-]{8,160}")


@dataclass(frozen=True, slots=True)
class DiscoveredBenchmarkTask:
    source: str
    title: str
    difficulty: str
    source_url: str
    trusted: bool
    notes: str

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def discover_benchmark_sources(
    *,
    source: str,
    difficulty: str,
    limit: int,
    fetch_text: Callable[[str], str] | None = None,
) -> list[DiscoveredBenchmarkTask]:
    if source not in SOURCE_URLS:
        raise ValueError(f"Unsupported benchmark source: {source}")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    url = SOURCE_URLS[source]
    text = (fetch_text or _fetch_text)(url)
    return [
        DiscoveredBenchmarkTask(
            source=source,
            title=title,
            difficulty=difficulty,
            source_url=url,
            trusted=False,
            notes=(
                "Discovered from an allowlisted public source. Convert to a runnable "
                "local fixture only after human review."
            ),
        )
        for title in _candidate_titles(text, source=source, difficulty=difficulty)[:limit]
    ]


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "pythinker-benchmark-discovery"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(500_000)
    return raw.decode("utf-8", errors="replace")


def _candidate_titles(text: str, *, source: str, difficulty: str) -> list[str]:
    candidates = _anchor_texts(text)
    if not candidates:
        candidates = _TITLE_RE.findall(html.unescape(re.sub(r"<[^>]+>", " ", text)))
    if source in _DIFFICULTY_LABELED_SOURCES:
        needle = difficulty.casefold()
        candidates = [candidate for candidate in candidates if needle in candidate.casefold()]
    seen: set[str] = set()
    titles: list[str] = []
    for candidate in candidates:
        title = _clean_title(candidate)
        if title and title not in seen:
            seen.add(title)
            titles.append(title)
    return titles


def _anchor_texts(text: str) -> list[str]:
    parser = _AnchorTextParser()
    parser.feed(text)
    return parser.texts


def _clean_title(value: str) -> str:
    title = re.sub(r"\s+", " ", html.unescape(value)).strip()
    return title if len(title) >= 8 and len(title.split()) >= 4 else ""


class _AnchorTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_anchor = False
        self._current: list[str] = []
        self.texts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._in_anchor = True
            self._current = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_anchor:
            text = _clean_title(" ".join(self._current))
            if text:
                self.texts.append(text)
            self._in_anchor = False
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._in_anchor:
            self._current.append(data)
