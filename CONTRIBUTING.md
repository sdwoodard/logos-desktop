# Contributing to LogosDesktop

Use disposable
configuration, cache and Wine-prefix paths. Do not run install, repair, restore or
uninstall against a personal library during development.

## Source setup

Use an installed Python 3.12 or newer and a virtual environment. Install the `gui`
extra for the default Qt frontend. Do not replace the system Python to follow
these instructions.

```bash
git clone https://github.com/sdwoodard/logos-desktop.git
cd logos-desktop
python3 -m venv .venv
.venv/bin/python -m pip install '.[dev,gui]'
```

Alternatively, run `scripts/ensure-venv.sh` from any working directory. It creates
`.venv` beside the source using installed Python 3.12 or newer. Set `PYTHON` to an
interpreter path if needed. It preserves existing environments. Install
`'.[dev,gui]'` afterward for Qt development. See [PACKAGING.md](PACKAGING.md) for
the artifact contract, notices and deferred distribution channels.

The Python namespace is `logos_desktop`. The `logos-desktop` console command is
the controller entry point. Existing runtime filenames and
configuration paths use the LogosDesktop identity with explicit legacy adoption.
Standalone self-replacement remains disabled. Provider-backed owner updates are
separate from contributor source installations.

## Focused checks

The product uses stdlib unittest. The manual workflow runs full inexpensive
discovery, including the retained utility tests. Use a disposable account/container
for the full suite. HOME/XDG overrides alone do not isolate the account-home
coordination namespace. For a focused offline test that uses its own fixtures:

```bash
.venv/bin/python -B -m unittest tests.test_artifacts -v
.venv/bin/ruff check
```

Qt uses the official PySide6 Essentials package. CLI and TUI use does not require
Qt. The legacy `--dialog tk` option is a compatibility alias for Qt.
Run Qt adapter checks with `QT_QPA_PLATFORM=offscreen` when no display is available.
A skipped GUI check is not a pass. A native Wayland check is still needed for
desktop integration.

Tests use local response and filesystem fixtures. They should run without public
network access or a particular checkout name, home setup or directory block size.
In a disposable account, the full command is
`.venv/bin/python -B -m unittest discover -s tests -t . -p 'test*.py' -v`.
The old manual binary
harness was retired. Build and installed-artifact checks are described in
[PACKAGING.md](PACKAGING.md). Source setup is for contributors and does not create
the managed wheel environment used by the canonical installation route.

## Changes and review

Keep useful regression tests around data safety, downloads, updates, configuration
and process ownership. Prefer deterministic offline fixtures. Add tests for
important behavior rather than every touched line or UI label.

Use clear names, logical whitespace and direct control flow. Explain consequential
constraints in comments. New abstractions need a concrete clarity, reuse or lifetime
benefit. Authored comments, docstrings and human-facing Markdown must not contain
em dashes, semicolons or generic AI phrasing. Preserve required license text and
literal source evidence.

Use repository issues and pull requests for feedback and changes. Include the triggering
condition and validation limits. Keep credentials and support archives out of
commits. Disclose AI assistance when relevant and preserve upstream attribution.
Retain [LICENSE](LICENSE) and the provenance described in [README.md](README.md).

The [architecture map](ARCHITECTURE.md) describes the inherited backend boundaries.
Domain operations use the shared App interface rather than hard-coding a frontend.

## Find a first task

These are suggested bounded contributions, not assigned or preapproved issues:

- Reproduce one documentation command in a disposable environment. Likely files:
  `INSTALL.md` and `SUPPORT.md`. Report exact versions and the failing step, then
  propose a corrected command without broadening support claims.
- Add one regression for a demonstrated profile-selection failure. Start at
  `tests/test_config.py` or `tests/test_adoption.py`. Show the failing behavior
  before the fix and verify that another profile stays unchanged.
- Improve one keyboard-access problem in a dialog. Start at
  `logos_desktop/qt/dialogs.py` and `tests/test_qt_frontend.py`. Describe the native
  reproduction and verify focus, Escape/cancel and the affected workflow.

Open a focused issue before a large feature or package-channel change. For a pull
request, describe the problem, relevant files, result and checks actually run.
Keep unrelated formatting out of the diff. The maintainer reviews data ownership,
failure handling, readability and user-facing claims before merging. No response
time or acceptance is promised.
