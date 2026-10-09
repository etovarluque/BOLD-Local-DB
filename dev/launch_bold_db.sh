#!/bin/bash
# Launches the BOLD DB web viewer (Linux/macOS).
# server.py opens the browser itself once it's listening (--open-browser).
cd "$(dirname "$0")/../app"
echo "Starting the server..."
echo "The browser will open as soon as the server is ready."
echo "To stop the server, close this window or press Ctrl+C"
python3 server.py --open-browser
