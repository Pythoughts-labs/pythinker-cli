# Pythinker Code — native Windows uninstaller.
#
# Usage:
#   irm https://pythinker.com/uninstall.ps1 | iex
#
# User data policy:
#   $env:PYTHINKER_PURGE_DATA = "1"  # delete $HOME\.pythinker without asking
#   $env:PYTHINKER_PURGE_DATA = "0"  # keep it without asking
#   unset                             # ask once when interactive; keep otherwise
#
# Safety and failure model:
#   - Runs inside an anonymous child scope and restores console encoding.
#   - Never calls exit and never executes a registry-selected binary while elevated.
#   - Never recursively deletes a registry-selected custom directory.
#   - Only known installation directories are eligible for manual recursive sweep.
#   - Registry-selected custom installations are never executed or recursively
#     swept automatically; they are left intact and reported for manual action.
#   - Elevated runs never execute HKCU or user-writable uninstallers.
#   - Recursive deletion fails closed on roots, UNC/device paths, reparse points,
#     incomplete tree inspection, and malformed paths.
#   - Process escalation is per-PID with StartTime and executable-path revalidation.
#   - Final verification fails closed: unknown state is unresolved, not success.
#   - Requires Windows PowerShell 5.1+ or PowerShell 7+ on Windows.

& {
  $ErrorActionPreference = "Stop"

  $originalEncoding = $null
  try {
    $originalEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  } catch {}

  try {
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
      throw "This uninstaller is for Windows."
    }

    # -------------------------------------------------------------------------
    # Constants and state
    # -------------------------------------------------------------------------

    $AppId = "{4F4F2EAE-9D55-4E8E-92BC-7C1FA38B6F02}_is1"
    $PurgeDataSetting = $env:PYTHINKER_PURGE_DATA
    $NoColor = $env:NO_COLOR

    $DefaultInstallDir = Join-Path $env:LOCALAPPDATA "Programs\Pythinker"
    $DataDir = Join-Path $HOME ".pythinker"
    $TempRoot = [System.IO.Path]::GetTempPath()

    $State = [pscustomobject]@{
      RemovedCount       = 0
      Warnings           = New-Object System.Collections.Generic.List[string]
      Unresolved         = New-Object System.Collections.Generic.List[string]
      PendingReboot      = New-Object System.Collections.Generic.List[string]
      TempCleanupTargets = New-Object System.Collections.Generic.List[string]
      PurgeDataRequested = $false
      EnvironmentChanged = $false
    }

    # -------------------------------------------------------------------------
    # Output helpers
    # -------------------------------------------------------------------------

    $ESC = [char]27
    $useColor = $false
    if (-not $NoColor) {
      try {
        $vtProperty = $Host.UI.PSObject.Properties["SupportsVirtualTerminal"]
        if ($vtProperty) {
          $useColor = [bool]$Host.UI.SupportsVirtualTerminal
        } elseif ($env:WT_SESSION -or $env:TERM_PROGRAM) {
          $useColor = $true
        }
      } catch {
        $useColor = $false
      }
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

    function Step($Message) { Write-Host "  $IRIS⠿$RESET $Message" }
    function OK($Message)   { Write-Host "  $IRIS✓$RESET $Message" }
    function Warn($Message) { Write-Host "  $CORAL!$RESET $Message" }
    function Dim($Message)  { Write-Host "  ${DIM}$Message${RESET}" }

    function Format-ErrorMessage($ErrorObject) {
      if ($null -eq $ErrorObject) { return "" }
      if ($ErrorObject -is [System.Management.Automation.ErrorRecord]) {
        return [string]$ErrorObject.Exception.Message
      }
      return [string]$ErrorObject
    }

    function Test-ListContainsInsensitive($List, [string]$Value) {
      foreach ($item in $List) {
        if ([string]::Equals([string]$item, $Value, [System.StringComparison]::OrdinalIgnoreCase)) {
          return $true
        }
      }
      return $false
    }

    function Add-UniqueString($List, [string]$Value) {
      if (-not (Test-ListContainsInsensitive $List $Value)) {
        [void]$List.Add($Value)
      }
    }

    function Record-Removed([string]$What) {
      $State.RemovedCount = [int]$State.RemovedCount + 1
      OK $What
    }

    function Record-Warning([string]$What, $ErrorObject = $null) {
      $detail = $What
      $message = Format-ErrorMessage $ErrorObject
      if ($message) { $detail = "$What — $message" }
      if (-not (Test-ListContainsInsensitive $State.Warnings $detail)) {
        [void]$State.Warnings.Add($detail)
        Warn $detail
      }
    }

    function Record-Unresolved([string]$What) {
      if (-not (Test-ListContainsInsensitive $State.Unresolved $What)) {
        [void]$State.Unresolved.Add($What)
        Warn $What
      }
    }

    function Invoke-Step([string]$Name, [scriptblock]$Action) {
      try {
        return & $Action
      } catch {
        Record-Warning $Name $_
        return $null
      }
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

    # -------------------------------------------------------------------------
    # Platform and path helpers
    # -------------------------------------------------------------------------

    function Test-IsAdmin {
      try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object -TypeName Security.Principal.WindowsPrincipal -ArgumentList $identity
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
      } catch {
        return $false
      }
    }

    function Test-Interactive {
      if ($env:CI -eq "true" -or $env:CI -eq "1") { return $false }
      try {
        if ([Console]::IsInputRedirected) { return $false }
        return ($null -ne $Host.UI.RawUI)
      } catch {
        return $false
      }
    }

    function Get-CanonicalPath($Path) {
      if ([string]::IsNullOrWhiteSpace([string]$Path)) { return $null }
      $clean = [Environment]::ExpandEnvironmentVariables(([string]$Path).Trim().Trim('"'))
      if ($clean -match '^[A-Za-z]:(?:$|[^\x5c/])') { return $null } # reject drive-relative paths such as C:foo
      if (-not [IO.Path]::IsPathRooted($clean)) { return $null }
      try {
        return ([IO.Path]::GetFullPath($clean)).TrimEnd('\', '/')
      } catch {
        return $null
      }
    }

    function Test-LocalDrivePath($Path) {
      $full = Get-CanonicalPath $Path
      if (-not $full) { return $false }
      try {
        $root = [IO.Path]::GetPathRoot($full)
        return ($root -match '^[A-Za-z]:\\$')
      } catch {
        return $false
      }
    }

    function Get-NormalizedPathToken($Value) {
      if ($null -eq $Value) { return "" }
      $clean = ([string]$Value).Trim().Trim('"')
      if ($clean -eq "") { return "" }
      $expanded = [Environment]::ExpandEnvironmentVariables($clean)
      $driveRelative = ($expanded -match '^[A-Za-z]:(?:$|[^\x5c/])')
      if (-not $driveRelative -and [IO.Path]::IsPathRooted($expanded)) {
        try { $expanded = [IO.Path]::GetFullPath($expanded) } catch {}
      }
      return $expanded.TrimEnd('\', '/')
    }

    function Test-PathEqual($Left, $Right) {
      $a = Get-NormalizedPathToken $Left
      $b = Get-NormalizedPathToken $Right
      if ($a -eq "" -or $b -eq "") { return $false }
      return [string]::Equals($a, $b, [System.StringComparison]::OrdinalIgnoreCase)
    }

    function Test-PathUnderDirs($Path, $Directories) {
      $full = Get-CanonicalPath $Path
      if (-not $full) { return $false }
      foreach ($directory in $Directories) {
        $parent = Get-CanonicalPath $directory
        if (-not $parent) { continue }
        if ([string]::Equals($full, $parent, [System.StringComparison]::OrdinalIgnoreCase)) {
          return $true
        }
        $prefix = $parent.TrimEnd('\') + [IO.Path]::DirectorySeparatorChar
        if ($full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
          return $true
        }
      }
      return $false
    }

    function Add-UniquePath($List, $Path) {
      $full = Get-CanonicalPath $Path
      if (-not $full) { return }
      foreach ($existing in $List) {
        if (Test-PathEqual $existing $full) { return }
      }
      [void]$List.Add($full)
    }

    function Test-PendingReboot($Path) {
      foreach ($pending in $State.PendingReboot) {
        if (Test-PathEqual $pending $Path) { return $true }
      }
      return $false
    }

    function Add-PendingReboot($Path) {
      $full = Get-CanonicalPath $Path
      if (-not $full) { $full = [string]$Path }
      Add-UniquePath $State.PendingReboot $full
    }

    function Get-KnownInstallDirectories {
      $directories = New-Object System.Collections.Generic.List[string]
      Add-UniquePath $directories $DefaultInstallDir

      $programFiles = [Environment]::GetFolderPath([System.Environment+SpecialFolder]::ProgramFiles)
      if ($programFiles) { Add-UniquePath $directories (Join-Path $programFiles "Pythinker") }

      $programFilesX86 = [Environment]::GetFolderPath([System.Environment+SpecialFolder]::ProgramFilesX86)
      if ($programFilesX86) { Add-UniquePath $directories (Join-Path $programFilesX86 "Pythinker") }

      return $directories
    }

    $KnownInstallDirs = Get-KnownInstallDirectories

    function Test-KnownInstallDirectory($Path) {
      foreach ($known in $KnownInstallDirs) {
        if (Test-PathEqual $Path $known) { return $true }
      }
      return $false
    }

    function Test-PathHasReparseComponent($Path) {
      $full = Get-CanonicalPath $Path
      if (-not $full) { return $true }

      $current = $full
      while ($current) {
        try {
          if (Test-Path -LiteralPath $current -ErrorAction Stop) {
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
              return $true
            }
          }
        } catch {
          Record-Warning "could not inspect reparse status of $Path — treating it as unsafe" $_
          return $true
        }

        $parent = Split-Path -Parent $current
        if (-not $parent -or (Test-PathEqual $parent $current)) { break }
        $current = $parent
      }

      return $false
    }

    function Get-SafeTreeSnapshot($Root) {
      $files = New-Object System.Collections.Generic.List[string]
      $directories = New-Object System.Collections.Generic.List[string]
      $reparsePoints = New-Object System.Collections.Generic.List[string]
      $errors = New-Object System.Collections.Generic.List[string]
      $complete = $true
      $rootIsDirectory = $false

      $fullRoot = Get-CanonicalPath $Root
      if (-not $fullRoot) {
        [void]$errors.Add("invalid or non-local root path")
        return [pscustomobject]@{
          Complete      = $false
          Root          = [string]$Root
          RootIsDirectory = $false
          Files         = $files
          Dirs          = $directories
          ReparsePoints = $reparsePoints
          Errors        = $errors
        }
      }

      try {
        $rootItem = Get-Item -LiteralPath $fullRoot -Force -ErrorAction Stop
        if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
          [void]$reparsePoints.Add($fullRoot)
        }
        $rootIsDirectory = [bool]($rootItem.Attributes -band [IO.FileAttributes]::Directory)
        if (-not $rootIsDirectory) {
          [void]$files.Add($fullRoot)
          return [pscustomobject]@{
            Complete        = $true
            Root            = $fullRoot
            RootIsDirectory = $false
            Files           = $files
            Dirs            = $directories
            ReparsePoints   = $reparsePoints
            Errors          = $errors
          }
        }
      } catch {
        [void]$errors.Add((Format-ErrorMessage $_))
        return [pscustomobject]@{
          Complete        = $false
          Root            = $fullRoot
          RootIsDirectory = $false
          Files           = $files
          Dirs            = $directories
          ReparsePoints   = $reparsePoints
          Errors          = $errors
        }
      }

      $stack = New-Object System.Collections.Generic.Stack[string]
      $stack.Push($fullRoot)

      while ($stack.Count -gt 0) {
        $directory = $stack.Pop()
        $entries = $null
        try {
          $entries = @([IO.Directory]::EnumerateFileSystemEntries($directory))
        } catch {
          $complete = $false
          [void]$errors.Add("$directory — $(Format-ErrorMessage $_)")
          continue
        }

        foreach ($entry in $entries) {
          $attributes = $null
          try {
            $attributes = [IO.File]::GetAttributes($entry)
          } catch {
            $complete = $false
            [void]$errors.Add("$entry — $(Format-ErrorMessage $_)")
            continue
          }

          if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
            [void]$reparsePoints.Add($entry)
            continue
          }

          if ($attributes -band [IO.FileAttributes]::Directory) {
            [void]$directories.Add($entry)
            $stack.Push($entry)
          } else {
            [void]$files.Add($entry)
          }
        }
      }

      return [pscustomobject]@{
        Complete        = $complete
        Root            = $fullRoot
        RootIsDirectory = $rootIsDirectory
        Files           = $files
        Dirs            = $directories
        ReparsePoints   = $reparsePoints
        Errors          = $errors
      }
    }

    function Get-SafeInstallDirectory($Candidate) {
      if ([string]::IsNullOrWhiteSpace([string]$Candidate)) { return $null }

      $full = Get-CanonicalPath $Candidate
      if (-not $full) {
        Record-Warning "ignoring malformed or non-absolute install path: $Candidate" $null
        return $null
      }

      if (-not (Test-LocalDrivePath $full)) {
        Record-Warning "ignoring non-local, UNC, or device install path: $full" $null
        return $null
      }

      $root = ([IO.Path]::GetPathRoot($full)).TrimEnd('\', '/')
      if ($full -ieq $root) {
        Record-Warning "refusing filesystem root as install directory: $full" $null
        return $null
      }

      if (-not [string]::Equals([IO.Path]::GetFileName($full), "Pythinker", [System.StringComparison]::OrdinalIgnoreCase)) {
        Record-Warning "refusing install directory not named 'Pythinker': $full" $null
        return $null
      }

      if (Test-PathHasReparseComponent $full) {
        Record-Warning "refusing install path with a reparse-point component: $full" $null
        return $null
      }

      $windowsDir = [Environment]::GetFolderPath([System.Environment+SpecialFolder]::Windows)
      $programData = [Environment]::GetFolderPath([System.Environment+SpecialFolder]::CommonApplicationData)
      $forbiddenRoots = @($windowsDir, $programData) | Where-Object { $_ }
      if (Test-PathUnderDirs $full $forbiddenRoots) {
        Record-Warning "refusing install path below a protected Windows directory: $full" $null
        return $null
      }

      $criticalTargets = @(
        $windowsDir,
        [Environment]::GetFolderPath([System.Environment+SpecialFolder]::ProgramFiles),
        [Environment]::GetFolderPath([System.Environment+SpecialFolder]::ProgramFilesX86),
        [Environment]::GetFolderPath([System.Environment+SpecialFolder]::UserProfile),
        $programData,
        [IO.Path]::GetPathRoot($env:SystemRoot)
      ) | Where-Object { $_ }

      foreach ($critical in $criticalTargets) {
        if (Test-PathUnderDirs $critical @($full)) {
          Record-Warning "refusing install path that is an ancestor of a critical directory: $full" $null
          return $null
        }
      }

      $exists = $false
      $isDirectory = $false
      try {
        $exists = Test-Path -LiteralPath $full -ErrorAction Stop
        if ($exists) { $isDirectory = Test-Path -LiteralPath $full -PathType Container -ErrorAction Stop }
      } catch {
        Record-Warning "could not inspect install directory: $full" $_
        return $null
      }

      if (-not $exists) {
        # A missing, lexically safe directory can represent a stale registry entry.
        return $full
      }
      if (-not $isDirectory) {
        Record-Warning "refusing install path that is not a directory: $full" $null
        return $null
      }

      if (Test-KnownInstallDirectory $full) { return $full }

      $evidence = @(
        (Join-Path $full "pythinker.exe"),
        (Join-Path $full "pythinker-code.exe")
      )
      foreach ($candidateFile in $evidence) {
        if (Test-Path -LiteralPath $candidateFile -PathType Leaf -ErrorAction SilentlyContinue) {
          return $full
        }
      }

      $innoFiles = @(Get-ChildItem -LiteralPath $full -File -Filter "unins*.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^unins\d+\.exe$' })
      if ($innoFiles.Count -gt 0) { return $full }

      Record-Warning "ignoring unrecognized custom install directory with no product evidence: $full" $null
      return $null
    }

    function Test-TrustedUninstaller($Exe, $Directory) {
      if (-not $Exe -or -not $Directory) { return $false }
      if (-not (Test-Path -LiteralPath $Exe -PathType Leaf -ErrorAction SilentlyContinue)) { return $false }
      if ([IO.Path]::GetFileName($Exe) -notmatch '^unins\d+\.exe$') { return $false }
      if (Test-PathHasReparseComponent $Exe) { return $false }

      try {
        $item = Get-Item -LiteralPath $Exe -Force -ErrorAction Stop
        if ($item.Length -le 0) { return $false }
        $parent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Exe))
      } catch {
        return $false
      }

      return (Test-PathEqual $parent $Directory)
    }

    # -------------------------------------------------------------------------
    # Registry discovery and installation records
    # -------------------------------------------------------------------------

    function Get-NativeRegistryView {
      if ([Environment]::Is64BitOperatingSystem) {
        return [Microsoft.Win32.RegistryView]::Registry64
      }
      return [Microsoft.Win32.RegistryView]::Registry32
    }

    function Get-UninstallRegistryCombos {
      $combos = New-Object System.Collections.Generic.List[object]
      $nativeView = Get-NativeRegistryView
      [void]$combos.Add([pscustomobject]@{ Hive = "CurrentUser"; View = $nativeView })
      if ([Environment]::Is64BitOperatingSystem) {
        [void]$combos.Add([pscustomobject]@{ Hive = "LocalMachine"; View = [Microsoft.Win32.RegistryView]::Registry64 })
        [void]$combos.Add([pscustomobject]@{ Hive = "LocalMachine"; View = [Microsoft.Win32.RegistryView]::Registry32 })
      } else {
        [void]$combos.Add([pscustomobject]@{ Hive = "LocalMachine"; View = [Microsoft.Win32.RegistryView]::Registry32 })
      }
      return $combos
    }

    function Get-RegistryHiveEnum([string]$Hive) {
      if ($Hive -eq "CurrentUser") { return [Microsoft.Win32.RegistryHive]::CurrentUser }
      return [Microsoft.Win32.RegistryHive]::LocalMachine
    }

    function Find-UninstallEntries {
      $uninstallPath = "Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"
      $entries = New-Object System.Collections.Generic.List[object]

      foreach ($combo in (Get-UninstallRegistryCombos)) {
        $base = $null
        $key = $null
        try {
          $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey((Get-RegistryHiveEnum $combo.Hive), $combo.View)
          $key = $base.OpenSubKey($uninstallPath, $false)
          if ($null -eq $key) { continue }

          [void]$entries.Add([pscustomobject]@{
            Hive                 = $combo.Hive
            View                 = $combo.View
            UninstallString      = $key.GetValue("UninstallString")
            QuietUninstallString = $key.GetValue("QuietUninstallString")
            InstallLocation      = $key.GetValue("InstallLocation")
            DisplayName          = $key.GetValue("DisplayName")
          })
        } catch {
          Record-Warning "could not inspect $($combo.Hive)\$($combo.View) uninstall registry" $_
        } finally {
          if ($key) { $key.Dispose() }
          if ($base) { $base.Dispose() }
        }
      }

      return $entries
    }

    function Get-UninstallerPath($Entry) {
      $raw = $Entry.QuietUninstallString
      if (-not $raw) { $raw = $Entry.UninstallString }
      if (-not $raw) { return $null }

      $rawText = [string]$raw
      $match = [regex]::Match($rawText, '^\s*"([^"]+)"')
      if ($match.Success) {
        return [Environment]::ExpandEnvironmentVariables($match.Groups[1].Value)
      }

      $match = [regex]::Match($rawText, "^\s*'([^']+)'")
      if ($match.Success) {
        return [Environment]::ExpandEnvironmentVariables($match.Groups[1].Value)
      }

      $match = [regex]::Match($rawText, '^\s*(.*?\.exe)(?:\s|$)', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
      if ($match.Success) {
        return [Environment]::ExpandEnvironmentVariables($match.Groups[1].Value.Trim())
      }

      return $null
    }

    function Get-InstallationRecords($Entries) {
      $records = New-Object System.Collections.Generic.List[object]

      foreach ($entry in $Entries) {
        $exe = Get-UninstallerPath $entry
        $validDirs = New-Object System.Collections.Generic.List[string]
        $candidates = New-Object System.Collections.Generic.List[string]

        if ($entry.InstallLocation) { [void]$candidates.Add([string]$entry.InstallLocation) }
        if ($exe) {
          try { [void]$candidates.Add((Split-Path -Parent $exe)) } catch {}
        }

        foreach ($candidate in $candidates) {
          $safe = Get-SafeInstallDirectory $candidate
          if ($safe) { Add-UniquePath $validDirs $safe }
        }

        $dir = $null
        $conflict = $false
        if ($validDirs.Count -eq 1) {
          $dir = $validDirs[0]
        } elseif ($validDirs.Count -gt 1) {
          $conflict = $true
          Record-Warning "conflicting install directories in $($entry.Hive)\$($entry.View) registration; refusing automatic handling" $null
        }

        $trusted = $false
        if ($exe -and $dir -and -not $conflict) {
          $trusted = Test-TrustedUninstaller $exe $dir
        }

        [void]$records.Add([pscustomobject]@{
          Hive        = $entry.Hive
          View        = $entry.View
          Dir         = $dir
          CanSweep    = ($dir -and (Test-KnownInstallDirectory $dir))
          Uninstaller = $exe
          Trusted     = $trusted
          Conflict    = $conflict
        })
      }

      return $records
    }

    # -------------------------------------------------------------------------
    # Process shutdown
    # -------------------------------------------------------------------------

    function Get-ProcessExecutablePath($Process) {
      try {
        $path = $Process.Path
        if ($path) { return [string]$path }
      } catch {}
      try {
        $path = $Process.MainModule.FileName
        if ($path) { return [string]$path }
      } catch {}
      return $null
    }

    function Stop-PythinkerProcesses($Directories) {
      $names = @("pythinker", "pythinker-code")
      $processes = @(Get-Process -Name $names -ErrorAction SilentlyContinue)
      if ($processes.Count -eq 0) { return }

      foreach ($process in $processes) {
        $path = Get-ProcessExecutablePath $process

        if (-not $path) {
          Record-Warning "cannot inspect $($process.ProcessName) PID $($process.Id); leaving an unidentified process running" $null
          continue
        }

        if (-not (Test-PathUnderDirs $path $Directories)) {
          Dim "skipping $($process.ProcessName) PID $($process.Id) — executable is outside a validated install directory"
          continue
        }

        $startTime = $null
        try { $startTime = $process.StartTime } catch { $startTime = $null }

        Step "Stopping $($process.ProcessName) (PID $($process.Id))"
        $stopError = $null
        try {
          Stop-Process -Id $process.Id -Force -ErrorAction Stop
        } catch {
          $stopError = $_
        }

        $deadline = (Get-Date).AddSeconds(3)
        while ((Get-Date) -lt $deadline) {
          if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) { break }
          Start-Sleep -Milliseconds 200
        }

        $survivor = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if (-not $survivor) {
          OK "Stopped $($process.ProcessName) (PID $($process.Id))"
          continue
        }

        $survivorPath = Get-ProcessExecutablePath $survivor
        $sameStart = $false
        if ($startTime) {
          try { $sameStart = ($survivor.StartTime -eq $startTime) } catch { $sameStart = $false }
        }

        if (-not ($sameStart -and $survivorPath -and (Test-PathEqual $survivorPath $path))) {
          Record-Warning "PID $($process.Id) identity changed after stop attempt; refusing taskkill escalation" $stopError
          continue
        }

        $taskkill = Get-Command taskkill.exe -ErrorAction SilentlyContinue
        if (-not $taskkill) {
          Record-Warning "taskkill.exe is unavailable; process PID $($process.Id) may remain running" $stopError
          continue
        }

        try {
          $output = & taskkill.exe /F /T /PID $process.Id 2>&1
          if ($LASTEXITCODE -ne 0) { throw "$output" }
        } catch {
          Record-Warning "taskkill failed for PID $($process.Id)" $_
        }
      }
    }

    # -------------------------------------------------------------------------
    # Inno uninstaller execution
    # -------------------------------------------------------------------------

    function Test-CanExecuteUninstaller($Record) {
      if (-not $Record.Trusted) { return $false }

      if (-not $Record.CanSweep) {
        Record-Warning "refusing execution of uninstaller from a custom registry-selected directory: $($Record.Uninstaller)" $null
        return $false
      }

      # Never execute a registry-selected binary with an elevated token. Known
      # directories are handled by the controlled cleanup below.
      if (Test-IsAdmin) {
        Record-Warning "refusing elevated execution of registry-selected uninstaller: $($Record.Uninstaller)" $null
        return $false
      }

      if ($Record.Hive -eq "LocalMachine") {
        Record-Warning "machine-scope uninstaller was not executed from a non-elevated shell: $($Record.Uninstaller)" $null
        return $false
      }

      return $true
    }

    function Invoke-InnoUninstaller($Record) {
      Step "Running Pythinker uninstaller ($($Record.Hive) scope)"
      $process = $null
      try {
        $processInfo = New-Object System.Diagnostics.ProcessStartInfo
        $processInfo.FileName = $Record.Uninstaller
        $processInfo.Arguments = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART"
        $processInfo.WorkingDirectory = Split-Path -Parent $Record.Uninstaller
        $processInfo.UseShellExecute = $false
        $processInfo.CreateNoWindow = $false

        $process = [System.Diagnostics.Process]::Start($processInfo)
        if ($null -eq $process) { throw "Process.Start returned null." }
        if (-not $process.WaitForExit(600000)) {
          try { $process.Kill() } catch {}
          Record-Warning "uninstaller exceeded the 10-minute timeout and was stopped: $($Record.Uninstaller)" $null
          return $false
        }

        if ($process.ExitCode -ne 0) {
          Record-Warning "uninstaller exited with code $($process.ExitCode); continuing with controlled cleanup" $null
          return $false
        }

        OK "Uninstaller completed"
        return $true
      } catch {
        Record-Warning "could not launch uninstaller $($Record.Uninstaller); continuing with controlled cleanup" $_
        return $false
      } finally {
        if ($process) { $process.Dispose() }
      }
    }

    # -------------------------------------------------------------------------
    # Robust deletion
    # -------------------------------------------------------------------------

    function Initialize-PendingDelete {
      if ("Win32.PendingDelete" -as [type]) { return $true }
      try {
        $signature = '[DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool MoveFileEx(string lpExistingFileName, string lpNewFileName, int dwFlags);'
        Add-Type -Namespace Win32 -Name PendingDelete -MemberDefinition $signature -ErrorAction Stop
      } catch {}
      return ($null -ne ("Win32.PendingDelete" -as [type]))
    }

    function Register-PendingDeleteSnapshot($Path, $Snapshot) {
      if (-not (Initialize-PendingDelete)) { return $false }
      if (-not $Snapshot.Complete -or $Snapshot.ReparsePoints.Count -gt 0) { return $false }

      $MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
      $ok = $true

      if (-not $Snapshot.RootIsDirectory) {
        try {
          return [Win32.PendingDelete]::MoveFileEx($Snapshot.Root, $null, $MOVEFILE_DELAY_UNTIL_REBOOT)
        } catch {
          return $false
        }
      }

      foreach ($file in $Snapshot.Files) {
        try {
          if (-not [Win32.PendingDelete]::MoveFileEx($file, $null, $MOVEFILE_DELAY_UNTIL_REBOOT)) { $ok = $false }
        } catch {
          $ok = $false
        }
      }

      foreach ($directory in @($Snapshot.Dirs | Sort-Object { $_.Length } -Descending)) {
        try {
          if (-not [Win32.PendingDelete]::MoveFileEx($directory, $null, $MOVEFILE_DELAY_UNTIL_REBOOT)) { $ok = $false }
        } catch {
          $ok = $false
        }
      }

      try {
        if (-not [Win32.PendingDelete]::MoveFileEx($Snapshot.Root, $null, $MOVEFILE_DELAY_UNTIL_REBOOT)) { $ok = $false }
      } catch {
        $ok = $false
      }

      return $ok
    }


    function Remove-SafeSnapshotNow($Snapshot) {
      if (-not $Snapshot.Complete -or $Snapshot.ReparsePoints.Count -gt 0) {
        throw "Unsafe or incomplete tree snapshot."
      }

      if (-not $Snapshot.RootIsDirectory) {
        Remove-Item -LiteralPath $Snapshot.Root -Force -ErrorAction Stop
        return
      }

      foreach ($file in $Snapshot.Files) {
        if (Test-Path -LiteralPath $file -ErrorAction SilentlyContinue) {
          Remove-Item -LiteralPath $file -Force -ErrorAction Stop
        }
      }

      foreach ($directory in @($Snapshot.Dirs | Sort-Object { $_.Length } -Descending)) {
        if (Test-Path -LiteralPath $directory -ErrorAction SilentlyContinue) {
          # Deliberately non-recursive: a directory that changed after the safe
          # snapshot remains non-empty and fails rather than being traversed.
          Remove-Item -LiteralPath $directory -Force -ErrorAction Stop
        }
      }

      if (Test-Path -LiteralPath $Snapshot.Root -ErrorAction SilentlyContinue) {
        Remove-Item -LiteralPath $Snapshot.Root -Force -ErrorAction Stop
      }
    }

    function Remove-PathRobust($Path, [string]$What) {
      if ([string]::IsNullOrWhiteSpace([string]$Path)) { return }

      $full = Get-CanonicalPath $Path
      if (-not $full) {
        Record-Unresolved "refusing malformed deletion path: $Path"
        return
      }

      if (-not (Test-LocalDrivePath $full)) {
        Record-Unresolved "refusing non-local, UNC, or device deletion path: $full"
        return
      }

      $exists = $false
      try { $exists = Test-Path -LiteralPath $full -ErrorAction Stop } catch {
        Record-Unresolved "could not determine whether $What exists: $full"
        return
      }
      if (-not $exists) { return }

      $root = ([IO.Path]::GetPathRoot($full)).TrimEnd('\', '/')
      if ($full -ieq $root) {
        Record-Unresolved "refusing to remove filesystem root: $full"
        return
      }

      if (Test-PathHasReparseComponent $full) {
        Record-Unresolved "refusing recursive deletion through a reparse point: $full"
        return
      }

      $lastError = $null
      for ($attempt = 1; $attempt -le 3; $attempt++) {
        $snapshot = Get-SafeTreeSnapshot $full
        if (-not $snapshot.Complete) {
          $firstError = ""
          if ($snapshot.Errors.Count -gt 0) { $firstError = " (first error: $($snapshot.Errors[0]))" }
          Record-Unresolved "could not safely inspect the complete directory tree for $What at $full$firstError"
          return
        }

        if ($snapshot.ReparsePoints.Count -gt 0) {
          Record-Unresolved "refusing controlled deletion because $($snapshot.ReparsePoints.Count) reparse point(s) exist inside $full (first: $($snapshot.ReparsePoints[0]))"
          return
        }

        try {
          Remove-SafeSnapshotNow $snapshot
          if (-not (Test-Path -LiteralPath $full -ErrorAction SilentlyContinue)) {
            Record-Removed $What
            return
          }
          throw "Deletion snapshot completed but the root still exists."
        } catch {
          $lastError = $_
          if ($attempt -lt 3) { Start-Sleep -Milliseconds (400 * $attempt) }
        }
      }

      # Re-inspect before scheduling deletion; the tree may have changed.
      $pendingSnapshot = Get-SafeTreeSnapshot $full
      if (-not $pendingSnapshot.Complete -or $pendingSnapshot.ReparsePoints.Count -gt 0) {
        Record-Unresolved "could not safely inspect the complete directory tree before reboot scheduling: $full"
        return
      }

      if (Register-PendingDeleteSnapshot $full $pendingSnapshot) {
        Add-PendingReboot $full
        OK "$What — locked now; scheduled for deletion on next reboot"
        return
      }

      $hint = Format-ErrorMessage $lastError
      if (-not (Test-IsAdmin)) { $hint = "$hint; an Administrator PowerShell may be required" }
      Record-Warning "could not remove $What" $hint
    }

    # -------------------------------------------------------------------------
    # PATH cleanup
    # -------------------------------------------------------------------------

    function Get-EnvironmentRegistryView {
      return (Get-NativeRegistryView)
    }

    function Split-PathValue([string]$Value) {
      if ($null -eq $Value) { return @() }
      return @($Value.Split([char[]]@(';'), [System.StringSplitOptions]::None))
    }

    function Remove-PathEntry($Directory, [string]$Hive) {
      $subkey = if ($Hive -eq "CurrentUser") {
        "Environment"
      } else {
        "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
      }
      $label = if ($Hive -eq "CurrentUser") { "user PATH" } else { "system PATH" }
      $target = Get-NormalizedPathToken $Directory
      if ($target -eq "") { return "NotFound" }

      $base = $null
      $readKey = $null
      try {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey((Get-RegistryHiveEnum $Hive), (Get-EnvironmentRegistryView))
        $readKey = $base.OpenSubKey($subkey, $false)
        if ($null -eq $readKey) { return "NotFound" }
        $current = $readKey.GetValue("Path", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($null -eq $current) { return "NotFound" }

        $matched = $false
        foreach ($token in (Split-PathValue ([string]$current))) {
          if ((Get-NormalizedPathToken $token) -ieq $target) {
            $matched = $true
            break
          }
        }
        if (-not $matched) { return "NotFound" }
      } catch {
        Record-Warning "could not inspect $label" $_
        return "InspectionFailed"
      } finally {
        if ($readKey) { $readKey.Dispose() }
        if ($base) { $base.Dispose() }
      }

      if ($Hive -eq "LocalMachine" -and -not (Test-IsAdmin)) {
        Record-Warning "$label still contains $Directory; re-run from an Administrator PowerShell to remove it" $null
        return "Failed"
      }

      $base = $null
      $writeKey = $null
      try {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey((Get-RegistryHiveEnum $Hive), (Get-EnvironmentRegistryView))
        $writeKey = $base.OpenSubKey($subkey, $true)
        if ($null -eq $writeKey) {
          Record-Warning "could not open $label for writing" $null
          return "Failed"
        }

        # Re-read under the writable handle so a concurrent PATH update is not
        # overwritten with the stale value from the read-only inspection phase.
        $latest = $writeKey.GetValue("Path", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($null -eq $latest) { return "NotFound" }
        $kind = $writeKey.GetValueKind("Path")

        $kept = New-Object System.Collections.Generic.List[string]
        $foundLatest = $false
        foreach ($token in (Split-PathValue ([string]$latest))) {
          if ((Get-NormalizedPathToken $token) -ieq $target) {
            $foundLatest = $true
          } else {
            [void]$kept.Add($token)
          }
        }

        if (-not $foundLatest) { return "NotFound" }
        $newValue = ($kept.ToArray() -join ";")
        $writeKey.SetValue("Path", $newValue, $kind)
        $State.EnvironmentChanged = $true
        Record-Removed "removed $Directory from $label"
        return "Removed"
      } catch {
        Record-Warning "could not update $label" $_
        return "Failed"
      } finally {
        if ($writeKey) { $writeKey.Dispose() }
        if ($base) { $base.Dispose() }
      }
    }

    function Test-PathEntryPresent($Directories, [string]$Hive) {
      $subkey = if ($Hive -eq "CurrentUser") {
        "Environment"
      } else {
        "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
      }

      $targets = New-Object System.Collections.Generic.List[string]
      foreach ($directory in $Directories) {
        $target = Get-NormalizedPathToken $directory
        if ($target) { Add-UniqueString $targets $target }
      }

      $base = $null
      $key = $null
      try {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey((Get-RegistryHiveEnum $Hive), (Get-EnvironmentRegistryView))
        $key = $base.OpenSubKey($subkey, $false)
        if ($null -eq $key) { return $false }
        $current = $key.GetValue("Path", $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($null -eq $current) { return $false }

        foreach ($token in (Split-PathValue ([string]$current))) {
          $probe = Get-NormalizedPathToken $token
          foreach ($target in $targets) {
            if ($probe -ieq $target) { return $true }
          }
        }
        return $false
      } catch {
        return $null
      } finally {
        if ($key) { $key.Dispose() }
        if ($base) { $base.Dispose() }
      }
    }

    function Remove-SessionPathEntries($Directories) {
      if ($null -eq $env:PATH) { return }
      try {
        $targets = New-Object System.Collections.Generic.List[string]
        foreach ($directory in $Directories) {
          $target = Get-NormalizedPathToken $directory
          if ($target) { Add-UniqueString $targets $target }
        }
        if ($targets.Count -eq 0) { return }

        $kept = New-Object System.Collections.Generic.List[string]
        $changed = $false
        foreach ($token in (Split-PathValue ([string]$env:PATH))) {
          $probe = Get-NormalizedPathToken $token
          $match = $false
          foreach ($target in $targets) {
            if ($probe -ieq $target) { $match = $true; break }
          }
          if ($match) {
            $changed = $true
          } else {
            [void]$kept.Add($token)
          }
        }

        if ($changed) {
          $env:PATH = ($kept.ToArray() -join ";")
          OK "Cleaned PATH for this session"
        }
      } catch {
        Record-Warning "could not clean this session's PATH" $_
      }
    }

    function Send-EnvironmentBroadcast {
      if (-not $State.EnvironmentChanged) { return }
      try {
        if (-not ("Win32.UninstallNativeMethods" -as [type])) {
          $signature = '[DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Auto)] public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);'
          Add-Type -Namespace Win32 -Name UninstallNativeMethods -MemberDefinition $signature -ErrorAction Stop
        }
        $result = [UIntPtr]::Zero
        [void][Win32.UninstallNativeMethods]::SendMessageTimeout(
          [IntPtr]0xffff,
          0x001A,
          [UIntPtr]::Zero,
          "Environment",
          0x0002,
          5000,
          [ref]$result
        )
      } catch {
        Record-Warning "could not broadcast the environment change to the desktop" $_
      }
    }

    # -------------------------------------------------------------------------
    # Registry cleanup
    # -------------------------------------------------------------------------

    function Remove-HandledRegistryEntries($Records) {
      $uninstallPath = "Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"

      foreach ($record in $Records) {
        if (-not $record.Dir) {
          Record-Unresolved "registry entry for an unvalidated installation was left in place ($($record.Hive)\$($record.View))"
          continue
        }

        $exists = $null
        try { $exists = Test-Path -LiteralPath $record.Dir -ErrorAction Stop } catch { $exists = $null }
        if ($null -eq $exists) {
          Record-Unresolved "could not verify installation directory before registry cleanup: $($record.Dir)"
          continue
        }
        if ($exists -and -not (Test-PendingReboot $record.Dir)) {
          Record-Unresolved "registry entry left in place because files remain at $($record.Dir) ($($record.Hive)\$($record.View))"
          continue
        }

        $base = $null
        $key = $null
        try {
          $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey((Get-RegistryHiveEnum $record.Hive), $record.View)
          $key = $base.OpenSubKey($uninstallPath, $false)
          if ($null -eq $key) { continue }
          $key.Dispose()
          $key = $null

          $base.DeleteSubKeyTree($uninstallPath, $false)
          Record-Removed "removed uninstall registry entry ($($record.Hive)\$($record.View))"
        } catch {
          if ($record.Hive -eq "LocalMachine" -and -not (Test-IsAdmin)) {
            Record-Warning "machine uninstall registry entry remains; re-run from an Administrator PowerShell" $_
          } else {
            Record-Warning "could not remove uninstall registry entry ($($record.Hive)\$($record.View))" $_
          }
        } finally {
          if ($key) { $key.Dispose() }
          if ($base) { $base.Dispose() }
        }
      }
    }

    # -------------------------------------------------------------------------
    # Owned stale installer temp directories
    # -------------------------------------------------------------------------

    function Test-InstallerTempDirectoryOwned($Directory) {
      if (Test-PathHasReparseComponent $Directory) { return $false }
      $snapshot = Get-SafeTreeSnapshot $Directory
      if (-not $snapshot.Complete -or $snapshot.ReparsePoints.Count -gt 0 -or $snapshot.Dirs.Count -gt 0) {
        return $false
      }

      $files = @($snapshot.Files)
      $markerPath = Join-Path $Directory ".pythinker-installer"
      $markerValid = $false
      if (Test-Path -LiteralPath $markerPath -PathType Leaf -ErrorAction SilentlyContinue) {
        try {
          $markerValid = ((Get-Content -LiteralPath $markerPath -Raw -ErrorAction Stop).Trim() -eq $AppId)
        } catch {
          $markerValid = $false
        }
      }

      $setupFiles = @($files | Where-Object { [IO.Path]::GetFileName($_) -match '^PythinkerSetup-[0-9]+(?:\.[0-9]+){1,3}\.exe$' })
      if ($setupFiles.Count -ne 1) { return $false }

      $setupPath = $setupFiles[0]
      $checksumPath = "${setupPath}.sha256"
      $checksumValid = $false
      if (Test-Path -LiteralPath $checksumPath -PathType Leaf -ErrorAction SilentlyContinue) {
        try {
          $expectedText = (Get-Content -LiteralPath $checksumPath -Raw -ErrorAction Stop).Trim()
          $expectedMatch = [regex]::Match($expectedText, '^[0-9a-fA-F]{64}')
          if ($expectedMatch.Success) {
            $actual = (Get-FileHash -LiteralPath $setupPath -Algorithm SHA256 -ErrorAction Stop).Hash
            $checksumValid = ($actual -ieq $expectedMatch.Value)
          }
        } catch {
          $checksumValid = $false
        }
      }

      foreach ($file in $files) {
        $name = [IO.Path]::GetFileName($file)
        if ($name -eq ".pythinker-installer") { continue }
        if (Test-PathEqual $file $setupPath) { continue }
        if (Test-PathEqual $file $checksumPath) { continue }
        return $false
      }

      return ($markerValid -or $checksumValid)
    }

    function Remove-StaleInstallerTempDirs {
      $setupRunning = @(Get-Process -Name "PythinkerSetup*" -ErrorAction SilentlyContinue).Count -gt 0
      if ($setupRunning) {
        Dim "a Pythinker setup is running; leaving installer temp directories untouched"
        return
      }

      $cutoff = (Get-Date).AddHours(-2)
      $guidPattern = '^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$'
      $candidates = @()
      try {
        $candidates = @(Get-ChildItem -LiteralPath $TempRoot -Directory -Filter "pythinker-install-*" -ErrorAction Stop |
          Where-Object {
            $_.Name.Substring("pythinker-install-".Length) -match $guidPattern -and
            $_.LastWriteTime -lt $cutoff
          })
      } catch {
        Record-Warning "could not inspect installer temp directory root" $_
        return
      }

      foreach ($candidate in $candidates) {
        if (-not (Test-InstallerTempDirectoryOwned $candidate.FullName)) {
          Dim "skipping unverified installer temp directory: $($candidate.FullName)"
          continue
        }
        Add-UniquePath $State.TempCleanupTargets $candidate.FullName
        Remove-PathRobust $candidate.FullName "installer temp directory $($candidate.FullName)"
      }
    }

    # -------------------------------------------------------------------------
    # Optional user-data purge
    # -------------------------------------------------------------------------

    function Invoke-DataPurge {
      if ($PurgeDataSetting -eq "1") { $State.PurgeDataRequested = $true }
      $exists = $false
      try { $exists = Test-Path -LiteralPath $DataDir -ErrorAction Stop } catch {
        Record-Warning "could not inspect user data directory $DataDir" $_
        return
      }
      if (-not $exists) { return }

      $purge = $false
      if ($PurgeDataSetting -eq "1") {
        $purge = $true
      } elseif ($PurgeDataSetting -eq "0") {
        $purge = $false
      } elseif (Test-Interactive) {
        Write-Host ""
        Write-Host "  ${BOLD}User data found at $DataDir${RESET} ${DIM}(config, sessions, logs)${RESET}"
        try {
          $answer = Read-Host "  Delete it too? [y/N]"
          $purge = ($answer -match '^(?i)y(es)?$')
        } catch {
          $purge = $false
        }
      }

      if ($purge) {
        $State.PurgeDataRequested = $true
        Remove-PathRobust $DataDir "user data $DataDir"
      } else {
        Write-Host ""
        Dim "User data kept at $DataDir — delete it manually or re-run with `$env:PYTHINKER_PURGE_DATA = '1'"
      }
    }

    # -------------------------------------------------------------------------
    # Final verification — unknown state is unresolved
    # -------------------------------------------------------------------------

    function Test-PathFinalState($Path, [string]$Label) {
      $exists = $null
      try { $exists = Test-Path -LiteralPath $Path -ErrorAction Stop } catch { $exists = $null }
      if ($null -eq $exists) {
        Record-Unresolved "could not verify $Label state: $Path"
        return
      }
      if ($exists -and -not (Test-PendingReboot $Path)) {
        Record-Unresolved "$Label still present: $Path"
      }
    }

    function Test-FinalState($InstallDirectories, $UserPathDirectories, $MachinePathDirectories, $Records, $StartDirectories) {
      foreach ($directory in $InstallDirectories) {
        Test-PathFinalState $directory "install directory"
      }

      $userPathPresent = Test-PathEntryPresent $UserPathDirectories "CurrentUser"
      if ($null -eq $userPathPresent) {
        Record-Unresolved "could not verify user PATH state"
      } elseif ($userPathPresent) {
        Record-Unresolved "user PATH still contains a Pythinker entry"
      }

      $machinePathPresent = Test-PathEntryPresent $MachinePathDirectories "LocalMachine"
      if ($null -eq $machinePathPresent) {
        Record-Unresolved "could not verify system PATH state"
      } elseif ($machinePathPresent) {
        $message = "system PATH still contains a Pythinker entry"
        if (-not (Test-IsAdmin)) { $message += "; re-run from an Administrator PowerShell" }
        Record-Unresolved $message
      }

      $uninstallPath = "Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"
      foreach ($combo in (Get-UninstallRegistryCombos)) {
        $base = $null
        $key = $null
        try {
          $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey((Get-RegistryHiveEnum $combo.Hive), $combo.View)
          $key = $base.OpenSubKey($uninstallPath, $false)
          if ($key) {
            $message = "uninstall registry entry still present ($($combo.Hive)\$($combo.View))"
            if ($combo.Hive -eq "LocalMachine" -and -not (Test-IsAdmin)) {
              $message += "; re-run from an Administrator PowerShell"
            }
            Record-Unresolved $message
          }
        } catch {
          Record-Unresolved "could not verify $($combo.Hive)\$($combo.View) uninstall registry state"
        } finally {
          if ($key) { $key.Dispose() }
          if ($base) { $base.Dispose() }
        }
      }

      foreach ($directory in $StartDirectories) {
        Test-PathFinalState $directory "Start Menu shortcut directory"
      }

      foreach ($process in @(Get-Process -Name @("pythinker", "pythinker-code") -ErrorAction SilentlyContinue)) {
        $path = Get-ProcessExecutablePath $process
        if (-not $path) {
          Record-Unresolved "could not verify remaining $($process.ProcessName) process PID $($process.Id)"
          continue
        }
        if (Test-PathUnderDirs $path $InstallDirectories) {
          Record-Unresolved "Pythinker process still running from an install directory (PID $($process.Id))"
        }
      }

      if ($State.PurgeDataRequested) {
        Test-PathFinalState $DataDir "requested user data"
      }

      foreach ($directory in $State.TempCleanupTargets) {
        Test-PathFinalState $directory "owned stale installer temp directory"
      }

      foreach ($record in $Records) {
        if (-not $record.Dir) {
          Record-Unresolved "installation registration could not be safely associated with a directory ($($record.Hive)\$($record.View))"
        }
      }
    }

    # -------------------------------------------------------------------------
    # Main workflow
    # -------------------------------------------------------------------------

    function Invoke-PythinkerUninstall {
      Write-Header
      Step "Uninstalling Pythinker Code"

      $entries = @()
      $records = @()
      $installDirs = New-Object System.Collections.Generic.List[string]
      $sweepDirs = New-Object System.Collections.Generic.List[string]
      $userPathDirs = New-Object System.Collections.Generic.List[string]
      $machinePathDirs = New-Object System.Collections.Generic.List[string]
      $startDirs = New-Object System.Collections.Generic.List[string]

      try {
        $entriesResult = Invoke-Step "could not read uninstall registry entries" { Find-UninstallEntries }
        if ($null -ne $entriesResult) { $entries = @($entriesResult) }

        $recordsResult = Invoke-Step "installation record validation failed" { Get-InstallationRecords $entries }
        if ($null -ne $recordsResult) { $records = @($recordsResult) }

        foreach ($known in $KnownInstallDirs) {
          $safe = Get-SafeInstallDirectory $known
          if ($safe) {
            Add-UniquePath $installDirs $safe
            Add-UniquePath $sweepDirs $safe
            Add-UniquePath $userPathDirs $safe
            Add-UniquePath $machinePathDirs $safe
          } else {
            Record-Unresolved "known installation directory failed safety validation: $known"
          }
        }

        foreach ($record in $records) {
          if ($record.Dir) {
            Add-UniquePath $installDirs $record.Dir
            if ($record.Hive -eq "CurrentUser") {
              Add-UniquePath $userPathDirs $record.Dir
            } else {
              Add-UniquePath $machinePathDirs $record.Dir
            }
          }
          if ($record.Dir -and $record.CanSweep) { Add-UniquePath $sweepDirs $record.Dir }
        }

        if ($records.Count -eq 0) {
          Dim "No registered Pythinker uninstaller found — running controlled manual cleanup only"
        }

        Invoke-Step "process shutdown failed" { Stop-PythinkerProcesses $installDirs } | Out-Null

        $executedUninstallers = New-Object System.Collections.Generic.List[string]
        foreach ($record in $records) {
          if (-not $record.Uninstaller) { continue }
          if (-not $record.Trusted) {
            Record-Warning "uninstaller failed trust validation: $($record.Uninstaller)" $null
            continue
          }
          if (-not (Test-CanExecuteUninstaller $record)) { continue }
          if (Test-ListContainsInsensitive $executedUninstallers $record.Uninstaller) { continue }
          [void]$executedUninstallers.Add($record.Uninstaller)
          Invoke-InnoUninstaller $record | Out-Null
        }

        Step "Removing leftover files, PATH entries, shortcuts, and registry keys"

        # Only known installation directories are recursively swept. A custom
        # registry-selected path is never passed to Remove-PathRobust.
        foreach ($directory in $sweepDirs) {
          Remove-PathRobust $directory "known installation directory $directory"
        }

        # Removing an exact PATH token is safe for both known and validated custom
        # installations, even when the custom directory itself is not swept.
        foreach ($directory in $userPathDirs) {
          [void](Remove-PathEntry $directory "CurrentUser")
        }
        foreach ($directory in $machinePathDirs) {
          [void](Remove-PathEntry $directory "LocalMachine")
        }
        Remove-SessionPathEntries $installDirs

        Add-UniquePath $startDirs (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Pythinker")
        Add-UniquePath $startDirs (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Pythinker")
        foreach ($directory in $startDirs) {
          Remove-PathRobust $directory "Start Menu shortcut directory $directory"
        }

        Invoke-Step "registry entry handling failed" { Remove-HandledRegistryEntries $records } | Out-Null
        Invoke-Step "temp cleanup failed" { Remove-StaleInstallerTempDirs } | Out-Null
        Send-EnvironmentBroadcast
        Invoke-Step "user data step failed" { Invoke-DataPurge } | Out-Null

        Step "Verifying final state"
        try {
          Test-FinalState $installDirs $userPathDirs $machinePathDirs $records $startDirs
        } catch {
          Record-Unresolved "final verification did not complete — $(Format-ErrorMessage $_)"
        }
      } catch {
        Record-Unresolved "unexpected uninstall failure — $(Format-ErrorMessage $_)"
      }

      Write-Host ""
      Write-Host "  ${BOLD}${FACE}Uninstall summary${RESET}"
      Write-Host "    $IRIS$($State.RemovedCount) item(s) removed$RESET"

      if ($State.PendingReboot.Count -gt 0) {
        Write-Host "    $CORAL$($State.PendingReboot.Count) item(s) scheduled for deletion on next reboot:${RESET}"
        foreach ($path in $State.PendingReboot) { Write-Host "      $CORAL•$RESET $path" }
      }

      if ($State.Warnings.Count -gt 0) {
        Write-Host "    ${DIM}$($State.Warnings.Count) warning(s) during the run:${RESET}"
        foreach ($warning in $State.Warnings) { Dim "      • $warning" }
      }

      Write-Host ""
      if ($State.Unresolved.Count -gt 0) {
        Write-Host "  $CORAL$($State.Unresolved.Count) item(s) could not be fully removed or verified:${RESET}"
        foreach ($issue in $State.Unresolved) { Write-Host "      $CORAL•$RESET $issue" }
        Write-Host ""
        Dim "Most permission issues resolve by re-running this script from an Administrator PowerShell."
        Write-Host ""
      } elseif ($State.PendingReboot.Count -gt 0) {
        Write-Host "  ${BOLD}${IRIS}pythinker$RESET cleanup complete — ${BOLD}a reboot is required${RESET} to finish removing locked items."
        Write-Host ""
      } else {
        Write-Host "  ${BOLD}${IRIS}pythinker$RESET has been uninstalled. Open a fresh PowerShell for persistent PATH changes to appear."
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
      throw "Pythinker uninstall did not produce a result."
    }
    if (-not $result.Success) {
      throw "Pythinker uninstall completed with $($result.Unresolved.Count) unresolved issue(s) — see the summary above."
    }
  } finally {
    if ($originalEncoding) {
      try { [Console]::OutputEncoding = $originalEncoding } catch {}
    }
  }
}
