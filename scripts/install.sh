#!/usr/bin/env bash
# Install the managed controller without changing system Python or installing Logos itself.

logos_desktop_install() {
    set -euo pipefail

    case "${1:-}" in
        -h|--help)
            printf '%s\n' 'Usage: install.sh [--root /absolute/home/path] [--no-path]' \
                'Installs LogosDesktop, its command and application-menu entry for your account.'
            return
            ;;
    esac
    if [[ $(id -u) == 0 ]]; then
        printf '%s\n' 'Run this installer as your ordinary user, without sudo.' >&2
        return 1
    fi
    if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
        printf '%s\n' 'LogosDesktop requires Linux x86_64.' >&2
        return 1
    fi

    local python=${PYTHON:-python3}
    if ! command -v "$python" >/dev/null || ! "$python" -I -c '
import sys
if sys.version_info < (3, 12):
    raise SystemExit(1)
import ensurepip
import venv
'; then
        printf '%s\n' 'Install Python 3.12 or newer with venv support, then run this command again.' \
            'Some distributions provide venv in the python3-venv package.' >&2
        return 1
    fi
    local curl_version
    curl_version=$(curl --version)
    if [[ ! $curl_version =~ ^curl[[:space:]]([0-9]+)\.([0-9]+) ]] || \
        (( BASH_REMATCH[1] < 8 || (BASH_REMATCH[1] == 8 && BASH_REMATCH[2] < 4) )); then
        printf '%s\n' 'Install curl 8.4 or newer, then run this command again.' >&2
        return 1
    fi

    local -a options=(--latest)
    while (($#)); do
        case "$1" in
            --root)
                if (($# < 2)); then
                    printf '%s\n' '--root requires a path.' >&2
                    return 1
                fi
                options+=(--root "$2")
                shift 2
                ;;
            --no-path)
                options+=(--no-path)
                shift
                ;;
            *)
                printf 'Unknown option: %s\n' "$1" >&2
                return 1
                ;;
        esac
    done

    LOGOS_DESKTOP_INSTALL_TEMP=$("$python" -I -c '
import os
from pathlib import Path
import stat
import tempfile

directory = Path(tempfile.gettempdir())
if directory != directory.resolve() or any(ord(char) < 32 for char in str(directory)):
    raise SystemExit("Choose a temporary directory without aliases or control characters.")
for parent in (directory, *directory.parents):
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
            or (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
        raise SystemExit("Choose an owned temporary directory or a root-owned sticky temporary directory.")
print(tempfile.mkdtemp(prefix="logos-desktop-install.", dir=directory))
')
    trap 'rm -rf -- "$LOGOS_DESKTOP_INSTALL_TEMP"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    curl -fsSL --proto '=https' --proto-redir '=https' --max-time 60 --max-filesize 262144 \
        https://github.com/sdwoodard/logos-desktop/releases/latest/download/install-wheel.py \
        -o "$LOGOS_DESKTOP_INSTALL_TEMP/install-wheel.py"
    "$python" -I "$LOGOS_DESKTOP_INSTALL_TEMP/install-wheel.py" "${options[@]}"
}

logos_desktop_install "$@"
