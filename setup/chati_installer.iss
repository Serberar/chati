; Instalador de Chati IA (Inno Setup). Se compila con build_release.ps1, que
; hace antes ChatiIA.exe y le pasa la version de orchestrator\version.py.
; No hace falta tocar este archivo salvo que anadas una carpeta nueva de
; nivel superior, o cambies el catalogo de modelos (ver
; orchestrator\model_catalog.py, que es la fuente real de verdad para las
; URLs de descarga - la lista de aqui abajo es solo para mostrarla en el
; asistente, mantenla sincronizada a mano si cambias el catalogo).
;
; Con /UPDATE=1 (lo pasa la propia app al actualizarse, app_update.py) no
; crea administrador ni baja modelos, y al acabar vuelve a abrir Chati.

#define MyAppName "Chati IA"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Sergio Bernabé
AppComments=Creada por Sergio Bernabé
DefaultDirName={autopf}\Chati IA
DisableProgramGroupPage=yes
OutputBaseFilename=ChatiIA-Instalador
SetupIconFile=..\icono.ico
Compression=lzma2
SolidCompression=yes
ShowLanguageDialog=no
WizardStyle=modern
; El codigo (esta carpeta) va a Archivos de Programa - eso SI necesita
; administrador, es normal para cualquier programa. Los datos/modelos NO
; (van a AppData - ver paths.py y install.ps1), asi que anadir un modelo
; nuevo desde la propia app despues nunca pedira permisos.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
; sin esto {autopf} es "Program Files (x86)" aunque todo sea de 64 bits
; (visto en Windows Sandbox, 2026-09-30)
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[CustomMessages]
spanish.WelcomeCredit=Creada por Sergio Bernabé.

[Tasks]
Name: "autostart"; Description: "Arrancar Chati al iniciar Windows (si no, arranca al abrirla; se puede cambiar en Opciones)"; Flags: unchecked

[InstallDelete]
; el lanzador de la version anterior entero: PyInstaller cambia sus archivos
; internos entre compilaciones y los que sobran no se borrarian
Type: filesandordirs; Name: "{app}\ChatiIA"

[Files]
; PrepareToInstall lo usa para cerrar Chati antes de copiar nada
Source: "stop_chati.ps1"; Flags: dontcopy
Source: "..\orchestrator\*"; DestDir: "{app}\orchestrator"; Flags: recursesubdirs ignoreversion; \
    Excludes: "venv\*,__pycache__\*,.pytest_cache\*,outputs\*,_persona_upload_*,test_*.png,test_*.mp4,test_*.wav,test_*.webm,i2v_*.png,server.log"
Source: "..\setup\*"; DestDir: "{app}\setup"; Flags: recursesubdirs ignoreversion; \
    Excludes: "watchdog.log,watchdog.log.old,last_backup.txt,chati_installer.iss,build_release.ps1,probar_instalador.wsb,Output\*"
Source: "..\AGENTS.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\MANUAL_DE_USO.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\icono.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\logo.png"; DestDir: "{app}"; Flags: ignoreversion
; lanzador compilado (build_launcher.ps1) - lleva su propio Python dentro
Source: "..\ChatiIA\*"; DestDir: "{app}\ChatiIA"; Flags: recursesubdirs ignoreversion

[Icons]
; ChatiIA.exe (desktop_app.py empaquetado): sin consola, y en el
; Administrador de tareas aparece como "Chati IA" con su icono, no "Python".
Name: "{userdesktop}\Chati IA"; Filename: "{app}\ChatiIA\ChatiIA.exe"; \
    WorkingDir: "{app}\ChatiIA"; AppUserModelID: "SergioBernabe.ChatiIA"

[Run]
Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -File ""{app}\setup\install.ps1"" -AiRoot ""{app}"" -Autostart {code:AutostartArg}"; \
    StatusMsg: "Instalando Ollama, ComfyUI y los entornos de Python (puede tardar varios minutos)..."; \
    Flags: waituntilterminated
Filename: "{app}\orchestrator\venv\Scripts\python.exe"; \
    Parameters: "installer_create_admin.py --from-file ""{tmp}\chati_admin.txt"""; \
    WorkingDir: "{app}\orchestrator"; \
    StatusMsg: "Creando el usuario administrador..."; \
    Flags: waituntilterminated runhidden; Check: not IsUpdate
