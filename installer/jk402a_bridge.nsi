Unicode True
!include "MUI2.nsh"
!include "LogicLib.nsh"

!define APP_NAME "JK-402A BLE Bridge"
!define APP_VERSION "2026.11.25.2"
!define APP_PUBLISHER "JK-402A Bridge"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\JK402ABleBridge"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "..\dist\JK402A-BLE-Bridge-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\JK402A-BLE-Bridge"
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
RequestExecutionLevel user

!define MUI_ABORTWARNING
!define MUI_ICON "..\assets\logo.ico"
!define MUI_UNICON "..\assets\logo.ico"

!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp
!define MUI_FINISHPAGE_RUN_TEXT "Iniciar ${APP_NAME} agora (bandeja do Windows)"

Function LaunchApp
  Exec '"$INSTDIR\.venv\Scripts\pythonw.exe" "$INSTDIR\jk402a_tray.pyw"'
FunctionEnd

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "PortugueseBR"

Section "Aplicacao Principal + Ambiente Python" SecMain
  SectionIn RO

  ; Encerra instancias anteriores antes de copiar os arquivos
  IfFileExists "$INSTDIR\scripts\stop_bridge.ps1" 0 +2
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\scripts\stop_bridge.ps1"'

  SetOutPath "$INSTDIR"
  File "..\scan_jk.py"
  File "..\jk402a_bridge.py"
  File "..\jk402a_bridge.pyw"
  File "..\jk402a_tray.pyw"
  File "..\requirements.txt"
  File "..\README.md"
  File "..\LICENSE"

  SetOutPath "$INSTDIR\src"
  File "..\src\__init__.py"
  File "..\src\config.py"
  File "..\src\ble_utils.py"
  File "..\src\scan_jk.py"
  File "..\src\bridge.py"
  File "..\src\tray_app.py"

  SetOutPath "$INSTDIR\assets"
  File "..\assets\logo.png"
  File "..\assets\logo.ico"

  SetOutPath "$INSTDIR\scripts"
  File "..\scripts\start_background.ps1"
  File "..\scripts\stop_bridge.ps1"
  File "..\scripts\install_startup.ps1"

  SetOutPath "$INSTDIR\installer"
  File "setup_env.ps1"

  SetOutPath "$INSTDIR"
  DetailPrint "Configurando Python e dependencias (pode baixar o Python se necessario)..."
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\installer\setup_env.ps1" -InstallDir "$INSTDIR"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Falha ao configurar o ambiente Python (codigo $0). Verifique sua conexao com a internet e tente novamente."
    Abort
  ${EndIf}

  ; Atalhos no Menu Iniciar
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\.venv\Scripts\pythonw.exe" '"$INSTDIR\jk402a_tray.pyw"' "$INSTDIR\assets\logo.ico" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\Desinstalar.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\assets\logo.ico" 0

  ; Registro do Desinstalador
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\assets\logo.ico"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Iniciar automaticamente com o Windows" SecStartup
  CreateShortCut "$SMSTARTUP\JK402A-BLE-Bridge.lnk" "$INSTDIR\.venv\Scripts\pythonw.exe" '"$INSTDIR\jk402a_tray.pyw" --minimized' "$INSTDIR\assets\logo.ico" 0 SW_SHOWMINIMIZED
SectionEnd

Section "Atalho na Area de Trabalho" SecDesktop
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\.venv\Scripts\pythonw.exe" '"$INSTDIR\jk402a_tray.pyw"' "$INSTDIR\assets\logo.ico" 0
SectionEnd

LangString DESC_SecMain ${LANG_PORTUGUESEBR} "Instala o bridge JK-402A, o painel na bandeja do Windows e configura o Python + bibliotecas."
LangString DESC_SecStartup ${LANG_PORTUGUESEBR} "Inicia o bridge minimizado na bandeja ao ligar o Windows."
LangString DESC_SecDesktop ${LANG_PORTUGUESEBR} "Cria um atalho na Area de Trabalho."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} $(DESC_SecMain)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStartup} $(DESC_SecStartup)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} $(DESC_SecDesktop)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  IfFileExists "$INSTDIR\scripts\stop_bridge.ps1" 0 +2
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\scripts\stop_bridge.ps1"'

  Delete "$SMSTARTUP\JK402A-BLE-Bridge.lnk"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"

  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
SectionEnd
