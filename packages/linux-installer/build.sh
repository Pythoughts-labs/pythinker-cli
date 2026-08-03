#!/usr/bin/env bash
# Build .deb + .rpm packages for Pythinker Code on the host Linux arch.
#
# Usage:  bash packages/linux-installer/build.sh <version> [<arch>]
#   arch defaults to the host's `uname -m` (x86_64 / aarch64).
#
# Outputs (all under dist/):
#   pythinker-code_<version>_<deb-arch>.deb         (Debian / Ubuntu)
#   pythinker-code-<version>.<rpm-arch>.rpm         (Fedora / RHEL / openSUSE)
#
# Portable tarballs for the curl-bash native installer come from the
# existing release-pythinker-cli.yml workflow (target-triple naming).
#
# Requirements (CI installs these; for local builds install once):
#   - python3 + pyinstaller
#   - ruby + fpm (`sudo apt-get install -y ruby ruby-dev && sudo gem install fpm`)
set -euo pipefail

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  echo "usage: $0 <version> [<arch>]" >&2
  exit 2
fi

# Map uname -m to the conventions the two packaging formats use.
HOST_ARCH="${2:-$(uname -m)}"
case "$HOST_ARCH" in
  x86_64)  DEB_ARCH="amd64";  RPM_ARCH="x86_64"   ;;
  aarch64) DEB_ARCH="arm64";  RPM_ARCH="aarch64"  ;;
  *) echo "unsupported arch: $HOST_ARCH" >&2; exit 2 ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")"/../.. && pwd)"
PKG_DIR="$REPO_ROOT/packages/linux-installer"
DIST_DIR="$REPO_ROOT/dist"
BUILD_DIR="$REPO_ROOT/build/linux-installer"

mkdir -p "$DIST_DIR" "$BUILD_DIR"

echo "==> Freezing pythinker (version=$VERSION arch=$HOST_ARCH)"
(
  cd "$PKG_DIR"
  python -m PyInstaller --noconfirm \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_DIR" \
    pythinker.spec
)

FROZEN_DIR="$DIST_DIR/pythinker"
if [[ ! -x "$FROZEN_DIR/pythinker" ]]; then
  echo "PyInstaller did not produce $FROZEN_DIR/pythinker" >&2
  exit 1
fi

# Drop the runtime sentinel + LICENSE next to the binary so the
# is_native_build() probe succeeds and rpm/deb metadata can reference
# the license file at a stable path.
echo "pythinker-native-build" > "$FROZEN_DIR/.pythinker-native"
cp "$REPO_ROOT/LICENSE" "$FROZEN_DIR/LICENSE"

# NOTE: We intentionally do NOT produce a portable tarball here — the
# existing release-pythinker-cli.yml workflow already publishes onefile
# tarballs with target-triple naming (pythinker-X.Y.Z-x86_64-unknown-linux-gnu.tar.gz),
# which scripts/install-native.sh downloads. This build is responsible only
# for the .deb / .rpm package-manager artifacts.

# Stage a /usr-prefixed layout fpm can wrap directly into .deb / .rpm.
STAGE="$BUILD_DIR/stage-$HOST_ARCH"
rm -rf "$STAGE"
mkdir -p "$STAGE/usr/lib/pythinker" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/doc/pythinker-code"
cp -a "$FROZEN_DIR/." "$STAGE/usr/lib/pythinker/"
# /usr/bin/pythinker is a small launcher script; the actual binary plus its
# _internal/ directory live under /usr/lib/pythinker/ to keep $PATH tidy.
cat > "$STAGE/usr/bin/pythinker" <<'EOF'
#!/bin/sh
exec /usr/lib/pythinker/pythinker "$@"
EOF
chmod 0755 "$STAGE/usr/bin/pythinker"
cp "$REPO_ROOT/LICENSE" "$STAGE/usr/share/doc/pythinker-code/LICENSE"

FPM_COMMON=(
  -s dir
  -C "$STAGE"
  -n pythinker-code
  -v "$VERSION"
  --license "Apache-2.0"
  --maintainer "Pythinker <moelkholy1995@gmail.com>"
  --url "https://github.com/Pythoughts-labs/pythinker-cli"
  --description "Pythinker Code: terminal-native review-first AI engineering agent."
  --vendor "Pythinker"
)

# --- 2. .deb -----------------------------------------------------------------
DEB_OUT="$DIST_DIR/pythinker-code_${VERSION}_${DEB_ARCH}.deb"
rm -f "$DEB_OUT"
echo "==> Building .deb: $(basename "$DEB_OUT")"
fpm "${FPM_COMMON[@]}" \
  -t deb \
  -a "$DEB_ARCH" \
  --deb-no-default-config-files \
  -p "$DEB_OUT" \
  usr
sha256sum "$DEB_OUT" | tee "$DEB_OUT.sha256" > /dev/null

# --- 3. .rpm -----------------------------------------------------------------
RPM_OUT="$DIST_DIR/pythinker-code-${VERSION}.${RPM_ARCH}.rpm"
rm -f "$RPM_OUT"
echo "==> Building .rpm: $(basename "$RPM_OUT")"
fpm "${FPM_COMMON[@]}" \
  -t rpm \
  -a "$RPM_ARCH" \
  --rpm-os linux \
  -p "$RPM_OUT" \
  usr
sha256sum "$RPM_OUT" | tee "$RPM_OUT.sha256" > /dev/null

echo ""
echo "  deb : $DEB_OUT"
echo "  rpm : $RPM_OUT"
