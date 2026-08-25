#!/bin/bash
# macOS launcher — double-click to open the BOLD DB web viewer.
# Template: generate_shortcuts.py copies this to the project root, where
# dirname resolves to ROOT itself. Running it in place (from dev/macos_shortcuts/)
# won't work.
ROOT="$(cd "$(dirname "$0")" && pwd)"
"$ROOT/dev/launch_bold_db.sh"
