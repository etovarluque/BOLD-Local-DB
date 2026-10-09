@echo off
cd /d "%~dp0..\app"
echo Starting the server...
echo The browser will open as soon as the server is ready.
echo To stop the server, close this window or press Ctrl+C
python server.py --open-browser
