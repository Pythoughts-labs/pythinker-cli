import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return project["project"]["version"]


def test_unix_readme_installer_uses_canonical_native_endpoint() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "curl -fsSL https://pythinker.com/install.sh | bash" in readme
    assert "scripts/install.sh | bash" not in readme
    assert "scripts/install.sh | sh" not in readme


def test_windows_readme_documents_powershell_one_liner() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "irm https://pythinker.com/install.ps1 \\| iex" in readme
    assert (
        "raw.githubusercontent.com/Pythoughts-labs/pythinker-code/main/scripts/install.ps1"
        not in readme
    )
    assert "-File $installer" not in readme


def test_getting_started_uses_canonical_native_installer_url() -> None:
    guide = (ROOT / "docs" / "en" / "guides" / "getting-started.md").read_text()

    assert "https://code.pythinker.com/install.sh" not in guide
    assert "https://code.pythinker.com/install.ps1" not in guide
    assert "curl -fsSL https://pythinker.com/install.sh | bash" in guide
    assert "irm https://pythinker.com/install.ps1 | iex" in guide
    assert (
        "https://raw.githubusercontent.com/Pythoughts-labs/pythinker-code/main/scripts/install.ps1"
    ) not in guide


def test_windows_installer_bootstrap_downloads_native_setup() -> None:
    installer = (ROOT / "scripts" / "install.ps1").read_text()

    assert "PythinkerSetup-$Version.exe" in installer
    assert "Get-FileHash -Algorithm SHA256" in installer
    assert "Start-Process -FilePath $installerPath" in installer
    assert "'/CURRENTUSER'" in installer
    assert "'/SILENT'" in installer
    assert "'/CLOSEAPPLICATIONS'" in installer
    assert "'/VERYSILENT'" not in installer
    assert "'/SUPPRESSMSGBOXES'" not in installer
    assert "uv tool install" not in installer


def test_readme_downloads_rpm_before_local_install() -> None:
    readme = (ROOT / "README.md").read_text()
    version = project_version()

    rpm = f"pythinker-code-{version}.x86_64.rpm"
    checksum = f"{rpm}.sha256"
    release_url = f"https://github.com/Pythoughts-labs/pythinker-code/releases/download/v{version}"

    assert f"curl -LO {release_url}/{rpm}" in readme
    assert f"curl -LO {release_url}/{checksum}" in readme
    assert f"sha256sum -c {checksum}" in readme
    rpm_block = readme[readme.index(f"curl -LO {release_url}/{rpm}") :]
    assert rpm_block.index(f"curl -LO {release_url}/{rpm}") < rpm_block.index(
        f"sudo dnf install ./{rpm}"
    )


def test_quick_start_standardizes_on_hosted_native_installers() -> None:
    readme = (ROOT / "README.md").read_text()

    assert not (ROOT / "scripts" / "install.sh").exists()
    assert "**🪟 Windows** | `irm https://pythinker.com/install.ps1 \\| iex`" in readme
    assert (
        'alt="macOS"> / <img src="https://img.shields.io/badge/-Linux-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux">** | `curl -fsSL https://pythinker.com/install.sh \\| bash`'
        in readme
    )
    assert "uvx pythinker-code" not in readme
    assert "uv tool install pythinker-code" not in readme
    assert "pipx install pythinker-code" not in readme


def test_native_curl_installer_shows_robot_logo() -> None:
    installer = (ROOT / "scripts" / "install-native.sh").read_text()

    assert "print_logo_static()" in installer
    assert "print_logo_animated()" in installer
    assert "print_logo_art()" in installer
    assert "GRID_ORIGIN_ROW=5" in installer
    assert "PROGRESS_ROW=17" in installer
    assert "Pythinker Code" in installer
    assert "Think first. Then code." in installer
    assert "Ready. Start with:" in installer


def test_native_powershell_installer_shows_robot_logo() -> None:
    installer = (ROOT / "scripts" / "install.ps1").read_text()

    assert "function Write-LogoStatic" in installer
    assert "function Write-LogoAnimated" in installer
    assert "function Write-Logo" in installer
    assert "pythinker code" in installer
    assert "is ready. Open a fresh PowerShell and run" in installer
    assert "to launch." in installer


