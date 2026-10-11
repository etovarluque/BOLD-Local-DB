@echo off
rem BOLD-Local-DB launcher for Windows. Double-click it to open the program;
rem "--viewer" opens the viewer instead (the desktop shortcuts use both).
rem
rem The first time it downloads uv (from GitHub, checksum verified), which
rem installs a Python of its own and the packages the program needs into
rem .runtime\ (about 150 MB download, only once).
rem Nothing is installed on the system: deleting .runtime\ undoes it all.
rem
rem Written without ( ) blocks around paths: a folder such as
rem "Program Files (x86)" would close the block early.

setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
set "PROG=%ROOT%.program"
if not exist "%PROG%\start.py" set "PROG=%ROOT%dev"
set "RT=%ROOT%.runtime"
set "UV=%RT%\uv.exe"
set "UV_CACHE_DIR=%RT%\cache"
set "UV_PYTHON_INSTALL_DIR=%RT%\python"
set "UV_PROJECT_ENVIRONMENT=%RT%\venv"
set "UV_PYTHON_PREFERENCE=only-managed"

if exist "%UV%" goto run
echo.
echo  BOLD-Local-DB - first start
echo  Downloading Python and the components it needs (about 150 MB to download).
echo  This only happens once and takes a few minutes.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROG%\bootstrap\get_uv.ps1" -Dest "%RT%"

:run
if not exist "%UV%" goto system_python
"%UV%" run --project "%PROG%" --frozen python "%PROG%\start.py" %*
set "CODE=%ERRORLEVEL%"
goto after

:system_python
rem No uv (no internet the first time, or a blocked download): the Python
rem installed on this computer, if any
where python >nul 2>nul
if errorlevel 1 goto no_python
echo  Using the Python installed on this computer.
python "%PROG%\start.py" %*
set "CODE=%ERRORLEVEL%"
goto after

:no_python
echo.
echo  Could not download the components, and there is no Python on this
echo  computer. Check the internet connection and open this file again.
echo.
pause
exit /b 1

:after
if "%CODE%"=="4" goto run
if "%CODE%"=="0" goto done
if not exist "%PROG%.old\start.py" goto failed
rem The version just installed doesn't start: back to the previous one
echo.
echo  The new version failed to start. Restoring the previous one...
rmdir /s /q "%PROG%"
move "%PROG%.old" "%PROG%" >nul
echo  Done. Open BOLD-Local-DB again.
pause
goto done

:failed
echo.
echo  BOLD-Local-DB closed with an error (code %CODE%). The details are above
echo  and in data\state\logs\.
pause

:done
rem An update may bring a new version of this file: it can only be swapped in
rem now, as the last thing this window does
if exist "%~f0.new" move /y "%~f0.new" "%~f0" >nul & exit /b %CODE%
exit /b %CODE%
