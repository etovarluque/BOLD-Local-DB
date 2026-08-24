#!/bin/bash
# Launches the BOLD DB web viewer (Linux/macOS).
cd "$(dirname "$0")/../app"
echo "Starting the server..."
python3 server.py &
SERVER_PID=$!
sleep 1
if [[ "$OSTYPE" == "darwin"* ]]; then
    open http://127.0.0.1:5001
else
    xdg-open http://127.0.0.1:5001
fi
echo "The browser has been opened. The server will now run."
echo "To stop the server, close this window or press Ctrl+C"
wait "$SERVER_PID"
