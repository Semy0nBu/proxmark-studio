; Per-user packaging only. The library is never an installer-owned directory.
#ifndef BundleDir
  #error BundleDir must identify the verified portable distribution
#endif
#ifndef SetupOutputDir
  #error SetupOutputDir must identify the installer output directory
#endif
#ifndef SetupIcon
  #error SetupIcon must identify the Windows setup icon
#endif
#ifndef AppVersion
  #define AppVersion "0.3.0-preview.6"
#endif
#ifndef NumericVersion
  #define NumericVersion "0.3.0.6"
#endif

[Setup]
AppId={{FC5D2092-EC11-5DEA-A27B-57AA5F549E1A}
AppName=Proxmark Studio
AppVersion={#AppVersion}
AppVerName=Proxmark Studio {#AppVersion}
VersionInfoVersion={#NumericVersion}
DefaultDirName={localappdata}\Programs\Proxmark Studio
DefaultGroupName=Proxmark Studio
PrivilegesRequired=lowest
SetupArchitecture=x64
ArchitecturesAllowed=x64compatible and not arm64
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.18362
WizardStyle=modern
DisableProgramGroupPage=yes
LicenseFile={#BundleDir}\LICENSE
SetupIconFile={#SetupIcon}
OutputDir={#SetupOutputDir}
OutputBaseFilename=ProxmarkStudio-{#AppVersion}-Setup-x64
UninstallDisplayName=Proxmark Studio
UninstallDisplayIcon={app}\ProxmarkStudio.exe
Compression=lzma2
SolidCompression=yes
CloseApplications=no
RestartApplications=no
AlwaysRestart=no
RestartIfNeededByRun=no
UninstallRestartComputer=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{group}\Uninstall Proxmark Studio.lnk"; Check: OwnPreviousInstall
Type: files; Name: "{group}\Деинсталлировать Proxmark Studio.lnk"; Check: OwnPreviousInstall
Type: files; Name: "{userdesktop}\Proxmark Studio.lnk"; Tasks: not desktopicon; Check: OwnPreviousInstall

[Icons]
Name: "{group}\Proxmark Studio"; Filename: "{app}\ProxmarkStudio.exe"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,Proxmark Studio}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Proxmark Studio"; Filename: "{app}\ProxmarkStudio.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ProxmarkStudio.exe"; Description: "{cm:LaunchProgram,Proxmark Studio}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[CustomMessages]
english.RunningApps=Close Proxmark Studio and all proxmark3.exe processes before installing or removing the application. They will not be stopped automatically.
russian.RunningApps=Закройте Proxmark Studio и все процессы proxmark3.exe перед установкой или удалением приложения. Установщик не будет завершать их автоматически.
english.ProcessScanFailed=Unable to check running processes. Installation or removal has been stopped without changing the application.
russian.ProcessScanFailed=Не удалось проверить запущенные процессы. Установка или удаление остановлены без изменения приложения.
english.LibraryDirectory=Choose a separate program folder. This directory overlaps the library folder:
russian.LibraryDirectory=Выберите отдельную папку программы. Эта папка пересекается с папкой библиотеки:
english.NonApplicationDirectory=The selected folder is not empty and does not contain a recognized Proxmark Studio installation. Choose an empty folder or the existing application folder.
russian.NonApplicationDirectory=Выбранная папка не пуста и не содержит распознанной установки Proxmark Studio. Выберите пустую папку или существующую папку приложения.
english.NewerVersionInstalled=A newer version of Proxmark Studio is already installed in this folder. Downgrading is blocked. Use the same or a newer installer.
russian.NewerVersionInstalled=В этой папке уже установлена более новая версия Proxmark Studio. Установка старой версии запрещена. Используйте установщик той же или более новой версии.
english.InvalidPackageVersion=The installer contains an invalid numeric application version.
russian.InvalidPackageVersion=В установщике указана некорректная числовая версия приложения.

[Code]
const
  TH32CS_SNAPPROCESS = $00000002;
  ERROR_NO_MORE_FILES = 18;

type
  { SetupArchitecture=x64: PROCESSENTRY32W is 568 bytes; filename starts at 44. }
  TProcessEntry32W = record
    Size: Cardinal;
    Data: array[0..563] of Byte;
  end;

function CreateToolhelp32Snapshot(Flags, ProcessId: Cardinal): THandle;
  external 'CreateToolhelp32Snapshot@kernel32.dll stdcall';
function Process32FirstW(Snapshot: THandle; var Entry: TProcessEntry32W): Integer;
  external 'Process32FirstW@kernel32.dll stdcall';
function Process32NextW(Snapshot: THandle; var Entry: TProcessEntry32W): Integer;
  external 'Process32NextW@kernel32.dll stdcall';
function WinCloseHandle(Handle: THandle): Integer;
  external 'CloseHandle@kernel32.dll stdcall';
function WinGetLastError: Cardinal;
  external 'GetLastError@kernel32.dll stdcall';

function ProcessFileName(const Entry: TProcessEntry32W): String;
var
  I, CodePoint: Integer;
begin
  Result := '';
  for I := 0 to 259 do
  begin
    CodePoint := Entry.Data[40 + I * 2] + Entry.Data[41 + I * 2] * 256;
    if CodePoint = 0 then
      Break;
    Result := Result + Chr(CodePoint);
  end;
end;

function BlockingProcessMessage: String;
var
  Snapshot: THandle;
  Entry: TProcessEntry32W;
  Name: String;
begin
  Result := '';
  Snapshot := CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
  if (Snapshot = 0) or (Snapshot = THandle(-1)) then
  begin
    Result := CustomMessage('ProcessScanFailed');
    Exit;
  end;
  try
    Entry.Size := 568;
    if Process32FirstW(Snapshot, Entry) = 0 then
    begin
      Result := CustomMessage('ProcessScanFailed');
      Exit;
    end;
    repeat
      Name := ProcessFileName(Entry);
      if (CompareText(Name, 'ProxmarkStudio.exe') = 0) or
         (CompareText(Name, 'proxmark3.exe') = 0) then
      begin
        Result := CustomMessage('RunningApps') + #13#10 + Name;
        Exit;
      end;
    until Process32NextW(Snapshot, Entry) = 0;
    if WinGetLastError <> ERROR_NO_MORE_FILES then
      Result := CustomMessage('ProcessScanFailed');
  finally
    WinCloseHandle(Snapshot);
  end;
end;

function NormalDirectory(const Directory: String): String;
begin
  Result := RemoveBackslash(ExpandFileName(Trim(Directory)));
end;

function SameOrBelow(const Directory, Parent: String): Boolean;
var
  Prefix: String;
begin
  Prefix := AddBackslash(Parent);
  Result := (CompareText(Directory, Parent) = 0) or
    (CompareText(Copy(AddBackslash(Directory), 1, Length(Prefix)), Prefix) = 0);
end;

function OwnPreviousInstall: Boolean;
var
  PreviousDirectory: String;
begin
  Result := RegQueryStringValue(HKCU64,
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{FC5D2092-EC11-5DEA-A27B-57AA5F549E1A}_is1',
    'InstallLocation', PreviousDirectory);
  if Result then
    Result := CompareText(NormalDirectory(PreviousDirectory),
      NormalDirectory(ExpandConstant('{app}'))) = 0;
end;

function DirectoryHasFiles(const Directory: String): Boolean;
var
  Found: TFindRec;
begin
  Result := False;
  if FindFirst(AddBackslash(Directory) + '*', Found) then
  begin
    try
      repeat
        if (Found.Name <> '.') and (Found.Name <> '..') then
        begin
          Result := True;
          Exit;
        end;
      until not FindNext(Found);
    finally
      FindClose(Found);
    end;
  end;
end;

function DirectoryError(const Directory: String): String;
var
  Target, LibraryPath: String;
begin
  Result := '';
  Target := NormalDirectory(Directory);
  LibraryPath := NormalDirectory(ExpandConstant('{localappdata}\Proxmark Studio'));
  if SameOrBelow(Target, LibraryPath) or SameOrBelow(LibraryPath, Target) then
  begin
    Result := CustomMessage('LibraryDirectory') + #13#10 + LibraryPath;
    Exit;
  end;
  if DirExists(Target) and DirectoryHasFiles(Target) and
     not FileExists(AddBackslash(Target) + 'ProxmarkStudio.exe') then
    Result := CustomMessage('NonApplicationDirectory');
end;

function ParseVersion(const Version: String; var MS, LS: Cardinal): Boolean;
var
  Parts: array[0..3] of Integer;
  Remaining, Part: String;
  I, Dot: Integer;
begin
  Result := False;
  Remaining := Version;
  for I := 0 to 3 do
  begin
    Dot := Pos('.', Remaining);
    if I < 3 then
    begin
      if Dot = 0 then Exit;
      Part := Copy(Remaining, 1, Dot - 1);
      Delete(Remaining, 1, Dot);
    end
    else
    begin
      if Dot <> 0 then Exit;
      Part := Remaining;
    end;
    Parts[I] := StrToIntDef(Part, -1);
    if (Parts[I] < 0) or (Parts[I] > 65535) then Exit;
  end;
  MS := (Cardinal(Parts[0]) shl 16) or Cardinal(Parts[1]);
  LS := (Cardinal(Parts[2]) shl 16) or Cardinal(Parts[3]);
  Result := True;
end;

function DowngradeError(const Directory: String): String;
var
  NewMS, NewLS, OldMS, OldLS: Cardinal;
  AppExe: String;
begin
  Result := '';
  if not ParseVersion('{#NumericVersion}', NewMS, NewLS) then
  begin
    Result := CustomMessage('InvalidPackageVersion');
    Exit;
  end;
  AppExe := AddBackslash(Directory) + 'ProxmarkStudio.exe';
  if FileExists(AppExe) then
  begin
    if not GetVersionNumbers(AppExe, OldMS, OldLS) then
      Result := CustomMessage('NonApplicationDirectory')
    else if (OldMS > NewMS) or ((OldMS = NewMS) and (OldLS > NewLS)) then
      Result := CustomMessage('NewerVersionInstalled');
  end;
end;

function InitializeSetup: Boolean;
var
  Problem: String;
begin
  Problem := BlockingProcessMessage;
  Result := Problem = '';
  if not Result then
    SuppressibleMsgBox(Problem, mbError, MB_OK, IDOK);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Problem: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Problem := DirectoryError(WizardDirValue);
    Result := Problem = '';
    if not Result then
      SuppressibleMsgBox(Problem, mbError, MB_OK, IDOK);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  NeedsRestart := False;
  Result := BlockingProcessMessage;
  if Result <> '' then Exit;
  Result := DirectoryError(ExpandConstant('{app}'));
  if Result <> '' then Exit;
  Result := DowngradeError(ExpandConstant('{app}'));
end;

function InitializeUninstall: Boolean;
var
  Problem: String;
begin
  Problem := BlockingProcessMessage;
  Result := Problem = '';
  if not Result then
    SuppressibleMsgBox(Problem, mbError, MB_OK, IDOK);
end;
