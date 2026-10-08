; BiliFav Organizer —— Windows 安装包脚本（Inno Setup 6）
;
; 构建（本地）：
;   ISCC.exe /DAppVersion=dev /DFileVersion=0.0.0.0 packaging\BiliFavOrganizer.iss
; 构建（CI，tag vX.Y.Z）：
;   ISCC.exe /DAppVersion=vX.Y.Z /DFileVersion=X.Y.Z.0 packaging\BiliFavOrganizer.iss
;
; 前提：源目录里已经放好便携包内容（BiliFavOrganizer.exe、_internal\、使用说明.txt、
; LICENSE.txt、NOTICE.txt 等），与 Release 里的便携 ZIP 同一份目录。
; 编码：本文件与「协议摘要.txt」都必须是带 BOM 的 UTF-8，否则中文在向导里会变乱码。

#ifndef AppVersion
  #define AppVersion "dev"
#endif
#ifndef FileVersion
  #define FileVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\BiliFavOrganizer"
#endif

#define AppName "BiliFav Organizer"
#define AppPublisher "Martin-soaring-dev"
#define AppURL "https://github.com/Martin-soaring-dev/bili-fav-organizer"
#define AppKey "Software\Martin-soaring-dev\BiliFavOrganizer"

[Setup]
AppId={{7C4D9A31-5B62-4E0F-9D18-3A6E0C7B21F5}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
; 安装程序自身的文件属性也带上署名（Inno 会把每条值截到 100 字符，故不写长 URL）
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} 安装程序（B站收藏夹智能整理）
VersionInfoCopyright=© 2026 Martin-soaring-dev · PolyForm Noncommercial License 1.0.0
VersionInfoVersion={#FileVersion}
DefaultDirName={autopf}\BiliFavOrganizer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 安装范围由用户在向导里选：仅当前用户（免管理员）/ 为所有用户（会弹 UAC）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile=协议摘要.txt
WizardImageFile=wizard-image.png
WizardSmallImageFile=wizard-small.png
OutputDir=..\dist
OutputBaseFilename=BiliFavOrganizer-Setup-{#AppVersion}
SetupIconFile=BiliFavOrganizer.ico
UninstallDisplayIcon={app}\BiliFavOrganizer.exe
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 许可与通知必须随包交付（与便携 ZIP 一致的义务）
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\NOTICE"; DestDir: "{app}"; DestName: "NOTICE.txt"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\BiliFavOrganizer.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\BiliFavOrganizer.exe"; WorkingDir: "{app}"; Tasks: desktopicon

; 应用自己的安装标记：让程序知道"我是安装版"，更新时改为运行新的安装包
[Registry]
Root: HKCU; Subkey: "{#AppKey}"; ValueType: string; ValueName: "InstallMode"; ValueData: "installed"; Flags: uninsdeletekey
Root: HKCU; Subkey: "{#AppKey}"; ValueType: string; ValueName: "InstallDir"; ValueData: "{app}"
Root: HKCU; Subkey: "{#AppKey}"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"
Root: HKCU; Subkey: "{#AppKey}"; ValueType: string; ValueName: "Scope"; ValueData: "{code:GetScopeName}"

[Run]
Filename: "{app}\BiliFavOrganizer.exe"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
var
  AttributionLbl: TNewStaticText;
  OpPage, DataPage: TInputOptionWizardPage;
  ExistingInstall: Boolean;
  Operation: Integer;     {0=安装/更新 1=修复 2=卸载}
  DeleteUserData: Boolean;
  UninstallDone: Boolean;

function ExistingInstallDir(): string;
begin
  Result := '';
  if not RegQueryStringValue(HKCU, '{#AppKey}', 'InstallDir', Result) then
    Result := '';
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  Operation := 0;
  DeleteUserData := False;
  UninstallDone := False;
  ExistingInstall := (ExistingInstallDir() <> '') and DirExists(ExistingInstallDir());
end;

procedure InitializeWizard();
begin
  {署名：两行灰字，底对齐到 Next 按钮那一行、靠左}
  AttributionLbl := TNewStaticText.Create(WizardForm);
  AttributionLbl.Parent := WizardForm;
  AttributionLbl.AutoSize := True;
  AttributionLbl.Font.Size := 8;
  AttributionLbl.Font.Color := clGray;
  AttributionLbl.Caption := 'BiliFav Organizer · B站收藏夹智能整理' + #13#10 +
    '@Martin-soaring-dev · PolyForm Noncommercial License 1.0.0';
  AttributionLbl.Left := ScaleX(8);
  AttributionLbl.Top := WizardForm.NextButton.Top + WizardForm.NextButton.Height -
    AttributionLbl.Height;
  AttributionLbl.BringToFront;

  {操作选择页：在用户协议页之前；注册表里找不到安装信息时，修复/卸载置灰不可选}
  OpPage := CreateInputOptionPage(wpWelcome, '选择操作',
    '请选择这次要执行的操作', '', True, False);
  OpPage.Add('安装 / 更新到本版本（保留个人数据）');
  OpPage.Add('修复安装（重新复制程序文件，不动数据）');
  OpPage.Add('卸载（删除本程序）');
  OpPage.Values[0] := True;
  if not ExistingInstall then
  begin
    OpPage.CheckListBox.ItemEnabled[1] := False;
    OpPage.CheckListBox.ItemEnabled[2] := False;
  end;

  {卸载的数据处理：只有选了"卸载"才会出现，而且只在这里确认一次}
  DataPage := CreateInputOptionPage(OpPage.ID, '卸载确认',
    '卸载时怎么处理个人数据？',
    '个人数据目录：' + ExpandConstant('{localappdata}\BiliFavOrganizer'),
    True, False);
  DataPage.Add('保留数据并卸载（推荐；以后装回来还能继续用）');
  DataPage.Add('清除数据并卸载（永久删除 Cookie、API Key、收藏库）');
  DataPage.Values[0] := True;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  {修复安装不必再接受一次许可}
  if PageID = wpLicense then
  begin
    if ExistingInstall and (Operation = 1) then
      Result := True;
  end;
  {没选卸载就不需要数据处理页}
  if PageID = DataPage.ID then
    Result := (Operation <> 2);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  UninstExe, Params, DataDir: string;
  ResultCode: Integer;
begin
  Result := True;

  if CurPageID = OpPage.ID then
  begin
    if OpPage.Values[2] then
      Operation := 2
    else if OpPage.Values[1] then
      Operation := 1
    else
      Operation := 0;
    Exit;
  end;

  if CurPageID = DataPage.ID then
  begin
    DeleteUserData := DataPage.Values[1];
    DataDir := ExpandConstant('{localappdata}\BiliFavOrganizer');
    UninstExe := AddBackslash(ExistingInstallDir()) + 'unins000.exe';
    if FileExists(UninstExe) then
    begin
      {数据删不删上一步已经问过：把选择写成一次性注册表标记，卸载程序读一次就清掉，
       它不会再问第二遍。}
      if DeleteUserData then
        RegWriteStringValue(HKCU, '{#AppKey}', 'UninstallData', 'delete')
      else
        RegWriteStringValue(HKCU, '{#AppKey}', 'UninstallData', 'keep');
      Exec(UninstExe, '/SILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_SHOW,
           ewWaitUntilTerminated, ResultCode);
      if ResultCode <> 0 then
        MsgBox('卸载程序返回代码 ' + IntToStr(ResultCode) + '，卸载可能没有完成。',
               mbError, MB_OK)
      else if DeleteUserData then
        MsgBox('卸载完成：程序文件已删除，个人数据也已按你的选择清除。',
               mbInformation, MB_OK)
      else
        MsgBox('卸载完成：程序文件已删除，个人数据保留在：' + #13#10 + DataDir,
               mbInformation, MB_OK);
    end
    else
      MsgBox('找不到卸载程序：' + UninstExe + #13#10 +
             '可以在 Windows「应用和功能」里卸载。', mbError, MB_OK);
    {卸载已经执行：这里退出安装向导。直接调用 Cancel 的处理函数走 Inno 正常收尾，
     比 WizardForm.Close 可靠（本机实测 Close 在本页事件里不生效）。}
    UninstallDone := True;
    Result := False;
    WizardForm.CancelButton.OnClick(nil);
    Exit;
  end;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  {选了"卸载"：这不是"安装未完成"，收尾时不要再弹那句确认}
  if UninstallDone or (Operation = 2) then
    Confirm := False;
end;

function GetScopeName(Param: string): string;
begin
  if IsAdminInstallMode then
    Result := 'all-users'
  else
    Result := 'current-user';
end;

function InitializeUninstall(): Boolean;
var
  DataDir, DataChoice: string;
begin
  Result := True;
  DataDir := ExpandConstant('{localappdata}\BiliFavOrganizer');

  {从安装程序里卸载时，数据选择已经在上一步确认过：读一次性标记执行，不再询问。
   读完立刻清掉标记，免得下次直接运行卸载程序时沿用旧选择。}
  DataChoice := '';
  if RegQueryStringValue(HKCU, '{#AppKey}', 'UninstallData', DataChoice) then
  begin
    RegWriteStringValue(HKCU, '{#AppKey}', 'UninstallData', '');
    if DataChoice = 'delete' then
    begin
      if DirExists(DataDir) then
        DelTree(DataDir, True, True, True);
      Exit;
    end;
    if DataChoice = 'keep' then
      Exit;
  end;

  {直接运行卸载程序（Windows「应用和功能」）：这里问这一次}
  if UninstallSilent() then
    Exit;                 {静默卸载默认保留数据}
  if not DirExists(DataDir) then
    Exit;
  if MsgBox('是否同时删除个人数据？' + #13#10 + #13#10 +
    DataDir + ' 里保存着 B 站 Cookie、模型 API Key、收藏夹索引与画像。' + #13#10 +
    '选择「是」将永久删除且不可恢复；选择「否」只卸载程序、保留数据。',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(DataDir, True, True, True);
end;
