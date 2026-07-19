; TapScribe Windows Bundle — Inno Setup script (ADR-0015).
;
; A Bundle is the Recorder packaged: an embedded CPython, the tapscribe wheel,
; and the tray Launcher. It is NOT a frozen binary — /setup pip-installs the
; operator's chosen model backends into this interpreter at runtime, so the
; layout has to stay a real, writable Python installation. (No venv: the
; embedded CPython IS the environment, and `sys.prefix\Scripts` is where pip
; puts console scripts like whisperlivekit-server, which live.py already looks
; for.)
;
; Built by the `bundle` job in .github/workflows/release.yml, which stages:
;   staging/python/    the embedded interpreter, core deps already installed
;   staging/wheel/     the tapscribe-X.Y.Z-*.whl this Bundle installs from
;   staging/launcher/  the published TapScribe.Bundle.Launcher exe
;
; Pass the version in:  ISCC /DMyAppVersion=1.1.0 packaging\windows\TapScribe.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "TapScribe"
#define MyAppPublisher "TapScribe contributors"
#define MyAppURL "https://github.com/Vortiago/TapScribe"
#define MyAppExeName "TapScribe.exe"

[Setup]
AppId={{8F3B6F2A-9C41-4E2D-B7A6-5D2C1E0A9B44}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={localappdata}\Programs\TapScribe
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE

; Per-user install, no UAC prompt. This is NOT cosmetic: /setup runs
; `pip install` into the bundled interpreter at runtime, so the install dir must
; be writable by the running user. Program Files would force us to either
; elevate the recorder — a process that also opens a network port — or break
; the model picker outright.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Multi-GB payload (torch alone is most of it), so compress hard but don't
; make the installer unopenable on a modest box.
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir=..\..\dist-bundle
; Stable and UNVERSIONED so releases/latest/download/<name> is a permanent URL
; across versions (ADR-0012).
OutputBaseFilename=TapScribe-Setup-win-x64
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "startup"; Description: "Start {#MyAppName} when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; The embedded interpreter AND its site-packages. `recursesubdirs` matters —
; this is a full Python installation, not a handful of files.
Source: "..\..\staging\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
; The wheel /setup and preflight install from. Kept as a file (not just
; installed) so a repair or an extras install can point pip back at the exact
; artifact this installer shipped.
Source: "..\..\staging\wheel\*.whl"; DestDir: "{app}\wheel"; Flags: ignoreversion
Source: "..\..\staging\launcher\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; /setup pip-installs model backends into the bundled interpreter AFTER install, so
; site-packages holds files this installer never wrote. Without this the
; uninstall leaves multi-GB of orphaned wheels behind.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\wheel"

[Messages]
; Operator data (recordings, transcripts, config, the dashboard password) lives
; in %USERPROFILE%\TapScribe and is deliberately NOT removed — uninstalling the
; program must never delete someone's meeting recordings. Model WEIGHTS are a
; third location again (%USERPROFILE%\.cache\huggingface) and are also left.
ConfirmUninstall=Remove {#MyAppName} and its bundled Python?%n%nYour recordings, transcripts and settings in %USERPROFILE%\TapScribe are NOT removed, and neither are downloaded model weights in %USERPROFILE%\.cache\huggingface — delete those folders by hand if you want them gone.
