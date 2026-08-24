@echo off
cd /d "%~dp0..\app"
echo Starting the server...
start "" http://127.0.0.1:5001
echo The browser has been opened. The server will now run.
echo To stop the server, close this window or press Ctrl+C
python server.py


