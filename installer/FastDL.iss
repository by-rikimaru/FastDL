; FastDL kurulum betiği (Inno Setup 6)
; Derleme: ISCC.exe /DAppVersion=1.0.0 installer\FastDL.iss
; installer\python-installer.exe dosyası derlemeden önce indirilmiş olmalı
; (GitHub Actions bunu otomatik yapar, bkz. .github/workflows/release.yml).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F2B8C41-3D7A-4E59-9B1E-FA57D0C2A913}
AppName=FastDL
AppVersion={#AppVersion}
AppVerName=FastDL {#AppVersion}
AppPublisher=FastDL
AppPublisherURL=https://github.com/by-rikimaru/FastDL
AppSupportURL=https://github.com/by-rikimaru/FastDL/issues
AppUpdatesURL=https://github.com/by-rikimaru/FastDL/releases
DefaultDirName={localappdata}\Programs\FastDL
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=FastDL-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
UninstallDisplayName=FastDL İndirme Yöneticisi

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\fastdl.pyw"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
; Python kurucusu kuruluma gömülü; sadece gerekirse çıkarılıp çalıştırılır.
Source: "python-installer.exe"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\FastDL"; Filename: "{code:PythonW}"; Parameters: """{app}\fastdl.pyw"""; WorkingDir: "{app}"; Comment: "FastDL İndirme Yöneticisi"
Name: "{autoprograms}\FastDL Tarayıcı Eklentisi (klasör)"; Filename: "{app}\extension"
Name: "{autodesktop}\FastDL"; Filename: "{code:PythonW}"; Parameters: """{app}\fastdl.pyw"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{code:PythonW}"; Parameters: """{app}\fastdl.pyw"""; WorkingDir: "{app}"; Description: "FastDL'i başlat"; Flags: postinstall nowait
Filename: "{app}\extension"; Description: "Tarayıcı eklentisi klasörünü aç (Chrome'a yüklemek için)"; Flags: postinstall shellexec skipifsilent unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
var
  PyW: String;

{ Python'u kayıt defterinde arar; pythonw.exe ve tkinter varsa yolunu döndürür. }
function TryPy(Root: Integer; Key: String): String;
var
  P: String;
begin
  Result := '';
  if RegQueryStringValue(Root, 'Software\Python\PythonCore\' + Key + '\InstallPath', '', P) then
  begin
    P := AddBackslash(P);
    if FileExists(P + 'pythonw.exe') and DirExists(P + 'Lib\tkinter') then
      Result := P + 'pythonw.exe';
  end;
end;

function FindPythonW(): String;
var
  i: Integer;
  V: String;
begin
  Result := '';
  for i := 14 downto 9 do
  begin
    V := '3.' + IntToStr(i);
    Result := TryPy(HKCU, V);
    if Result = '' then Result := TryPy(HKLM64, V);
    if Result = '' then Result := TryPy(HKLM32, V);
    if Result = '' then Result := TryPy(HKCU, V + '-32');
    if Result = '' then Result := TryPy(HKLM32, V + '-32');
    if Result <> '' then Exit;
  end;
end;

function InitializeSetup(): Boolean;
begin
  PyW := FindPythonW();
  Result := True;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine + 'Python:' + NewLine + Space;
  if PyW <> '' then
    Result := Result + 'Kurulu (' + PyW + ')'
  else
    Result := Result + 'Bulunamadı, Python 3.12 kurulacak (yönetici izni gerekmez)';
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + NewLine + MemoTasksInfo;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  RC: Integer;
begin
  { Kısayollar oluşturulmadan önce Python hazır olmalı. }
  if (CurStep = ssInstall) and (PyW = '') then
  begin
    WizardForm.StatusLabel.Caption := 'Python kuruluyor, bu birkaç dakika sürebilir...';
    ExtractTemporaryFile('python-installer.exe');
    if not Exec(ExpandConstant('{tmp}\python-installer.exe'),
      '/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_tcltk=1 Include_test=0 Shortcuts=0',
      '', SW_HIDE, ewWaitUntilTerminated, RC) or (RC <> 0) then
      SuppressibleMsgBox('Python kurulamadı (hata kodu ' + IntToStr(RC) + ').' + #13#10 +
        'python.org adresinden Python 3 kurup FastDL kurulumunu tekrar çalıştırın.',
        mbError, MB_OK, IDOK);
    PyW := FindPythonW();
  end;
end;

function PythonW(Param: String): String;
begin
  if PyW = '' then
    PyW := FindPythonW();
  Result := PyW;
  if Result = '' then
    Result := 'pythonw.exe';
end;
