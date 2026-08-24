#!/bin/bash
# macOS launcher — double-click to build the BOLD local database.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT/dev/bold_db_creator.sh"
