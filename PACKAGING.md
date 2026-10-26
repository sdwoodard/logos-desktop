# LogosDesktop packaging

The release candidates are a Python source distribution, a wheel and an Arch
source recipe named `logos-desktop`. They target Linux x86_64 with Python 3.12
or newer. A `py3-none-any` wheel tag describes the controller's Python code,
not support for running Logos on other operating systems or architectures.

These are private development candidates. No public release, package index,
AUR submission or public update feed is enabled by this metadata.

## Artifact contract

`ou_dedetai/constants.py:APP_VERSION` is the version source. The runtime aliases
`APP_RELEASE_VERSION` and `LLI_CURRENT_VERSION` reference it. Setuptools reads
the same source for distribution metadata. Version identity does not grant
permission to replace the controller or enable a release feed.

The import namespace remains `ou_dedetai`. Both `logos-desktop` and the transitional
`oudedetai` command call `ou_dedetai.main:main`. Only runtime Python packages are
discovered. Explicit package data includes the MSI transform, runtime icons,
Lucide attribution and license. Documentation and the recipe template install
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
Keep final checksum-bearing recipes outside the source archive. Rebuild after
source changes rather than modifying or relabeling completed artifacts.

For a disposable installed-artifact smoke check, verify the wheel's SHA-256
against the reviewed private artifact record before installing it:

```bash
python3 -m venv /path/to/smoke-venv
/path/to/smoke-venv/bin/python -m pip install '/path/to/logos_desktop-VERSION-py3-none-any.whl[gui]'
/path/to/smoke-venv/bin/python -m pip check
/path/to/smoke-venv/bin/logos-desktop --help
/path/to/smoke-venv/bin/logos-desktop --version
/path/to/smoke-venv/bin/oudedetai --version
```

Run checks from outside the checkout without `PYTHONPATH`. Verify distribution
version, imported module locations and nonempty installed resources under
`ou_dedetai/img` and `ou_dedetai/assets`. A CLI smoke check does not validate
native Qt, Wine, Logos setup or an installation owner's update transaction.

The canonical repository route is a verified wheel with `[gui]` in a dedicated,
versioned user-owned environment with an owned stable launcher. A generic pip,
pipx, editable or source install does not establish that ownership. The disposable
smoke environment above is not a managed installation. Owner-aware installation
and update acceptance must pass before end-user instructions are offered.

### Canonical private wheel installation

Use the bootstrap script from the same reviewed source revision as the wheel.
Provide the artifact record's exact SHA-256 and byte count. Do not copy placeholder
values literally. Create the parent first, then choose a fresh root beneath it.

```bash
mkdir -p "$HOME/.local/lib"
python3 scripts/install-wheel.py \
  --root "$HOME/.local/lib/logos-desktop" \
  --wheel /path/to/logos_desktop-1.0.0-py3-none-any.whl \
  --version 1.0.0 --sha256 VERIFIED_SHA256 --size VERIFIED_BYTES
"$HOME/.local/lib/logos-desktop/logos-desktop" --dialog qt
```

The helper validates the wheel before loading its packaged implementation. It
creates a final-path environment with the gui extra, checks dependencies/resources,
and publishes an owned stable launcher. Use that launcher for desktop/profile links,
not the interpreter inside one version directory. No system Python files change.

Update requires an owned mode-0600 `provider.json` inside the private installation
root. During private development the maintainer supplies the loopback/provider
fixture URL explicitly. No public URL is configured automatically.

```json
{
  "schema": 1,
  "installation": "/absolute/home/path/.local/lib/logos-desktop",
  "manifest_url": "http://127.0.0.1:PORT/manifest.json",
  "allow_loopback": true
}
```

The stable release manifest names `version`, exact wheel `filename`, `url`, `size`
and `sha256`. Outside explicitly enabled loopback fixtures, HTTPS is required.
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

## Arch source candidate

`packaging/arch/PKGBUILD.in` is a template, not an executable release recipe.
Replace `@VERSION@` from `APP_VERSION`, `@SDIST_SHA256@` with the exact sdist's
SHA-256. The maintainer contact is Stephen Woodard, as supplied for this candidate.
Reject remaining placeholders before building. The recipe license is
[MIT](packaging/LICENSE).

Render `PKGBUILD` into a separate private directory. Its source must be the exact
`logos_desktop-VERSION.tar.gz` at the versioned release URL, with a real digest.
Do not use a branch archive, `SKIP` or an archive containing its own final recipe.
During private validation, place the matching reviewed bytes in `SRCDEST` and
record that private retrieval mapping. This does not prove public URL access.

