#!/usr/bin/env bash
# Pythinker Code — native curl-bash installer.
#
# Downloads the PyInstaller-built single-file binary for your OS + arch from
# the latest GitHub Release, verifies its SHA-256, and installs it at
#   ~/.local/bin/pythinker
#
# Usage:
#   curl -fsSL https://pythinker.com/install.sh | bash
#
#   # Pin a specific version:
#   curl -fsSL https://pythinker.com/install.sh | bash -s -- --version 0.27.0
#
#   # Custom install prefix (default $HOME/.local):
#   curl -fsSL https://pythinker.com/install.sh | bash -s -- --prefix /opt/pythinker
#
# Supported targets (target triples — matches existing release artifacts):
#   x86_64-unknown-linux-gnu       (Linux x86_64)
#   aarch64-unknown-linux-gnu      (Linux ARM64)
#   aarch64-apple-darwin           (macOS Apple Silicon)
#   x86_64-apple-darwin            (macOS Intel)
#
# Windows users: download PythinkerSetup-x.y.z.exe from the Releases page.
set -euo pipefail

VERSION=""
INSTALL_PREFIX="${PYTHINKER_INSTALL_PREFIX:-$HOME/.local}"
NO_COLOR="${NO_COLOR:-}"

usage() {
  cat <<'EOF'
Pythinker Code — native curl-bash installer.

Downloads the PyInstaller-built single-file binary for your OS + arch from
the latest GitHub Release, verifies its SHA-256, and installs it at
  ~/.local/bin/pythinker

Usage:
  curl -fsSL https://pythinker.com/install.sh | bash

  # Pin a specific version:
  curl -fsSL https://pythinker.com/install.sh | bash -s -- --version 0.27.0

  # Custom install prefix (default $HOME/.local):
  curl -fsSL https://pythinker.com/install.sh | bash -s -- --prefix /opt/pythinker

Supported targets (target triples — matches existing release artifacts):
  x86_64-unknown-linux-gnu       (Linux x86_64)
  aarch64-unknown-linux-gnu      (Linux ARM64)
  aarch64-apple-darwin           (macOS Apple Silicon)
  x86_64-apple-darwin            (macOS Intel)

Windows users: download PythinkerSetup-x.y.z.exe from the Releases page.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [ -n "${2:-}" ] || { echo "--version requires a value" >&2; exit 2; }
      VERSION="$2"; shift 2 ;;
    --prefix)
      [ -n "${2:-}" ] || { echo "--prefix requires a value" >&2; exit 2; }
      INSTALL_PREFIX="$2"; shift 2 ;;
    -h|--help)
      usage
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REPO="Pythoughts-labs/pythinker-code"

if [ -t 1 ] && [ -z "$NO_COLOR" ] && [ "${TERM:-}" != "dumb" ]; then
  NAVY=$'\033[38;5;24m'; FACE=$'\033[38;5;255m'
  ACCENT=$'\033[38;5;147m'; TIP=$'\033[38;5;216m'
  EYE=$'\033[38;5;189m'; BAR=$'\033[38;5;250m'; DIM=$'\033[2m'
  BOLD=$'\033[1m'; RESET=$'\033[0m'
  SHINE=$'\033[38;5;231m'; SOFT=$'\033[38;5;111m'
else
  NAVY=""; FACE=""; ACCENT=""; TIP=""; EYE=""; BAR=""; DIM=""; BOLD=""; RESET=""
  SHINE=""; SOFT=""
fi

_anim=""
[ -t 1 ] \
  && [ -z "$NO_COLOR" ] \
  && [ "${TERM:-}" != "dumb" ] \
  && [ -z "${PYTHINKER_NO_ANIMATION:-}" ] \
  && [ -z "${CI:-}" ] \
  && _anim=1

LOGO_CURSOR_ROWS=0
ANTENNA_SPIN_ACTIVE=""

