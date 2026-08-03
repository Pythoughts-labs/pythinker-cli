# Pythinker Code — native Windows installer bootstrap.
#
# Downloads the latest PythinkerSetup-x.y.z.exe from GitHub Releases, verifies
# its SHA-256 file, and runs the per-user Inno Setup installer silently.
#
# Usage:
#   irm https://pythinker.com/install.ps1 | iex
#
# To pin a version when running the hosted script, set:
#   $env:PYTHINKER_VERSION = "0.27.0"; irm https://pythinker.com/install.ps1 | iex

$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$Repo = "Pythoughts-labs/pythinker-cli"
$Version = $env:PYTHINKER_VERSION
$NoColor = $env:NO_COLOR

$ESC = [char]27
$useColor = $Host.UI.RawUI -ne $null -and -not $NoColor
if ($useColor) {
  $NAVY  = "$ESC[38;5;24m"
  $FACE  = "$ESC[38;5;255m"
  $IRIS  = "$ESC[38;5;152m"
  $CORAL = "$ESC[38;5;216m"
  $DIM   = "$ESC[2m"
  $BOLD  = "$ESC[1m"
  $RESET = "$ESC[0m"
  $HIDE  = "$ESC[?25l"
  $SHOW  = "$ESC[?25h"
} else {
  $NAVY = $FACE = $IRIS = $CORAL = $DIM = $BOLD = $RESET = $HIDE = $SHOW = ""
}

function Step($msg) { Write-Host "  $IRIS⠿$RESET $msg" }
function OK($msg)   { Write-Host "  $IRIS✓$RESET $msg" }
function Fail($msg) { Write-Host "  $CORAL✗$RESET $msg" -ForegroundColor Red; exit 1 }

# Static logo. Used as the animation fallback (non-TTY, NO_COLOR, CI, or
# PYTHINKER_NO_ANIMATION=1) and as the source of truth for the final frame.
function Write-LogoStatic {
  Write-Host ""
  Write-Host "      $CORAL●$RESET"
  Write-Host "      $NAVY│$RESET"
  Write-Host "  $NAVY▛$RESET$FACE▀▀▀▀▀▀▀$RESET$NAVY▜$RESET"
  Write-Host " $CORAL◖$RESET$NAVY█$RESET $IRIS◉$RESET   $IRIS◉$RESET $NAVY█$RESET$CORAL◗$RESET"
  Write-Host "  $NAVY▙▄▄▄$RESET$FACE≡$RESET$NAVY▄▄▄▟$RESET"
  Write-Host ""
  Write-Host "  ${BOLD}${FACE}pythinker code${RESET} ${DIM}· your next CLI agent${RESET}"
  Write-Host ""
}

