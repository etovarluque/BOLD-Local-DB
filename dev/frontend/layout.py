"""Where everything lives in a BOLD-Local-DB folder, and the move from the
old layout to the current one.

    BOLD-Local-DB/
    ├── Start BOLD-Local-DB.bat / .command / .sh   the only thing to open
    ├── guide/
    ├── data/                    everything the user has: never touched by updates
    │   ├── raw/                 BOLD's .tar.gz goes here
    │   ├── processed/           Step 1's output
    │   ├── exports/             what the viewer exports
    │   ├── identify_index/
    │   ├── database/            bold_db.db
    │   └── state/               settings, logs, shortcuts made, package name
    ├── .program/                the program (dev/ in a clone of the repository)
    └── .runtime/                Python and its packages, made by the launcher;
                                 deleting it only costs a new download

Until 1.2.0 the viewer ran from a copy of dev/frontend in app/, which also
held the database, the logs and the window settings. migrate_legacy_layout()
moves those into data/ and drops the copy.

Shared by the Creator, the viewer and the launcher: standard library only.
"""

import filecmp
import os
import shutil
import sys

try:
    from db_contract import DB_FILENAME
except ImportError:          # imported from outside frontend/ (start.py)
    DB_FILENAME = "bold_db.db"

FRONTEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROGRAM_DIR  = os.path.dirname(FRONTEND_DIR)
ROOT         = os.path.dirname(PROGRAM_DIR)

DATA_DIR       = os.path.join(ROOT, "data")
RAW_DIR        = os.path.join(DATA_DIR, "raw")
PROCESSED_DIR  = os.path.join(DATA_DIR, "processed")
EXPORTS_DIR    = os.path.join(DATA_DIR, "exports")
DATABASE_DIR   = os.path.join(DATA_DIR, "database")
STATE_DIR      = os.path.join(DATA_DIR, "state")
LOGS_DIR       = os.path.join(STATE_DIR, "logs")
# An empty file named after the BOLD package the data comes from
# (BOLD_Public.09-Oct-2026.tsv): the viewer shows it in its header
PACKAGE_DIR    = os.path.join(STATE_DIR, "package")
UI_STATE_FILE  = os.path.join(STATE_DIR, "ui_state.ini")
SHORTCUTS_FILE = os.path.join(STATE_DIR, "shortcuts.json")
# The field selection made in the Creator. The program's fields_config.json
# is only the default: an update replaces the program folder.
USER_FIELDS_FILE    = os.path.join(STATE_DIR, "fields_config.json")
DEFAULT_FIELDS_FILE = os.path.join(PROGRAM_DIR, "fields_config.json")
GUIDE_DIR      = os.path.join(ROOT, "guide")
RUNTIME_DIR    = os.path.join(ROOT, ".runtime")

LEGACY_APP_DIR = os.path.join(ROOT, "app")

# Launchers at the root, one per OS
LAUNCHERS = {
    "win32":  "Start BOLD-Local-DB.bat",
    "darwin": "Start BOLD-Local-DB.command",
    "linux":  "Start BOLD-Local-DB.sh",
}


def launcher_path():
    return os.path.join(ROOT, LAUNCHERS.get(sys.platform, LAUNCHERS["linux"]))


def is_repository_clone():
    """A git clone (development) rather than a downloaded release: the
    program is updated with git, never replaced by the launcher."""
    return os.path.isdir(os.path.join(ROOT, ".git"))


def database_path():
    """The database, at data/database/, or still in app/ when the move
    couldn't be made (the file was open)."""
    new = os.path.join(DATABASE_DIR, DB_FILENAME)
    old = os.path.join(LEGACY_APP_DIR, DB_FILENAME)
    if not os.path.exists(new) and os.path.exists(old):
        return old
    return new


def fields_config_path():
    """The field selection to read: the user's, or the program's default."""
    return USER_FIELDS_FILE if os.path.exists(USER_FIELDS_FILE) else DEFAULT_FIELDS_FILE


def ensure_dirs():
    for d in (RAW_DIR, PROCESSED_DIR, EXPORTS_DIR, DATABASE_DIR, STATE_DIR, LOGS_DIR, PACKAGE_DIR):
        os.makedirs(d, exist_ok=True)


