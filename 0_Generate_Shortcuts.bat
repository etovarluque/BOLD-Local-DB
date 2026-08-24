@echo off
rem Run this once after downloading or moving the project: it creates the
rem 1_Create_DB.lnk and 2_Open_web_viewer_BOLD_DB.lnk shortcuts for this
rem location.
cd /d "%~dp0"
python dev\generate_shortcuts.py
pause
