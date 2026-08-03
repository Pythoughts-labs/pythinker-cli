# Pythinker Code — native Windows uninstaller.
#
# Reverses everything `irm https://pythinker.com/install.ps1 | iex` sets up:
#   1. Runs registered Inno Setup uninstallers (unins000.exe) silently.
#   2. Sweeps installer artifacts: validated install dirs, PATH entries (user +
#      system, value kind preserved), Start Menu shortcuts, uninstall registry
#      keys (both 32/64-bit views), stale installer temp dirs, session PATH.
#
# Session model:
#   - Everything runs inside one anonymous child scope: no functions, variables,
#     or preference settings leak into the caller's session when piped through
#     `irm ... | iex`. Console encoding is restored on exit; TLS settings are
#     never touched. The script never calls `exit` — failure surfaces as a
#     thrown error, so iex cannot close the user's window while
#     `powershell.exe -File` still gets a non-zero exit code.
#
# Safety model:
#   - Registry-provided paths are NEVER deleted or executed blindly. A directory
#     is only touched after Get-SafeInstallDirectory proves it is a plausible
#     Pythinker install: absolute, not a filesystem root, not a critical
#     directory, leaf named "Pythinker", containing no reparse-point component,
#     and either the default location or containing pythinker.exe / unins000.exe.
#   - Uninstaller executables must additionally be named unins<N>.exe and live
#     directly in a validated install dir.
#   - The script NEVER elevates a registry-selected executable (no -Verb RunAs):
#     machine-scope work requires re-running the whole script elevated, which
#     keeps a tampered user-writable file from becoming a privilege escalation.
#   - Recursive deletion refuses any path that contains, or sits beneath, a
#     reparse point (junction/symlink), and never descends into nested ones.
#   - Processes are killed only when their executable path resolves inside a
#     validated install dir; escalation is per-PID with StartTime+Path
#     revalidation, never machine-wide by image name.
#   - Registry uninstall keys are removed only for installations that were
#     actually handled (files gone or pending reboot); keys for unvalidated
#     installations are left in place and reported.
#
# Failure model:
#   - Step failures are recorded as WARNINGS and the run continues.
#   - A final verification phase inspects real machine state and FAILS CLOSED:
#     anything it cannot confirm clean becomes an UNRESOLVED item, and the
#     result succeeds only when zero items are unresolved.
#   - Locked paths scheduled for deletion on next reboot are tracked separately.
#
# Usage (paste into PowerShell, or host and pipe like the installer):
#   irm https://pythinker.com/uninstall.ps1 | iex
#
# User data (config, sessions, logs under $HOME\.pythinker):
#   $env:PYTHINKER_PURGE_DATA = "1"  -> delete it without asking (verified)
#   $env:PYTHINKER_PURGE_DATA = "0"  -> keep it without asking
#   unset                            -> ask once when interactive; keep otherwise

