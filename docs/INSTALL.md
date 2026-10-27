# Install LogosDesktop

Use the verified wheel installer from
[Releases](https://github.com/sdwoodard/logos-desktop/releases). It keeps the
controller in its own Python environment, separate from system Python and your
Logos library. If no release is listed yet, the [contributor guide](../CONTRIBUTING.md)
explains how to work from source.

## Supported installation route

- **Environment:** Linux x86_64 with Python 3.12 or newer.
- **Install:** verified wheel bootstrap into a dedicated user-owned root.
- **Update:** choose **Update environment** in the installed controller.

There is no AUR package for this release. Arch packaging is deferred to a separate
future effort. It is not required to install or update the managed wheel environment.

Read [compatibility](COMPATIBILITY.md) for Wine, graphics and feature limits.
Distribution and desktop integration can vary. The wheel's Python tag does not
establish support for other architectures or operating systems.

You also need working desktop libraries, a compatible Wine runtime, space for the
installer/library and a separately chosen backup destination. The application
checks installation prerequisites before mutation. Install the controller as an
ordinary user. Accept vendor terms and enter credentials yourself.

Graphical controller updates use Konsole or GNOME Terminal, or xterm when an X11
display is available. Without a usable terminal, the application keeps the
controller open and provides the exact helper command for your terminal.

## Managed wheel environment

Download these assets from the chosen release into a new directory:

- `install-wheel.py`
- `logos_desktop-VERSION-py3-none-any.whl`
- `SHA256SUMS`
- `controller-release.json`

The release notes list the exact sizes and hashes. Verify the bootstrap and wheel
against that record before running either. A checksum file from the same download
channel detects changes but is not an independent publisher signature.

```bash
sha256sum install-wheel.py logos_desktop-VERSION-py3-none-any.whl
wc -c < logos_desktop-VERSION-py3-none-any.whl
```

Replace VERSION, VERIFIED_SHA256 and VERIFIED_BYTES below with the chosen release's
values. Its release notes provide a filled-in command. A hash calculated only from
an untrusted download does not authenticate that download.

```bash
mkdir -p "$HOME/.local/lib"
python3 ./install-wheel.py \
  --root "$HOME/.local/lib/logos-desktop" \
  --wheel ./logos_desktop-VERSION-py3-none-any.whl \
  --version VERSION --sha256 VERIFIED_SHA256 --size VERIFIED_BYTES
"$HOME/.local/lib/logos-desktop/current/bin/logos-desktop-controller-install" configure-provider
"$HOME/.local/lib/logos-desktop/logos-desktop" --dialog qt
```

The bootstrap verifies the wheel before loading its implementation. It installs
the GUI extra and dependencies in a versioned environment. System Python remains
package-managed. Always use the stable launcher shown above, including in custom
shortcuts. Do not launch an interpreter inside an old version directory.

### Wheel controller updates

On **Home**, choose **Check controller updates**, then **Update environment** when available.
The independent terminal asks for `UPDATE`. It prepares a sibling environment,
restarts the selected profile and requires readiness before reporting success.
Previous environments are retained for recovery.

The configure-provider step installs the scoped GitHub stable-release provider.
It refuses to overwrite an existing provider, including a custom private provider.
Keep existing configuration unless you intentionally review and remove it before
running setup again. See [provider details](PACKAGING.md#managed-wheel-installation).
A generic pip, pipx or contributor install is not automatically managed.

### Remove the wheel controller

Close the controller and its update helpers. Verify the exact managed root, then
remove only that root using your file manager. It is normally
`~/.local/lib/logos-desktop`. Keep your separate configuration, Wine prefix,
library and backups. Remove profile-specific desktop entries only after checking
that they belong to this controller. Do not delete broad XDG directories.

## Set up Logos

Choose **Set up Logos** on Home. The installation form suggests
`~/.local/share/logos-desktop/installation`, or the corresponding location under
your XDG data directory. You can edit the path or browse to another folder. Nothing
is installed or saved just because the recommendation is displayed.

Review the location and runtime before continuing. Accept the vendor terms and
sign in with your own account when prompted. An existing library from another
controller is not automatically imported or reconstructed.

## Use a saved profile

### Select your launcher for shared commands

Set this to your actual managed wheel root:

```bash
controller="$HOME/.local/lib/logos-desktop/logos-desktop"
```

Use `"$controller"` in the shared configuration, backup and support examples. This
avoids accidentally choosing another installation on PATH. Set it again in each
new terminal. Contributor checkouts use their own `.venv/bin/logos-desktop`.

To reopen an installation already configured in LogosDesktop, use **Settings →
Advanced installation settings → Choose installation profile**, or select its
configuration when starting the controller:

```bash
"$controller" -c /absolute/path/profile.json --dialog qt
```

This selects a profile without importing a separate installation or moving its data.
See [configuration](CONFIGURATION.md) for path and ownership rules. Keep recovery
journals from earlier development builds. If an old migration is unfinished, use
the controller that created it or request help before changing its files.

## If something fails

Keep the error text and previous environment. Do not delete an operation receipt
to force another update. Read [support](SUPPORT.md) and the
[owner recovery contract](PACKAGING.md#managed-wheel-installation).
An unknown startup outcome needs inspection before rollback because a successor
may still be running. Controller migration does not repair a damaged Wine prefix.
