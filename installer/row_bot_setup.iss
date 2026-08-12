; =============================================================================
; Row-Bot v4.7.0 - Inno Setup Script
; Self-contained installer: bundles embedded Python with all pip packages
; pre-installed.  No internet downloads at install time.
; =============================================================================
;
; Prerequisites (placed in installer\build\ by build_installer.ps1):
;   build\python\          â€“ Embedded Python with all packages pre-installed
;
; Compile with:  iscc installer\row_bot_setup.iss

#ifnexist "build\python\Lib\site-packages\sentence_transformers\__init__.py"
  #error Embedded Python is missing sentence_transformers. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\langchain_huggingface\__init__.py"
  #error Embedded Python is missing langchain_huggingface. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\transformers\__init__.py"
  #error Embedded Python is missing transformers. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\torch\__init__.py"
  #error Embedded Python is missing torch. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\httpx\__init__.py"
  #error Embedded Python is missing httpx. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\keyring\__init__.py"
  #error Embedded Python is missing keyring. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\langchain_ollama\__init__.py"
  #error Embedded Python is missing langchain_ollama. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\langchain_openai\__init__.py"
  #error Embedded Python is missing langchain_openai. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\langchain_anthropic\__init__.py"
  #error Embedded Python is missing langchain_anthropic. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\langchain_google_genai\__init__.py"
  #error Embedded Python is missing langchain_google_genai. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\langchain_openrouter\__init__.py"
  #error Embedded Python is missing langchain_openrouter. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\langchain_xai\__init__.py"
  #error Embedded Python is missing langchain_xai. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\google\genai\__init__.py"
  #error Embedded Python is missing google.genai. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\openai\__init__.py"
  #error Embedded Python is missing openai. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\mcp\__init__.py"
  #error Embedded Python is missing mcp. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\youtube_search\__init__.py"
  #error Embedded Python is missing youtube_search. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#ifnexist "build\python\Lib\site-packages\youtube_transcript_api\__init__.py"
  #error Embedded Python is missing youtube_transcript_api. Run installer\build_installer.ps1 before compiling row_bot_setup.iss.
#endif

#define MyAppName      "Row-Bot"
#define MyAppVersion   "4.7.0"
#define MyAppPublisher "Row-Bot"
#define MyAppURL       "https://row-bot.ai"
#define MyAppExeName   "launch_row_bot.vbs"

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
OutputBaseFilename=Row-Bot-{#MyAppVersion}-Windows-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\row-bot.ico
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; The Windows build is self-contained. Replace the embedded Python on repair
; and upgrade so user-installed or broken optional packages cannot survive.
Type: filesandordirs; Name: "{app}\python"

[Files]
; App payload copied from scripts/app_payload_manifest.py
Source: "..\app.py";                  DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\launcher.py";             DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\pyproject.toml";          DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\uv.lock";                 DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\requirements.txt";       DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\row-bot.ico";            DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\scripts\verify_runtime_dependencies.py"; DestDir: "{app}\app\scripts"; Flags: ignoreversion
Source: "..\src\row_bot\*";        DestDir: "{app}\app\src\row_bot"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__\*,*.pyc,node_modules\*,.pytest_cache\*,tests\*,test\*,test-results\*,*.test.js,*.spec.js,*.bak,*.bak[0-9]*"
; Source-layout coverage: recursive src\row_bot include covers stability.py, embedding_config.py, embedding_providers.py, Agent budgets/settings, the Computer Use package and JSON manifest, cache-only embedding fallback, mobile access/PWA, cancellation, provider transports/tool schemas/catalogs, Developer, Skills Hub, self-evolution, and shared/native/plugin channel streaming.
Source: "..\static\*";              DestDir: "{app}\app\static"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\sounds\*";              DestDir: "{app}\app\sounds"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\bundled_skills\*";      DestDir: "{app}\app\bundled_skills"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\tool_guides\*";         DestDir: "{app}\app\tool_guides"; Flags: ignoreversion recursesubdirs createallsubdirs

; â”€â”€ Embedded Python (with all packages pre-installed) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Source: "build\python\*";              DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

; â”€â”€ Launcher scripts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Source: "launch_row_bot.bat";            DestDir: "{app}"; Flags: ignoreversion
Source: "launch_row_bot.vbs";            DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";                    Filename: "wscript.exe"; Parameters: """{app}\{#MyAppExeName}"""; IconFilename: "{app}\app\row-bot.ico"; Comment: "Launch Row-Bot"
Name: "{group}\Uninstall {#MyAppName}";           Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";               Filename: "wscript.exe"; Parameters: """{app}\{#MyAppExeName}"""; IconFilename: "{app}\app\row-bot.ico"; Tasks: desktopicon

[Run]
; â”€â”€ Launch app after install (optional) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Filename: "wscript.exe"; Parameters: """{app}\{#MyAppExeName}"""; Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app\__pycache__"
Type: filesandordirs; Name: "{app}\app\tools\__pycache__"
Type: filesandordirs; Name: "{app}\app\channels\__pycache__"
Type: filesandordirs; Name: "{app}\app\ui\__pycache__"
Type: filesandordirs; Name: "{app}\app\plugins\__pycache__"
