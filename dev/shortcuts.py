"""Desktop and menu shortcuts: made, removed and kept pointing at this folder.

Two shortcuts, each wherever the user asked (desktop, applications menu):
  - BOLD-Local-DB         the launcher (build or update the database)
  - BOLD-Local-DB Viewer  the viewer straight away, in the browser

Every file made is recorded in data/state/shortcuts.json, so "Remove"
deletes exactly those and repair() can tell when the project has moved:
the shortcuts then point at the old folder and are made again.

All of them run the launcher at the project root (layout.launcher_path()),
never a file inside the program: an update replaces the program folder and
the shortcuts keep working.

    Windows  .lnk on the Desktop and in the Start menu (Programs)
    Linux    .desktop on the desktop and in ~/.local/share/applications
    macOS    a small .app in ~/Applications (Launchpad, Spotlight, the Dock)
             and a link to it on the Desktop
"""

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend"))
import layout  # noqa: E402

ICON_DIR = os.path.join(layout.PROGRAM_DIR, "icons")

# (id, name per language, launcher argument, icon base name)
ENTRIES = (
    ("launcher", {"es": "BOLD-Local-DB", "en": "BOLD-Local-DB"}, "", "create_db"),
    ("viewer", {"es": "Visor BOLD-Local-DB", "en": "BOLD-Local-DB Viewer"}, "--viewer", "web_viewer"),
)
COMMENTS = {
    "launcher": {"es": "Crear o actualizar la base de datos local de BOLD",
                 "en": "Build or update the local BOLD database"},
    "viewer": {"es": "Buscar y exportar registros de la base local de BOLD",
               "en": "Search and export records of the local BOLD database"},
}


# ── Record of what was made ───────────────────────────────────────────────────

def _read_record():
    try:
        with open(layout.SHORTCUTS_FILE, encoding="utf-8") as f:
            rec = json.load(f)
        if isinstance(rec, dict) and isinstance(rec.get("items"), list):
            return rec
    except (OSError, ValueError):
        pass
    return None


def _write_record(rec):
    os.makedirs(os.path.dirname(layout.SHORTCUTS_FILE), exist_ok=True)
    tmp = layout.SHORTCUTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2, ensure_ascii=False)
    os.replace(tmp, layout.SHORTCUTS_FILE)


def current():
    """{'desktop': bool, 'menu': bool, 'lang': str} as last made, or None."""
    rec = _read_record()
    if not rec or not rec["items"]:
        return None
    return {"desktop": bool(rec.get("desktop")), "menu": bool(rec.get("menu")),
            "lang": rec.get("lang", "en")}


# ── Per OS ────────────────────────────────────────────────────────────────────

def _run_powershell(script, env):
    full_env = dict(os.environ)
    full_env.update(env)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-Command", script], check=True, env=full_env,
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _windows_folder(name):
    """Desktop or Programs (Start menu), as Windows resolves them: a desktop
    moved into OneDrive is found there, not at %USERPROFILE%\\Desktop."""
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          f"[Environment]::GetFolderPath('{name}')"],
                         capture_output=True, text=True, check=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return out.stdout.strip()


def _make_windows(folder, name, arg, icon, comment):
    path = os.path.join(folder, name + ".lnk")
    # Values travel as environment variables: a path with an apostrophe
    # can't break the script
    _run_powershell(
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SC_PATH);"
        "$s.TargetPath = $env:SC_TARGET; $s.Arguments = $env:SC_ARGS;"
        "$s.WorkingDirectory = $env:SC_DIR; $s.IconLocation = $env:SC_ICON;"
        "$s.Description = $env:SC_DESC; $s.Save()",
        {"SC_PATH": path, "SC_TARGET": layout.launcher_path(), "SC_ARGS": arg,
         "SC_DIR": layout.ROOT, "SC_ICON": os.path.join(ICON_DIR, icon + ".ico"), "SC_DESC": comment})
    return [path]


def _linux_desktop_dir():
    try:
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, check=True)
        if out.stdout.strip() and os.path.isdir(out.stdout.strip()):
            return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    return os.path.expanduser("~/Desktop")


def _desktop_exec_arg(text):
    """A quoted argument for the Exec key of a .desktop file."""
    for ch in ("\\", '"', "`", "$"):
        text = text.replace(ch, "\\" + ch)
    return f'"{text}"'


def _make_linux(folder, file_id, name, arg, icon, comment):
    path = os.path.join(folder, file_id + ".desktop")
    exec_line = _desktop_exec_arg(layout.launcher_path()) + (f" {arg}" if arg else "")
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("[Desktop Entry]\nType=Application\n"
                f"Name={name}\nComment={comment}\n"
                f"Exec={exec_line}\nPath={layout.ROOT}\n"
                f"Icon={os.path.join(ICON_DIR, icon + '.png')}\n"
                "Terminal=true\nCategories=Science;Education;\n")
    os.chmod(path, 0o755)
    # GNOME asks to "Allow Launching" a desktop file it doesn't trust
    try:
        subprocess.run(["gio", "set", path, "metadata::trusted", "true"],
                       capture_output=True, check=False)
    except OSError:
        pass
    return [path]


