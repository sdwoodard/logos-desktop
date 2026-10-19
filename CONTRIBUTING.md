# Contributing to LogosDesktop

This is private development source. Repository access is required. Use disposable
configuration, cache and Wine-prefix paths. Do not run install, repair, restore or
uninstall against a personal library during development.

## Source setup

Use an installed Python 3.12 or newer and a virtual environment. The inherited
graphical frontend needs Tcl/Tk supplied by your distribution. Do not replace the
system Python to follow these instructions.

```bash
git clone https://github.com/sdwoodard/logos-desktop.git
cd logos-desktop
python3 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
```

Alternatively, run `scripts/ensure-venv.sh` from any working directory. It creates
`.venv` beside the source using installed Python 3.12 or newer. Set `PYTHON` to an
interpreter path if needed. It preserves existing environments. The inherited
`ensure-python.sh` compiler script is not a supported setup route. Snap, Flatpak,
Nix and binary recipes remain unvalidated for LogosDesktop.

The Python namespace remains `ou_dedetai`. The `logos-desktop` and transitional
`oudedetai` console commands share an entry point. Existing runtime filenames and
configuration paths have not migrated yet. Controller self-updates are disabled.

## Focused checks

The product uses stdlib unittest. The private manual workflow runs full inexpensive
discovery, including the retained utility tests:

```bash
.venv/bin/python -B -m unittest discover -s tests -t . -p 'test*.py' -v
.venv/bin/ruff check
```

Tk availability is required for the GUI-specific fixture checks. A skipped GUI
check is not a native GUI pass.

Tests use local response and filesystem fixtures. They should run without public
network access or a particular checkout name, home setup or directory block size.
Use a disposable HOME and XDG paths when running the suite. `tests/integration.py`
is an obsolete manual harness with old commands, X11 assumptions and install/remove
side effects. It is excluded from discovery and must not be used as a routine test
command. Build scripts and package recipes require separate review before use.

## Changes and review

Keep useful regression tests around data safety, downloads, updates, configuration
and process ownership. Prefer deterministic offline fixtures. Add tests for
important behavior rather than every touched line or UI label.

Use clear names, logical whitespace and direct control flow. Explain consequential
constraints in comments. New abstractions need a concrete clarity, reuse or lifetime
benefit. Authored comments, docstrings and human-facing Markdown must not contain
em dashes, semicolons or generic AI phrasing. Preserve required license text and
literal source evidence.

Use the private repository for feedback and changes. Include the triggering
condition and validation limits. Keep credentials and support archives out of
commits. Disclose AI assistance when relevant and preserve upstream attribution.
Retain [LICENSE](LICENSE) and the provenance described in [README.md](README.md).

The [architecture map](ARCHITECTURE.md) describes the inherited backend boundaries.
Domain operations use the shared App interface rather than hard-coding a frontend.
