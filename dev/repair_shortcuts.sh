#!/bin/bash
# repair_shortcuts.sh
# Regenerates the Linux .desktop launchers to point at the current folder,
# regardless of where it was copied to. Run ONCE after copying this project
# to another computer or location.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"

cat > "$DIR/1_Create_DB.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=1 - Create DB
Comment=Build the BOLD local database
Exec=bash -c 'cd "$DIR" && ./dev/bold_db_creator.sh'
Icon=utilities-terminal
Terminal=true
Categories=Utility;
EOF
chmod +x "$DIR/1_Create_DB.desktop"

cat > "$DIR/2_Open_web_viewer_BOLD_DB.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=2 - Open web viewer BOLD DB
Comment=Open the BOLD DB web viewer
Exec=bash -c 'cd "$DIR" && ./dev/launch_bold_db.sh'
Icon=web-browser
Terminal=true
Categories=Utility;
EOF
chmod +x "$DIR/2_Open_web_viewer_BOLD_DB.desktop"

echo "Shortcuts repaired for this location:"
echo "  $DIR"
echo
echo "If your file manager still won't launch them, right-click each"
echo ".desktop file and choose \"Allow Launching\" / \"Trust\" (one-time"
echo "security step required by GNOME/KDE for downloaded launchers)."
