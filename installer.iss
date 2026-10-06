; Inno Setup 6 脚本：生成「微信AI润色助手」安装程序
; 重要：本文件必须保存为 UTF-8 **带 BOM**，否则 Inno Setup 会按 ANSI 读取导致中文乱码。
; 用法：ISCC installer.iss   （产出 dist\Setup_微信AI润色助手.exe）

#define MyAppName "ai-reply-helper"
#define MyAppVersion "1.0"
#define MyAppPublisher "WeChat AI Polish"
#define MyAppExeName "ai-reply-helper.exe"
#define MyAppDirName "ai-reply-helper"

[Setup]
AppId={{7F3A9C21-4B6E-4D58-9A1C-2E5D8B0F4A17}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 装到用户目录：免管理员(UAC)提权，且保证配置/日志可写
DefaultDirName={localappdata}\Programs\{#MyAppDirName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputBaseFilename=Setup_{#MyAppName}
OutputDir=dist
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 不需要管理员权限
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 打包产物为 64 位。用 x64compatible（而非已废弃的 x64 / 过严的 x64os），
; 这样普通 64 位 Win10/11 都能装，ARM64 机器上的 x64 兼容层也允许安装。
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; 安装向导与生成的 Setup.exe 使用微信风格图标（快捷方式图标跟随 exe 自身图标）
SetupIconFile=icon.ico

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked

[Files]
; 动图资源(1.webp/2.webp)已打包进 exe，无需单独安装
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 注意：不做开机自启（作者要求手动启动）。这里只是安装完可选「立即运行」。
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
