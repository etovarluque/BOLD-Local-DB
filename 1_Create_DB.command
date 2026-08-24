#!/bin/bash
# macOS launcher — double-click to build the BOLD local database.
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/dev/bold_db_creator.sh"
