#!/usr/bin/env bash
# BOLD-Local-DB launcher for macOS: double-click it in Finder.
# The first time, macOS may refuse to open it because it was downloaded:
# right-click it, choose "Open", then "Open" again. Only once.
#
# It runs the same steps as "Start BOLD-Local-DB.sh"; it's a separate file
# only because Finder opens .command files in Terminal. In a release for
# macOS only this file is included, so it carries the steps itself.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1
PROG="$ROOT/.program"
[ -f "$PROG/start.py" ] || PROG="$ROOT/dev"
RT="$ROOT/.runtime"
UV="$RT/uv"
export UV_CACHE_DIR="$RT/cache"
export UV_PYTHON_INSTALL_DIR="$RT/python"
export UV_PROJECT_ENVIRONMENT="$RT/venv"
export UV_PYTHON_PREFERENCE=only-managed

pause_on_error() {
    echo
    read -r -p "Press Enter to close this window. " _
}

if [ ! -x "$UV" ]; then
    echo
    echo " BOLD-Local-DB - first start"
    echo " Downloading Python and the components it needs (about 150 MB to download)."
    echo " This only happens once and takes a few minutes."
    echo
    bash "$PROG/bootstrap/get_uv.sh" "$RT"
fi

while true; do
    if [ -x "$UV" ]; then
        "$UV" run --project "$PROG" --frozen python "$PROG/start.py" "$@"
        CODE=$?
    else
        PY="$(command -v python3 || command -v python)"
        if [ -z "$PY" ]; then
            echo
            echo " Could not download the components, and there is no Python on this"
            echo " Mac. Check the internet connection and open this file again."
            pause_on_error
            exit 1
        fi
        echo " Using the Python installed on this Mac."
        "$PY" "$PROG/start.py" "$@"
        CODE=$?
    fi
    [ "$CODE" -eq 4 ] || break
done

if [ "$CODE" -ne 0 ]; then
    if [ -f "$PROG.old/start.py" ]; then
        echo
        echo " The new version failed to start. Restoring the previous one..."
        rm -rf "$PROG" && mv "$PROG.old" "$PROG"
        echo " Done. Open BOLD-Local-DB again."
    else
        echo
        echo " BOLD-Local-DB closed with an error (code $CODE). The details are above"
        echo " and in data/state/logs/."
    fi
    pause_on_error
fi
exit "$CODE"
