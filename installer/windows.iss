; 出品写真ツール Windows インストーラー（Inno Setup 6）
#define AppVer GetEnv("APP_VERSION")
#if AppVer == ""
  #define AppVer "1.2.0"
#endif

[Setup]
AppId={{6B1F3C2A-8E4D-4C1B-9A57-2F0D5E7C9B13}
AppName=出品写真ツール
AppVersion={#AppVer}
AppPublisher=ShuppinPhoto
DefaultDirName={autopf}\ShuppinPhoto
DefaultGroupName=出品写真ツール
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\installer_out
OutputBaseFilename=ShuppinPhoto-Setup-Windows
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\ShuppinPhoto.exe
UninstallDisplayName=出品写真ツール
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\ShuppinPhoto\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\出品写真ツール"; Filename: "{app}\ShuppinPhoto.exe"
Name: "{autodesktop}\出品写真ツール"; Filename: "{app}\ShuppinPhoto.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ShuppinPhoto.exe"; Description: "{cm:LaunchProgram,出品写真ツール}"; Flags: nowait postinstall skipifsilent
