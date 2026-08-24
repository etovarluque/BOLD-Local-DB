#!/bin/bash
# Run this once after downloading or moving the project: it creates the
# shortcuts in linux_launchers/ for this location.
cd "$(dirname "$0")"
python3 dev/generate_shortcuts.py
read -p "Press Enter to close..."
