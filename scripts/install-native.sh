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
  # Shimmer / pulse tones for the "piece landed" + "leading edge" beats.
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

ANTENNA_SPIN_ACTIVE=""
# Bookmarked cursor position for the antenna-tip spin. We save the
# cursor right after print_logo_animated's final static re-render, and
# _antenna_tip restores from that bookmark before writing the tip.
# That way the tip always lands on the same screen row as the
# bookmark regardless of how many rows the metadata block consumed
# (which varies if any metadata row wraps). Using an absolute bookmark
# avoids the relative-cursor-up arithmetic that miscounted when the
# install layout differed from the developer's test environment.
ANTENNA_TIP_BOOKMARK=""
ANTENNA_TIP_COL=7
# Absolute row for the progress bar and "Waiting" line. Set by
# print_intro after the metadata block finishes. Both the waiting
# retry and the download progress use this row via absolute positioning
# so they never depend on where the cursor happens to be — the bar
# always lands one row below the metadata, not on whatever row the
# cursor drifted to.
PROGRESS_ROW=""

_antenna_tip() {
  [ -z "$_anim" ] && return
  [ -n "$ANTENNA_TIP_BOOKMARK" ] || return
  # Restore the bookmarked position (row right below the base), then
  # move up 5 rows to reach the antenna tip row, and write. We do NOT
  # re-save the bookmark at the tip row — that would drag the
  # reference point up 5 rows on every call, so the second tick
  # would land 5 rows above the tip, the third 10 rows above, and
  # the fourth would clamp to row 0. The bookmark stays at the
  # row right below the base for the lifetime of the install.
  # Restore the bookmark (row right below the grid base), then
  # absolute-position to the antenna tip row. We do NOT re-save the
  # bookmark at the tip row — that would drag the reference point
  # up on every call.
  printf '\033[u\033[%d;%dH%s%s%s' "$GRID_ORIGIN_ROW" "$ANTENNA_TIP_COL" "$TIP" "$1" "$RESET"
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

# (Eye blink during the install was attempted here but the row offset
# depends on the terminal's starting cursor position, which varies
# between environments. The intro's own bounce at _blink_eyes is the
# reliable eye animation; the download phase keeps the antenna spin
# going but leaves the eyes static at their final `◉` color.)

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

  # Leading-edge pulse: when the bar is moving and there is at least
  # one filled cell, paint the last filled cell in SHINE so the tip
  # reads as actively advancing. At 100% every cell is solid so the
  # pulse is omitted.
  local rendered_bar head
  if [ "$pulse" = "1" ] && [ "$filled" -gt 0 ] && [ "$filled" -lt "$width" ]; then
    head=$((filled - 1))
    local bar_head="${bar:0:$head}"
    local bar_tail="${bar:$filled}"
    rendered_bar="${BAR}${bar_head}${SHINE}▰${RESET}${BAR}${bar_tail}${RESET}"
  else
    rendered_bar="${BAR}${bar}${RESET}"
  fi

  if [ -n "$_anim" ]; then
    # Use absolute row positioning so the bar always lands on the
    # progress row (one below the metadata), regardless of where the
    # cursor drifted to. The previous `\r\033[K` wrote to whatever
    # row the cursor happened to be on — after the intro animation
    # the cursor was on the grid's base row, so the bar overwrote
    # the grid instead of appearing below it.
    if [ -n "$PROGRESS_ROW" ]; then
      printf '\033[%d;1H\033[K  %s%s%s %s %3d%%' "$PROGRESS_ROW" "$ACCENT" "$frame" "$RESET" "$rendered_bar" "$percent"
    else
      printf '\r\033[K  %s%s%s %s %3d%%' "$ACCENT" "$frame" "$RESET" "$rendered_bar" "$percent"
    fi
  else
    printf '  %s%s%s %s %3d%%' "$ACCENT" "$frame" "$RESET" "$rendered_bar" "$percent"
  fi
}