_antenna_tip() {
  [ -z "$_anim" ] && return
  [ "$LOGO_CURSOR_ROWS" -gt 0 ] || return
  printf '\033[s\033[%dA\r\033[6C%s%s%s\033[u' "$LOGO_CURSOR_ROWS" "$TIP" "$1" "$RESET"
}

_antenna_spin_start() {
  [ -z "$_anim" ] && return
  ANTENNA_SPIN_ACTIVE=1
}

_antenna_spin_stop() {
  [ -z "$ANTENNA_SPIN_ACTIVE" ] && return
  ANTENNA_SPIN_ACTIVE=""
  _antenna_tip "●"
}

_content_length() {
  curl -fsIL "$1" 2>/dev/null \
    | awk 'tolower($1) == "content-length:" { gsub("\r", "", $2); bytes=$2 } END { if (bytes ~ /^[0-9]+$/) print bytes }'
}

_download_percent() {
  local output="$1" total="$2" size
  [ -n "$total" ] && [ "$total" -gt 0 ] 2>/dev/null || return 1
  if [ ! -f "$output" ]; then
    printf '0'
    return 0
  fi
  size="$(wc -c < "$output" | tr -d ' ')"
  [ -n "$size" ] || size=0
  local percent=$((size * 100 / total))
  [ "$percent" -gt 99 ] && percent=99
  printf '%s' "$percent"
}

_print_download_progress() {
  local percent="$1" frame="$2" pulse="${3:-0}"
  local width=48 filled empty bar=""
  filled=$((percent * width / 100))
  empty=$((width - filled))

  local i
  for ((i=0; i<filled; i++)); do bar+="▰"; done
  for ((i=0; i<empty; i++)); do bar+="▱"; done

  local rendered_bar head
  if [ "$pulse" = "1" ] && [ "$filled" -gt 0 ] && [ "$filled" -lt "$width" ]; then
    head=$((filled - 1))
    local bar_head="${bar:0:$head}"
    local bar_tail="${bar:$filled}"
    rendered_bar="${BAR}${bar_head}${SOFT}▰${RESET}${BAR}${bar_tail}${RESET}"
  else
    rendered_bar="${BAR}${bar}${RESET}"
  fi

  if [ -n "$_anim" ]; then
    printf '\r\033[K  %s%s%s %s %3d%%' "$BAR" "$frame" "$RESET" "$rendered_bar" "$percent"
  else
    printf '  %s%s%s %s %3d%%' "$BAR" "$frame" "$RESET" "$rendered_bar" "$percent"
  fi
}

_download_with_progress() {
  local url="$1" output="$2"
  if command -v curl >/dev/null 2>&1; then
    if [ -n "$_anim" ]; then
      local total percent i=0 curl_pid
      local -a frames=('●' '◐' '◌' '◍' '◌' '◑' '◍' '⬤')
      total="$(_content_length "$url" || true)"
      _antenna_spin_start
      printf '\033[?25l'
      curl -fsSL "$url" -o "$output" &
      curl_pid=$!
      while kill -0 "$curl_pid" 2>/dev/null; do
        local frame_idx=$((i % 8))
        percent="$(_download_percent "$output" "$total" || printf '%s' $((i % 20 * 5)))"
        _antenna_tip "${frames[$frame_idx]}"
        local pulse=0
        (( i % 2 == 1 )) && pulse=1
        _print_download_progress "$percent" "${frames[$frame_idx]}" "$pulse"
        sleep 0.10
        i=$((i + 1))
      done
      wait "$curl_pid" || { printf '\033[?25h'; return 1; }
      _antenna_spin_stop
      _print_download_progress 100 "✓"
      printf '\n\033[?25h'
    else
      curl -fsSL "$url" -o "$output" || return 1
      _print_download_progress 100 "✓"
      printf '\n'
    fi
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$output" || return 1
    _print_download_progress 100 "✓"
    printf '\n'
  else
    return 127
  fi
}

