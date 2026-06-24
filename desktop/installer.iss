; Inno Setup script for the Better Agent Windows app.
;
; UNVERIFIED ON WINDOWS YET (see task A3). Per-USER install into
; {localappdata} so the tufup auto-updater can replace the app directory
; without elevation. pywebview's Windows backend needs the Edge WebView2
; runtime — the installer ensures it (bootstrapper) before finishing.
;
; Build: ISCC.exe installer.iss   (run by build_windows.ps1)

#define AppName "Better Agent"
#define AppExe "Better Agent.exe"
; Version is sourced from desktop/_version.py at build time; the build
; script can pass /DAppVersion=... — default kept in sync manually here.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{C0FFEE00-BC11-4A11-9E11-BETTERCLAUDE00}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Better Agent
; Per-user install — no admin rights, and the app dir stays writable so
; tufup can self-update.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=BetterAgentSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
; The PyInstaller onedir output (dist\Better Agent\*).
Source: "dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; WebView2 evergreen bootstrapper — placed beside the script before build.
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: WebView2Missing

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
; Install the WebView2 runtime silently if it is missing.
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; Check: WebView2Missing; StatusMsg: "Installing Edge WebView2 runtime..."
; Offer to launch after install.
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function WebView2Missing(): Boolean;
var
  v: String;
begin
  // The evergreen runtime registers its version under these keys (per-machine
  // or per-user). If neither is present, the runtime is missing.
  Result := not (
    RegQueryStringValue(HKLM,
      'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', v) or
    RegQueryStringValue(HKCU,
      'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', v));
end;
