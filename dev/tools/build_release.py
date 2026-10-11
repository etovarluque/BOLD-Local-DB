#!/usr/bin/env python3
"""Builds the three downloads of a release: BOLD-Local-DB-windows.zip,
-macos.zip and -linux.zip, in what a user sees after unpacking:

    BOLD-Local-DB/
    ├── Start BOLD-Local-DB.bat | .command | .sh   (only this OS's)
    ├── guide/
    └── .program/       dev/ of this repository, plus LICENSE and CHANGELOG.md

The launcher, the program's updater (dev/updater.py) and the GitHub Action
(.github/workflows/release.yml) all rely on these names.

    python dev/tools/build_release.py --version 1.3.0 --out dist
    python dev/tools/build_release.py --notes 1.3.0      # that version's CHANGELOG section

--version must match db_contract.APP_VERSION and dev/pyproject.toml.
"""

import argparse
import os
import re
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEV = os.path.join(REPO, "dev")
TOP = "BOLD-Local-DB"

LAUNCHERS = {
    "windows": "Start BOLD-Local-DB.bat",
    "macos": "Start BOLD-Local-DB.command",
    "linux": "Start BOLD-Local-DB.sh",
}
# Inside dev/, not shipped
SKIP_DIRS = {"__pycache__", "screenshots", "tools", "data", ".venv"}
SKIP_EXTS = (".pyc", ".pyo", ".log", ".db")


def _versions():
    with open(os.path.join(DEV, "frontend", "db_contract.py"), encoding="utf-8") as f:
        app = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', f.read()).group(1)
    with open(os.path.join(DEV, "pyproject.toml"), encoding="utf-8") as f:
        proj = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M).group(1)
    return app, proj


def _add(zf, src, arc, executable=False):
    info = zipfile.ZipInfo.from_file(src, arc)
    info.compress_type = zipfile.ZIP_DEFLATED
    # Unix permissions in the high bits: Finder and unzip keep the execute bit
    info.external_attr = ((0o100755 if executable else 0o100644) & 0xFFFF) << 16
    with open(src, "rb") as f:
        zf.writestr(info, f.read())


def _program_files():
    for dirpath, dirnames, filenames in os.walk(DEV):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(SKIP_EXTS):
                continue
            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, DEV).replace(os.sep, "/")
            yield src, rel


def build(version, out):
    app, proj = _versions()
    if not (version == app == proj):
        sys.exit(f"Version mismatch: tag {version}, db_contract.APP_VERSION {app}, pyproject {proj}")
    os.makedirs(out, exist_ok=True)
    made = []
    for osname, launcher in LAUNCHERS.items():
        path = os.path.join(out, f"BOLD-Local-DB-{osname}.zip")
        with zipfile.ZipFile(path, "w") as zf:
            _add(zf, os.path.join(REPO, launcher), f"{TOP}/{launcher}", executable=not launcher.endswith(".bat"))
            for name in sorted(os.listdir(os.path.join(REPO, "guide"))):
                _add(zf, os.path.join(REPO, "guide", name), f"{TOP}/guide/{name}")
            for src, rel in _program_files():
                _add(zf, src, f"{TOP}/.program/{rel}", executable=rel.endswith((".sh", ".command")))
            for name in ("LICENSE", "CHANGELOG.md"):
                _add(zf, os.path.join(REPO, name), f"{TOP}/.program/{name}")
        made.append(path)
        print(f"{path}  ({os.path.getsize(path) / 1e6:.1f} MB)")
    return made


def notes(version):
    """The CHANGELOG section of that version, for the release page."""
    with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as f:
        text = f.read()
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    body = m.group(1).strip() if m else ""
    return (body + "\n\n" if body else "") + (
        "**Download** the zip for your system, unpack it and open "
        "*Start BOLD-Local-DB*. To update an existing copy, use *Update* on the "
        "program's Home screen.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--version")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--notes", metavar="VERSION")
    args = ap.parse_args()
    if args.notes:
        print(notes(args.notes))
    elif args.version:
        build(args.version, args.out)
    else:
        ap.error("--version or --notes")