def test_native_powershell_installer_is_parseable_when_pwsh_is_available() -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        return
    installer = (ROOT / "scripts" / "install.ps1").resolve()
    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-Command",
            (
                "$errs = $null;"
                f"[System.Management.Automation.Language.Parser]::ParseFile('{installer}',"
                " [ref]$null, [ref]$errs) | Out-Null;"
                " if ($errs) { $errs | ForEach-Object { $_.Message }; exit 1 }"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_shell_installers_are_parseable_when_bash_is_available() -> None:
    bash = shutil.which("bash")
    if bash is None:
        return
    # bash -n only parses its first script argument (the rest become positional
    # params), so check each tracked copy individually.
    for rel in (
        ("scripts", "install-native.sh"),
        ("docs", "public", "install.sh"),
        ("web", "public", "install.sh"),
    ):
        subprocess.run([bash, "-n", str(ROOT.joinpath(*rel))], check=True)


def test_public_install_scripts_match_native_sources_of_truth() -> None:
    native_sh = (ROOT / "scripts" / "install-native.sh").read_bytes()
    native_ps1 = (ROOT / "scripts" / "install.ps1").read_bytes()

    assert (ROOT / "docs" / "public" / "install.sh").read_bytes() == native_sh
    assert (ROOT / "web" / "public" / "install.sh").read_bytes() == native_sh
    assert (ROOT / "docs" / "public" / "install.ps1").read_bytes() == native_ps1
    assert (ROOT / "web" / "public" / "install.ps1").read_bytes() == native_ps1

    expected_sh_headers = (
        "/install.sh\n"
        "  Content-Type: text/x-shellscript; charset=utf-8\n"
        "  Cache-Control: public, max-age=300, s-maxage=900, stale-if-error=86400\n"
    )
    expected_ps1_headers = (
        "/install.ps1\n"
        "  Content-Type: text/plain; charset=utf-8\n"
        "  Cache-Control: public, max-age=300, s-maxage=900, stale-if-error=86400\n"
    )
    assert expected_sh_headers in (ROOT / "docs" / "public" / "_headers").read_text()
    assert expected_sh_headers in (ROOT / "web" / "public" / "_headers").read_text()
    assert expected_ps1_headers in (ROOT / "docs" / "public" / "_headers").read_text()
    assert expected_ps1_headers in (ROOT / "web" / "public" / "_headers").read_text()


def test_installation_docs_do_not_use_placeholder_package_artifacts() -> None:
    docs = [
        ROOT / "README.md",
        ROOT / "packages" / "linux-installer" / "README.md",
    ]

    for doc in docs:
        text = doc.read_text()
        assert "releases/download/vx.y.z" not in text
        assert "pythinker-code-x.y.z" not in text
        assert "pythinker-code_x.y.z" not in text


def test_readme_references_existing_terminal_demo_asset() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "docs/media/pythinker-code.gif" not in readme
    assert "docs/media/pythinker_cli.gif" in readme
    assert (ROOT / "docs" / "media" / "pythinker_cli.gif").exists()


def test_no_old_repo_owner_references() -> None:
    """Regression guard: old repo owner must not reappear in tracked files."""
    # Split so this file's own source doesn't self-trigger.
    old_owner = "mohamed-elkholy95"
    forbidden = [
        old_owner + "/Pythinker-Code",
        old_owner + "/pythinker-code",
        old_owner + ".github.io/Pythinker-Code",
    ]
    skip_dirs = {
        "graphify-out",
        ".git",
        "dist",
        ".vitepress",
        ".venv",
        ".pythinker-review",
        ".claude",
        "__pycache__",
        ".pytest_cache",
    }
    # Excluded paths that legitimately reference old owner for non-main-repo projects
    # (pythinker-home, pythinker-agent-rs) or are this guard itself.
    skip_files = {
        ROOT / "tests" / "test_installation_docs.py",
        ROOT / ".github" / "workflows" / "dispatch-pythinker-home-sync.yml",
        ROOT / "docs" / "en" / "customization" / "wire-mode.md",
    }
    violations: list[str] = []
    for path in ROOT.rglob("*"):
        if (
            path.is_file()
            and path not in skip_files
            and not any(p in skip_dirs for p in path.parts)
        ):
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for pattern in forbidden:
                if pattern in text:
                    violations.append(f"{path.relative_to(ROOT)}: contains '{pattern}'")
    assert not violations, "Old repo owner references found:\n" + "\n".join(violations)