Filename: "{app}\orchestrator\venv\Scripts\python.exe"; \
    Parameters: "installer_download_models.py --selected-file ""{tmp}\chati_selected_models.txt"""; \
    WorkingDir: "{app}\orchestrator"; \
    StatusMsg: "Descargando los modelos elegidos..."; \
    Flags: waituntilterminated; Check: not IsUpdate
Filename: "{app}\ChatiIA\ChatiIA.exe"; \
    WorkingDir: "{app}\ChatiIA"; \
    Description: "Arrancar Chati IA ahora"; \
    Flags: postinstall nowait skipifsilent runasoriginaluser
; al actualizar se vuelve a abrir lo que cerro stop_chati.ps1 (la ventana
; arranca tambien los servicios y el vigilante, ver start_all.ps1)
Filename: "{app}\ChatiIA\ChatiIA.exe"; WorkingDir: "{app}\ChatiIA"; \
    Flags: nowait runasoriginaluser; Check: IsUpdate

[Code]
type
  TCatalogItem = record
    Id: String;
    Modality: String;
    Label_: String;
    Description: String;
    Checked: Boolean;
  end;

var
  AdminPage: TInputQueryWizardPage;
  ModelsPage: TWizardPage;
  ModelsList: TNewCheckListBox;
  GpuInfoLabel: TNewStaticText;
  Catalog: array of TCatalogItem;

procedure AddCatalogItem(Id, Modality, Lbl, Desc: String; DefaultChecked: Boolean);
var
  Idx: Integer;
begin
  Idx := GetArrayLength(Catalog);
  SetArrayLength(Catalog, Idx + 1);
  Catalog[Idx].Id := Id;
  Catalog[Idx].Modality := Modality;
  Catalog[Idx].Label_ := Lbl;
  Catalog[Idx].Description := Desc;
  Catalog[Idx].Checked := DefaultChecked;
end;

// Mismo catalogo que orchestrator\model_catalog.py - solo texto para
// mostrar en el asistente, las URLs de verdad viven alli.
procedure BuildCatalog(IncludeVideo: Boolean);
begin
  SetArrayLength(Catalog, 0);
  AddCatalogItem('texto-rapido', 'Texto', 'Chat rapido',
    'Respuestas breves, conversacion general.', True);
  AddCatalogItem('texto-calidad', 'Texto', 'Chat de mejor calidad',
    'Mas lento, menos probable que invente datos.', True);
  AddCatalogItem('texto-vision', 'Texto', 'Vision (comentar fotos)',
    'Necesario para poder subir una foto en el chat.', True);
  AddCatalogItem('agente-rapido', 'Texto', 'Agente (tareas en el ordenador)',
    'Necesario para el modo Agente: crea, mueve y ordena archivos por ti.', True);
  AddCatalogItem('imagen-flux', 'Imagen', 'FLUX (rapido, buena calidad)',
    'El generador de imagen recomendado por defecto.', True);
  AddCatalogItem('imagen-sdxl', 'Imagen', 'SDXL base',
    'Necesario para preservar caras, ControlNet e inpaint.', True);
  // sin GPU, editar una foto tardaria horas
  if IncludeVideo then
    AddCatalogItem('imagen-editar-qwen', 'Imagen', 'Editar fotos (Qwen-Image-Edit, ~24 GB)',
      'Edita tus fotos con una frase (ropa, fondo, postura, mirar a camara) manteniendo las caras.', True);
  if IncludeVideo then
    AddCatalogItem('imagen-editar', 'Imagen', 'Editar fotos con FLUX Kontext (respaldo)',
      'El editor anterior: solo hace falta si no instalas Qwen-Image-Edit.', False);
  if IncludeVideo then
    AddCatalogItem('video-ltxv', 'Video', 'LTX-Video (2B, destilado)',
      'El generador de video recomendado por defecto.', True);
  AddCatalogItem('voz-piper', 'Voz', 'Voz en espanol (Piper)',
    'Necesario para que Chati responda hablando.', True);
end;

function DetectGpuTier(out GpuName: String; out VramMib: Integer): String;
var
  ResultCode: Integer;
  TmpFile, Line: String;
  Lines: TArrayOfString;
  CommaPos: Integer;