_download_quiet() {
  local url="$1" output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$output" || return 1
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$output" || return 1
  else
    return 127
  fi
}

print_logo_art() {
  printf '      %s●%s\n'                                        "$TIP" "$RESET"
  printf '      %s│%s\n'                                        "$NAVY"  "$RESET"
  printf '  %s▛%s%s▀▀▀▀▀▀▀%s%s▜%s\n'                            "$NAVY" "$RESET" "$FACE" "$RESET" "$NAVY" "$RESET"
  printf ' %s◖%s%s█%s %s◉%s   %s◉%s %s█%s%s◗%s\n'               "$TIP" "$RESET" "$NAVY" "$RESET" "$EYE" "$RESET" "$EYE" "$RESET" "$NAVY" "$RESET" "$TIP" "$RESET"
  printf '  %s▙▄▄▄%s%s≡%s%s▄▄▄▟%s\n'                            "$NAVY" "$RESET" "$FACE" "$RESET" "$NAVY" "$RESET"
}

print_logo_static() {
  printf '\n\n'
  print_logo_art
  printf '\n'
  printf '  %s%sPythinker Code%s  %sThink first. Then code.%s\n\n' "$BOLD" "$FACE" "$RESET" "$DIM" "$RESET"
}

_type_tagline() {
  local tagline='Pythinker Code  Think first. Then code.'
  local i ch
  printf '  '
  for ((i=0; i<${#tagline}; i++)); do
    ch="${tagline:$i:1}"
    printf '%s' "$ch"
    sleep 0.018
  done
  printf '\n\n'
}

print_logo_animated() {
  local ROWS=5 COLS=13
  local FRAME_DELAY="${PYTHINKER_LOGO_FRAME_DELAY:-0.06}"
  local STAGGER_DELAY="${PYTHINKER_LOGO_STAGGER_DELAY:-0.04}"

  local -a grid_chars grid_colors
  local i
  for ((i=0; i<ROWS*COLS; i++)); do
    grid_chars[i]=" "
    grid_colors[i]=""
  done

  _set_cell() {
    grid_chars[$1 * COLS + $2]="$3"
    grid_colors[$1 * COLS + $2]="$4"
  }

  _render() {
    local piece_r="$1" piece_c="$2"
    shift 2
    local -a cells=("$@")
    local -a tc=("${grid_chars[@]}") tk=("${grid_colors[@]}")

    if [ -n "$piece_r" ]; then
      local cell dr dc ch color rr cc
      for cell in "${cells[@]}"; do
        IFS=',' read -r dr dc ch color <<<"$cell"
        rr=$((piece_r + dr)); cc=$((piece_c + dc))
        if (( rr >= 0 && rr < ROWS && cc >= 0 && cc < COLS )); then
          tc[rr * COLS + cc]="$ch"
          tk[rr * COLS + cc]="$color"
        fi
      done
    fi

    local r c idx color ch line
    for ((r=0; r<ROWS; r++)); do
      line=""
      for ((c=0; c<COLS; c++)); do
        idx=$((r*COLS+c))
        color="${tk[$idx]}"; ch="${tc[$idx]}"
        if [ -n "$color" ]; then line+="${color}${ch}${RESET}"; else line+="$ch"; fi
      done
      printf '%s\033[K\n' "$line"
    done
  }

  _drop_piece() {
    local target_r=$1 target_c=$2; shift 2
    local -a cells=("$@")
    local r
    for ((r=-1; r<=target_r; r++)); do
      printf '\033[%dA\r' "$ROWS"
      _render "$r" "$target_c" "${cells[@]}"
      sleep "$FRAME_DELAY"
    done
    local cell dr dc ch color
    for cell in "${cells[@]}"; do
      IFS=',' read -r dr dc ch color <<<"$cell"
      _set_cell $((target_r + dr)) $((target_c + dc)) "$ch" "$color"
    done
    # Shimmer: flash landed cells white for one beat, then settle.
    local -a shine_cells=()
    for cell in "${cells[@]}"; do
      IFS=',' read -r dr dc ch color <<<"$cell"
      shine_cells+=("$dr,$dc,$ch,$SHINE")
    done
    printf '\033[%dA\r' "$ROWS"
    _render "$target_r" "$target_c" "${shine_cells[@]}"
    sleep 0.05
    printf '\033[%dA\r' "$ROWS"
    _render "" ""
    if [ "$STAGGER_DELAY" != "0" ]; then sleep "$STAGGER_DELAY"; fi
  }

  _blink_eyes() {
    local target_r=$1 target_c=$2 eye_ch=$3
    # Frame 1: glance left in SHINE tone.
    printf '\033[%dA\r' "$ROWS"
    _render "$target_r" "$((target_c - 1))" "0,0,$eye_ch,$SHINE"
    sleep 0.06
    # Frame 2: closed eye at final column.
    printf '\033[%dA\r' "$ROWS"
    _render "$target_r" "$target_c" "0,0,─,$EYE"
    sleep 0.05
    _set_cell $target_r $target_c "─" "$EYE"
    printf '\033[%dA\r' "$ROWS"
    _render "" ""
    sleep 0.04
    # Frame 3: open with shine flash, then settle.
    _set_cell $target_r $target_c "$eye_ch" "$SHINE"
    printf '\033[%dA\r' "$ROWS"
    _render "" ""
    sleep 0.06
    _set_cell $target_r $target_c "$eye_ch" "$EYE"
    printf '\033[%dA\r' "$ROWS"
    _render "" ""
  }

  _drop_antenna_tip() {
    local target_r=$1 target_c=$2
    local -a cells=("0,0,●,$TIP")
    local r
    for ((r=-1; r<=target_r; r++)); do
      printf '\033[%dA\r' "$ROWS"
      _render "$r" "$target_c" "${cells[@]}"
      sleep "$FRAME_DELAY"
    done
    _set_cell $target_r $target_c "●" "$TIP"
    printf '\033[%dA\r' "$ROWS"
    _render "$target_r" "$target_c" "0,0,●,$SHINE"
    sleep 0.07
    _set_cell $target_r $target_c "●" "$SHINE"
    printf '\033[%dA\r' "$ROWS"
    _render "" ""
    sleep 0.05
    _set_cell $target_r $target_c "●" "$TIP"
    printf '\033[%dA\r' "$ROWS"
    _render "" ""
  }

  printf '\033[?25l'
  local _cursor_hidden=1

  printf '\n\n'
  for ((i=0; i<ROWS; i++)); do printf '\n'; done

  _drop_piece 2 2  "0,0,▛,$NAVY" "1,0,█,$NAVY" "2,0,▙,$NAVY"
  _drop_piece 2 10 "0,0,▜,$NAVY" "1,0,█,$NAVY" "2,0,▟,$NAVY"
  _drop_piece 2 3  "0,0,▀,$FACE" "0,1,▀,$FACE" "0,2,▀,$FACE" "0,3,▀,$FACE" "0,4,▀,$FACE" "0,5,▀,$FACE" "0,6,▀,$FACE"
  _drop_piece 4 3  "0,0,▄,$NAVY" "0,1,▄,$NAVY" "0,2,▄,$NAVY" "0,3,≡,$FACE" "0,4,▄,$NAVY" "0,5,▄,$NAVY" "0,6,▄,$NAVY"
  _blink_eyes 3 4 "◉"
  _blink_eyes 3 8 "◉"
  _drop_piece 3 1  "0,0,◖,$TIP"
  _drop_piece 3 11 "0,0,◗,$TIP"
  _drop_piece 1 6  "0,0,│,$NAVY"
  _drop_antenna_tip 0 6

  printf '\033[%dA\r' "$ROWS"
  print_logo_art
  printf '\n'
  _type_tagline
  LOGO_CURSOR_ROWS=8

  if [ -n "${_cursor_hidden:-}" ]; then
    printf '\033[?25h'
    unset _cursor_hidden
  fi
}

print_intro() {
  if [ -n "$_anim" ]; then
    print_logo_animated
  else
    print_logo_static
  fi
  printf '  %-11s %s\n' "Version" "$VERSION"
  printf '  %-11s %s\n' "Platform" "$platform_display"
  printf '  %-11s %s\n\n' "Package" "$tarball"
  [ "$LOGO_CURSOR_ROWS" -gt 0 ] && LOGO_CURSOR_ROWS=$((LOGO_CURSOR_ROWS + 4))
}

phase_ok() {
  if [ -n "$_anim" ]; then
    printf '\033[?25l'
    local i dot
    for i in 1 2 3; do
      dot=""
      local d
      for ((d=0; d<i; d++)); do dot+="."; done
      printf '\r\033[K  %-11s %sOK%s%s' "$1" "$ACCENT" "$dot" "$RESET"
      sleep 0.06
    done
    printf '\r\033[K  %-11s %sOK %s✓%s\n' "$1" "$ACCENT" "$SHINE" "$RESET"
    printf '\033[?25h'
  else
    printf '  %-11s %sOK %s✓%s\n' "$1" "$ACCENT" "$SHINE" "$RESET"
  fi
}

print_done() {
  local sep="──────────────────────────────────────────────────"
  if [ -n "$_anim" ]; then
    printf '\033[?25l'
    printf '\n  %s%s%s' "$DIM" "$sep" "$RESET"
    sleep 0.12
    printf '\r\033[K  %s%s%s' "$BAR" "$sep" "$RESET"
    sleep 0.06
    printf '\n  %sReady. Start with:%s\n\n' "$ACCENT" "$RESET"
    printf '      %s%spythinker%s' "$DIM" "$BAR" "$RESET"
    sleep 0.10
    printf '\r\033[K      %s%spythinker%s\n\n' "$BOLD" "$BAR" "$RESET"
    printf '\033[?25h'
  else
    printf '\n  %s%s%s\n' "$BAR" "$sep" "$RESET"
    printf '  %sReady. Start with:%s\n\n' "$ACCENT" "$RESET"
    printf '      %s%spythinker%s\n\n' "$BOLD" "$BAR" "$RESET"
  fi
}

fail() {
  printf '  %s✗%s %s\n' "$TIP" "$RESET" "$1" >&2
  exit 1
}

trap 'printf "\033[?25h" 2>/dev/null || true; exit 130' INT
trap 'printf "\033[?25h" 2>/dev/null || true; exit 143' TERM

# --- detect target -------------------------------------------------------
os="$(uname -s)"
arch="$(uname -m)"
case "$os/$arch" in
  Linux/x86_64|Linux/amd64)
    target="x86_64-unknown-linux-gnu"
    platform_display="Linux x64" ;;
  Linux/aarch64|Linux/arm64)
    target="aarch64-unknown-linux-gnu"
    platform_display="Linux arm64" ;;
  Darwin/arm64)
    target="aarch64-apple-darwin"
    platform_display="macOS arm64" ;;
  Darwin/x86_64)
    target="x86_64-apple-darwin"
    platform_display="macOS x64" ;;
  MINGW*/*|MSYS*/*|CYGWIN*/*)
    fail "On Windows, download PythinkerSetup-x.y.z.exe from:
https://github.com/${REPO}/releases/latest

PowerShell installer:
powershell -c \"irm https://pythinker.com/install.ps1 | iex\"" ;;
  *)
    fail "unsupported target: $os/$arch" ;;
esac

# --- resolve version -----------------------------------------------------
if [ -z "$VERSION" ]; then
  api="https://api.github.com/repos/${REPO}/releases/latest"
  if command -v curl >/dev/null 2>&1; then
    payload="$(curl -fsSL "$api")"
  elif command -v wget >/dev/null 2>&1; then
    payload="$(wget -qO- "$api")"
  else
    fail "need curl or wget to fetch the release index"
  fi
  VERSION="$(printf '%s' "$payload" | sed -nE 's/.*"tag_name": *"v([0-9]+\.[0-9]+\.[0-9]+)".*/\1/p' | head -n 1)"
  [ -z "$VERSION" ] && fail "could not parse latest release tag from $api"
