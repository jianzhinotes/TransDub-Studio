; Inno Setup script for TransDub Studio (Windows graphical installer).
; Compiled in CI with:  ISCC.exe /DMyAppVersion=1.0.0 installer.iss
; The payload (tracked source, produced by `git archive`) must sit at repo-root\payload.

#define MyAppName "TransDub Studio"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppPublisher "jianzhinotes"
#define MyAppURL "https://github.com/jianzhinotes/TransDub-Studio"

[Setup]
AppId={{9F5B2E7A-3C4D-4E1F-8A2B-1D6C7E0F9A31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\TransDub Studio
DefaultGroupName=TransDub Studio
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=TransDub-Studio-Setup-{#MyAppVersion}
SetupIconFile=..\..\videotrans\styles\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\payload\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "bootstrap.ps1";  DestDir: "{app}"; Flags: ignoreversion
Source: "launch.vbs";     DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\TransDub Studio"; Filename: "{app}\launch.vbs"; IconFilename: "{app}\videotrans\styles\icon.ico"; WorkingDir: "{app}"
Name: "{userdesktop}\TransDub Studio"; Filename: "{app}\launch.vbs"; IconFilename: "{app}\videotrans\styles\icon.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; First-time setup: installs uv + ffmpeg and runs `uv sync`. Shows a console so the
; user can watch the multi-GB download progress. Blocks the wizard until finished.
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\bootstrap.ps1"" -InstallDir ""{app}"""; \
  StatusMsg: "Installing dependencies (downloads several GB, please be patient)..."; \
  Flags: waituntilterminated
Filename: "{app}\launch.vbs"; Description: "Launch TransDub Studio now"; Flags: postinstall shellexec skipifsilent nowait