# Tetris-style animated logo. Pieces fall from above the canvas one at a time
# and settle into a 5-row x 13-col grid forming the robot head.
function Write-LogoAnimated {
  $rows = 5; $cols = 13
  $frameMs = 60; $staggerMs = 40
  if ($env:PYTHINKER_LOGO_FRAME_DELAY)   { try { $frameMs   = [int]([double]$env:PYTHINKER_LOGO_FRAME_DELAY   * 1000) } catch {} }
  if ($env:PYTHINKER_LOGO_STAGGER_DELAY) { try { $staggerMs = [int]([double]$env:PYTHINKER_LOGO_STAGGER_DELAY * 1000) } catch {} }

  $chars  = New-Object 'string[]' ($rows * $cols)
  $colors = New-Object 'string[]' ($rows * $cols)
  for ($i = 0; $i -lt $chars.Length; $i++) { $chars[$i] = ' '; $colors[$i] = '' }

  # Each piece: anchor row/col + cells "dr,dc,char,color" dropped together.
  $pieces = @(
    @{ r = 2; c = 2;  cells = @("0,0,▛,$NAVY", "1,0,█,$NAVY", "2,0,▙,$NAVY") },
    @{ r = 2; c = 10; cells = @("0,0,▜,$NAVY", "1,0,█,$NAVY", "2,0,▟,$NAVY") },
    @{ r = 2; c = 3;  cells = @("0,0,▀,$FACE", "0,1,▀,$FACE", "0,2,▀,$FACE", "0,3,▀,$FACE", "0,4,▀,$FACE", "0,5,▀,$FACE", "0,6,▀,$FACE") },
    @{ r = 4; c = 3;  cells = @("0,0,▄,$NAVY", "0,1,▄,$NAVY", "0,2,▄,$NAVY", "0,3,≡,$FACE", "0,4,▄,$NAVY", "0,5,▄,$NAVY", "0,6,▄,$NAVY") },
    @{ r = 3; c = 4;  cells = @("0,0,◉,$IRIS") },
    @{ r = 3; c = 8;  cells = @("0,0,◉,$IRIS") },
    @{ r = 3; c = 1;  cells = @("0,0,◖,$CORAL") },
    @{ r = 3; c = 11; cells = @("0,0,◗,$CORAL") },
    @{ r = 1; c = 6;  cells = @("0,0,│,$NAVY") },
    @{ r = 0; c = 6;  cells = @("0,0,●,$CORAL") }
  )

  $out = [Console]::Out
  $out.Write($HIDE)
  try {
    for ($i = 0; $i -lt $rows; $i++) { $out.Write("`n") }

    foreach ($p in $pieces) {
      for ($r = -1; $r -le $p.r; $r++) {
        $out.Write("$ESC[${rows}A`r")
        $line = New-Object System.Text.StringBuilder
        for ($rr = 0; $rr -lt $rows; $rr++) {
          for ($cc = 0; $cc -lt $cols; $cc++) {
            $idx = $rr * $cols + $cc
            $ch = $chars[$idx]; $col = $colors[$idx]
            foreach ($cell in $p.cells) {
              $parts = $cell -split ',', 4
              if (($r + [int]$parts[0]) -eq $rr -and ($p.c + [int]$parts[1]) -eq $cc) {
                $ch = $parts[2]; $col = $parts[3]
              }
            }
            if ($col) { [void]$line.Append("$col$ch$RESET") } else { [void]$line.Append($ch) }
          }
          [void]$line.Append("$ESC[K`n")
        }
        $out.Write($line.ToString())
        Start-Sleep -Milliseconds $frameMs
      }
      foreach ($cell in $p.cells) {
        $parts = $cell -split ',', 4
        $idx = ($p.r + [int]$parts[0]) * $cols + ($p.c + [int]$parts[1])
        $chars[$idx] = $parts[2]; $colors[$idx] = $parts[3]
      }
      if ($staggerMs -gt 0) { Start-Sleep -Milliseconds $staggerMs }
    }

    $out.Write("`n")
    $out.Write("  ${BOLD}${FACE}pythinker code${RESET} ${DIM}· your next CLI agent${RESET}`n`n")
  } finally {
    $out.Write($SHOW)
  }
}

function Write-Logo {
  if ($env:PYTHINKER_NO_ANIMATION -or $env:CI -or $NoColor -or -not $useColor -or [Console]::IsOutputRedirected) {
    Write-LogoStatic
  } else {
    Write-LogoAnimated
  }
}

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
  Fail "This installer is for Windows. Use: curl -fsSL https://pythinker.com/install.sh | bash"
}

Write-Logo

function Test-ReleaseHasInstaller($release) {
  if ($release.draft -or $release.prerelease) { return $null }
  $tag = [string]$release.tag_name
  if (-not $tag) { return $null }
  $candidate = $tag.TrimStart('v')
  $exe = "PythinkerSetup-$candidate.exe"
  $names = @($release.assets | ForEach-Object { [string]$_.name })
  if (($names -contains $exe) -and ($names -contains "$exe.sha256")) { return $candidate }
  return $null
}

function Format-ReleaseApiError($Uri, $ErrorRecord) {
  $message = $ErrorRecord.Exception.Message
  $status = $null
  try { $status = [int]$ErrorRecord.Exception.Response.StatusCode } catch { }
  if ($status) { return "$Uri failed with HTTP ${status}: $message" }
  return "$Uri failed: $message"
}

