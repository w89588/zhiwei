#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef ProjectRoot
  #define ProjectRoot "..\.."
#endif
#ifndef OutputRoot
  #define OutputRoot ProjectRoot + "\release"
#endif
#ifndef AppSource
  #define AppSource ProjectRoot + "\dist\WechatHelper"
#endif

[Setup]
AppId={{C26B0B44-9EF1-450A-ADBF-C82C47F704C1}
AppName=知微客户端
AppVersion={#AppVersion}
AppPublisher=w89588
AppPublisherURL=https://github.com/w89588/zhiwei
AppSupportURL=https://github.com/w89588/zhiwei/issues
AppUpdatesURL=https://github.com/w89588/zhiwei/releases
DefaultDirName={localappdata}\Programs\Zhiwei
DefaultGroupName=Zhiwei
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.18362
OutputDir={#OutputRoot}
OutputBaseFilename=WechatHelper-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayIcon={app}\WechatHelper.exe
SetupIconFile={#ProjectRoot}\helper\collector_static\app.ico
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName=WechatHelper
VersionInfoDescription=WechatHelper Windows Installer
InfoBeforeFile={#ProjectRoot}\packaging\windows\INSTALL-NOTES.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "autostart"; Description: "Start with Windows (quietly in the system tray)"; GroupDescription: "Startup:"; Check: IsFirstInstall

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Zhiwei"; Filename: "{app}\WechatHelper.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Zhiwei"; Filename: "{app}\WechatHelper.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\WechatHelper.exe"; Description: "Launch WechatHelper"; Flags: nowait postinstall skipifsilent

[Registry]
; Always register uninstall cleanup, including when the in-app switch enables it later.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Zhiwei"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Zhiwei"; ValueData: """{app}\WechatHelper.exe"" --startup"; Check: ShouldEnableAutostart
Root: HKCU; Subkey: "Software\Zhiwei"; ValueType: dword; ValueName: "Installed"; ValueData: "1"; Flags: uninsdeletevalue uninsdeletekeyifempty

[Code]
var
  FirstInstall: Boolean;

function InitializeSetup(): Boolean;
begin
  FirstInstall := not RegValueExists(HKCU, 'Software\Zhiwei', 'Installed')
    and not RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{C26B0B44-9EF1-450A-ADBF-C82C47F704C1}_is1');
  Result := True;
end;

function IsFirstInstall(): Boolean;
begin
  Result := FirstInstall;
end;

function ShouldEnableAutostart(): Boolean;
begin
  if FirstInstall then
    Result := WizardIsTaskSelected('autostart')
  else
    { Preserve changes made inside the app; only refresh the executable path. }
    Result := RegValueExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Zhiwei');
end;
