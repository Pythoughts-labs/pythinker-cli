"""Dependency-manifest parsing and OSV-backed enrichment for Pythinker Security Scan."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pythinker_review.security_intel.models import DependencyIntel, PackageRef
from pythinker_review.security_intel.service import scan_packages
from pythinker_review.security_scan.paths import data_dir

_VERSION_PREFIX_RE = re.compile(r"^[\^~>=<\s]+")
_REQUIREMENT_RE = re.compile(
    r"^([A-Za-z0-9_.\-]+(?:\[[A-Za-z0-9_,]+\])?)\s*(==|>=|<=|~=|!=|>|<)\s*([A-Za-z0-9_.\-+*,<>=]+)"
)
_BARE_REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9_.\-]+(?:\[[A-Za-z0-9_,]+\])?)\s*$")


class DependencyScanReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(alias="projectId")
    package_count: int = Field(alias="packageCount", ge=0)
    vulnerable_count: int = Field(alias="vulnerableCount", ge=0)
    dependencies: list[DependencyIntel]
    source_errors: list[str] = Field(default_factory=list, alias="sourceErrors")


def parse_dependency_manifests(root: Path) -> list[PackageRef]:
    """Parse dependency manifests at the given root directory (non-recursive).

    Only manifest files directly under *root* are considered. Manifests in
    subdirectories are not discovered; callers that need recursive discovery
    should walk the tree and call this function (or the individual helpers) per
    directory.
    """
    packages: list[PackageRef] = []
    for rel in ("requirements.txt", "requirements-dev.txt"):
        path = root / rel
        if path.exists():
            packages.extend(_parse_requirements(path, rel))
    package_json = root / "package.json"
    if package_json.exists():
        packages.extend(_parse_package_json(package_json, "package.json"))
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        packages.extend(_parse_pyproject(pyproject, "pyproject.toml"))
    pom = root / "pom.xml"
    if pom.exists():
        packages.extend(_parse_pom_xml(pom, "pom.xml"))
    return _dedupe(packages)


async def scan_project_dependencies(
    *, project_id: str, root: Path, data_root: Path
) -> DependencyScanReport:
    packages = parse_dependency_manifests(root)
    errors: list[str] = []
    try:
        vulnerable = await scan_packages(packages, data_root=data_root)
    except Exception as exc:  # noqa: BLE001 - surfaced in report instead of crashing local scans
        vulnerable = []
        errors.append(f"OSV lookup failed: {type(exc).__name__}: {exc}")
    report = DependencyScanReport.model_validate(
        {
            "projectId": project_id,
            "packageCount": len(packages),
            "vulnerableCount": len(vulnerable),
            "dependencies": [item.model_dump() for item in vulnerable],
            "sourceErrors": errors,
        }
    )
    write_dependency_report(report, data_root=data_root)
    return report


def write_dependency_report(report: DependencyScanReport, *, data_root: Path) -> Path:
    path = dependency_report_path(report.project_id, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8")
    return path


def read_dependency_report(project_id: str, *, data_root: Path) -> DependencyScanReport | None:
    path = dependency_report_path(project_id, data_root=data_root)
    if not path.exists():
        return None
    try:
        return DependencyScanReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def dependency_report_path(project_id: str, *, data_root: Path) -> Path:
    return data_dir(project_id, data_root=data_root) / "dependencies.json"


def _parse_requirements(path: Path, rel: str) -> list[PackageRef]:
    out: list[PackageRef] = []
    for lineno, line in enumerate(_read_text(path).splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        match = _REQUIREMENT_RE.match(stripped)
        if match:
            version = _clean_version(match.group(3)) if match.group(2) == "==" else ""
            out.append(
                PackageRef(
                    name=match.group(1).split("[", 1)[0],
                    ecosystem="PyPI",
                    version=version,
                    manifest_path=rel,
                    line=lineno,
                )
            )
            continue
        bare = _BARE_REQUIREMENT_RE.match(stripped)
        if bare:
            out.append(
                PackageRef(
                    name=bare.group(1).split("[", 1)[0],
                    ecosystem="PyPI",
                    manifest_path=rel,
                    line=lineno,
                )
            )
    return out


def _parse_package_json(path: Path, rel: str) -> list[PackageRef]:
    try:
        package = json.loads(_read_text(path))
    except json.JSONDecodeError:
        return []
    if not isinstance(package, dict):
        return []
    line_by_name = _line_index(path)
    out: list[PackageRef] = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = package.get(key, {})
        if not isinstance(deps, dict):
            continue
        for name, raw_version in deps.items():
            version = _clean_version(str(raw_version))
            out.append(
                PackageRef(
                    name=name,
                    ecosystem="npm",
                    version=version,
                    manifest_path=rel,
                    line=line_by_name.get(name),
                )
            )
    return out


def _parse_pyproject(path: Path, rel: str) -> list[PackageRef]:
    try:
        project = tomllib.loads(_read_text(path))
    except tomllib.TOMLDecodeError:
        return []
    out: list[PackageRef] = []
    deps = project.get("project", {}).get("dependencies", [])
    if isinstance(deps, list):
        out.extend(_python_dep_refs(deps, rel, path))
    optional = project.get("project", {}).get("optional-dependencies", {})
    if isinstance(optional, dict):
        for values in optional.values():
            if isinstance(values, list):
                out.extend(_python_dep_refs(values, rel, path))
    poetry_deps = project.get("tool", {}).get("poetry", {}).get("dependencies", {})
    if isinstance(poetry_deps, dict):
        for name, raw_version in poetry_deps.items():
            if name.lower() == "python":
                continue
            version = _clean_version(str(raw_version)) if isinstance(raw_version, str) else ""
            out.append(
                PackageRef(
                    name=name,
                    ecosystem="PyPI",
                    version=version,
                    manifest_path=rel,
                    line=_find_line(path, name),
                )
            )
    return out


def _parse_pom_xml(path: Path, rel: str) -> list[PackageRef]:
    # Minimal Maven parser without external XML dependencies. It intentionally ignores complex
    # property resolution and only extracts direct dependency coordinates.
    text = _read_text(path)
    out: list[PackageRef] = []
    for match in re.finditer(r"<dependency>(.*?)</dependency>", text, flags=re.DOTALL):
        block = match.group(1)
        group = _xml_text(block, "groupId")
        artifact = _xml_text(block, "artifactId")
        version = _xml_text(block, "version")
        if group and artifact:
            if version.startswith("${"):
                version = ""
            line = text[: match.start()].count("\n") + 1
            out.append(
                PackageRef(
                    name=f"{group}:{artifact}",
                    ecosystem="Maven",
                    version=version,
                    manifest_path=rel,
                    line=line,
                )
            )
    return out


def _python_dep_refs(items: list[Any], rel: str, path: Path) -> list[PackageRef]:
    out: list[PackageRef] = []
    for item in items:
        if not isinstance(item, str):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^]]+\])?\s*([><=!~]+)?\s*([^;\s]+)?", item)
        if not match:
            continue
        name = match.group(1)
        operator = match.group(2) or ""
        version = _clean_version(match.group(3) or "") if operator == "==" else ""
        out.append(
            PackageRef(
                name=name,
                ecosystem="PyPI",
                version=version,
                manifest_path=rel,
                line=_find_line(path, name),
            )
        )
    return out


def _clean_version(raw: str) -> str:
    value = _VERSION_PREFIX_RE.sub("", raw).strip().strip("\"'")
    return "" if value in {"", "*", "latest"} else value


def _xml_text(block: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL)
    return match.group(1).strip() if match else ""


def _line_index(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for lineno, line in enumerate(_read_text(path).splitlines(), start=1):
        match = re.search(r'"([^"\\]+)"\s*:', line)
        if match:
            out.setdefault(match.group(1), lineno)
    return out


def _find_line(path: Path, needle: str) -> int | None:
    pattern = re.compile(r"\b" + re.escape(needle) + r"\b")
    for lineno, line in enumerate(_read_text(path).splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith(("#", "//")):
            continue
        if pattern.search(line):
            return lineno
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _dedupe(packages: list[PackageRef]) -> list[PackageRef]:
    seen: set[tuple[str, str, str]] = set()
    out: list[PackageRef] = []
    for package in packages:
        key = (package.ecosystem.lower(), package.name.lower(), package.version)
        if key in seen:
            continue
        seen.add(key)
        out.append(package)
    return out