function Get-LatestVersion {
  Step "Looking up latest Pythinker release"
  # /releases/latest is prerelease-excluding and not page-bound, so it is the
  # correct primary source (fixes the per_page=20 pagination cliff). The
  # GitHub Release can briefly advertise a version whose Windows installer is
  # still uploading, so retry with exponential backoff (~6m). A paginated
  # scan is only a fallback if /latest somehow lacks the asset pair.
  $latestApi = "https://api.github.com/repos/$Repo/releases/latest"
  $listApi   = "https://api.github.com/repos/$Repo/releases?per_page=100"
  $delay = 4
  $elapsed = 0
  $maxElapsed = 360
  $lastApiError = $null
  while ($true) {
    try {
      $latest = Invoke-RestMethod -UseBasicParsing -Uri $latestApi
      $found = Test-ReleaseHasInstaller $latest
      if ($found) { OK "Latest version is $found"; return $found }
    } catch {
      $lastApiError = Format-ReleaseApiError $latestApi $_
    }
    try {
      $releases = Invoke-RestMethod -UseBasicParsing -Uri $listApi
      foreach ($release in @($releases)) {
        $found = Test-ReleaseHasInstaller $release
        if ($found) { OK "Latest version is $found"; return $found }
      }
    } catch {
      $lastApiError = Format-ReleaseApiError $listApi $_
    }
    if ($elapsed -ge $maxElapsed) {
      $detail = if ($lastApiError) { " Last API error: $lastApiError" } else { "" }
      Fail "no published release has a ready Windows installer asset after ~${maxElapsed}s; try again shortly or pin `$env:PYTHINKER_VERSION.$detail"
    }
    Step "Windows installer asset not ready yet; retry in ${delay}s"
    Start-Sleep -Seconds $delay
    $elapsed += $delay
    $delay = [Math]::Min($delay * 2, 120)
  }
}

function Read-ExpectedHash($Path) {
  $text = Get-Content -Raw -Path $Path
  $match = [regex]::Match($text, '(?i)[a-f0-9]{64}')
  if (-not $match.Success) { Fail "could not parse SHA-256 from $Path" }
  return $match.Value.ToLowerInvariant()
}

if (-not $Version) { $Version = Get-LatestVersion }
$Version = $Version.TrimStart('v')

$asset = "PythinkerSetup-$Version.exe"
$baseUrl = "https://github.com/$Repo/releases/download/v$Version"
$installerUrl = "$baseUrl/$asset"
$shaUrl = "$installerUrl.sha256"

$tempRoot = [System.IO.Path]::GetTempPath()
$tempDir = Join-Path $tempRoot ("pythinker-install-" + [System.Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDir | Out-Null
$installerPath = Join-Path $tempDir $asset
$shaPath = "$installerPath.sha256"

try {
  Step "Downloading $asset"
  Invoke-WebRequest -UseBasicParsing -Uri $installerUrl -OutFile $installerPath
  Invoke-WebRequest -UseBasicParsing -Uri $shaUrl -OutFile $shaPath

  Step "Verifying SHA-256"
  $expected = Read-ExpectedHash $shaPath
  $actual = (Get-FileHash -Algorithm SHA256 -Path $installerPath).Hash.ToLowerInvariant()
  if ($expected -ne $actual) {
    Fail "SHA-256 mismatch: expected $expected, got $actual"
  }
  OK "Checksum OK"

  Step "Running Pythinker installer"
  $installerArgs = @(
    '/SILENT',
    '/NORESTART',
    '/CURRENTUSER',
    '/CLOSEAPPLICATIONS',
    '/NORESTARTAPPLICATIONS'
  )
  $process = Start-Process -FilePath $installerPath -ArgumentList $installerArgs -Wait -PassThru
  if ($process.ExitCode -ne 0) {
    Fail "installer exited with code $($process.ExitCode)"
  }
  OK "Installed Pythinker $Version"

  $installDir = Join-Path $env:LOCALAPPDATA "Programs\Pythinker"
  if (Test-Path (Join-Path $installDir "pythinker.exe")) {
    if (($env:PATH -split ';') -notcontains $installDir) {
      $env:PATH = "$installDir;$env:PATH"
    }
  }

  Write-Host ""
  Write-Host "  $BOLD$IRIS`pythinker$RESET is ready. Open a fresh PowerShell and run $BOLD$IRIS`pythinker$RESET to launch."
  Write-Host ""
} finally {
  Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
}
