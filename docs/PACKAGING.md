# LogosDesktop packaging

The release artifacts are a Python source distribution and a wheel. The managed
wheel environment is the end-user route. They target Linux x86_64 with Python 3.12
or newer. A `py3-none-any` wheel tag describes the controller's Python code,
not support for running Logos on other operating systems or architectures.

Publication requires a reviewed release and its named assets. This metadata does
not publish a release, package index or update manifest. AUR packaging is deferred
to a separate future maintenance effort and is not part of this launch.

## Artifact contract

`logos_desktop/constants.py:APP_VERSION` is the version source. The runtime aliases
`APP_RELEASE_VERSION` and `LLI_CURRENT_VERSION` reference it. Setuptools reads
the same source for distribution metadata. Version identity does not grant
permission to replace the controller or enable a release feed.

The import namespace is `logos_desktop`. The `logos-desktop` command calls
`logos_desktop.main:main`. Only runtime Python packages are
discovered. Explicit package data includes the MSI transform, runtime icons,
Lucide attribution and license. Documentation and reference desktop metadata install
under `share/doc/logos-desktop`, not as active desktop or package-manager files.

The `gui` extra installs PySide6 Essentials 6.12, including QtCore, QtGui,
QtWidgets and QtDBus. CLI and TUI imports do not require Qt. The legacy
`--dialog tk` option is a compatibility alias for Qt and does not require Tcl/Tk.
External runtime prerequisites, including Wine and desktop libraries, are not
bundled into the Python wheel.

## Build and inspect

Use an ordinary user and a fresh virtual environment with an installed supported
Python. Build from an exact reviewed source revision with no local modifications.

```bash
python3 -m venv /path/to/build-venv
/path/to/build-venv/bin/python -m pip install build
/path/to/build-venv/bin/python -m build
```

The default build produces an sdist and builds its wheel from that sdist. Record
the source commit, tree, Python and build-tool versions, filenames, sizes and
SHA-256 digests. Inspect both archives for runtime resources and license notices.
Keep generated checksum manifests outside the source archive. Rebuild after source
changes rather than modifying or relabeling completed artifacts.

For a disposable installed-artifact smoke check, verify the wheel's SHA-256
against the reviewed private artifact record before installing it:

```bash
python3 -m venv /path/to/smoke-venv
/path/to/smoke-venv/bin/python -m pip install '/path/to/logos_desktop-VERSION-py3-none-any.whl[gui]'
/path/to/smoke-venv/bin/python -m pip check
/path/to/smoke-venv/bin/logos-desktop --help
/path/to/smoke-venv/bin/logos-desktop --version
```

Run checks from outside the checkout without `PYTHONPATH`. Verify distribution
version, imported module locations and nonempty installed resources under
`logos_desktop/img` and `logos_desktop/assets`. A CLI smoke check does not validate
native Qt, Wine, Logos setup or an installation owner's update transaction.

The canonical repository route is a verified wheel with `[gui]` in a dedicated,
versioned user-owned environment with an owned stable launcher. A generic pip,
pipx, editable or source install does not establish that ownership. The disposable
smoke environment above is not a managed installation. Owner-aware installation
and update acceptance must pass before end-user instructions are offered.

### Managed wheel installation

