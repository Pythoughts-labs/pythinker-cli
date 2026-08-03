"""Generate the Scoop manifest for pythinker-code from GitHub Releases.

Runs in scoop-bucket.yml after the Windows onedir zip is attached to the
Pythinker GitHub Release. Points at the existing
pythinker-{version}-x86_64-pc-windows-msvc-onedir.zip asset produced by
release-pythinker-cli.yml; it does not enumerate macOS/Linux native targets.

Usage:
    python generate-manifest.py \
        --version 0.27.0 \
        --template packages/scoop-bucket/pythinker-code.json.tmpl \
        --output bucket/pythinker-code.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

GITHUB_REPO = "Pythoughts-labs/pythinker-cli"
GITHUB_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/v{{version}}"


def windows_zip_asset_name(version: str) -> str:
    """Exact Windows onedir zip name from release-pythinker-cli.yml."""
    return f"pythinker-{version}-x86_64-pc-windows-msvc-onedir.zip"


def _github_headers(url: str, *, accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": "pythinker-scoop-manifest-generator"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlparse(url).netloc == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_github_headers(url))
    with urllib.request.urlopen(request, timeout=30) as resp:
        data = json.load(resp)
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected JSON payload from {url}")
    return data


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers=_github_headers(url, accept="text/plain"))
    with urllib.request.urlopen(request, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_sha256_text(text: str) -> str | None:
    match = re.search(r"(?i)\b([a-f0-9]{64})\b", text)
    return match.group(1).lower() if match else None


def _asset_digest_sha256(asset: dict[str, Any]) -> str | None:
    digest = asset.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    sha = digest[len("sha256:") :].lower()
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


def manifest_replacements(version: str, assets: dict[str, dict[str, Any]]) -> dict[str, str]:
    url, sha = _asset_url_and_sha(assets, windows_zip_asset_name(version))
    return {"__VERSION__": version, "__URL__": url, "__SHA256__": sha}


def render_manifest(template: str, replacements: dict[str, str]) -> str:
    manifest = template
    for placeholder, value in replacements.items():
        manifest = manifest.replace(placeholder, value)
    leftovers = sorted(set(re.findall(r"__[A-Z0-9_]+__", manifest)))
    if leftovers:
        raise RuntimeError(f"unresolved template placeholders: {', '.join(leftovers)}")
    json.loads(manifest)
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--template", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    assets = fetch_release_assets(args.version)
    replacements = manifest_replacements(args.version, assets)
    manifest = render_manifest(args.template.read_text(encoding="utf-8"), replacements)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(manifest, encoding="utf-8")

    digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    print(f"manifest written to {args.output}")
    print(f"version     : {args.version}")
    print(f"manifest sha: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
