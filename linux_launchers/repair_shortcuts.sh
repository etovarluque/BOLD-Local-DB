#!/bin/bash
# repair_shortcuts.sh
# Regenerates the Linux .desktop launchers (in this same folder) to point
# at the current project location. Run ONCE after copying/moving the
# project to another computer or location.
set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SELF_DIR/.." && pwd)"

cat > "$SELF_DIR/1_Create_DB.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=1 - Create DB
Comment=Build the BOLD local database
Exec=bash -c 'cd "$PROJECT_ROOT" && ./dev/bold_db_creator.sh'
Icon=utilities-terminal
Terminal=true
Categories=Utility;
EOF
chmod +x "$SELF_DIR/1_Create_DB.desktop"

cat > "$SELF_DIR/2_Open_web_viewer_BOLD_DB.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=2 - Open web viewer BOLD DB
Comment=Open the BOLD DB web viewer
Exec=bash -c 'cd "$PROJECT_ROOT" && ./dev/launch_bold_db.sh'
Icon=web-browser
Terminal=true
Categories=Utility;
EOF
chmod +x "$SELF_DIR/2_Open_web_viewer_BOLD_DB.desktop"

echo "Shortcuts repaired for this location:"
echo "  $SELF_DIR"
echo
echo "If your file manager still won't launch them, right-click each"
echo ".desktop file and choose \"Allow Launching\" / \"Trust\" (one-time"
echo "security step required by GNOME/KDE for downloaded launchers)."
