; =============================================================================
; Thoth v2.0.0 – Inno Setup Script
; Lightweight installer: bundles embedded Python + app source code.
; Downloads Ollama and Python packages at install time.
; =============================================================================
;
; Prerequisites (placed in installer\build\ by build_installer.ps1):
;   build\python\          – Extracted Python embeddable package
;   build\get-pip.py       – pip bootstrap script
;
; Compile with:  iscc installer\thoth_setup.iss

#define MyAppName      "Thoth"
#define MyAppVersion   "2.0.0"
#define MyAppPublisher "Thoth"
#define MyAppURL       "https://github.com/siddsachar/Thoth"
#define MyAppExeName   "launch_thoth.vbs"

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
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; ── App source code ──────────────────────────────────────────────────────────
Source: "..\app.py";                   DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\agent.py";                 DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\memory.py";                DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\models.py";                DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\documents.py";             DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\threads.py";               DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\api_keys.py";              DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\voice.py";                 DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\tts.py";                   DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\vision.py";                DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\launcher.py";              DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\requirements.txt";         DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\thoth.ico";                DestDir: "{app}\app"; Flags: ignoreversion

; ── Tools package ────────────────────────────────────────────────────────────
Source: "..\tools\__init__.py";        DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\base.py";            DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\registry.py";        DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\arxiv_tool.py";      DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\calculator_tool.py"; DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\calendar_tool.py";   DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\documents_tool.py";  DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\duckduckgo_tool.py"; DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\filesystem_tool.py"; DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\gmail_tool.py";      DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\memory_tool.py";     DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\timer_tool.py";      DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\url_reader_tool.py"; DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\vision_tool.py";     DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\weather_tool.py";    DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\web_search_tool.py"; DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\wikipedia_tool.py";  DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\wolfram_tool.py";    DestDir: "{app}\app\tools"; Flags: ignoreversion
Source: "..\tools\youtube_tool.py";    DestDir: "{app}\app\tools"; Flags: ignoreversion

; ── Wake word models ─────────────────────────────────────────────────────────
Source: "..\wake_models\*.onnx";       DestDir: "{app}\app\wake_models"; Flags: ignoreversion

; ── Embedded Python ──────────────────────────────────────────────────────────
Source: "build\python\*";              DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

; ── get-pip.py ───────────────────────────────────────────────────────────────
Source: "build\get-pip.py";            DestDir: "{app}"; Flags: ignoreversion

; ── Launcher & helper scripts ────────────────────────────────────────────────
Source: "launch_thoth.bat";            DestDir: "{app}"; Flags: ignoreversion
Source: "launch_thoth.vbs";            DestDir: "{app}"; Flags: ignoreversion
Source: "install_deps.bat";            DestDir: "{app}"; Flags: ignoreversion deleteafterinstall

[Icons]
Name: "{group}\{#MyAppName}";                    Filename: "wscript.exe"; Parameters: """{app}\{#MyAppExeName}"""; IconFilename: "{app}\app\thoth.ico"; Comment: "Launch Thoth"
Name: "{group}\Uninstall {#MyAppName}";           Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";               Filename: "wscript.exe"; Parameters: """{app}\{#MyAppExeName}"""; IconFilename: "{app}\app\thoth.ico"; Tasks: desktopicon

[Run]
; ── Install Python packages + download & install Ollama ──────────────────────
Filename: "{app}\install_deps.bat";  Parameters: """{app}"""; \
    StatusMsg: "Setting up Thoth (downloading dependencies — this may take several minutes)..."; \
    Flags: waituntilterminated

; ── Launch app after install (optional) ──────────────────────────────────────
Filename: "wscript.exe"; Parameters: """{app}\{#MyAppExeName}"""; Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app\__pycache__"
Type: filesandordirs; Name: "{app}\app\tools\__pycache__"
