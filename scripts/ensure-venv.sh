#!/usr/bin/env bash
set -euo pipefail

script=$(realpath -- "${BASH_SOURCE[0]}")
root=$(dirname -- "$(dirname -- "$script")")
venv="$root/.venv"
python=${PYTHON:-python3}

if [[ ! -f "$root/pyproject.toml" || ! -f "$root/logos_desktop/main.py" ]]
then
    printf '%s\n' 'Cannot locate the LogosDesktop source beside this script.' >&2
    exit 1
fi
if [[ -e "$venv" || -L "$venv" ]]
then
    printf 'Preserving existing environment: %s\n' "$venv" >&2
    exit 1
fi
if ! "$python" -c $'import sys\nsys.exit(0 if sys.version_info >= (3, 12) else 1)'
then
    printf '%s\n' 'Select an installed Python 3.12 or newer with PYTHON=/path/to/python.' >&2
    exit 1
fi

"$python" -m venv "$venv"
printf 'Created environment: %s\n' "$venv"
printf 'Activate with: source %q\n' "$venv/bin/activate"
printf 'Install dependencies with: %q -m pip install %q\n' "$venv/bin/python" "$root[dev]"
