from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "pythinker_code"


def _python_files(path: Path) -> list[Path]:
    return sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def test_package_init_is_empty() -> None:
    assert (SRC / "__init__.py").read_text(encoding="utf-8") == ""


def test_cli_has_no_heavy_top_level_pythinker_imports() -> None:
    tree = ast.parse((SRC / "cli" / "__init__.py").read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.level > 0:
                violations.append(
                    f"line {node.lineno}: relative import from {'.' * node.level}{node.module}"
                )
                continue
            if node.module == "pythinker_code.constant":
                continue
            if node.module.startswith(("pythinker_code", "pythinker_core")):
                violations.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(("pythinker_code", "pythinker_core")):
                    violations.append(f"line {node.lineno}: import {alias.name}")
    assert violations == []


def test_text_encoding_is_explicit_in_pythinker_code_sources() -> None:
    violations: list[str] = []
    for path in _python_files(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            if name in {"read_text", "write_text"} and not _has_keyword(node, "encoding"):
                violations.append(f"{_relative(path)}:{node.lineno} missing encoding")
            if (
                name in {"encode", "decode"}
                and not node.args
                and not _has_keyword(node, "encoding")
            ):
                violations.append(f"{_relative(path)}:{node.lineno} implicit {name} encoding")
            if name in {"encode", "decode"} and _constant_encoding(node) not in {None, "utf-8"}:
                violations.append(f"{_relative(path)}:{node.lineno} non-UTF-8 {name} encoding")
            if _subprocess_text_mode_without_encoding(node):
                violations.append(
                    f"{_relative(path)}:{node.lineno} subprocess text missing encoding"
                )
    assert violations == []


def test_tool_decoding_replaces_malformed_utf8() -> None:
    violations: list[str] = []
    tools = SRC / "tools"
    for path in _python_files(tools):
        if path == tools / "utils.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "read_text" and not _has_keyword(node, "errors"):
                violations.append(f"{_relative(path)}:{node.lineno} read_text missing errors")
            if node.func.attr == "decode" and not _has_keyword_value(node, "errors", "replace"):
                violations.append(f"{_relative(path)}:{node.lineno} decode missing errors=replace")
            if node.func.attr == "text" and _looks_like_aiohttp_response(node.func.value):
                violations.append(f"{_relative(path)}:{node.lineno} response.text() missing errors")
    assert violations == []


def test_no_machine_local_debug_paths_in_sources() -> None:
    violations: list[str] = []
    for path in _python_files(SRC):
        text = path.read_text(encoding="utf-8")
        if "/Users/" in text:
            violations.append(f"{_relative(path)} contains a machine-local /Users/ path")
    assert violations == []


def _has_keyword(node: ast.Call, keyword: str) -> bool:
    return any(kw.arg == keyword for kw in node.keywords)


def _constant_encoding(node: ast.Call) -> str | None:
    for kw in node.keywords:
        if kw.arg == "encoding" and isinstance(kw.value, ast.Constant):
            value = kw.value.value
            if isinstance(value, str):
                return value
    if node.args and isinstance(node.args[0], ast.Constant):
        value = node.args[0].value
        if isinstance(value, str):
            return value
    return None


def _subprocess_text_mode_without_encoding(node: ast.Call) -> bool:
    try:
        func = ast.unparse(node.func)
    except Exception:
        return False
    if not (
        func.startswith("subprocess.")
        or func.endswith(".run")
        or func.endswith(".Popen")
        or func.endswith(".check_output")
    ):
        return False
    return _has_keyword_value(node, "text", True) and not _has_keyword(node, "encoding")


def _looks_like_aiohttp_response(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "response"


def _has_keyword_value(node: ast.Call, keyword: str, value: object) -> bool:
    for kw in node.keywords:
        if kw.arg == keyword and isinstance(kw.value, ast.Constant) and kw.value.value == value:
            return True
    return False
