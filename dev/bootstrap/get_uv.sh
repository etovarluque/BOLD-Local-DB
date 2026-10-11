#!/usr/bin/env bash
# Downloads uv (https://github.com/astral-sh/uv) into the given folder, for
# the macOS and Linux launchers. The version is fixed and the file is checked
# against the SHA-256 published with it.
#   get_uv.sh <dest> [version]
set -euo pipefail
DEST="$1"
VERSION="${2:-0.12.20}"

case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)               TARGET=aarch64-apple-darwin ;;
    Darwin-x86_64)              TARGET=x86_64-apple-darwin ;;
    Linux-x86_64)               TARGET=x86_64-unknown-linux-gnu ;;
    Linux-aarch64|Linux-arm64)  TARGET=aarch64-unknown-linux-gnu ;;
    *) echo "This system ($(uname -s) $(uname -m)) isn't supported by uv's downloads." >&2; exit 1 ;;
esac
NAME="uv-$TARGET.tar.gz"
URL="https://github.com/astral-sh/uv/releases/download/$VERSION/$NAME"
mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fetch() {
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
    else wget -q "$1" -O "$2"; fi
}
fetch "$URL" "$TMP/$NAME"
fetch "$URL.sha256" "$TMP/$NAME.sha256"
EXPECTED="$(awk '{print $1}' "$TMP/$NAME.sha256")"
if command -v sha256sum >/dev/null 2>&1; then ACTUAL="$(sha256sum "$TMP/$NAME" | awk '{print $1}')"
else ACTUAL="$(shasum -a 256 "$TMP/$NAME" | awk '{print $1}')"; fi
if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "The uv download doesn't match its checksum." >&2
    exit 1
fi
tar -xzf "$TMP/$NAME" -C "$DEST" --strip-components 1
chmod +x "$DEST/uv"