begin
  Result := 'cpu_only';
  GpuName := '';
  VramMib := 0;
  TmpFile := ExpandConstant('{tmp}\chati_gpu.txt');
  if Exec(ExpandConstant('{cmd}'),
       '/C nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits > "' + TmpFile + '" 2>nul',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if (ResultCode = 0) and LoadStringsFromFile(TmpFile, Lines) and (GetArrayLength(Lines) > 0) then
    begin
      Line := Trim(Lines[0]);
      CommaPos := Pos(',', Line);
      if CommaPos > 0 then
      begin
        GpuName := Trim(Copy(Line, 1, CommaPos - 1));
        VramMib := StrToIntDef(Trim(Copy(Line, CommaPos + 1, Length(Line))), 0);
        if VramMib >= 14000 then Result := '16gb_plus'
        else if VramMib >= 10000 then Result := '12gb'
        else if VramMib >= 6000 then Result := '8gb'
        else if VramMib >= 4000 then Result := 'minimo'
        else Result := 'cpu_only';
      end;
    end;
  end;
end;

// Reinstalacion/actualizacion: si ya hay usuarios no se pide nada (y
// installer_create_admin.py tampoco crearia otro admin aunque se lo pasaran).
function UsersAlreadyExist: Boolean;
var
  DataRoot: String;
begin
  DataRoot := GetEnv('CHATI_DATA_ROOT');
  if DataRoot = '' then
    DataRoot := ExpandConstant('{localappdata}\ChatiIA');
  Result := FileExists(AddBackslash(DataRoot) + 'data\users.db');
end;

function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;

// al actualizar se respeta lo que se eligio en Opciones de la app
function AutostartArg(Param: String): String;
begin
  if IsUpdate then
    Result := 'igual'
  else if WizardIsTaskSelected('autostart') then
    Result := 'si'
  else
    Result := 'no';
end;

// Con Chati abierta no se pueden cambiar sus archivos (la ventana los
// bloquea) y el orquestador seguiria con el codigo viejo
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  ExtractTemporaryFile('stop_chati.ps1');
  Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\stop_chati.ps1') +
       '" -AiRoot "' + ExpandConstant('{app}') + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (AdminPage <> nil) and (PageID = AdminPage.ID) and UsersAlreadyExist;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Lines: TArrayOfString;
begin
  Result := True;
  if (AdminPage = nil) or (CurPageID <> AdminPage.ID) then
    Exit;
  if Trim(AdminPage.Values[0]) = '' then
  begin
    MsgBox('Escribe un nombre de usuario.', mbError, MB_OK);
    Result := False;
  end
  else if Length(AdminPage.Values[1]) < 8 then
  begin
    MsgBox('La contraseña debe tener al menos 8 caracteres.', mbError, MB_OK);
    Result := False;
  end
  else if AdminPage.Values[1] <> AdminPage.Values[2] then
  begin
    MsgBox('Las contraseñas no coinciden.', mbError, MB_OK);
    Result := False;
  end
  else if (Trim(AdminPage.Values[3]) <> '') <> (Trim(AdminPage.Values[4]) <> '') then
  begin
    MsgBox('Rellena la pregunta de seguridad y su respuesta, o deja las dos vacias.', mbError, MB_OK);
    Result := False;
  end
  else
  begin
    // se escribe aqui (no en CurStepChanged) para que ya exista cuando
    // [Run] lance installer_create_admin.py, que lo borra al leerlo
    SetArrayLength(Lines, 4);
    Lines[0] := Trim(AdminPage.Values[0]);
    Lines[1] := AdminPage.Values[1];
    Lines[2] := Trim(AdminPage.Values[3]);
    Lines[3] := Trim(AdminPage.Values[4]);
    SaveStringsToUTF8File(ExpandConstant('{tmp}\chati_admin.txt'), Lines, False);
  end;
end;

procedure InitializeWizard;
var
  Tier, GpuName: String;
  VramMib, I: Integer;
  InfoText: String;
begin
  if WizardForm.WelcomeLabel2 <> nil then
    WizardForm.WelcomeLabel2.Caption := WizardForm.WelcomeLabel2.Caption + #13#10#13#10 +
      CustomMessage('WelcomeCredit');

  AdminPage := CreateInputQueryPage(wpSelectDir, 'Administrador principal',
    'Crea la cuenta con la que vas a entrar en Chati IA.',
    'Esta cuenta podra gestionar modelos y otros usuarios. Tus conversaciones se cifran con tu contraseña: ' +
    'si la olvidas, solo la pregunta de seguridad permite recuperar el acceso (perdiendo lo cifrado).');
  AdminPage.Add('Usuario:', False);
  AdminPage.Add('Contraseña (minimo 8 caracteres):', True);
  AdminPage.Add('Repite la contraseña:', True);
  AdminPage.Add('Pregunta de seguridad (opcional):', False);
  AdminPage.Add('Respuesta:', False);

  Tier := DetectGpuTier(GpuName, VramMib);
  BuildCatalog(Tier <> 'cpu_only');

  ModelsPage := CreateCustomPage(AdminPage.ID, 'Modelos a descargar',
    'Elige que modelos quieres instalar - se pueden anadir mas despues desde Opciones > Modelos.');

  GpuInfoLabel := TNewStaticText.Create(ModelsPage);
  GpuInfoLabel.Parent := ModelsPage.Surface;
  GpuInfoLabel.Left := 0;
  GpuInfoLabel.Top := 0;
  GpuInfoLabel.Width := ModelsPage.SurfaceWidth;
  GpuInfoLabel.AutoSize := False;
  GpuInfoLabel.WordWrap := True;
  if GpuName <> '' then
    InfoText := 'GPU detectada: ' + GpuName + ' (' + IntToStr(VramMib) + ' MB) - recomendaciones marcadas abajo.'
  else
    InfoText := 'No se detecto una GPU NVIDIA - se recomienda saltar video (mas lento en CPU).';
  GpuInfoLabel.Caption := InfoText;

  ModelsList := TNewCheckListBox.Create(ModelsPage);
  ModelsList.Parent := ModelsPage.Surface;
  ModelsList.Left := 0;
  ModelsList.Top := GpuInfoLabel.Top + 32;
  ModelsList.Width := ModelsPage.SurfaceWidth;
  ModelsList.Height := ModelsPage.SurfaceHeight - 32;

  for I := 0 to GetArrayLength(Catalog) - 1 do
  begin
    ModelsList.AddCheckBox('[' + Catalog[I].Modality + '] ' + Catalog[I].Label_,
      Catalog[I].Description, 0, Catalog[I].Checked, True, False, True, nil);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Lines: TArrayOfString;
  I, Count: Integer;
  LogFile, DataRoot: String;
begin
  // Al terminar: si alguna descarga de modelos fallo, decirlo (antes fallaba
  // en silencio y el chat decia luego "model not found" - Windows Sandbox,
  // 2026-09-30). Lo escribe installer_download_models.py. Al actualizar no
  // se bajan modelos y el registro seria el de la instalacion anterior.
  if (CurStep = ssDone) and not IsUpdate then
  begin
    DataRoot := GetEnv('CHATI_DATA_ROOT');
    if DataRoot = '' then
      DataRoot := ExpandConstant('{localappdata}\ChatiIA');
    LogFile := AddBackslash(DataRoot) + 'data\logs\instalacion_modelos.log';
    if LoadStringsFromFile(LogFile, Lines) then
      for I := 0 to GetArrayLength(Lines) - 1 do
        if Pos('No se pudieron descargar', Lines[I]) > 0 then
        begin
          MsgBox('Chati IA se ha instalado, pero algunos modelos no se pudieron descargar.' + #13#10#13#10 +
                 Copy(Lines[I], Pos('No se pudieron', Lines[I]), Length(Lines[I])) + #13#10#13#10 +
                 'Comprueba la conexion a internet y vuelve a ejecutar el instalador: lo ya descargado no se repite.',
                 mbError, MB_OK);
          Break;
        end;
  end;
  // ssInstall y no ssPostInstall: las entradas de [Run] se ejecutan ANTES de
  // ssPostInstall (comprobado con un instalador de prueba), asi que el
  // descargador arrancaba sin este archivo y no bajaba nada (2026-10-01).
  if CurStep = ssInstall then
  begin
    Count := 0;
    SetArrayLength(Lines, GetArrayLength(Catalog));
    for I := 0 to GetArrayLength(Catalog) - 1 do
    begin
      if ModelsList.Checked[I] then
      begin
        Lines[Count] := Catalog[I].Id;
        Count := Count + 1;
      end;
    end;
    SetArrayLength(Lines, Count);
    SaveStringsToFile(ExpandConstant('{tmp}\chati_selected_models.txt'), Lines, False);
  end;
end;
