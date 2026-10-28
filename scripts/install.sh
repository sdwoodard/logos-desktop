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

    # Validate parents before creating executable files, including when Python is absent.
    logos_desktop_safe_directory() {
        local directory=$1 owner mode
        [[ $directory == /* && $directory != *[$'\001'-$'\037'$'\177']* ]] || return 1
        [[ $(readlink -f -- "$directory") == "$directory" ]] || return 1
        while :; do
            [[ -d $directory && ! -L $directory ]] || return 1
            read -r owner mode < <(stat -c '%u %a' -- "$directory")
            [[ $owner == 0 || $owner == "$UID" ]] || return 1
            if (( (8#$mode & 0022) != 0 )); then
                [[ $owner == 0 ]] && (( (8#$mode & 01000) != 0 )) || return 1
            fi
            [[ $directory == / ]] && break
            directory=${directory%/*}
            directory=${directory:-/}
        done
    }

    local tool
    for tool in curl mktemp stat readlink tar gzip sha256sum; do
        if ! command -v "$tool" >/dev/null; then
            printf 'Install %s with your distribution package manager, then retry.\n' "$tool" >&2
            return 1
        fi
    done
    local temporary_parent=${TMPDIR:-/tmp}
    if ! logos_desktop_safe_directory "$temporary_parent"; then
        printf '%s\n' 'Choose a temporary directory without aliases or shared writable parents.' >&2
        return 1
    fi
    LOGOS_DESKTOP_INSTALL_TEMP=$(mktemp -d "$temporary_parent/logos-desktop-install.XXXXXXXX")
    LOGOS_DESKTOP_PYTHON_DIRECTORY=''
    LOGOS_DESKTOP_PYTHON_IN_USE=0
    logos_desktop_cleanup() {
        rm -rf -- "$LOGOS_DESKTOP_INSTALL_TEMP"
        if [[ $LOGOS_DESKTOP_PYTHON_IN_USE == 0 && -n $LOGOS_DESKTOP_PYTHON_DIRECTORY ]]; then
            rm -rf -- "$LOGOS_DESKTOP_PYTHON_DIRECTORY"
        fi
    }
    trap logos_desktop_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    local python='' candidate
    for candidate in "${PYTHON:-python3}" python3.12 python3.13 python3.14; do
        if command -v "$candidate" >/dev/null && "$candidate" -I -c '
import sys
if not (3, 12) <= sys.version_info < (3, 15):
    raise SystemExit(1)
import ensurepip
import venv
' 2>/dev/null; then
            python=$candidate
            break
        fi
    done
    if [[ -z $python ]]; then
        printf '%s\n' 'Preparing a private Python runtime for LogosDesktop (about 33 MB)…'
        local archive="$LOGOS_DESKTOP_INSTALL_TEMP/python.tar.gz"
        curl -fsSL --proto '=https' --proto-redir '=https' --max-time 300 --max-filesize 34505639 \
            'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz' \
            -o "$archive"
        printf '%s  %s\n' 'ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20' "$archive" | sha256sum -c -

        local parent
        for parent in "$HOME" "$HOME/.local" "$HOME/.local/lib"; do
            if [[ ! -e $parent && ! -L $parent ]]; then
                mkdir -m 700 -- "$parent"
            fi
            if ! logos_desktop_safe_directory "$parent"; then
                printf 'Unsafe runtime parent: %s\n' "$parent" >&2
                return 1
            fi
        done
        LOGOS_DESKTOP_PYTHON_DIRECTORY=$(mktemp -d "$HOME/.local/lib/logos-desktop-python.XXXXXXXX")
        tar -xzf "$archive" --strip-components=1 -C "$LOGOS_DESKTOP_PYTHON_DIRECTORY"
        python="$LOGOS_DESKTOP_PYTHON_DIRECTORY/bin/python3"
        "$python" -I -c 'import ssl, ensurepip, venv'
    fi
    curl -fsSL --proto '=https' --proto-redir '=https' --max-time 60 --max-filesize 262144 \
        https://github.com/sdwoodard/logos-desktop/releases/latest/download/install-wheel.py \
        -o "$LOGOS_DESKTOP_INSTALL_TEMP/install-wheel.py"
    if (( $(stat -c %s "$LOGOS_DESKTOP_INSTALL_TEMP/install-wheel.py") > 262144 )); then
        printf '%s\n' 'Controller bootstrap exceeded its size limit.' >&2
        return 1
    fi
    # Once the bootstrap starts, even a partial environment may retain this base interpreter.
    LOGOS_DESKTOP_PYTHON_IN_USE=1
    "$python" -I "$LOGOS_DESKTOP_INSTALL_TEMP/install-wheel.py" "${options[@]}"
}

logos_desktop_install "$@"