_download_with_progress() {
  local url="$1" output="$2"
  if command -v curl >/dev/null 2>&1; then
    if [ -n "$_anim" ]; then
      local total percent i=0 curl_pid
      # 8-frame spin: ◌ ◍ ◎ ◍ ● ◍ ◎ ◍ — every 4th tick the tip "blooms"
      # to a filled circle so the head reads as alive, not as a spinner.
      local -a frames=('●' '◐' '◌' '◍' '◌' '◑' '◍' '⬤')
      total="$(_content_length "$url" || true)"
      _antenna_spin_start
      # Hide the cursor during the spin loop so the in-place bar updates
      # do not flash a stray cursor block at the rewrite position.
      printf '\033[?25l'
      curl -fsSL "$url" -o "$output" &
      curl_pid=$!
      while kill -0 "$curl_pid" 2>/dev/null; do
        local frame_idx=$((i % 8))
        percent="$(_download_percent "$output" "$total" || printf '%s' $((i % 20 * 5)))"
        _antenna_tip "${frames[$frame_idx]}"
        # Pulse the leading edge on odd ticks so the bar visibly advances
        # even when the byte count has not yet ticked.
        local pulse=0
        (( i % 2 == 1 )) && pulse=1
        _print_download_progress "$percent" "${frames[$frame_idx]}" "$pulse"
        sleep 0.10
        i=$((i + 1))
      done
      wait "$curl_pid" || { printf '\033[?25h'; return 1; }
      _antenna_spin_stop
      # Settle on a solid bar with the check mark, no pulse.
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

print_intro() {
  if [ -n "$_anim" ]; then
    print_logo_animated
  else
    print_logo_static
  fi
  printf '  %-11s %s\n' "Version" "$VERSION"
  printf '  %-11s %s\n' "Platform" "$platform_display"
  printf '  %-11s %s\n' "Package" "$tarball"
  # Reserve the progress row one line below the metadata. The cursor
  # is now on that row; save it so the "Waiting" retry and the
  # download progress bar can absolute-position to it without
  # relying on cursor tracking (which drifted to the grid's base
  # row in some terminals and caused the bar to overwrite the grid).
  # Layout from the top: 4 blank rows, grid (5 rows), 1 blank row,
  # tagline, 2 blank rows, 3 metadata rows → progress row is row 17.
  PROGRESS_ROW=17
  printf '\n'
}

print_logo_static() {
  printf '\n\n\n\n'
  print_logo_art
  printf '\n'
  # Bookmark the cursor (row right below the grid) for the antenna
  # spin during download. In the static path the cursor is at the
  # same position as the animated path's final re-render (row
  # immediately below the base), so the bookmark is equivalent.
  printf '\033[s'
  ANTENNA_TIP_BOOKMARK=1
  printf '  %s%sPythinker Code%s  %sThink first. Then code.%s\n\n' "$BOLD" "$FACE" "$RESET" "$DIM" "$RESET"
}

_type_tagline() {
  # Print the tagline one glyph at a time so the intro breathes. The static
  # path prints the same line in one shot via print_logo_static.
  local tagline='Pythinker Code  Think first. Then code.'
  local i ch out=""
  printf '  '
  for ((i=0; i<${#tagline}; i++)); do
    ch="${tagline:$i:1}"
    out+="$ch"
    printf '%s' "$ch"
    sleep 0.018
  done
  printf '\n\n'
}

print_logo_art() {
  printf '      %s●%s\n'                                        "$TIP" "$RESET"
  printf '      %s│%s\n'                                        "$NAVY"  "$RESET"
  printf '  %s▛%s%s▀▀▀▀▀▀▀%s%s▜%s\n'                            "$NAVY" "$RESET" "$FACE" "$RESET" "$NAVY" "$RESET"
  printf ' %s◖%s%s█%s %s◉%s   %s◉%s %s█%s%s◗%s\n'               "$TIP" "$RESET" "$NAVY" "$RESET" "$EYE" "$RESET" "$EYE" "$RESET" "$NAVY" "$RESET" "$TIP" "$RESET"
  printf '  %s▙▄▄▄%s%s≡%s%s▄▄▄▟%s\n'                            "$NAVY" "$RESET" "$FACE" "$RESET" "$NAVY" "$RESET"
}

print_logo_animated() {
  local ROWS=5 COLS=13
  local FRAME_DELAY="${PYTHINKER_LOGO_FRAME_DELAY:-0.06}"
  local STAGGER_DELAY="${PYTHINKER_LOGO_STAGGER_DELAY:-0.04}"

  # Hide the text cursor while we redraw in place — otherwise the block
  # cursor on terminals like Terminal.app renders as a stray white block
  # at the cursor's current cell. Show it again before returning so the
  # user can type after the installer finishes. We use a flag and restore
  # in the function footer so this works regardless of how the function
  # exits (note: `trap ... RETURN` does not fire on plain function return
  # in bash, so an explicit show at every return site is required).
  printf '\033[?25l'
  _cursor_hidden=1

  # The 4 leading newlines push the cursor from row 1 to row 5, so the
  # grid sits at rows 5-9. Publish this to the top-level so the antenna
  # spin can use absolute cursor moves after the intro returns. We use
  # absolute positioning for every render so a caller that left the
  # cursor at the wrong row can't leave a ghost frame behind.
  GRID_ORIGIN_ROW=5

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

    # Always position the cursor at the absolute top of the grid before
    # writing. The previous version used `\033[5A\r` (cursor up 5) which
    # is fragile: if any caller left the cursor at the wrong row, the
    # render writes at the wrong position and leaves a ghost frame on
    # the terminal. Absolute positioning makes the render idempotent
    # regardless of where the cursor was.
    printf '\033[%d;1H' "$GRID_ORIGIN_ROW"

    local r c idx color ch line
    for ((r=0; r<ROWS; r++)); do
      line=""
      for ((c=0; c<COLS; c++)); do
        idx=$((r*COLS+c))
        color="${tk[$idx]}"; ch="${tc[$idx]}"
        if [ -n "$color" ]; then line+="${color}${ch}${RESET}"; else line+="$ch"; fi
      done
      # Move to the right row, then write the line and clear to EOL.
      printf '\033[%d;1H%s\033[K' "$((GRID_ORIGIN_ROW + r))" "$line"
    done
  }

  _drop_piece() {
    local target_r=$1 target_c=$2; shift 2
    local -a cells=("$@")
    local r
    for ((r=-1; r<=target_r; r++)); do
      _render "$r" "$target_c" "${cells[@]}"
      sleep "$FRAME_DELAY"
    done
    # Commit the piece to the static grid.
    local cell dr dc ch color
    for cell in "${cells[@]}"; do
      IFS=',' read -r dr dc ch color <<<"$cell"
      _set_cell $((target_r + dr)) $((target_c + dc)) "$ch" "$color"
    done
    # Shimmer: redraw the just-landed cells in white for one beat so the
    # piece reads as "clicked into place", then settle back to its color.
    local -a shine_cells=()
    for cell in "${cells[@]}"; do
      IFS=',' read -r dr dc ch color <<<"$cell"
      shine_cells+=("$dr,$dc,$ch,$SHINE")
    done
    _render "$target_r" "$target_c" "${shine_cells[@]}"
    sleep 0.05
    _render "" ""  # empty piece, just redraw committed grid
    if [ "$STAGGER_DELAY" != "0" ]; then sleep "$STAGGER_DELAY"; fi
  }

  _blink_eyes() {
    # The eyes "bounce" awake: glance one column to the left, then close
    # into a line, then open with a soft shine, then settle. Reads as
    # the same kind of moving-head beat the progress bar uses — a shape
    # that travels one cell and lands — so the intro and the download
    # speak the same visual language.
    local target_r=$1 target_c=$2 eye_ch=$3
    # Frame 1: glance left — eye renders one column to the left of its
    # target, in the SHINE tone so it reads as "fresh / active".
    _render "$target_r" "$((target_c - 1))" "0,0,$eye_ch,$SHINE"
    sleep 0.06
    # Frame 2: closed eye, at the final column so the glance lands.
    _render "$target_r" "$target_c" "0,0,─,$EYE"
    sleep 0.05
    # Commit the closed eye and hold one beat so the blink registers.
    _set_cell $target_r $target_c "─" "$EYE"
    _render "" ""
    sleep 0.04
    # Frame 3: open with a shine flash, then settle to the final color.
    _set_cell $target_r $target_c "$eye_ch" "$SHINE"
    _render "" ""
    sleep 0.06
    _set_cell $target_r $target_c "$eye_ch" "$EYE"
    _render "" ""
  }

  _drop_antenna_tip() {
    # The head piece: drops, then glows white for one frame before settling
    # to the warm TIP color — gives the logo a tiny "hello" beat.
    local target_r=$1 target_c=$2
    local -a cells=("0,0,●,$TIP")
    local r
    for ((r=-1; r<=target_r; r++)); do
      _render "$r" "$target_c" "${cells[@]}"
      sleep "$FRAME_DELAY"
    done
    _set_cell $target_r $target_c "●" "$TIP"
    _render "$target_r" "$target_c" "0,0,●,$SHINE"
    sleep 0.07
    _set_cell $target_r $target_c "●" "$SHINE"
    _render "" ""
    sleep 0.05
    _set_cell $target_r $target_c "●" "$TIP"
    _render "" ""
  }

  # The 4 leading newlines push the cursor from row 1 to row 5. The grid
  # is rendered with absolute positioning at GRID_ORIGIN_ROW=5 via _render,
  # so we do NOT add extra newlines here — the old `for ROWS` loop left the
  # cursor far below the grid, and the relative `\033[5A` rewind to re-print
  # the static art miscounted by one row, causing the static re-render to
  # land one row above the animated grid (visible as a duplicated base row
  # and a misaligned tagline).
  printf '\n\n\n\n'

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

  # Re-paint the static logo with absolute positioning so it lands on the
  # exact same rows (5-9) as the animated grid, regardless of where the
  # cursor ended up. _render's `\033[K` does not advance the cursor, so the
  # cursor is on row 9 after the last render — we cannot rely on a relative
  # move from here.
  printf '\033[%d;1H' "$GRID_ORIGIN_ROW"
  print_logo_art
  # After print_logo_art's trailing \n the cursor is on row GRID_ORIGIN_ROW
  # + ROWS = 10. Step down to row 11 so the bookmark + tagline sit one row
  # below the grid, matching print_logo_static's layout exactly.
  printf '\n'
  # Bookmark the cursor position (row right below the grid) so the
  # download-phase antenna spin can restore from here on every tick.
  printf '\033[s'
  ANTENNA_TIP_BOOKMARK=1
  _type_tagline

  # Restore the cursor — every return path from this function must end
  # here. The top-level INT/TERM/EXIT traps also cover us in case of a
  # signal mid-animation.
  if [ -n "${_cursor_hidden:-}" ]; then
    printf '\033[?25h'
    unset _cursor_hidden
  fi
}

phase_ok() {
  if [ -n "$_anim" ]; then
    # Trailing-dot completion: 3 dots fill in one by one, then a check.
    # Hide the cursor for the duration of the animation so the trailing
    # \r\033[K rewrites don't leave a visible block at each step.
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
    # Fade-in separator: dim version first, then settle to BAR.
    # Hide the cursor for the duration of the fade so the in-place
    # rewrite (\r\033[K) does not flash a stray cursor block.
    printf '\033[?25l'
    printf '\n  %s%s%s' "$DIM" "$sep" "$RESET"
    sleep 0.12
    printf '\r\033[K  %s%s%s' "$BAR" "$sep" "$RESET"
    sleep 0.06
    printf '\n  %sReady. Start with:%s\n\n' "$ACCENT" "$RESET"
    # Ready pulse: dim → bright for the command line.
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

# Always restore the text cursor on signal/exit — the animation paths
# hide it, and a stray Ctrl-C would otherwise leave the user's terminal
# with no cursor until they run `tput cnorm` themselves. The EXIT trap
# is intentionally set here without cleanup; the tmpdir cleanup is
# layered on top later in this script.
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

# --- wait for assets to finish publishing -------------------------------
# The GitHub Release is published before every platform asset finishes
# uploading, and /releases/latest is date-based, so it can briefly advertise a
# version whose archive is still in flight. Confirm this version's archive and
# checksum are attached (via the GitHub API, like the in-app updater) before
# downloading, so a release caught mid-publish does not 404.
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
# Exponential backoff: the GitHub Release can briefly advertise a version
# whose assets are still uploading. Wait 4,8,16,...,120s (capped), ~6m total,
# before giving up — long enough to ride out a slow multi-arch upload.
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
  if [ -n "$_anim" ]; then
    printf '\033[%d;1H\033[K  %s%-11s%s release assets, retrying in %s%ss%s' "$PROGRESS_ROW" "$DIM" "Waiting" "$RESET" "$BAR" "$delay" "$RESET"
  else
    printf '  Waiting for release assets, retrying in %ss\n' "$delay"
  fi
  sleep "$delay"
  elapsed=$((elapsed + delay))
  delay=$((delay * 2))
  [ "$delay" -gt 120 ] && delay=120
done
[ -n "$_anim" ] && [ "$attempt" -gt 0 ] && printf '\033[%d;1H\033[K' "$PROGRESS_ROW"

# --- download + verify --------------------------------------------------
tmpdir="$(mktemp -d -t pythinker-install.XXXXXX)"
# Layer: keep the cursor-show on every exit path, then clean up tmpdir.
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

# --- install -----------------------------------------------------------
bin_dir="$INSTALL_PREFIX/bin"
mkdir -p "$bin_dir"
# The existing release tarball contains a single `pythinker` file at the
# tarball root (PyInstaller --onefile output).
tar -C "$tmpdir" -xzf "$tmpdir/$tarball"
[ -x "$tmpdir/pythinker" ] || fail "tarball did not contain an executable named 'pythinker'"
install -m 0755 "$tmpdir/pythinker" "$bin_dir/pythinker"
printf 'pythinker-native-build\n' > "$bin_dir/.pythinker-native"
phase_ok "Installing"

# --- PATH guidance --------------------------------------------------------
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    printf '\n  %sNote:%s %s%s%s is not on your PATH.\n' "$BOLD" "$RESET" "$DIM" "$bin_dir" "$RESET"
    printf '  %sAdd this to your shell profile (~/.bashrc, ~/.zshrc, ~/.config/fish/config.fish):%s\n' "$DIM" "$RESET"
    printf '\n    %sexport PATH="%s:%sPATH"%s\n\n' "$DIM" "$bin_dir" "\$" "$RESET"
    ;;
esac

print_done
