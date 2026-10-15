; NDS Synth's Windows installer, for NSIS 3 (make_installer.sh builds it): installs NDSSynth.vst3 into the common
; VST3 folder, with an uninstaller in Add or Remove Programs.
Unicode true
!include "MUI2.nsh"
!include "x64.nsh"

!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\NDSSynth"

Name "NDS Synth ${VERSION}"
OutFile "NDSSynth-${VERSION}-Setup.exe"
RequestExecutionLevel admin
InstallDir "$COMMONFILES64\VST3"
BrandingText "NDS Synth - DS sound driver VST3"

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "This installs NDS Synth ${VERSION}, a VST3 instrument that plays the DS sound banks of Pokémon Black 2 and White 2 (exported as DLS files) as the game's sound driver does.$\r$\n$\r$\nClose your DAW before continuing."
!define MUI_DIRECTORYPAGE_TEXT_TOP "NDS Synth goes into your VST3 plugin folder. Most DAWs, FL Studio included, look in the default one."
!define MUI_FINISHPAGE_TEXT "NDS Synth is installed. In FL Studio, open Options > Manage plugins and click Find installed plugins, then add NDS Synth to a channel."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "NDS Synth needs 64-bit Windows."
    Abort
  ${EndIf}
  SetRegView 64
FunctionEnd

Section "NDS Synth"
  SetOutPath "$INSTDIR"
  File /r "NDSSynth.vst3"
  CreateDirectory "$PROGRAMFILES64\NDS Synth"
  WriteUninstaller "$PROGRAMFILES64\NDS Synth\Uninstall.exe"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayName" "NDS Synth"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "Publisher" "pokebw2"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR\NDSSynth.vst3"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "UninstallString" '"$PROGRAMFILES64\NDS Synth\Uninstall.exe"'
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Function un.onInit
  SetRegView 64
FunctionEnd

Section "Uninstall"
  ReadRegStr $0 HKLM "${UNINSTALL_KEY}" "InstallLocation"
  StrCmp $0 "" +2
  RMDir /r "$0"
  DeleteRegKey HKLM "${UNINSTALL_KEY}"
  Delete "$PROGRAMFILES64\NDS Synth\Uninstall.exe"
  RMDir "$PROGRAMFILES64\NDS Synth"
SectionEnd
