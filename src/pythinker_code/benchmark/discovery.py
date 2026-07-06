from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from html.parser import HTMLParser

from pythinker_code.benchmark.errors import BenchmarkDiscoveryError

SOURCE_URLS = {
    "terminal-bench": "https://www.tbench.ai/",
    "swe-bench": "https://www.swebench.com/",
    "codeclash": "https://codeclash.ai/",
    "deepswe": "https://deepswe.datacurve.ai/",
}

DISCOVER_NETWORK_ENV = "PYTHINKER_BENCHMARK_DISCOVER_NETWORK"
_ALLOWED_HOSTS = frozenset(urllib.parse.urlparse(url).hostname for url in SOURCE_URLS.values())
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


def quiz_fixture_from_discovery(
    task: DiscoveredBenchmarkTask,
    *,
    question: str,
    expected_substrings: list[str],
) -> dict[str, object]:
    if not expected_substrings:
        raise ValueError("expected_substrings must not be empty")
    return {
        "instance_id": f"online-{task.source}-{_slug(task.title)}",
        "repo": task.source,
        "base_commit": "online-discovery",
        "problem_statement": question,
        "workspace": {"files": {}},
        "verification": {
            "type": "answer_contains",
            "expected_substrings": expected_substrings,
        },
        "trusted": False,
        "source_url": task.source_url,
    }


def _fetch_text(url: str) -> str:
    if os.environ.get(DISCOVER_NETWORK_ENV) != "1":
        raise BenchmarkDiscoveryError(
            f"/benchmark discover network fetch is disabled. Set {DISCOVER_NETWORK_ENV}=1 "
            "after reviewing the allowlisted source hosts."
        )
    host = urllib.parse.urlparse(url).hostname
    if host not in _ALLOWED_HOSTS:
        raise BenchmarkDiscoveryError(f"Unsupported benchmark source host: {host or '(none)'}")
    request = urllib.request.Request(url, headers={"User-Agent": "pythinker-benchmark-discovery"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(500_000)
    except OSError as exc:
        raise BenchmarkDiscoveryError(f"Failed to fetch benchmark source {url}: {exc}") from exc
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


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "task"


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
