; =============================================================================
; Thoth – Inno Setup Script
; Creates a Windows installer that bundles embedded Python, installs Ollama,
; installs Python dependencies, and creates launcher shortcuts.
; =============================================================================
;
; Prerequisites (placed in installer\build\ by build_installer.ps1):
;   build\python\          – Extracted Python embeddable package
;   build\get-pip.py       – pip bootstrap script
;   build\OllamaSetup.exe  – Ollama Windows installer
;
; Compile with:  iscc installer\thoth_setup.iss

#define MyAppName      "Thoth"
#define MyAppVersion   "1.0.0"
#define MyAppPublisher "Thoth"
#define MyAppURL       "https://github.com/your-repo/thoth"
#define MyAppExeName   "launch_thoth.bat"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=ThothSetup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\thoth.ico
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; ── App source code ──────────────────────────────────────────────────────────
Source: "..\app.py";                   DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\models.py";                DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\rag.py";                   DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\documents.py";             DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\threads.py";               DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\api_keys.py";              DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\requirements.txt";         DestDir: "{app}\app"; Flags: ignoreversion

; ── Vector store (if it exists) ──────────────────────────────────────────────
Source: "..\vector_store\*";           DestDir: "{app}\app\vector_store"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

; ── Embedded Python ──────────────────────────────────────────────────────────
Source: "build\python\*";              DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

; ── get-pip.py ───────────────────────────────────────────────────────────────
Source: "build\get-pip.py";            DestDir: "{app}"; Flags: ignoreversion

; ── Ollama installer ─────────────────────────────────────────────────────────
Source: "build\OllamaSetup.exe";       DestDir: "{tmp}"; Flags: ignoreversion deleteafterinstall

; ── Launcher & helper scripts ────────────────────────────────────────────────
Source: "launch_thoth.bat";            DestDir: "{app}"; Flags: ignoreversion
Source: "install_deps.bat";            DestDir: "{app}"; Flags: ignoreversion deleteafterinstall

[Icons]
Name: "{group}\{#MyAppName}";                    Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app\thoth.ico"; Comment: "Launch Thoth"
Name: "{group}\Uninstall {#MyAppName}";           Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";               Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app\thoth.ico"; Tasks: desktopicon

[Run]
; ── Install Ollama (silent) ──────────────────────────────────────────────────
Filename: "{tmp}\OllamaSetup.exe";  Parameters: "/VERYSILENT /NORESTART"; \
    StatusMsg: "Installing Ollama..."; Flags: waituntilterminated

; ── Kill Ollama UI that auto-launches after install ─────────────────────────
Filename: "taskkill"; Parameters: "/F /IM ollama app.exe"; \
    StatusMsg: "Closing Ollama UI..."; Flags: waituntilterminated runhidden; Check: not WizardSilent

; ── Install Python packages ──────────────────────────────────────────────────
Filename: "{app}\install_deps.bat";  Parameters: """{app}"""; \
    StatusMsg: "Installing Python packages (this may take a few minutes)..."; \
    Flags: waituntilterminated

; ── Launch app after install (optional) ──────────────────────────────────────
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app\vector_store"
Type: filesandordirs; Name: "{app}\app\__pycache__"
Type: files;          Name: "{app}\app\threads.db"
Type: files;          Name: "{app}\app\processed_files.json"
Type: files;          Name: "{app}\app\api_keys.json"
