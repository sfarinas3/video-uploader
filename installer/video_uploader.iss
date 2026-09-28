; Inno Setup script for Video Uploader.
; Builds a single Setup.exe that installs the app per-user (no admin
; required), with Start Menu / optional desktop shortcuts and a real
; uninstaller. Run scripts\build_installer.ps1 rather than invoking this
; directly -- that script builds dist\video-uploader.exe first.

#define MyAppName "Video Uploader"
#define MyAppVersion "0.1.0"
#define MyAppExeName "video-uploader.exe"

[Setup]
AppId={{FE658756-9DE7-4AC4-8678-1E192ECA7D7D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Video Uploader
DefaultDirName={localappdata}\Programs\VideoUploader
DefaultGroupName=Video Uploader
DisableProgramGroupPage=yes
; Per-user install, no admin/UAC prompt -- also means the app can write
; config.yaml/data\ next to itself without permission issues.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist_installer
OutputBaseFilename=VideoUploaderSetup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "..\dist\video-uploader.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.example.yaml"; DestDir: "{app}"; Flags: ignoreversion
; Give first-time installs a ready-to-edit config.yaml; never overwrite one
; an existing install (or the user) already created.
Source: "..\config.example.yaml"; DestDir: "{app}"; DestName: "config.yaml"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
