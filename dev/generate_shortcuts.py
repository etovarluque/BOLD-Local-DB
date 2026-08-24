#!/usr/bin/env python3
"""
Generates the double-click launch shortcuts for this project.

Run once after downloading or moving the project to a new location:

    python dev/generate_shortcuts.py

Replaces the old repair_shortcuts.bat / repair_shortcuts.sh: a shortcut's
target is an absolute path, so it has to be (re)written for wherever this
copy of the project actually lives.
"""

import os
import sys
import subprocess

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
ICON_DIR     = os.path.join(SCRIPT_DIR, "icons")

# One icon file per shortcut.
CREATE_DB_ICON_WIN   = os.path.join(ICON_DIR, "create_db.ico")
CREATE_DB_ICON_LINUX = os.path.join(ICON_DIR, "create_db.png")
WEB_VIEWER_ICON_WIN   = os.path.join(ICON_DIR, "web_viewer.ico")
WEB_VIEWER_ICON_LINUX = os.path.join(ICON_DIR, "web_viewer.png")


def _generate_windows():
    ps_script = f"""
$sh = New-Object -ComObject WScript.Shell

$s = $sh.CreateShortcut('{PROJECT_ROOT}\\1_Create_DB.lnk')
$s.TargetPath = '{PROJECT_ROOT}\\dev\\bold_db_creator.bat'
$s.WorkingDirectory = '{PROJECT_ROOT}\\dev'
$s.IconLocation = '{CREATE_DB_ICON_WIN}'
$s.Save()

$s = $sh.CreateShortcut('{PROJECT_ROOT}\\2_Open_web_viewer_BOLD_DB.lnk')
$s.TargetPath = '{PROJECT_ROOT}\\dev\\launch_bold_db.bat'
$s.WorkingDirectory = '{PROJECT_ROOT}\\dev'
$s.IconLocation = '{WEB_VIEWER_ICON_WIN}'
$s.Save()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        check=True,
    )
    print("Windows shortcuts created at the project root:")
    print(f"  {PROJECT_ROOT}\\1_Create_DB.lnk")
    print(f"  {PROJECT_ROOT}\\2_Open_web_viewer_BOLD_DB.lnk")


def _write_desktop_entry(path, name, comment, script, icon):
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        f.write(
            f"[Desktop Entry]\n"
            f"Type=Application\n"
            f"Name={name}\n"
            f"Comment={comment}\n"
            f"Exec=bash -c 'cd \"{PROJECT_ROOT}\" && {script}'\n"
            f"Icon={icon}\n"
            f"Terminal=true\n"
            f"Categories=Utility;\n"
        )
    os.chmod(path, 0o755)


def _generate_linux():
    out_dir = os.path.join(PROJECT_ROOT, "linux_launchers")
    os.makedirs(out_dir, exist_ok=True)

    _write_desktop_entry(
        os.path.join(out_dir, "1_Create_DB.desktop"),
        "1 - Create DB", "Build the BOLD local database",
        "./dev/bold_db_creator.sh", CREATE_DB_ICON_LINUX,
    )
    _write_desktop_entry(
        os.path.join(out_dir, "2_Open_web_viewer_BOLD_DB.desktop"),
        "2 - Open web viewer BOLD DB", "Open the BOLD DB web viewer",
        "./dev/launch_bold_db.sh", WEB_VIEWER_ICON_LINUX,
    )
    print("Linux shortcuts created in linux_launchers/.")
    print("If your file manager still won't launch them, right-click each")
    print('and choose "Allow Launching" / "Trust" (one-time security step')
    print("required by GNOME/KDE for downloaded launchers).")


def _generate_macos():
    out_dir = os.path.join(PROJECT_ROOT, "macos_launchers")
    os.makedirs(out_dir, exist_ok=True)
    # .command files resolve their own folder at run time (no absolute path
    # baked in), so there's nothing to regenerate - just make sure they're
    # present and executable. Custom Finder icons for a plain .command file
    # would require wrapping it as a real .app bundle, which is out of
    # scope here.
    for filename in ("1_Create_DB.command", "2_Open_web_viewer_BOLD_DB.command"):
        path = os.path.join(out_dir, filename)
        if os.path.exists(path):
            os.chmod(path, 0o755)
    print("macOS shortcuts in macos_launchers/ are self-locating - nothing")
    print("to regenerate. (They can't carry a custom Finder icon; see the")
    print("README for details.)")


def main():
    if sys.platform == "win32":
        _generate_windows()
    elif sys.platform == "darwin":
        _generate_macos()
    else:
        _generate_linux()


if __name__ == "__main__":
    main()
