"""What the launcher at the project root runs ("Start BOLD-Local-DB").

    start.py            opens BOLD-Local-DB (build, update and the Home screen)
    start.py --viewer   opens the viewer in the browser

Before that it:
  - puts in place an update downloaded earlier (then asks the launcher to
    start again, so the new version is the one that runs: exit code 4);
  - moves a project of 1.2.0 or earlier to the current layout (layout.py);
  - remakes the desktop/menu shortcuts if the project was moved.

Exit codes the launcher acts on:
    0  done
    4  start again (an update was just put in place, or one was staged)
    other: failed. Right after an update, the launcher then restores the
    previous version (.program.old).
"""

import os
import subprocess
import sys
import time

PROGRAM_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROGRAM_DIR, "frontend"))
sys.path.insert(0, PROGRAM_DIR)

import layout     # noqa: E402
import shortcuts  # noqa: E402
import updater    # noqa: E402

RESTART = 4
# A version that ran this long (or closed normally) is taken to work
HEALTHY_AFTER_SECS = 15


def _hide_on_windows():
    """.program and .runtime are hidden on macOS and Linux by their dot;
    on Windows they get the hidden attribute."""
    if os.name != "nt":
        return
    import ctypes
    for path in (layout.PROGRAM_DIR, layout.RUNTIME_DIR):
        if os.path.isdir(path) and os.path.basename(path).startswith("."):
            try:
                attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
                if attrs != -1 and not attrs & 0x2:
                    ctypes.windll.kernel32.SetFileAttributesW(path, attrs | 0x2)
            except Exception:
                pass


def _ui_lang():
    try:
        with open(layout.UI_STATE_FILE, encoding="utf-8") as f:
            for line in f:
                key, _, val = line.partition("=")
                if key.strip() == "language" and val.strip() in ("es", "en"):
                    return val.strip()
    except OSError:
        pass
    return None


def main(argv):
    os.chdir(layout.ROOT)

    if updater.staged_version() and updater.self_update_supported():
        version = updater.apply_staged()
        if version:
            print(f"BOLD-Local-DB {version} installed. Starting it…")
            return RESTART

    done, problems = layout.migrate_legacy_layout()
    for key, detail in done + problems:
        print(f"[layout] {key}: {detail}")
    layout.ensure_dirs()
    _hide_on_windows()
    if shortcuts.repair(_ui_lang()):
        print("Shortcuts updated to this folder.")

    viewer = "--viewer" in argv
    if viewer and not os.path.exists(layout.database_path()):
        print("There is no database yet: opening BOLD-Local-DB to build it.")
        viewer = False

    if viewer:
        cmd = [sys.executable, os.path.join(layout.FRONTEND_DIR, "server.py"), "--open-browser"]
        cwd = layout.FRONTEND_DIR
    else:
        cmd = [sys.executable, os.path.join(PROGRAM_DIR, "bold_db_creator.py")]
        cwd = PROGRAM_DIR

    started = time.monotonic()
    try:
        code = subprocess.call(cmd, cwd=cwd)
    except KeyboardInterrupt:
        code = 0
    if code in (0, RESTART) or time.monotonic() - started >= HEALTHY_AFTER_SECS:
        updater.discard_old_program()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
