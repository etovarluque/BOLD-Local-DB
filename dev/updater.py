"""Program updates from GitHub Releases.

Each release carries one zip per OS (BOLD-Local-DB-windows.zip, -macos.zip,
-linux.zip, made by tools/build_release.py). Updating replaces the program
folder (.program/), the guides and the launcher at the root; data/ is never
touched.

    1. check:   latest_release() asks GitHub for the newest version.
    2. stage:   download_and_stage() downloads its zip and unpacks it into
                .runtime/update/, then marks it READY. Nothing changes yet.
    3. apply:   on the next start, start.py calls apply_staged(): the current
                program becomes .program.old and the new one takes its place.
    4. keep:    when the new version has run fine, start.py deletes
                .program.old. If it fails straight away, the launcher puts
                .program.old back.

Only in a downloaded release: a clone of the repository is updated with git.
Standard library only.
"""

import json
import os
import re
import shutil
import stat
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend"))
import layout  # noqa: E402

REPO = "etovarluque/BOLD-Local-DB"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"

STAGE_DIR = os.path.join(layout.RUNTIME_DIR, "update")
READY_FILE = os.path.join(STAGE_DIR, "READY")
OLD_PROGRAM = layout.PROGRAM_DIR + ".old"


def platform_name():
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def asset_name():
    return f"BOLD-Local-DB-{platform_name()}.zip"


def parse_version(text):
    """'v1.10.2' → (1, 10, 2). Unparseable → (0,)."""
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) or (0,)


def is_newer(candidate, current):
    return parse_version(candidate) > parse_version(current)


def self_update_supported():
    """The launcher can replace the program only in a downloaded release."""
    return (os.path.basename(layout.PROGRAM_DIR) == ".program"
            and not layout.is_repository_clone())


def _get(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": "BOLD-Local-DB-updater", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest_release(timeout=6):
    """{'version', 'page', 'asset_url', 'size'} of the newest release, or None
    (no connection, no release, no zip for this OS). Never raises."""
    try:
        with _get(API_LATEST, timeout) as r:
            data = json.load(r)
        asset = next((a for a in data.get("assets", []) if a.get("name") == asset_name()), None)
        return {
            "version": (data.get("tag_name") or "").lstrip("vV"),
            "page": data.get("html_url") or RELEASES_PAGE,
            "asset_url": asset["browser_download_url"] if asset else None,
            "size": asset.get("size", 0) if asset else 0,
        }
    except Exception:
        return None


# ── Stage ─────────────────────────────────────────────────────────────────────

class Stopped(Exception):
    pass


def _find_release_root(folder):
    """The folder of the unpacked zip that holds .program/start.py."""
    for dirpath, dirnames, _files in os.walk(folder):
        if os.path.isfile(os.path.join(dirpath, ".program", "start.py")):
            return dirpath
        if dirpath.count(os.sep) - folder.count(os.sep) >= 2:
            dirnames[:] = []
    return None


def _extract(zip_path, dest):
    """Unpacks keeping the execute bit of the launchers, refusing any entry
    that would land outside dest."""
    dest_real = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = os.path.realpath(os.path.join(dest, info.filename))
            if not (target == dest_real or target.startswith(dest_real + os.sep)):
                raise ValueError(f"unsafe entry in the update: {info.filename}")
            zf.extract(info, dest)
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir() and os.name != "nt":
                os.chmod(target, mode)


def download_and_stage(release, progress=None, should_stop=None):
    """Downloads the release's zip and unpacks it, ready for the next start.

    progress(done_bytes, total_bytes) is called as it downloads; should_stop()
    returning True cancels (raises Stopped). Raises on any failure, leaving
    no half-staged update behind.
    """
    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    os.makedirs(STAGE_DIR)
    zip_path = os.path.join(STAGE_DIR, "download.zip")
    try:
        with _get(release["asset_url"], 30) as r, open(zip_path, "wb") as out:
            total = int(r.headers.get("Content-Length") or release.get("size") or 0)
            done = 0
            while True:
                if should_stop and should_stop():
                    raise Stopped()
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        unpacked = os.path.join(STAGE_DIR, "new")
        _extract(zip_path, unpacked)
        os.remove(zip_path)
        root = _find_release_root(unpacked)
        if not root:
            raise ValueError("the update doesn't contain the program")
        with open(READY_FILE, "w", encoding="utf-8") as f:
            json.dump({"version": release["version"], "root": root}, f)
    except BaseException:
        shutil.rmtree(STAGE_DIR, ignore_errors=True)
        raise


def staged_version():
    try:
        with open(READY_FILE, encoding="utf-8") as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


# ── Apply ─────────────────────────────────────────────────────────────────────

def _replace_tree(src, dst):
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.move(src, dst)


def apply_staged():
    """Puts the staged program in place. Returns the version applied, or
    None (nothing staged, or it couldn't be done: the current program is
    then left as it was). Never raises."""
    try:
        with open(READY_FILE, encoding="utf-8") as f:
            info = json.load(f)
        new_root = info["root"]
        new_prog = os.path.join(new_root, ".program")
        if not os.path.isfile(os.path.join(new_prog, "start.py")):
            raise ValueError("incomplete")
    except (OSError, ValueError, KeyError):
        shutil.rmtree(STAGE_DIR, ignore_errors=True)
        return None

    prog = layout.PROGRAM_DIR
    try:
        if os.path.exists(OLD_PROGRAM):
            shutil.rmtree(OLD_PROGRAM)
        os.replace(prog, OLD_PROGRAM)
    except OSError:
        return None                         # left staged: tried again next start
    try:
        shutil.move(new_prog, prog)
    except OSError:
        # Back to the version that was running
        shutil.rmtree(prog, ignore_errors=True)
        os.replace(OLD_PROGRAM, prog)
        return None

    # The guides and the launcher at the root: not critical
    try:
        new_guide = os.path.join(new_root, "guide")
        if os.path.isdir(new_guide):
            _replace_tree(new_guide, layout.GUIDE_DIR)
    except OSError:
        pass
    for name in os.listdir(new_root):
        if not name.startswith("Start BOLD-Local-DB"):
            continue
        src, dst = os.path.join(new_root, name), os.path.join(layout.ROOT, name)
        try:
            if name.endswith(".bat") and os.path.exists(dst):
                # cmd.exe reads a running .bat as it goes: the launcher swaps
                # it in itself, as its last action
                shutil.copyfile(src, dst + ".new")
            else:
                shutil.copyfile(src, dst)
                if os.name != "nt":
                    os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass
    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    return info.get("version")


def discard_old_program():
    """The new version runs: the previous one is no longer needed."""
    shutil.rmtree(OLD_PROGRAM, ignore_errors=True)
