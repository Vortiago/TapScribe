; TapScribe Windows Bundle — Inno Setup script (ADR-0015).
;
; A Bundle is the Recorder packaged: an embedded CPython, the tapscribe wheel,
; and the Tray Bridge, which carries the host role because that payload is beside
; it on disk (ADR-0022). It is NOT a frozen binary — /setup pip-installs the
; operator's chosen model backends into this interpreter at runtime, so the
; layout has to stay a real, writable Python installation. (No venv: the
; embedded CPython IS the environment, and `sys.prefix\Scripts` is where pip
; puts console scripts like whisperlivekit-server, which live.py already looks
; for.)
;
; Built by the `bundle` job in .github/workflows/release.yml, which stages:
;   staging/python/    the embedded interpreter, core deps already installed
;   staging/wheel/     the tapscribe-X.Y.Z-*.whl this Bundle installs from
;   staging/tray/      the published tray exe, taken from the bridge-only artifact
;
; Pass the version in:  ISCC /DMyAppVersion=1.1.0 packaging\windows\TapScribe.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "TapScribe"
#define MyAppPublisher "TapScribe contributors"
#define MyAppURL "https://github.com/Vortiago/TapScribe"
; The tray's assembly name (TapScribe.TrayBridge.Windows.csproj), which is what the
; bridge-only zip contains and what the `bundle` job stages into staging\tray\.
#define MyAppExeName "TapScribe.TrayBridge.exe"

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

; The same names the tray's single-instance guard uses
; (TapScribe.TrayBridge.Windows/Program.cs). Without it Setup happily overwrites
; files while the tray app is running them, which on Windows means a locked
; TapScribe.exe and a half-applied upgrade.
;
; BOTH names, for at least one release. The Bundle's tray used to be a separate
; Launcher holding Local\TapScribe.Bundle.Launcher (ADR-0015); listing only the
; new name would leave an installer upgrading over a RUNNING old Launcher unable
; to see it, and it would hit files-in-use instead of asking it to quit. Drop the
; old name once no supported upgrade path starts from a Launcher install.
AppMutex=Local\TapScribe.TrayBridge,Local\TapScribe.Bundle.Launcher

; The staged payload is embedded CPython + TapScribe's CORE deps only (fastapi,
; uvicorn, numpy, websockets, cryptography, onnxruntime) — order of ~150 MB, not
; the multi-GB it was when torch was core (#374 dropped it). Model backends are
; pip-installed at /setup, after the installer. Compression stays high because
; the embedded interpreter + onnxruntime still compress well and the download is
; the operator's first impression; revisit if build time becomes the constraint.
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

[InstallDelete]
; RUNS BEFORE [Files]. Load-bearing for UPGRADES, not cosmetic.
;
; The wheel filename carries the version (tapscribe-1.1.0-py3-none-any.whl), so
; installing 1.2.0 over 1.1.0 would ADD a second .whl rather than replace it —
; and BundleLayout.ResolveWheel() deliberately refuses to guess between two,
; throwing BundleLayoutException. Without this line the first upgrade after a
; release bricks the tray for every operator at once, with the only recourse
; being to delete a file out of %LOCALAPPDATA% by hand.
;
; Scoped to the wheel folder ONLY. Emphatically NOT {app}\python: /setup
; pip-installs the operator's chosen model backends in there, and wiping it on
; upgrade would re-download multi-GB of extras every time.
Type: files; Name: "{app}\wheel\*.whl"

; The retired Launcher's exe, which shipped as {app}\TapScribe.exe. The tray is
; TapScribe.TrayBridge.exe, so an upgrade would otherwise leave the old binary in
; place — launchable from Explorer or any pinned shortcut, holding the old mutex,
; with no bridge role. Drop this line once no supported upgrade path starts from a
; Launcher install, alongside the AppMutex entry above.
Type: files; Name: "{app}\TapScribe.exe"

[Files]
; The embedded interpreter AND its site-packages. `recursesubdirs` matters —
; this is a full Python installation, not a handful of files.
Source: "..\..\staging\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
; The wheel /setup and preflight install from. Kept as a file (not just
; installed) so a repair or an extras install can point pip back at the exact
; artifact this installer shipped.
Source: "..\..\staging\wheel\*.whl"; DestDir: "{app}\wheel"; Flags: ignoreversion
Source: "..\..\staging\tray\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

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
