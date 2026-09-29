; Inno Setup script for SaveGuard.
;
; Produces a normal Windows installer (Setup.exe) that installs the app,
; creates Start Menu / optional Desktop shortcuts, and registers an
; uninstaller in "Add or Remove Programs" — this is the file VS Code's
; terminal can't run for you; see BUILDING.md for how to compile it.
;
; Requires: Inno Setup (free) — https://jrsoftware.org/isinfo.php
; Requires: the PyInstaller build already done (dist\SaveGuard\SaveGuard.exe
; must exist — run `pyinstaller saveguard.spec` first).
;
; To build the installer: open this file in the Inno Setup Compiler (or run
; ISCC.exe against it) and click Compile. Output goes to installer_output\.

#define MyAppName "SaveGuard"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "SaveGuard"
#define MyAppExeName "SaveGuard.exe"

[Setup]
AppId={{B7B1E3C4-6A2F-4E4D-9B1A-6D8C2F9E4A11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=installer_output
OutputBaseFilename=SaveGuard-Setup-{#MyAppVersion}
SetupIconFile=assets\icon.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; SaveGuard's own data lives in the user's home folder (~\.saveguard), not
; Program Files, so a per-machine install (needs admin once) is fine and
; simpler than juggling per-user install quirks. Change to "lowest" if you'd
; rather it install without asking for admin rights.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Pulls in everything PyInstaller produced -- the exe, bundled Python
; runtime, Qt libraries, and the assets folder -- as one folder build.
Source: "dist\SaveGuard\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Doesn't touch ~\.saveguard (tracked games, settings, backups) on
; uninstall -- only removes the installed program files. If you'd rather
; the uninstaller offer to wipe app data too, that needs a small custom
; uninstall page; ask if you want that added.
Type: filesandordirs; Name: "{app}"
