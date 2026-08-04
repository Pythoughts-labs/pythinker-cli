"""Generate the native Homebrew Formula for pythinker-code from GitHub Releases.

Runs in the homebrew-tap.yml workflow after release assets have been uploaded.
The formula points directly at PyInstaller-built native tarballs attached to the
Pythinker GitHub Release; it does not install from PyPI or enumerate Python
resources.

Usage:
    python generate-formula.py \
        --version 0.27.0 \
        --template packages/homebrew-tap/pythinker-code.rb.tmpl \
        --output Formula/pythinker-code.rb
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GITHUB_REPO = "Pythoughts-labs/pythinker-cli"
GITHUB_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/v{{version}}"


@dataclass(frozen=True)
class NativeTarget:
    key: str
    asset_name_template: str

    def asset_name(self, version: str) -> str:
        return self.asset_name_template.format(version=version)


# Use the onedir PyInstaller artifacts: a onefile binary re-extracts its full
# ~70MB payload to a temp dir on every launch, which makes macOS cold starts
# take 10+ seconds (Gatekeeper re-validates every extracted file each run) and
# is fragile. The onedir build extracts nothing at runtime.
NATIVE_TARGETS = (
    NativeTarget("MACOS_ARM", "pythinker-{version}-aarch64-apple-darwin-onedir.tar.gz"),
    NativeTarget("MACOS_INTEL", "pythinker-{version}-x86_64-apple-darwin-onedir.tar.gz"),
    NativeTarget("LINUX_ARM", "pythinker-{version}-aarch64-unknown-linux-gnu-onedir.tar.gz"),
    NativeTarget("LINUX_X86_64", "pythinker-{version}-x86_64-unknown-linux-gnu-onedir.tar.gz"),
)


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as resp:
        data = json.load(resp)
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected JSON payload from {url}")
    return data


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_sha256_text(text: str) -> str | None:
    match = re.search(r"(?i)\b([a-f0-9]{64})\b", text)
    return match.group(1).lower() if match else None


def _asset_digest_sha256(asset: dict[str, Any]) -> str | None:
    digest = asset.get("digest")
    if not isinstance(digest, str):
        return None
    prefix = "sha256:"
    if not digest.startswith(prefix):
        return None
    sha = digest[len(prefix) :].lower()
    return sha if re.fullmatch(r"[a-f0-9]{64}", sha) else None


def fetch_release_assets(version: str) -> dict[str, dict[str, Any]]:
    release = _fetch_json(GITHUB_RELEASE_API.format(version=version))
    tag_name = release.get("tag_name")
    if tag_name != f"v{version}":
        raise RuntimeError(f"release tag mismatch: expected v{version}, got {tag_name!r}")

    assets: dict[str, dict[str, Any]] = {}
    for asset in release.get("assets", []):
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if isinstance(name, str):
            assets[name] = asset
    return assets


def _asset_url_and_sha(assets: dict[str, dict[str, Any]], asset_name: str) -> tuple[str, str]:
    asset = assets.get(asset_name)
    if asset is None:
        raise RuntimeError(f"release asset missing: {asset_name}")
    url = asset.get("browser_download_url")
    if not isinstance(url, str) or not url:
        raise RuntimeError(f"release asset {asset_name} has no browser_download_url")

    sha = _asset_digest_sha256(asset)
    if sha is not None:
        return url, sha

    sha_asset = assets.get(asset_name + ".sha256")
    if sha_asset is None:
        raise RuntimeError(f"release asset checksum missing: {asset_name}.sha256")
    sha_url = sha_asset.get("browser_download_url")
    if not isinstance(sha_url, str) or not sha_url:
        raise RuntimeError(f"release asset checksum {asset_name}.sha256 has no download URL")
    sha = _parse_sha256_text(_fetch_text(sha_url))
    if sha is None:
        raise RuntimeError(f"could not parse SHA-256 for {asset_name}")
    return url, sha


def native_replacements(version: str, assets: dict[str, dict[str, Any]]) -> dict[str, str]:
    replacements = {"__VERSION__": version}
    for target in NATIVE_TARGETS:
        url, sha = _asset_url_and_sha(assets, target.asset_name(version))
        replacements[f"__{target.key}_URL__"] = url
        replacements[f"__{target.key}_SHA256__"] = sha
    return replacements


def render_formula(template: str, replacements: dict[str, str]) -> str:
    formula = template
    for placeholder, value in replacements.items():
        formula = formula.replace(placeholder, value)
    leftovers = sorted(set(re.findall(r"__[A-Z0-9_]+__", formula)))
    if leftovers:
        raise RuntimeError(f"unresolved template placeholders: {', '.join(leftovers)}")
    return formula


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--template", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    assets = fetch_release_assets(args.version)
    replacements = native_replacements(args.version, assets)
    formula = render_formula(args.template.read_text(encoding="utf-8"), replacements)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(formula, encoding="utf-8")

    digest = hashlib.sha256(formula.encode("utf-8")).hexdigest()
    print(f"formula written to {args.output}")
    print(f"version      : {args.version}")
    print(f"native assets: {len(NATIVE_TARGETS)}")
    print(f"formula sha  : {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
