@echo off
rem ============================================================
rem  repair_shortcuts.bat
rem  Regenerates the shortcuts (.lnk) to point to the current
rem  folder, regardless of the drive letter. Run ONCE after
rem  copying the folder to another computer or drive.
rem ============================================================
setlocal
set "BASE=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$base='%BASE%';" ^
  "$sh=New-Object -ComObject WScript.Shell;" ^
  "$s=$sh.CreateShortcut($base+'1_Create_DB.lnk'); $s.TargetPath=$base+'dev\bold_db_creator.bat'; $s.WorkingDirectory=$base+'dev'; $s.IconLocation=$env:SystemRoot+'\System32\SHELL32.dll,80'; $s.Save();" ^
  "$s=$sh.CreateShortcut($base+'2_Open_web_viewer_BOLD_DB.lnk'); $s.TargetPath=$base+'dev\launch_bold_db.bat'; $s.WorkingDirectory=$base+'dev'; $s.IconLocation=$env:SystemRoot+'\System32\SHELL32.dll,263'; $s.Save();"

echo.
echo Shortcuts repaired for this location:
echo   %BASE%
echo.
pause
endlocal