fi

tarball="pythinker-${VERSION}-${target}.tar.gz"
tarball_url="https://github.com/${REPO}/releases/download/v${VERSION}/${tarball}"
sha_url="${tarball_url}.sha256"

print_intro

# --- wait for assets to finish publishing --------------------------------
release_has_assets() {
  _api="https://api.github.com/repos/${REPO}/releases/tags/v${VERSION}"
  if command -v curl >/dev/null 2>&1; then
    _body="$(curl -fsSL "$_api" 2>/dev/null)" || return 1
  else
    _body="$(wget -qO- "$_api" 2>/dev/null)" || return 1
  fi
  printf '%s' "$_body" | grep -Fq "\"${tarball}\"" \
    && printf '%s' "$_body" | grep -Fq "\"${tarball}.sha256\""
}
attempt=0
delay=4
elapsed=0
max_elapsed=360
until release_has_assets; do
  attempt=$((attempt + 1))
  if [ "$elapsed" -ge "$max_elapsed" ]; then
    fail "release assets for v${VERSION} are not available after ~${max_elapsed}s: ${tarball_url}
The latest release may still be publishing. Try again shortly, or pin a known-good version with --version X.Y.Z"
  fi
  printf '\r\033[K  %s%-11s%s release assets, retrying in %s%ss%s' "$DIM" "Waiting" "$RESET" "$BAR" "$delay" "$RESET"
  sleep "$delay"
  elapsed=$((elapsed + delay))
  delay=$((delay * 2))
  [ "$delay" -gt 120 ] && delay=120