# ── From the old layout ───────────────────────────────────────────────────────

# Shortcuts the old "step 0" scripts left at the root: they point to scripts
# that no longer exist
_OLD_ROOT_FILES = (
    "1_Create_DB.lnk", "1_Create_DB.desktop", "1_Create_DB.command",
    "2_Open_web_viewer_BOLD_DB.lnk", "2_Open_web_viewer_BOLD_DB.desktop",
    "2_Open_web_viewer_BOLD_DB.command",
    "0_Generate_Shortcuts_Windows.bat", "0_Generate_Shortcuts_Linux.sh",
    "0_Generate_Shortcuts_macOS.command",
)
# What the viewer's copy in app/ was made of
_CODE_EXTS = (".py", ".pyc", ".pyo", ".html", ".png", ".ico", ".gif", ".css", ".js")


def _move(src, dst):
    """Moves a file unless the destination exists. Raises OSError on failure."""
    if os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)
    return True


def _only_code_left(folder):
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            if not name.lower().endswith(_CODE_EXTS):
                return False
    return True


def migrate_legacy_layout():
    """Moves the database and the state out of app/ and removes what the
    old layout leaves behind. Safe to call on every start.

    Returns (done, problems): lists of (key, detail) for the caller to word.
    A database that's open (a viewer of the old version still running) stays
    where it is: database_path() keeps finding it there, and the next start
    tries again.
    """
    done, problems = [], []
    app = LEGACY_APP_DIR

    if os.path.isdir(app):
        ensure_dirs()
        # The database, a half-built one ("<db>.building") and SQLite's side files
        db_files = sorted(n for n in os.listdir(app) if n.startswith(DB_FILENAME))
        try:
            moved = [n for n in db_files if _move(os.path.join(app, n), os.path.join(DATABASE_DIR, n))]
            if moved:
                done.append(("database", DATABASE_DIR))
        except OSError as e:
            problems.append(("database_in_use", str(e)))

        moves = [(os.path.join(app, "ui_state.ini"), UI_STATE_FILE)]
        logs = os.path.join(app, "logs")
        if os.path.isdir(logs):
            moves += [(os.path.join(logs, n), os.path.join(LOGS_DIR, n)) for n in os.listdir(logs)]
        static = os.path.join(app, "static")
        if os.path.isdir(static):
            moves += [(os.path.join(static, n), os.path.join(PACKAGE_DIR, n))
                      for n in os.listdir(static) if n.endswith(".tsv")]
        for src, dst in moves:
            if os.path.isfile(src):
                try:
                    if not _move(src, dst):
                        os.remove(src)          # an older copy of something already moved
                except OSError as e:
                    problems.append(("state_not_moved", f"{src}: {e}"))

        # What's left is the old copy of the viewer's code
        if _only_code_left(app):
            try:
                shutil.rmtree(app)
                done.append(("old_viewer_removed", app))
            except OSError as e:
                problems.append(("old_viewer_not_removed", str(e)))
        elif not any(key == "database_in_use" for key, _ in problems):
            problems.append(("app_not_empty", app))

    for name in _OLD_ROOT_FILES:
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                done.append(("old_shortcut_removed", name))
            except OSError:
                pass

    # A release unpacked over an older download: the old program in dev/
    old_prog = os.path.join(ROOT, "dev")
    if (os.path.basename(PROGRAM_DIR) != "dev" and not is_repository_clone()
            and os.path.isfile(os.path.join(old_prog, "bold_db_creator.py"))):
        old_fields = os.path.join(old_prog, "fields_config.json")
        try:
            if (os.path.isfile(old_fields) and not os.path.exists(USER_FIELDS_FILE)
                    and not filecmp.cmp(old_fields, DEFAULT_FIELDS_FILE, shallow=False)):
                ensure_dirs()
                shutil.copy2(old_fields, USER_FIELDS_FILE)
                done.append(("fields_kept", USER_FIELDS_FILE))
            shutil.rmtree(old_prog)
            done.append(("old_program_removed", old_prog))
        except OSError as e:
            problems.append(("old_program_not_removed", str(e)))

    return done, problems