def _make_macos_app(name, file_id, arg, icon):
    """~/Applications/<name>.app: opens the launcher in Terminal.

    Made here rather than downloaded, so it carries no quarantine flag and
    Gatekeeper doesn't stop it.
    """
    apps = os.path.expanduser("~/Applications")
    app = os.path.join(apps, name + ".app")
    if os.path.exists(app):
        shutil.rmtree(app)
    macos = os.path.join(app, "Contents", "MacOS")
    res = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(res)
    # Terminal runs a .command file; this one holds the launcher's path and argument
    run = os.path.join(res, "run.command")
    with open(run, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/bash\nexec " + shlex.quote(layout.launcher_path()) + (f" {arg}" if arg else "") + "\n")
    os.chmod(run, 0o755)
    exe = os.path.join(macos, "launch")
    with open(exe, "w", encoding="utf-8", newline="\n") as f:
        f.write('#!/bin/bash\nopen -a Terminal "$(dirname "$0")/../Resources/run.command"\n')
    os.chmod(exe, 0o755)
    plist = {"CFBundleName": name, "CFBundleDisplayName": name, "CFBundleExecutable": "launch",
             "CFBundleIdentifier": f"org.boldlocaldb.{file_id}", "CFBundlePackageType": "APPL",
             "CFBundleShortVersionString": "1.0", "LSMinimumSystemVersion": "10.13"}
    try:
        subprocess.run(["sips", "-s", "format", "icns", os.path.join(ICON_DIR, icon + ".png"),
                        "--out", os.path.join(res, "icon.icns")], capture_output=True, check=True)
        plist["CFBundleIconFile"] = "icon"
    except (OSError, subprocess.CalledProcessError):
        pass                                        # no icon: the app still works
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(plist, f)
    return app


# ── Public ────────────────────────────────────────────────────────────────────

def create(desktop=True, menu=True, lang="en"):
    """Makes the shortcuts asked for, after removing earlier ones. Returns the paths."""
    remove()
    made = []
    for file_id, names, arg, icon in ENTRIES:
        name = names.get(lang, names["en"])
        comment = COMMENTS[file_id].get(lang, COMMENTS[file_id]["en"])
        slug = "bold-local-db" + ("" if file_id == "launcher" else "-" + file_id)
        if sys.platform == "win32":
            if desktop:
                made += _make_windows(_windows_folder("Desktop"), name, arg, icon, comment)
            if menu:
                made += _make_windows(_windows_folder("Programs"), name, arg, icon, comment)
        elif sys.platform == "darwin":
            app = _make_macos_app(name, slug, arg, icon)
            if menu:
                made.append(app)
            if desktop:
                link = os.path.join(os.path.expanduser("~/Desktop"), name + ".app")
                os.makedirs(os.path.dirname(link), exist_ok=True)
                if os.path.lexists(link):
                    os.remove(link)
                os.symlink(app, link)
                made.append(link)
            if not menu:
                # The desktop link needs the app to point at; it's recorded so
                # that removing the shortcuts removes it too
                made.append(app)
        else:
            if desktop:
                made += _make_linux(_linux_desktop_dir(), slug, name, arg, icon, comment)
            if menu:
                made += _make_linux(os.path.expanduser("~/.local/share/applications"),
                                    slug, name, arg, icon, comment)
    _write_record({"root": layout.ROOT, "desktop": desktop, "menu": menu, "lang": lang, "items": made})
    return made


def remove():
    """Deletes the shortcuts made earlier (and only those). Returns the paths removed."""
    rec = _read_record()
    if not rec:
        return []
    removed = []
    for path in rec["items"]:
        try:
            if os.path.islink(path) or os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path) and path.endswith(".app"):
                shutil.rmtree(path)
            else:
                continue
            removed.append(path)
        except OSError:
            pass
    try:
        os.remove(layout.SHORTCUTS_FILE)
    except OSError:
        pass
    return removed


def repair(lang=None):
    """Makes the shortcuts again if the project was moved: they point at the
    old folder. Returns True if it did. Never raises.

    A shortcut the user deleted by hand isn't brought back."""
    rec = _read_record()
    if not rec or not rec["items"]:
        return False
    if os.path.normcase(os.path.abspath(rec.get("root", ""))) == os.path.normcase(layout.ROOT):
        return False
    try:
        create(rec.get("desktop", True), rec.get("menu", True), lang or rec.get("lang", "en"))
        return True
    except Exception:
        return False