& {
  $ErrorActionPreference = "Stop"

  # Save/restore console encoding so the caller's session is untouched.
  $originalEncoding = $null
  try {
    $originalEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  } catch {}

  try {
    $AppId     = "{4F4F2EAE-9D55-4E8E-92BC-7C1FA38B6F02}_is1"
    $PurgeData = $env:PYTHINKER_PURGE_DATA
    $NoColor   = $env:NO_COLOR

    # --- Color detection (RawUI access can throw in some hosts; probe defensively)
    $ESC = [char]27
    $useColor = $false
    if (-not $NoColor) {
      try {
        if ($null -ne $Host.UI.RawUI) {
          $vt = $Host.UI.PSObject.Properties["SupportsVirtualTerminal"]
          if ($vt) { $useColor = [bool]$Host.UI.SupportsVirtualTerminal }
          else { $useColor = ([Environment]::OSVersion.Version.Major -ge 10) } # Win10+ conhost parses ANSI
        }
      } catch { $useColor = $false }
    }
    if ($useColor) {
      $NAVY  = "$ESC[38;5;24m"
      $FACE  = "$ESC[38;5;255m"
      $IRIS  = "$ESC[38;5;152m"
      $CORAL = "$ESC[38;5;216m"
      $DIM   = "$ESC[2m"
      $BOLD  = "$ESC[1m"
      $RESET = "$ESC[0m"
    } else {
      $NAVY = $FACE = $IRIS = $CORAL = $DIM = $BOLD = $RESET = ""
    }

    # --- All mutable state lives in one reference object inside this child
    # scope; functions read it via normal (dynamic) scope lookup. No $script:
    # variables exist, so nothing can leak into an iex caller's session.
    $State = [pscustomobject]@{
      RemovedCount  = 0
      Warnings      = New-Object System.Collections.Generic.List[string]
      Unresolved    = New-Object System.Collections.Generic.List[string]
      PendingReboot = New-Object System.Collections.Generic.List[string]
      DefaultInstallDir = $null
    }

    function Step($msg) { Write-Host "  $IRIS⠿$RESET $msg" }
    function OK($msg)   { Write-Host "  $IRIS✓$RESET $msg" }
    function Warn($msg) { Write-Host "  $CORAL!$RESET $msg" }
    function Dim($msg)  { Write-Host "  ${DIM}$msg${RESET}" }

    function Record-Removed($what) { $State.RemovedCount++; OK $what }

    function Format-Err($err) {
      if ($null -eq $err) { return "" }
      if ($err -is [System.Management.Automation.ErrorRecord]) { return $err.Exception.Message }
      return [string]$err
    }

    function Record-Warning($what, $err) {
      $detail = $what
      $message = Format-Err $err
      if ($message) { $detail = "$what — $message" }
      $State.Warnings.Add($detail)
      Warn $detail
    }

    function Record-Unresolved($what) {
      $State.Unresolved.Add($what)
      Warn $what
    }

    # Isolated step runner: a throwing step becomes a warning, never an abort.
    function Invoke-Step($Name, [scriptblock]$Action) {
      try { return & $Action }
      catch { Record-Warning $Name $_; return $null }
    }

    function Write-Header {
      Write-Host ""
      Write-Host "      $CORAL●$RESET"
      Write-Host "      $NAVY│$RESET"
      Write-Host "  $NAVY▛$RESET$FACE▀▀▀▀▀▀▀$RESET$NAVY▜$RESET"
      Write-Host " $CORAL◖$RESET$NAVY█$RESET $IRIS◉$RESET   $IRIS◉$RESET $NAVY█$RESET$CORAL◗$RESET"
      Write-Host "  $NAVY▙▄▄▄$RESET$FACE≡$RESET$NAVY▄▄▄▟$RESET"
      Write-Host ""
      Write-Host "  ${BOLD}${FACE}pythinker code${RESET} ${DIM}· uninstaller${RESET}"
      Write-Host ""
    }

    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
      throw "This uninstaller is for Windows."
    }

    $State.DefaultInstallDir = Join-Path $env:LOCALAPPDATA "Programs\Pythinker"
    $DataDir   = Join-Path $HOME ".pythinker"
    $TempRoot  = [System.IO.Path]::GetTempPath()

    function Test-IsAdmin {
      try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
      } catch { return $false }
    }

    function Test-Interactive {
      if ($env:CI -eq "true" -or $env:CI -eq "1") { return $false }
      try { if ([Console]::IsInputRedirected) { return $false } } catch { return $false }
      return ($Host.UI.RawUI -ne $null)
    }

    # Canonical form for PATH comparisons ONLY: trims quotes, expands env vars,
    # canonicalizes rooted paths (. / ..), strips trailing separators. Relative
    # tokens are returned un-canonicalized (never resolved against the cwd).
    # Original registry tokens are never rewritten — this is only a match key.
    function Get-NormalizedPathToken($Value) {
      if ($null -eq $Value) { return "" }
      $clean = $Value.Trim().Trim('"')
      if ($clean -eq "") { return "" }
      $expanded = [Environment]::ExpandEnvironmentVariables($clean)
      if ([IO.Path]::IsPathRooted($expanded)) {
        try { $expanded = [IO.Path]::GetFullPath($expanded) } catch { }
      }
      return $expanded.TrimEnd('\', '/')
    }

    # True when the path itself or any existing ancestor is a reparse point
    # (junction/symlink). Fails CLOSED when inspection is impossible.
    function Test-PathHasReparseComponent($Path) {
      $p = $Path
      while ($p -and -not (Test-Path -LiteralPath $p)) {
        $p = Split-Path -Parent $p
      }
      if (-not $p) { return $false }
      try {
        $current = Get-Item -LiteralPath $p -Force -ErrorAction Stop
        while ($current) {
          if ($current.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $true }
          $current = $current.Parent
        }
        return $false
      } catch {
        Record-Warning "could not inspect reparse status of $Path — treating it as unsafe" $_
        return $true
      }
    }

    # Reparse-point directories inside a tree, without ever descending into
    # them (raw .NET enumeration; PS 5.1 provider traversal is not trusted).
    function Get-NestedReparsePoints($Root) {
      $found = New-Object System.Collections.Generic.List[string]
      $stack = New-Object System.Collections.Generic.Stack[string]
      $stack.Push($Root)
      while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        $entries = $null
        try { $entries = [IO.Directory]::EnumerateFileSystemEntries($dir) } catch { continue }
        foreach ($e in $entries) {
          $attrs = $null
          try { $attrs = [IO.File]::GetAttributes($e) } catch { continue }
          $isDir = [bool]($attrs -band [IO.FileAttributes]::Directory)
          if ($isDir -and ($attrs -band [IO.FileAttributes]::ReparsePoint)) { $found.Add($e); continue }
          if ($isDir) { $stack.Push($e) }
        }
      }
      return $found
    }

    # Full tree listing (files + dirs) that never descends into reparse-point
    # directories; the links themselves are returned as leaf directories.
    function Get-TreeSafe($Root) {
      $files = New-Object System.Collections.Generic.List[string]
      $dirs = New-Object System.Collections.Generic.List[string]
      $stack = New-Object System.Collections.Generic.Stack[string]
      $stack.Push($Root)
      while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        $entries = $null
        try { $entries = [IO.Directory]::EnumerateFileSystemEntries($dir) } catch { continue }
        foreach ($e in $entries) {
          $attrs = $null
          try { $attrs = [IO.File]::GetAttributes($e) } catch { continue }
          $isDir = [bool]($attrs -band [IO.FileAttributes]::Directory)
          if (-not $isDir) { $files.Add($e); continue }
          $dirs.Add($e)
          if ($attrs -band [IO.FileAttributes]::ReparsePoint) { continue } # link is a leaf
          $stack.Push($e)
        }
      }
      return [pscustomobject]@{ Files = $files; Dirs = $dirs }
    }

    # --- Path safety: the ONLY guard between a registry value and recursive
    # deletion / execution. Returns the canonical dir or $null.
    function Get-SafeInstallDirectory($Candidate) {
      if (-not $Candidate) { return $null }
      $expanded = [Environment]::ExpandEnvironmentVariables(($Candidate.Trim().Trim('"')))
      if (-not [IO.Path]::IsPathRooted($expanded)) {
        Record-Warning "ignoring non-absolute install path: $Candidate" $null
        return $null
      }
      try { $raw = [IO.Path]::GetFullPath($expanded) }
      catch { Record-Warning "ignoring malformed install path: $Candidate" $_; return $null }

      $root = ([IO.Path]::GetPathRoot($raw)).TrimEnd('\', '/')
      $full = $raw.TrimEnd('\', '/')
      if ($full -eq "" -or $full -ieq $root) {
        Record-Warning "refusing filesystem root as install dir: $raw" $null
        return $null
      }

      # Never touch critical directories or any ancestor of them.
      $critical = @(
        [Environment]::GetFolderPath("Windows"),
        [Environment]::GetFolderPath("ProgramFiles"),
        [Environment]::GetFolderPath("ProgramFilesX86"),
        [Environment]::GetFolderPath("UserProfile"),
        [Environment]::GetFolderPath("CommonApplicationData"),
        $env:SystemDrive
      ) | Where-Object { $_ }
      foreach ($c in $critical) {
        $cc = ([IO.Path]::GetFullPath($c)).TrimEnd('\', '/')
        if ($full -ieq $cc -or $cc.StartsWith($full + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
          Record-Warning "refusing critical directory as install dir: $full" $null
          return $null
        }
      }

      if ([IO.Path]::GetFileName($full) -ine "Pythinker") {
        Record-Warning "refusing directory not named 'Pythinker': $full" $null
        return $null
      }

      if (Test-PathHasReparseComponent $full) {
        Record-Warning "refusing path with a reparse-point component: $full" $null
        return $null
      }

      # The default location is always plausible; custom locations must contain
      # on-disk evidence of a real install.
      if ($full -ieq $State.DefaultInstallDir) { return $full }
      if ((Test-Path -LiteralPath (Join-Path $full "pythinker.exe")) -or
          (Test-Path -LiteralPath (Join-Path $full "unins000.exe"))) {
        return $full
      }
      Record-Warning "ignoring unrecognized install directory (no pythinker.exe or unins000.exe inside): $full" $null
      return $null
    }

    # An uninstaller executable is trusted only when it looks like an Inno
    # uninstaller (unins<N>.exe) AND lives directly in a validated install dir.
    function Test-TrustedUninstaller($Exe, $Dir) {
      if (-not $Exe -or -not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return $false }
      if ([IO.Path]::GetFileName($Exe) -notmatch '^unins\d+\.exe$') { return $false }
      try {
        $parent = ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Exe))).TrimEnd('\', '/')
        $dd = ([IO.Path]::GetFullPath($Dir)).TrimEnd('\', '/')
      } catch { return $false }
      return ($parent -ieq $dd)
    }

    function Test-PathUnderDirs($ProcessPath, $Dirs) {
      if (-not $ProcessPath) { return $false }
      try { $full = ([IO.Path]::GetFullPath($ProcessPath)).TrimEnd('\', '/') } catch { return $false }
      foreach ($d in $Dirs) {
        $dd = ([IO.Path]::GetFullPath($d)).TrimEnd('\', '/')
        if ($full.StartsWith($dd + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or $full -ieq $dd) { return $true }
      }
      return $false
    }

    # --- 1. Registry discovery (both hives, 32/64-bit views; handles always disposed)
    function Find-UninstallEntries {
      $uninstallPath = "Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"
      $entries = @()
      foreach ($hive in @("CurrentUser", "LocalMachine")) {
        foreach ($view in @("Registry64", "Registry32")) {
          $base = $null; $key = $null
          try {
            $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
            $key = $base.OpenSubKey($uninstallPath)
            if ($key) {
              $uninstall = $key.GetValue("UninstallString")
              $quiet = $key.GetValue("QuietUninstallString")
              $location = $key.GetValue("InstallLocation")
              if ($uninstall -or $quiet) {
                $entries += [pscustomobject]@{
                  Hive = $hive; View = $view
                  UninstallString = $uninstall; QuietUninstallString = $quiet
                  InstallLocation = $location
                }
              }
            }
            # key absent = not installed at this scope/view; not an error.
          } catch {
            Record-Warning "could not inspect $hive\$view uninstall registry" $_
          } finally {
            if ($key) { $key.Dispose() }
            if ($base) { $base.Dispose() }
          }
        }
      }
      return $entries
    }

    function Get-UninstallerPath($entry) {
      $raw = $entry.QuietUninstallString
      if (-not $raw) { $raw = $entry.UninstallString }
      if ($raw) {
        $match = [regex]::Match($raw, '^"([^"]+)"')
        if ($match.Success) { return $match.Groups[1].Value }
        $match = [regex]::Match($raw, '^(.*?\.exe)')
        if ($match.Success) { return $match.Groups[1].Value }
      }
      return $null
    }

    # Each registry entry becomes an installation record: hive/view, validated
    # dir (or $null), uninstaller exe, and whether that exe is trusted.
    # HKCU views alias the same key (no WOW64 redirection there), so user-scope
    # records are deduplicated across views.
    function Get-InstallationRecords($Entries) {
      $records = @()
      $seen = @{}
      foreach ($e in $Entries) {
        $dedupe = if ($e.Hive -eq "CurrentUser") {
          "CU|$($e.UninstallString)|$($e.QuietUninstallString)|$($e.InstallLocation)"
        } else {
          "LM|$($e.View)|$($e.UninstallString)|$($e.QuietUninstallString)|$($e.InstallLocation)"
        }
        if ($seen.ContainsKey($dedupe)) { continue }
        $seen[$dedupe] = $true

        $exe = Get-UninstallerPath $e
        $dir = $null
        $candidates = @($e.InstallLocation)
        if ($exe) { $candidates += (Split-Path -Parent $exe) }
        foreach ($c in $candidates) {
          if (-not $c) { continue }
          $dir = Get-SafeInstallDirectory $c
          if ($dir) { break }
        }
        $trusted = $false
        if ($exe -and $dir) { $trusted = Test-TrustedUninstaller $exe $dir }
        $records += [pscustomobject]@{
          Hive = $e.Hive; View = $e.View
          Dir = $dir; Uninstaller = $exe; Trusted = $trusted
        }
      }
      return $records
    }

    # --- 2. Stop processes, but only ones rooted in a validated install dir.
    function Stop-PythinkerProcesses($Dirs) {
      $procs = @(Get-Process -Name "pythinker*" -ErrorAction SilentlyContinue)
      if ($procs.Count -eq 0) { return }

      foreach ($p in $procs) {
        $procPath = $null
        try { $procPath = $p.Path } catch { $procPath = $null }
        if (-not $procPath) {
          Record-Warning "cannot inspect $($p.ProcessName) (PID $($p.Id)) — likely elevated; leaving it running rather than killing an unidentified process" $null
          continue
        }
        if (-not (Test-PathUnderDirs $procPath $Dirs)) {
          Dim "skipping $($p.ProcessName) (PID $($p.Id)) — $procPath is outside the install dir"
          continue
        }
        $start = $null
        try { $start = $p.StartTime } catch { $start = $null }

        Step "Stopping $($p.ProcessName) (PID $($p.Id))"
        Invoke-Step "could not stop $($p.ProcessName) (PID $($p.Id))" {
          Stop-Process -Id $p.Id -Force -ErrorAction Stop
        } | Out-Null

        $survivor = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
        if (-not $survivor) { continue }

        # Revalidate identity before per-PID escalation (PID reuse race).
        $sameStart = $false
        if ($start) { try { $sameStart = ($survivor.StartTime -eq $start) } catch { $sameStart = $false } }
        $survivorPath = $null
        try { $survivorPath = $survivor.Path } catch { $survivorPath = $null }
        $samePath = ($survivorPath -and ($survivorPath -ieq $procPath))
        if (-not ($sameStart -and $samePath)) {
          Record-Warning "PID $($p.Id) identity changed after the stop attempt — refusing taskkill escalation (possible PID reuse)" $null
          continue
        }
        $taskkill = Get-Command taskkill.exe -ErrorAction SilentlyContinue
        if ($taskkill) {
          Invoke-Step "taskkill failed for PID $($p.Id)" {
            $out = & taskkill.exe /F /T /PID $p.Id 2>&1
            if ($LASTEXITCODE -ne 0) { throw "$out" }
          } | Out-Null
        }
      }
    }

    # --- 3. Run a trusted Inno uninstaller silently. NEVER elevates a
    # registry-selected executable: machine-scope runs require an elevated shell.
    function Invoke-InnoUninstaller($Exe, $Scope) {
      if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
        Record-Warning "registered uninstaller missing on disk: $Exe — using manual cleanup" $null
        return
      }
      if ([IO.Path]::GetFileName($Exe) -notmatch '^unins\d+\.exe$') {
        Record-Warning "refusing to run an executable that is not an Inno uninstaller: $Exe" $null
        return
      }
      $parent = Split-Path -Parent $Exe
      if (-not (Get-SafeInstallDirectory $parent)) {
        Record-Warning "refusing to run uninstaller from an unvalidated directory: $Exe" $null
        return
      }
      if ($Scope -eq "LocalMachine" -and -not (Test-IsAdmin)) {
        Record-Warning "machine-scope uninstall requires elevation — re-run this script from an Administrator PowerShell instead of elevating a registry-selected executable" $null
        return
      }

      Step "Running Pythinker uninstaller ($Scope scope)"
      $uninstArgs = @("/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES")
      try {
        $process = Start-Process -FilePath $Exe -ArgumentList $uninstArgs -Wait -PassThru -ErrorAction Stop
        if ($process.ExitCode -ne 0) {
          Record-Warning "uninstaller exited with code $($process.ExitCode) — sweeping what remains" $null
          return
        }
        OK "Uninstaller completed"
      } catch {
        Record-Warning "could not launch uninstaller $Exe — using manual cleanup" $_
      }
    }

    # --- 4. Removal helpers

    # MoveFileEx(MOVEFILE_DELAY_UNTIL_REBOOT) — best-effort last resort for
    # locked paths. Idempotent across repeated runs in the same session.
    function Initialize-PendingDelete {
      if ("Win32.PendingDelete" -as [type]) { return $true }
      try {
        $sig = '[DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool MoveFileEx(string lpExistingFileName, string lpNewFileName, int dwFlags);'
        Add-Type -Namespace Win32 -Name PendingDelete -MemberDefinition $sig -ErrorAction Stop
      } catch { }
      return ($null -ne ("Win32.PendingDelete" -as [type]))
    }

    function Register-PendingDeleteTree($Path) {
      if (-not (Initialize-PendingDelete)) { return $false }
      $MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
      $ok = $true
      $tree = Get-TreeSafe $Path
      foreach ($f in $tree.Files) {
        try {
          if (-not [Win32.PendingDelete]::MoveFileEx($f, $null, $MOVEFILE_DELAY_UNTIL_REBOOT)) { $ok = $false }
        } catch { $ok = $false }
      }
      # Deepest directories first so they are empty when their turn comes.
      foreach ($d in @($tree.Dirs | Sort-Object { $_.Length } -Descending)) {
        try {
          if (-not [Win32.PendingDelete]::MoveFileEx($d, $null, $MOVEFILE_DELAY_UNTIL_REBOOT)) { $ok = $false }
        } catch { $ok = $false }
      }
      try {
        if (-not [Win32.PendingDelete]::MoveFileEx($Path, $null, $MOVEFILE_DELAY_UNTIL_REBOOT)) { $ok = $false }
      } catch { $ok = $false }
      return $ok
    }

    # Remove a file/dir with retry + backoff, pending-delete-on-reboot fallback,
    # an absolute refusal to touch a filesystem root, and fail-closed reparse
    # protection (never recurse through junctions/symlinks).
    function Remove-PathRobust($Path, $What) {
      if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
      try {
        $pathRoot = ([IO.Path]::GetPathRoot($Path)).TrimEnd('\', '/')
        if ($Path.TrimEnd('\', '/') -ieq $pathRoot) {
          Record-Unresolved "refusing to remove filesystem root: $Path"
          return
        }
      } catch { Record-Unresolved "refusing malformed path: $Path"; return }

      if (Test-PathHasReparseComponent $Path) {
        Record-Unresolved "refusing recursive deletion through a reparse point: $Path — inspect and remove it manually"
        return
      }
      $nested = @(Get-NestedReparsePoints $Path)
      if ($nested.Count -gt 0) {
        Record-Unresolved "refusing recursive deletion: $($nested.Count) reparse point(s) inside $Path (first: $($nested[0])) — remove them manually"
        return
      }

      $lastErr = $null
      for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
          Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
          if (-not (Test-Path -LiteralPath $Path)) { Record-Removed $What; return }
        } catch {
          $lastErr = $_
          if ($attempt -lt 3) { Start-Sleep -Milliseconds (400 * $attempt) }
        }
      }
      if (Register-PendingDeleteTree $Path) {
        $State.PendingReboot.Add($Path)
        OK "$What — locked now; scheduled for deletion on next reboot"
        return
      }
      $hint = $lastErr
      if (-not (Test-IsAdmin)) { $hint = "$lastErr (retry from an Administrator PowerShell may succeed)" }
      Record-Warning "could not remove $What" $hint
    }

    # Remove one directory from a registry PATH value via the .NET registry API:
    # missing value = NotFound (not an error), original value kind preserved,
    # non-matching entries kept verbatim. Comparison expands env vars + quotes.
    function Remove-PathEntry($Dir, $Hive) {
      $subkey = if ($Hive -eq "CurrentUser") { "Environment" } else { "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" }
      $label = if ($Hive -eq "CurrentUser") { "user PATH" } else { "system PATH" }
      $base = $null; $key = $null
      try {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, "Registry64")
        $key = $base.OpenSubKey($subkey, $false)
        if ($null -eq $key) { return "NotFound" }
        $current = $key.GetValue("Path", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($null -eq $current) { return "NotFound" } # no user/system Path value is normal
        $kind = $key.GetValueKind("Path")
        $key.Dispose(); $key = $null

        $target = Get-NormalizedPathToken $Dir
        $matched = $false
        $kept = @(
          foreach ($e in ([string]$current -split ';')) {
            if ((Get-NormalizedPathToken $e) -ieq $target) { $matched = $true } else { $e }
          }
        )
        if (-not $matched) { return "NotFound" }
        $newPath = $kept -join ';'

        if ($Hive -eq "LocalMachine" -and -not (Test-IsAdmin)) {
          Record-Warning "$label still contains $Dir — re-run from an Administrator PowerShell to clean it" $null
          return "Failed"
        }
        $key = $base.OpenSubKey($subkey, $true)
        if ($null -eq $key) { Record-Warning "could not open $label for writing" $null; return "Failed" }
        $key.SetValue("Path", $newPath, $kind)
        Record-Removed "removed $Dir from $label"
        return "Removed"
      } catch {
        if ($Hive -eq "LocalMachine" -and -not (Test-IsAdmin)) {
          Record-Warning "$label could not be inspected without elevation — if it contains $Dir, re-run from an Administrator PowerShell" $_
        } else {
          Record-Warning "could not update $label" $_
        }
        return "Failed"
      } finally {
        if ($key) { $key.Dispose() }
        if ($base) { $base.Dispose() }
      }
    }

    # True when a registry PATH value still contains any of $Dirs; $null when it
    # could not be determined (caller must treat $null as UNRESOLVED).
    function Test-PathEntryPresent($Dirs, $Hive) {
      $subkey = if ($Hive -eq "CurrentUser") { "Environment" } else { "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" }
      $base = $null; $key = $null
      try {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, "Registry64")
        $key = $base.OpenSubKey($subkey, $false)
        if ($null -eq $key) { return $false }
        $current = $key.GetValue("Path", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($null -eq $current) { return $false }
        $targets = @($Dirs | ForEach-Object { Get-NormalizedPathToken $_ })
        foreach ($e in ([string]$current -split ';')) {
          $probe = Get-NormalizedPathToken $e
          foreach ($t in $targets) { if ($probe -ieq $t) { return $true } }
        }
        return $false
      } catch {
        return $null
      } finally {
        if ($key) { $key.Dispose() }
        if ($base) { $base.Dispose() }
      }
    }

    # Strip the install dirs from this session's PATH so the current window is
    # usable immediately. Non-matching tokens (including empty ones) are kept verbatim.
    function Remove-SessionPathEntries($Dirs) {
      try {
        $targets = @($Dirs | Where-Object { $_ } | ForEach-Object { Get-NormalizedPathToken $_ })
        if ($targets.Count -eq 0) { return }
        $kept = @($env:PATH -split ';' | Where-Object {
            $probe = Get-NormalizedPathToken $_
            ($targets | Where-Object { $probe -ieq $_ }).Count -eq 0
          })
        $newPath = $kept -join ';'
        if ($newPath -ne $env:PATH) { $env:PATH = $newPath; OK "Cleaned PATH for this session" }
      } catch { Record-Warning "could not clean this session's PATH" $_ }
    }

    # Tell Explorer & new processes the PATH changed. Idempotent across repeated runs.
    function Send-EnvironmentBroadcast {
      try {
        if (-not ("Win32.UninstallNativeMethods" -as [type])) {
          $sig = '[DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Auto)] public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);'
          Add-Type -Namespace Win32 -Name UninstallNativeMethods -MemberDefinition $sig -ErrorAction Stop
        }
        $result = [UIntPtr]::Zero
        [void][Win32.UninstallNativeMethods]::SendMessageTimeout([IntPtr]0xffff, 0x001A, [UIntPtr]::Zero, "Environment", 0x0002, 5000, [ref]$result)
      } catch { Record-Warning "could not broadcast the environment change to the desktop" $_ }
    }

    # Delete registry keys ONLY for installations that were actually handled:
    # validated dir and (files gone or pending reboot). Keys for unvalidated or
    # unfinished installations stay in place and are reported as unresolved.
    function Remove-HandledRegistryEntries($Records) {
      $uninstallPath = "Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"
      foreach ($r in $Records) {
        if (-not $r.Dir) {
          Record-Unresolved "registry entry for an unvalidated installation was left in place ($($r.Hive)\$($r.View)) — handle its files manually, then remove the key"
          continue
        }
        if ((Test-Path -LiteralPath $r.Dir) -and -not $State.PendingReboot.Contains($r.Dir)) {
          Record-Unresolved "registry entry left in place because files remain at $($r.Dir) ($($r.Hive)\$($r.View))"
          continue
        }
        $base = $null; $key = $null
        try {
          $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($r.Hive, $r.View)
          $key = $base.OpenSubKey($uninstallPath)
          if ($null -eq $key) { continue } # absent = already clean
          $key.Dispose(); $key = $null
          try {
            $base.DeleteSubKeyTree($uninstallPath, $false)
            Record-Removed "removed uninstall registry entry ($($r.Hive)\$($r.View))"
          } catch {
            if ($r.Hive -eq "LocalMachine" -and -not (Test-IsAdmin)) {
              Record-Warning "machine uninstall registry entry remains — re-run from an Administrator PowerShell to remove it" $null
            } else {
              Record-Warning "could not remove uninstall registry entry ($($r.Hive)\$($r.View))" $_
            }
          }
        } catch {
          Record-Warning "could not access $($r.Hive)\$($r.View) uninstall registry for cleanup" $_
        } finally {
          if ($key) { $key.Dispose() }
          if ($base) { $base.Dispose() }
        }
      }
    }

    # Stale installer bootstrap temp dirs left by interrupted installs. Removed
    # only when ALL ownership signals hold: strict name shape (GUID suffix),
    # older than 1 hour, no setup process running, and contents limited to the
    # exact installer asset names (PythinkerSetup-x.y.z.exe[.sha256]).
    function Remove-StaleInstallerTempDirs {
      $setupRunning = @(Get-Process -Name "PythinkerSetup*" -ErrorAction SilentlyContinue).Count -gt 0
      if ($setupRunning) {
        Dim "a Pythinker setup is currently running — leaving installer temp dirs alone"
        return
      }
      $cutoff = (Get-Date).AddHours(-1)
      $candidates = @(Get-ChildItem -LiteralPath $TempRoot -Directory -ErrorAction SilentlyContinue |
          Where-Object { $_.Name -match '^pythinker-install-[0-9a-fA-F]{32}$' -and $_.LastWriteTime -lt $cutoff })
      foreach ($t in $candidates) {
        $children = @(Get-ChildItem -LiteralPath $t.FullName -Force -ErrorAction SilentlyContinue)
        $foreign = @($children | Where-Object { $_.Name -notmatch '^PythinkerSetup-[\d.]+\.exe(\.sha256)?$' })
        if ($foreign.Count -gt 0) {
          Dim "skipping $($t.FullName) — contents do not match Pythinker installer assets"
          continue
        }
        Remove-PathRobust $t.FullName "installer temp dir $($t.FullName)"
      }
    }

    # --- 5. Optional user-data purge (verified by Test-FinalState)
    function Invoke-DataPurge {
      if (-not (Test-Path -LiteralPath $DataDir)) { return }

      $purge = $false
      if ($PurgeData -eq "1") {
        $purge = $true
      } elseif ($PurgeData -eq "0") {
        $purge = $false
      } elseif (Test-Interactive) {
        Write-Host ""
        Write-Host "  ${BOLD}User data found at $DataDir${RESET} ${DIM}(config, sessions, logs)${RESET}"
        try {
          $answer = Read-Host "  Delete it too? [y/N]"
          $purge = ($answer -match '^(?i)y(es)?$')
        } catch { $purge = $false }
      }

      if ($purge) {
        Remove-PathRobust $DataDir "user data $DataDir"
      } else {
        Write-Host ""
        Dim "User data kept at $DataDir — delete it manually or re-run with `$env:PYTHINKER_PURGE_DATA = '1'"
      }
    }

    # --- 6. Final verification, FAIL CLOSED: anything that cannot be confirmed
    # clean becomes unresolved. The result succeeds only at zero unresolved.
    function Test-FinalState($Dirs, $Records) {
      foreach ($dir in $Dirs) {
        if ((Test-Path -LiteralPath $dir) -and -not $State.PendingReboot.Contains($dir)) {
          Record-Unresolved "install directory still present: $dir"
        }
      }

      foreach ($hive in @("CurrentUser", "LocalMachine")) {
        $present = Test-PathEntryPresent $Dirs $hive
        $label = if ($hive -eq "CurrentUser") { "user PATH" } else { "system PATH" }
        if ($null -eq $present) {
          Record-Unresolved "could not verify $label state"
        } elseif ($present) {
          $msg = "$label still contains a Pythinker entry"
          if ($hive -eq "LocalMachine" -and -not (Test-IsAdmin)) { $msg += " — re-run from an Administrator PowerShell" }
          Record-Unresolved $msg
        }
      }

      # HKCU has no WOW64 redirection here, so one view is authoritative there.
      $combos = @(
        @("CurrentUser", "Registry64"),
        @("LocalMachine", "Registry64"),
        @("LocalMachine", "Registry32")
      )
      $uninstallPath = "Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"
      foreach ($combo in $combos) {
        $hive = $combo[0]; $view = $combo[1]
        $base = $null; $key = $null
        $exists = $null
        try {
          $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
          $key = $base.OpenSubKey($uninstallPath)
          $exists = ($null -ne $key)
        } catch {
          Record-Unresolved "could not verify $hive\$view uninstall registry state"
          continue
        } finally {
          if ($key) { $key.Dispose() }
          if ($base) { $base.Dispose() }
        }
        if ($exists) {
          $rec = @($Records | Where-Object { $_.Hive -eq $hive -and $_.View -eq $view } | Select-Object -First 1)
          if ($rec.Count -gt 0 -and -not $rec[0].Dir) { continue } # already reported as left-in-place
          $msg = "uninstall registry entry still present ($hive\$view)"
          if ($hive -eq "LocalMachine" -and -not (Test-IsAdmin)) { $msg += " — re-run from an Administrator PowerShell" }
          Record-Unresolved $msg
        }
      }

      $startDirs = @(
        (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Pythinker"),
        (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Pythinker")
      )
      foreach ($dir in $startDirs) {
        if ((Test-Path -LiteralPath $dir) -and -not $State.PendingReboot.Contains($dir)) {
          Record-Unresolved "Start Menu shortcuts still present: $dir"
        }
      }

      foreach ($p in @(Get-Process -Name "pythinker*" -ErrorAction SilentlyContinue)) {
        $procPath = $null
        try { $procPath = $p.Path } catch { $procPath = $null }
        if ($procPath -and (Test-PathUnderDirs $procPath $Dirs)) {
          Record-Unresolved "pythinker process still running from install dir (PID $($p.Id))"
        }
      }

      if ($PurgeData -eq "1" -and (Test-Path -LiteralPath $DataDir) -and -not $State.PendingReboot.Contains($DataDir)) {
        Record-Unresolved "requested user-data purge did not complete: $DataDir"
      }
    }

    # --- Main (function-wrapped: returns a structured result, never calls exit)
    function Invoke-PythinkerUninstall {
      # Last-resort safety net for anything no step caught. Scoped INSIDE this
      # function so it can never swallow the top-level failure signal below.
      trap { Record-Warning "unexpected error" $_; continue }

      Write-Header
      Step "Uninstalling Pythinker Code"

      $entries = @(Invoke-Step "could not read uninstall registry entries" { Find-UninstallEntries })
      if ($null -eq $entries) { $entries = @() }
      $records = @(Invoke-Step "installation record validation failed" { Get-InstallationRecords $entries })

      $installDirs = New-Object System.Collections.Generic.List[string]
      $defaultSafe = Get-SafeInstallDirectory $State.DefaultInstallDir
      if ($defaultSafe) {
        $installDirs.Add($defaultSafe)
      } else {
        Record-Unresolved "default install directory failed safety validation ($($State.DefaultInstallDir)) — manual removal may be required"
      }
      foreach ($r in $records) {
        if ($r.Dir -and -not $installDirs.Contains($r.Dir)) { $installDirs.Add($r.Dir) }
      }

      if ($records.Count -eq 0) {
        Dim "No registered Pythinker uninstaller found — running manual cleanup only"
      }

      Invoke-Step "process shutdown failed" { Stop-PythinkerProcesses $installDirs } | Out-Null

      foreach ($r in $records) {
        if (-not $r.Uninstaller) { continue }
        if (-not $r.Trusted) {
          Record-Warning "uninstaller failed trust validation (must be unins<N>.exe inside a validated install dir): $($r.Uninstaller)" $null
          continue
        }
        Invoke-Step "uninstaller run failed for $($r.Uninstaller)" { Invoke-InnoUninstaller $r.Uninstaller $r.Hive } | Out-Null
      }

      # Sweep everything, whether or not an uninstaller ran (idempotent).
      Step "Removing leftover files, PATH entries, shortcuts, and registry keys"

      foreach ($dir in $installDirs) {
        Remove-PathRobust $dir "install directory $dir"
        Invoke-Step "PATH cleanup failed for $dir" {
          [void](Remove-PathEntry $dir "CurrentUser")
          [void](Remove-PathEntry $dir "LocalMachine")
        } | Out-Null
      }
      Remove-SessionPathEntries $installDirs

      $startDirs = @(
        (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Pythinker"),
        (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Pythinker")
      )
      foreach ($dir in $startDirs) { Remove-PathRobust $dir "Start Menu shortcuts ($dir)" }

      Invoke-Step "registry entry handling failed" { Remove-HandledRegistryEntries $records } | Out-Null
      Invoke-Step "temp cleanup failed" { Remove-StaleInstallerTempDirs } | Out-Null

      Send-EnvironmentBroadcast
      Invoke-Step "user data step failed" { Invoke-DataPurge } | Out-Null

      # Final verification must never degrade to a warning: unknown = unresolved.
      Step "Verifying final state"
      try {
        Test-FinalState $installDirs $records
      } catch {
        Record-Unresolved "final verification did not complete — $(Format-Err $_)"
      }

      # --- Summary
      Write-Host ""
      Write-Host "  ${BOLD}${FACE}Uninstall summary${RESET}"
      Write-Host "    $IRIS$($State.RemovedCount) item(s) removed$RESET"

      if ($State.PendingReboot.Count -gt 0) {
        Write-Host "    $CORAL$($State.PendingReboot.Count) item(s) scheduled for deletion on next reboot:${RESET}"
        foreach ($p in $State.PendingReboot) { Write-Host "      $CORAL•$RESET $p" }
      }

      if ($State.Warnings.Count -gt 0) {
        Write-Host "    ${DIM}$($State.Warnings.Count) transient warning(s) during the run:${RESET}"
        foreach ($w in $State.Warnings) { Dim "      • $w" }
      }

      Write-Host ""
      if ($State.Unresolved.Count -gt 0) {
        Write-Host "  $CORAL$($State.Unresolved.Count) thing(s) could not be fully removed or verified:${RESET}"
        foreach ($u in $State.Unresolved) { Write-Host "      $CORAL•$RESET $u" }
        Write-Host ""
        Dim "Most permission issues resolve by re-running this script from an Administrator PowerShell."
        Write-Host ""
      } elseif ($State.PendingReboot.Count -gt 0) {
        Write-Host "  ${BOLD}${IRIS}pythinker$RESET cleanup complete — ${BOLD}a reboot is required${RESET} to finish removing $($State.PendingReboot.Count) locked item(s)."
        Write-Host ""
      } else {
        Write-Host "  ${BOLD}${IRIS}pythinker$RESET has been uninstalled. Open a fresh PowerShell for PATH changes to apply."
        Write-Host ""
      }

      return [pscustomobject]@{
        Success       = ($State.Unresolved.Count -eq 0)
        Removed       = $State.RemovedCount
        PendingReboot = @($State.PendingReboot)
        Warnings      = @($State.Warnings)
        Unresolved    = @($State.Unresolved)
      }
    }

    $result = Invoke-PythinkerUninstall
    if ($null -eq $result) {
      # The function's trap should make this unreachable, but a swallowed
      # failure must never look like success.
      throw "Pythinker uninstall did not produce a result."
    }
    if (-not $result.Success) {
      # Top-level throw, OUTSIDE the function trap: `irm | iex` shows the error
      # and the host stays open; `powershell.exe -File` exits non-zero.
      throw "Pythinker uninstall completed with $($result.Unresolved.Count) unresolved issue(s) — see summary above."
    }
  } finally {
    if ($originalEncoding) {
      try { [Console]::OutputEncoding = $originalEncoding } catch {}
    }
  }
}
