; Pythinker Code — Windows native installer
; Inno Setup 6 syntax. Per-user install, no UAC by default.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{4F4F2EAE-9D55-4E8E-92BC-7C1FA38B6F02}
AppName=Pythinker Code
AppVersion={#AppVersion}
AppPublisher=Pythinker
AppPublisherURL=https://pythinker.com
AppSupportURL=https://github.com/Pythoughts-labs/pythinker-cli/issues
AppUpdatesURL=https://github.com/Pythoughts-labs/pythinker-cli/releases
DefaultDirName={localappdata}\Programs\Pythinker
DefaultGroupName=Pythinker
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
OutputDir=..\..\dist
OutputBaseFilename=PythinkerSetup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupIconFile=assets\pythinker.ico
UninstallDisplayIcon={app}\pythinker.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=assets\LICENSE.rtf
ChangesEnvironment=yes
CloseApplications=yes
RestartApplications=no
#ifdef UseInnoSignTool
SignTool=PythinkerSign
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "modifypath"; Description: "Add Pythinker to your PATH"; \
  GroupDescription: "Shell integration:"; Check: not IsAdminInstallMode

Name: "modifypathmachine"; Description: "Add Pythinker to the system PATH"; \
  GroupDescription: "Shell integration:"; Check: IsAdminInstallMode

[InstallDelete]
; Remove the entire _internal directory before installing new files so that
; in-place upgrades do not accumulate stale version-stamped dist-info dirs
; (e.g. pythinker_code-0.28.0.dist-info alongside 0.31.0.dist-info).
; importlib.metadata.version() picks the first match it finds, which is the
; alphabetically-lower (older) version, causing the UI to display a stale
; version number and re-trigger the update prompt after every upgrade.
; _internal is 100% app payload — no user data lives there.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\pythinker\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs
; Drop the native-build sentinel directly next to pythinker.exe (NOT inside
; _internal/). PyInstaller >=6.1 hides bundled data files under _internal/,
; which would make is_native_build() always return False.
Source: ".pythinker-native"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Pythinker"; Filename: "{app}\pythinker.exe"
Name: "{group}\Uninstall Pythinker"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\pythinker.exe"; Description: "Launch Pythinker"; \
  Flags: nowait postinstall skipifsilent unchecked

[Code]
const
  SYNCHRONIZE = $00100000;

function OpenProcess(DesiredAccess: Cardinal; InheritHandle: Boolean;
  ProcessId: Cardinal): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: THandle; Milliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

{ When the running pythinker.exe launches this installer for an in-app update,
  it passes its own process id via /PID and exits. Wait for that process to
  fully terminate before Setup's Restart Manager scan runs (it runs at the
  start of the actual install, after InitializeSetup). Otherwise the scan can
  race the launcher's teardown, find pythinker.exe still locking _internal,
  and show a spurious "could not close the program" dialog. Bounded so a hung
  launcher never blocks the install: on timeout we fall through to Setup's
  normal CloseApplications handling. }
procedure WaitForLauncherExit;
var
  Pid: Integer;
  Handle: THandle;
begin
  Pid := StrToIntDef(ExpandConstant('{param:PID|0}'), 0);
  if Pid <= 0 then
    exit;
  Handle := OpenProcess(SYNCHRONIZE, False, Cardinal(Pid));
  if Handle = 0 then
    exit;
  WaitForSingleObject(Handle, 15000);
  CloseHandle(Handle);
end;

function NeedsAddPath(Param, RootHive: string): Boolean;
var
  OrigPath: string;
  Root: Integer;
  Subkey, ValueName: string;
begin
  if RootHive = 'HKCU' then begin
    Root := HKEY_CURRENT_USER;
    Subkey := 'Environment';
  end else begin
    Root := HKEY_LOCAL_MACHINE;
    Subkey := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
  end;
  ValueName := 'Path';
  if not RegQueryStringValue(Root, Subkey, ValueName, OrigPath) then begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + UpperCase(Param) + ';',
                ';' + UpperCase(OrigPath) + ';') = 0;
end;

function PathWithoutEntry(OrigPath, Param: string): string;
var
  BoundedPath: string;
begin
  BoundedPath := ';' + OrigPath + ';';
  StringChangeEx(BoundedPath, ';' + Param + ';', ';', True);
  while Pos(';;', BoundedPath) > 0 do
    StringChangeEx(BoundedPath, ';;', ';', True);
  if BoundedPath = ';' then
    Result := ''
  else
    Result := Copy(BoundedPath, 2, Length(BoundedPath) - 2);
end;

procedure AddToPath(Param, RootHive: string);
var
  OrigPath, NewPath: string;
  Root: Integer;
  Subkey: string;
begin
  if RootHive = 'HKCU' then begin
    Root := HKEY_CURRENT_USER;
    Subkey := 'Environment';
  end else begin
    Root := HKEY_LOCAL_MACHINE;
    Subkey := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
  end;
  if not RegQueryStringValue(Root, Subkey, 'Path', OrigPath) then
    OrigPath := '';
  OrigPath := PathWithoutEntry(OrigPath, Param);
  if OrigPath = '' then
    NewPath := Param
  else
    NewPath := Param + ';' + OrigPath;
  RegWriteExpandStringValue(Root, Subkey, 'Path', NewPath);
end;

procedure RemoveFromPath(Param, RootHive: string);
var
  OrigPath: string;
  Root: Integer;
  Subkey: string;
begin
  if RootHive = 'HKCU' then begin
    Root := HKEY_CURRENT_USER;
    Subkey := 'Environment';
  end else begin
    Root := HKEY_LOCAL_MACHINE;
    Subkey := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
  end;
  if not RegQueryStringValue(Root, Subkey, 'Path', OrigPath) then exit;
  OrigPath := PathWithoutEntry(OrigPath, Param);
  RegWriteExpandStringValue(Root, Subkey, 'Path', OrigPath);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDir: string;
begin
  if CurStep = ssPostInstall then begin
    AppDir := ExpandConstant('{app}');
    if WizardIsTaskSelected('modifypath') then
      AddToPath(AppDir, 'HKCU');
    if WizardIsTaskSelected('modifypathmachine') then
      AddToPath(AppDir, 'HKLM');
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir: string;
begin
  if CurUninstallStep = usUninstall then begin
    AppDir := ExpandConstant('{app}');
    RemoveFromPath(AppDir, 'HKCU');
    if IsAdminInstallMode then
      RemoveFromPath(AppDir, 'HKLM');
  end;
end;

function InitializeSetup(): Boolean;
var
  OtherScopeKey: string;
  Found: Boolean;
begin
  WaitForLauncherExit;

  OtherScopeKey :=
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
    '{4F4F2EAE-9D55-4E8E-92BC-7C1FA38B6F02}_is1';

  if IsAdminInstallMode then
    Found := RegKeyExists(HKEY_CURRENT_USER, OtherScopeKey)
  else
    Found := RegKeyExists(HKEY_LOCAL_MACHINE, OtherScopeKey);

  if Found then begin
    MsgBox('An existing Pythinker install was found at a different scope. '
           + 'Please uninstall it from Apps & Features before continuing.',
           mbError, MB_OK);
    Result := False;
  end else
    Result := True;
end;