For the latest stable release, the [quick installer](INSTALL.md#quick-install)
uses --latest to select the canonical metadata, verify the wheel and configure
updates. It defaults to ~/.local/lib/logos-desktop. Missing safe parent directories
inside the user's home are created privately. Existing controller roots are refused.

The explicit artifact route below remains available for pinned and offline wheels.
Use the bootstrap script from the same reviewed source revision as the wheel.
Provide the artifact record's exact SHA-256 and byte count. Do not copy placeholder
values literally. Create the parent first, then choose a fresh root beneath it.

```bash
mkdir -p "$HOME/.local/lib"
python3 scripts/install-wheel.py \
  --root "$HOME/.local/lib/logos-desktop" \
  --wheel /path/to/logos_desktop-VERSION-py3-none-any.whl \
  --version VERSION --sha256 VERIFIED_SHA256 --size VERIFIED_BYTES
"$HOME/.local/lib/logos-desktop/logos-desktop" --dialog qt
```

The helper validates the wheel before loading its packaged implementation. It
creates a final-path environment with the gui extra, checks dependencies/resources,
and publishes an owned stable launcher. Use that launcher for desktop/profile links,
not the interpreter inside one version directory. No system Python files change.

Update requires an owned mode-0600 `provider.json` inside the private installation
root. Quick installation configures this automatically. For an explicit wheel,
run the installed helper as part of installation:

```bash
"$HOME/.local/lib/logos-desktop/current/bin/logos-desktop-controller-install" configure-provider
```

This explicitly configures the stable GitHub release provider. Existing files are
never replaced. Custom-provider users retain their configuration. Review and remove
an existing provider only when intentionally changing it, with helpers closed.

```json
{
  "schema": 1,
  "installation": "/absolute/home/path/.local/lib/logos-desktop",
  "manifest_url": "https://github.com/sdwoodard/logos-desktop/releases/latest/download/controller-release.json"
}
```

The stable release manifest names `version`, exact wheel `filename`, `url`, `size`
and `sha256`. It is a release asset, generated after the wheel. Its wheel URL is
versioned. The release must be designated latest stable for discovery. HTTPS is
required. Private rehearsals use separate explicit loopback configurations with
`allow_loopback: true`, never a patched release artifact.
The actual Check controller updates and Update environment actions use this owner.
An independent terminal asks for UPDATE, prepares a sibling environment and launches
the selected config/frontend. Previous environments are retained. Startup confirmation
is required before reporting Updated. Ambiguous startup remains unknown without
killing the successor. Confirmed failure restores only the helper's unchanged pointer.

The packaged `logos-desktop-controller-install recover` command reconciles an
unpublished interruption. `rollback` explicitly restores the previous environment
only if ownership and publication still match. Invoke these through the selected
environment's installed command. Preserve both environments when investigating an
unknown operation rather than deleting its receipt or editing package files.

To remove this controller, close its UI and helpers, then remove only the selected
installation root after verifying its path. Configuration, prefixes, libraries and
backups are separate and must be retained. Profile-specific desktop launchers can
be removed separately after checking their ownership. No data-removal action is
implied by uninstalling the controller.

## Deferred Arch packaging

No AUR recipe or Arch binary package is offered for this release. Packaging research
and private candidate evidence are retained by the maintainer for a separate future
effort. The release archives do not ship an Arch recipe template.

Package-ownership checks and owner adapters remain for earlier private installations.
They do not make an AUR package available, and must not be bypassed to replace
package-managed files with a wheel. Use the managed wheel instructions for a new
controller installation. Keep existing profiles, prefixes, libraries and backups
separate from controller files.

## Notices and redistribution review

| Material | Provenance and required treatment |
| --- | --- |
| Controller source | Imported MIT source history. Preserve [LICENSE](../LICENSE), including DanielDevBR's notice and contributor credit |
| Book Open Text project icon | Lucide revision and adaptation recorded in [ATTRIBUTION.md](../logos_desktop/img/ATTRIBUTION.md). Preserve the [ISC notice](../logos_desktop/img/lucide-LICENSE.txt) |
| MSI transform | `LogosStubFailOK.mst` is supplied under the upstream MIT license, introduced on 2025-01-23 in commit `eb6f604e7366f6ade6073837c7446fe81fddd85c`. It changes installer action tables and contains vendor summary metadata, with no listed installer or executable payload. Preserve the applicable MIT notices. This does not claim vendor endorsement |
| Historical artwork | Nate Marti's 2024-10-21 contribution remains in Git history at `919bbec`. Current application and desktop entries use Book Open Text |
| Vendor product graphics | Removed from the current distribution because their redistribution basis was unresolved. Product names remain text. Home uses the attributed Book Open Text project artwork, as approved for this packaging change |
| Desktop metadata | MIT, with the retained [packaging notice](../packaging/LICENSE) |
| Python and Qt dependencies | Resolved separately, not embedded in the controller wheel. Preserve each installed distribution's notices. PySide6/Qt and shiboken have their own LGPL/GPL/commercial terms and component notices |

The project metadata describes the controller and Lucide licenses, not a grant
of rights to commercial software or vendor marks. Dependencies such
as requests, distro, psutil, pythondialog, inotify, Textual, olefile and zstandard
retain their own licenses. Record exact resolved versions and notices with each
candidate. Do not bundle Logos installers, books, accounts or personal databases.

## Retired distribution entrypoints

| Removed material | Decision |
| --- | --- |
| `shell.nix` | Retired. Its pinned shell omitted current dependencies and was not a validated development route. Use an installed Python and a venv |
| `snap/` | Retired with its exclusive launchers and graphics. Its wrapper split arguments and its runtime was unvalidated |
| Inherited Flatpak manifest | Deferred. Process visibility, remapped path identity, document access and installed-app update behavior have not passed |
| `Dockerfile`, inherited PyInstaller spec, `scripts/build-binary.sh`, `scripts/frozen_identity.py` | Standalone binaries and `logos-desktop-bin` deferred. The inherited PyInstaller toolchain is not a supported build route. Standalone ownership and recovery guards remain in runtime code and regression tests |
| `scripts/quick-install.sh` | Retired. It bypassed current artifact, process and installation safeguards |
| `scripts/ensure-python.sh` | Retired. Contributor setup uses an installed supported interpreter rather than compiling into host paths |

ARM, Snap, Nix and Flatpak are not supported launch channels. Historical recipes
remain available in source history for attribution and investigation. The obsolete
manual binary harness and historical frontend screenshots were retired. Meaningful
installation, update and frontend regressions remain in the offline suite.

## Earlier controller environments

The verified bootstrap retains an explicit `--migrate --config /absolute/profile.json`
mode for a selected older managed controller environment. This is controller-owned
environment recovery, not legacy Logos installation import or library reconstruction.
It verifies installed file records, reserves profile ownership and retains predecessor
bytes for recovery. Resolve older pending runtime/restore journals with the controller
that created them before attempting migration. Do not change journal paths to bypass
recovery checks. See [installation recovery](INSTALL.md#if-something-fails).
