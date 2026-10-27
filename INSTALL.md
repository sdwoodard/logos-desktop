# Install LogosDesktop

These instructions target release 1.0.0. Publication is pending until the
[v1.0.0 release](https://github.com/sdwoodard/logos-desktop/releases/tag/v1.0.0)
and the named AUR recipe are available. During private rehearsal, use the same
verified files from the candidate packet. Do not substitute an unrelated package
with the same name or run global pip against system Python.

## Choose your controller owner

| Environment | Route | Controller updates |
| --- | --- | --- |
| CachyOS or Arch Linux | Reviewed Arch source recipe | Update via AUR, recipe review, makepkg and pacman |
| CachyOS x86_64 with Python 3.12+, when you prefer a user-owned controller | Verified wheel bootstrap | Update environment, in a dedicated user-owned root |

CachyOS is the native reference. Container source checks on other distributions
remain experimental. Read [compatibility](COMPATIBILITY.md) for Wine, graphics
and feature limits. A Python wheel tag does not establish other-platform support.

You also need working desktop libraries, a compatible Wine runtime, space for the
installer/library and a separately chosen backup destination. The application
checks installation prerequisites before mutation. Package builds must run as an
ordinary user. Accept vendor terms and enter credentials yourself.

Graphical controller updates use Konsole or GNOME Terminal, or xterm when an X11
display is available. Without a usable terminal, the application keeps the
controller open and provides the exact helper command for your terminal.

## Managed wheel environment

Download these assets from the v1.0.0 release into a new directory:

- `install-wheel.py`
- `logos_desktop-1.0.0-py3-none-any.whl`
- `SHA256SUMS`
- `controller-release.json`

The release notes list the exact sizes and hashes. Verify the bootstrap and wheel
against that record before running either. A checksum file from the same download
channel detects changes but is not an independent publisher signature.

```bash
sha256sum install-wheel.py logos_desktop-1.0.0-py3-none-any.whl
wc -c < logos_desktop-1.0.0-py3-none-any.whl
```

Replace the two uppercase values below with the trusted release record. A hash
calculated only from an untrusted download does not authenticate that download.

```bash
mkdir -p "$HOME/.local/lib"
python3 ./install-wheel.py \
  --root "$HOME/.local/lib/logos-desktop" \
  --wheel ./logos_desktop-1.0.0-py3-none-any.whl \
  --version 1.0.0 --sha256 VERIFIED_SHA256 --size VERIFIED_BYTES
"$HOME/.local/lib/logos-desktop/current/bin/logos-desktop-controller-install" configure-provider
"$HOME/.local/lib/logos-desktop/logos-desktop" --dialog qt
```

The bootstrap verifies the wheel before loading its implementation. It installs
the GUI extra and dependencies in a versioned environment. System Python remains
package-managed. Always use the stable launcher shown above, including in custom
shortcuts. Do not launch an interpreter inside an old version directory.

### Wheel controller updates

In **Backups & tools**, choose **Check controller updates**, then **Update environment** when available.
The independent terminal asks for `UPDATE`. It prepares a sibling environment,
restarts the selected profile and requires readiness before reporting success.
Previous environments are retained for recovery.

The configure-provider step installs the scoped GitHub stable-release provider.
It refuses to overwrite an existing provider, including a custom private provider.
Keep existing configuration unless you intentionally review and remove it before
running setup again. See [provider details](PACKAGING.md#canonical-private-wheel-installation).
A generic pip, pipx or contributor install is not automatically managed.

### Remove the wheel controller

Close the controller and its update helpers. Verify the exact managed root, then
remove only that root using your file manager. It is normally
`~/.local/lib/logos-desktop`. Keep your separate configuration, Wine prefix,
library and backups. Remove profile-specific desktop entries only after checking
that they belong to this controller. Do not delete broad XDG directories.

## Arch source package

Obtain the `logos-desktop` source recipe from its exact AUR repository:

```bash
git clone https://aur.archlinux.org/logos-desktop.git
cd logos-desktop
```

During private rehearsal, use the reviewed rendered `PKGBUILD`, `.SRCINFO` and
matching sdist from the candidate packet instead. The product repository's
`packaging/arch/PKGBUILD.in` is a template, not a ready-to-run package.

Use a fully upgraded Arch system. Review the complete recipe and source checksum,
including the separate AUR dependencies `python-pythondialog` and `python-inotify`.
Build/install those reviewed dependencies first if missing. makepkg does not fetch
AUR dependencies automatically. The recipe retrieves the checksum-pinned release
sdist. Private rehearsal places those same bytes in `SRCDEST`. Build as an ordinary user:

```bash
makepkg --verifysource
makepkg -si
/usr/bin/logos-desktop-controller-install configure-provider
logos-desktop --dialog qt
```

Review any dependency and pacman authorization requests. The package has no hook
that starts Wine, accepts terms or creates a prefix. See
[Arch packaging details](PACKAGING.md#arch-source-candidate) for prerequisites,
source retrieval and metadata checks.

### Arch controller updates and removal

**Update via AUR** opens an independent terminal. Review the pinned recipe, type
`REVIEWED` only after review, and complete makepkg/pacman authorization. Follow
the printed profile-specific restart command. Check the restarted version.

AUR supplies recipes. `pacman -Syu` alone does not rebuild AUR packages. The
configure-provider step selects the AUR recipe and metadata endpoints for this
user. It does not install a helper or modify an existing provider. Before AUR
publication, the source release can be available while its package is still pending.

To remove the controller package:

```bash
sudo pacman -R logos-desktop
```

Review the package transaction. Your user configuration, prefixes, libraries and
backups are separate from the controller package and remain in place.

## Adopt an existing Logos installation

### Select your launcher for shared commands

For the managed wheel route, set this to your actual selected root:

```bash
controller="$HOME/.local/lib/logos-desktop/logos-desktop"
```

For the Arch package instead:

```bash
controller=/usr/bin/logos-desktop
```

Use `"$controller"` in the shared configuration, backup and support examples. This
avoids accidentally choosing another installation on PATH. Set it again in each
new terminal. Contributor checkouts use their own `.venv/bin/logos-desktop`.

Close its previous controller. On Home, use the explicit configuration or folder
picker. Prefer the existing configuration when it specifies custom paths. Review
the installation, prefix and new profile destination before confirming. Adoption
keeps the library in place and does not intentionally redownload it.

For an older controller that does not participate in shared ownership locks,
first inspect the explicitly selected configuration without adopting it:

```bash
"$controller" -c /absolute/path/to/existing-config.json --discover-installations
```

After reviewing the selected paths, use the GUI preview or explicitly confirm
the separate destination from the terminal:

```bash
"$controller" --previous-controller /absolute/path/to/previous-controller \
  --adopt-installation /absolute/path/to/existing-config.json \
  --adoption-destination /absolute/path/to/new-profile.json --confirm-adoption
```

The executable selection is remembered in private controller coordination state.
It verifies an owned executable and supported launcher form. It does
not automatically identify every historical installation on the machine. Close
the selected previous controller before confirming adoption. Unsupported wrappers
need their actual executable selected instead. See [configuration](CONFIGURATION.md).

## Migrate a private candidate

An unchanged earlier private wheel helper cannot load the renamed package. Its
same-version update check is not a migration mechanism. Use the verified new
bootstrap with the selected existing root and existing JSON profile:

```bash
python3 ./install-wheel.py \
  --root "$HOME/.local/lib/logos-desktop" \
  --wheel ./logos_desktop-1.0.0-py3-none-any.whl \
  --version 1.0.0 --sha256 VERIFIED_SHA256 --size VERIFIED_BYTES \
  --migrate --config /absolute/path/to/config.json --frontend qt
```

Close previous controllers/helpers first and unset `CONFIG_FILE`, `INSTALLDIR`
and `WINEPREFIX` environment overrides for this operation. The helper validates
owned installed file records, reserves profile ownership, preserves provider and
configuration, and retains the previous environment. This is explicit migration,
not an old in-app Update. Subsequent updates use the renamed owner's normal route.

Complete pending runtime-selection or restore recovery with the previous controller
before migration or adoption. Migration refuses known pending records before
activation and preserves them. Historical runtime journals targeting an earlier
portable filename must be recovered by the controller that created them. Do not
edit their target paths or delete the journal to bypass recovery.

Arch users upgrade through the reviewed package transaction. Do not run the wheel
migration against `/usr` or replace package-owned launchers manually.

## If something fails

Keep the error text and previous environment. Do not delete an operation receipt
to force another update. Read [support](SUPPORT.md) and the
[owner recovery contract](PACKAGING.md#canonical-private-wheel-installation).
An unknown startup outcome needs inspection before rollback because a successor
may still be running. Controller migration does not repair a damaged Wine prefix.