Build as an ordinary user in a clean pinned Arch environment. Install declared
dependencies through pacman, including separately reviewed AUR dependencies
`python-pythondialog` and `python-inotify`. The other Python dependencies and
`pyside6` are official Arch packages as checked during candidate preparation.
Recheck availability before a release. The recipe uses PEP 517 without network
build isolation and stages its wheel with `python -m installer`. It never installs
with pip into system Python.

From the rendered recipe directory in that disposable environment:

```bash
makepkg --verifysource
makepkg --printsrcinfo > .SRCINFO
makepkg --cleanbuild
```

Compare `.SRCINFO` with `PKGBUILD`, inspect the package file list, then test pacman
install, upgrade and removal in the disposable environment. The package installs
the controller desktop entry, project icons and licenses. It has no install hooks
that launch Wine, initialize prefixes, accept terms or migrate user data. Normal
package removal preserves user profiles, prefixes, libraries and backups.

System Wine is optional because a user can select a separately managed compatible
runtime. Declared runtime libraries also support that route. Installing whichever
Wine is newest in Arch does not establish Logos compatibility. The application
retains its runtime qualification and process-ownership safeguards.

AUR distributes recipes, not a binary repository. `pacman -Syu` alone does not
rebuild AUR packages. A controller update must use the installed owner's reviewed
recipe/build and pacman transaction, with normal package authorization. Opening a
terminal is a handoff, not proof of a successful update. Updating Logos inside its
prefix and selecting or updating Wine remain separate operations.

The installed Arch owner reads an owned private provider file at
`~/.local/state/logos-desktop/aur/provider.json`, scoped to
`/usr/bin/logos-desktop`. It names `schema`, `installation`, `rpc_url`, `git_url`
and an explicit `allow_loopback` for private fixtures. Optional
`release_manifest_url` compares the stable source release against AUR metadata to
show packaging lag. It never converts an Arch installation into a wheel install.
Without it, current means current for AUR and source availability is not checked.

Update via AUR opens a separate terminal, pins and displays the complete supported
recipe, and requires REVIEWED before makepkg. The terminal keeps the result and an
exact config/frontend restart command visible until acknowledgement. Run that
command after successful installation, then check the restarted version. Opening
the terminal or installing a package while the old process runs is not Updated.
Package authorization remains in makepkg/pacman, never in a root controller GUI.

## Notices and redistribution review

| Material | Provenance and required treatment |
| --- | --- |
| Controller source | OuDedetai ancestry, MIT. Preserve [LICENSE](LICENSE), including DanielDevBR's notice and upstream credit |
| Book Open Text project icon | Lucide revision and adaptation recorded in [ATTRIBUTION.md](ou_dedetai/img/ATTRIBUTION.md). Preserve the [ISC notice](ou_dedetai/img/lucide-LICENSE.txt) |
| MSI transform | John Goodman contributed `LogosStubFailOK.mst` under the upstream MIT license on 2025-01-23 in commit `eb6f604e7366f6ade6073837c7446fe81fddd85c`. It changes installer action tables and contains vendor summary metadata, with no listed installer or executable payload. Preserve upstream MIT attribution. This does not claim vendor endorsement |
| Legacy project icon | Nate Marti contributed `icon.png` and its editable broken-chain artwork under upstream MIT on 2024-10-21 in commit `919bbec`. It is project artwork, distinct from the Logos product mark |
| Vendor product graphics | Removed from the current distribution because their redistribution basis was unresolved. Product names remain text. Home uses the attributed Book Open Text project artwork, as approved for this packaging change |
| Arch template and desktop entry | MIT, with a separate [recipe notice](packaging/LICENSE) |
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
| `io.github.FaithlifeCommunity.OuDedetai.yml` | Flatpak deferred. Process visibility, remapped path identity, document access and installed-app update behavior have not passed. Its inherited product-path disagreement is retired with the manifest |
| `Dockerfile`, `ou_dedetai.spec`, `scripts/build-binary.sh`, `scripts/frozen_identity.py` | Standalone binaries and `logos-desktop-bin` deferred. The inherited PyInstaller toolchain is not a supported build route. Standalone ownership and recovery guards remain in runtime code and regression tests |
| `scripts/quick-install.sh` | Retired. It bypassed current artifact, process and installation safeguards |
| `scripts/ensure-python.sh` | Retired. Contributor setup uses an installed supported interpreter rather than compiling into host paths |

ARM, Snap, Nix and Flatpak are not supported launch channels. Historical recipes
remain available in source history for attribution and investigation. The obsolete
manual `tests/integration.py` harness still names the removed binary builder and
must not be used as a supported test or installation route.
