; ══════════════════════════════════════════════════════════════════
; ShadowBuster — Inno Setup 安装器（per-machine，默认装 Program Files，
; 用户可在向导中自选安装目录）
;
; 前置：
;   1) powershell -File packaging\build_shell.ps1     → dist\ShadowBuster\
;   2) powershell -File packaging\runtime_sync.ps1    → packaging\stage\runtime\
;   3) iscc packaging\installer.iss                    → packaging\out\ShadowBuster-Setup-*.exe
;
; 中文界面可选：将 Inno 的 ChineseSimplified.isl 放入 compiler 语言目录后
; 取消下方 [Languages] 注释。
; ══════════════════════════════════════════════════════════════════
#define MyAppName "ShadowBuster"
#define MyAppVersion "1.4.0"  ; 与 studio_backend.APP_VERSION 保持一致（tests/test_app_version.py 校验）
#define MyAppExeName "ShadowBuster.exe"

[Setup]
AppId={{9F4B7A3C-2D5E-4B1A-9C6D-9E2A1F5B7C3D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=ShadowBuster
DefaultDirName={autopf}\ShadowBuster
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=out
OutputBaseFilename=ShadowBuster-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

;[Languages]
;Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; UI 外壳（PyInstaller onedir，整目录递归）—— 相对本 .iss 的路径
Source: "..\dist\ShadowBuster\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; AI 推理运行时（CUDA torch / demucs / look2hear / 模型 / ffmpeg / 预置权重）
Source: "stage\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; runasoriginaluser：安装完以普通用户身份启动，避免应用带着管理员令牌运行
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent runasoriginaluser