done
[ "$attempt" -gt 0 ] && printf '\r\033[K'

# --- download + verify ---------------------------------------------------
tmpdir="$(mktemp -d -t pythinker-install.XXXXXX)"
trap 'printf "\033[?25h" 2>/dev/null || true; rm -rf "$tmpdir"' EXIT

_download_with_progress "$tarball_url" "$tmpdir/$tarball" || fail "download failed: $tarball_url"
_download_quiet "$sha_url" "$tmpdir/$tarball.sha256" || fail "sha256 missing: $sha_url"

expected="$(awk '{print $1}' "$tmpdir/$tarball.sha256")"
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$tmpdir/$tarball" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$tmpdir/$tarball" | awk '{print $1}')"
else
  fail "need sha256sum or shasum to verify the download"
fi
[ "$expected" != "$actual" ] && fail "SHA-256 mismatch: expected $expected, got $actual"
phase_ok "Verifying"

# --- install -------------------------------------------------------------
bin_dir="$INSTALL_PREFIX/bin"
mkdir -p "$bin_dir"
tar -C "$tmpdir" -xzf "$tmpdir/$tarball"
[ -x "$tmpdir/pythinker" ] || fail "tarball did not contain an executable named 'pythinker'"
install -m 0755 "$tmpdir/pythinker" "$bin_dir/pythinker"
printf 'pythinker-native-build\n' > "$bin_dir/.pythinker-native"
phase_ok "Installing"

# --- PATH guidance -------------------------------------------------------
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    printf '\n  %sNote:%s %s%s%s is not on your PATH.\n' "$BOLD" "$RESET" "$DIM" "$bin_dir" "$RESET"
    printf '  %sAdd this to your shell profile (~/.bashrc, ~/.zshrc, ~/.config/fish/config.fish):%s\n' "$DIM" "$RESET"
    printf '\n    %sexport PATH="%s:$PATH"%s\n\n' "$DIM" "$bin_dir" "$RESET"
    ;;
esac

print_done
