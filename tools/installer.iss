; Inno Setup 安装脚本 —— 把 onedir 打成安装包（安装版，支持自动更新）
; 用法：安装 Inno Setup 后，在项目根目录执行：
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=2.2.0 "tools\installer.iss"
; 产物：dist\报销材料整理助手-setup-v<version>.exe
#define AppName "报销材料整理助手"
#ifndef AppVersion
  #define AppVersion "2.3.0"
#endif
#ifndef SourceDir
  #define SourceDir SourcePath + "\..\dist\报销材料整理助手"
#endif
#ifndef OutputDir
  #define OutputDir SourcePath + "\..\dist"
#endif
#ifndef AppIcon
  #define AppIcon SourcePath + "\..\assets\app.ico"
#endif
#define AppExeName "报销材料整理助手.exe"
; 固定 AppId：保证后续版本被识别为同一软件、覆盖升级（勿改）
#define AppId "{{8B7A6C5D-1E2F-4A9B-8C3D-7E5F6A4B2C1D}}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=reimburse-setup-v{#AppVersion}
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
PrivilegesRequired=lowest
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; 把 dist\报销材料整理助手\（onedir：exe + _internal + 依赖）整个装进安装目录
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; AppUserModelID: "ReimburseOrganizer.Desktop"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; AppUserModelID: "ReimburseOrganizer.Desktop"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
