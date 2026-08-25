#!/bin/bash
# Run this once after downloading or moving the project: it creates the
# 1_Create_DB.command and 2_Open_web_viewer_BOLD_DB.command shortcuts at
# the project root for this location.
cd "$(dirname "$0")"
python3 dev/generate_shortcuts.py
read -p "Press Enter to close..."
