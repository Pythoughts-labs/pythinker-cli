# Pythinker Linux Native Packages

Builds `.deb` (Debian / Ubuntu) and `.rpm` (Fedora / RHEL / openSUSE) packages
for Pythinker Code by freezing the CLI with PyInstaller and wrapping the
output with [`fpm`](https://github.com/jordansissel/fpm).

End-user install from the current GitHub Release:

```sh
# Debian / Ubuntu
curl -LO https://github.com/Pythoughts-labs/pythinker-code/releases/download/v0.48.0/pythinker-code_0.48.0_amd64.deb
curl -LO https://github.com/Pythoughts-labs/pythinker-code/releases/download/v0.48.0/pythinker-code_0.48.0_amd64.deb.sha256
sha256sum -c pythinker-code_0.48.0_amd64.deb.sha256
sudo dpkg -i pythinker-code_0.48.0_amd64.deb
sudo apt-get install -f       # only needed if dependencies fail to resolve

# Fedora / RHEL / openSUSE
curl -LO https://github.com/Pythoughts-labs/pythinker-code/releases/download/v0.48.0/pythinker-code-0.48.0.x86_64.rpm
curl -LO https://github.com/Pythoughts-labs/pythinker-code/releases/download/v0.48.0/pythinker-code-0.48.0.x86_64.rpm.sha256
sha256sum -c pythinker-code-0.48.0.x86_64.rpm.sha256
sudo dnf install ./pythinker-code-0.48.0.x86_64.rpm
# or, on openSUSE:
sudo zypper install ./pythinker-code-0.48.0.x86_64.rpm
```

The package drops a single executable at `/usr/bin/pythinker` and a license
file at `/usr/share/doc/pythinker-code/LICENSE`.

## Prerequisites (local builds)

- Linux x86_64 host (use `arch=arm64` on Apple Silicon under Docker for
  aarch64 builds — CI handles this with QEMU).
- Python 3.13 + a venv with `pyinstaller` available
- Ruby (for `fpm`) — `sudo apt-get install -y ruby ruby-dev` then `sudo gem install fpm`

## Build

```sh
bash packages/linux-installer/build.sh 0.48.0
```

Outputs to `dist/`:

- `pythinker-code_0.48.0_amd64.deb`
- `pythinker-code-0.48.0.x86_64.rpm`

The portable tarball used by `scripts/install-native.sh` is published by
the existing `release-pythinker-cli.yml` workflow under the cargo-dist
target-triple naming (e.g. `pythinker-0.48.0-x86_64-unknown-linux-gnu.tar.gz`).

## CI

`.github/workflows/linux-installer.yml` runs this build on every
`v[0-9]+.[0-9]+.[0-9]+` tag (matrix: x86_64 + aarch64) and uploads every
artifact to the corresponding GitHub Release.
