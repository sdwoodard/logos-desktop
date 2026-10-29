# Install LogosDesktop

## Quick install

```bash
curl -fsSL https://github.com/sdwoodard/logos-desktop/releases/latest/download/install.sh | bash
```

Run `logos-desktop`, or search for **LogosDesktop** in your application menu.
Open a new terminal if the installer adds the command to PATH.

### Dependencies

Use Linux x86_64 with Bash, HTTPS-capable curl, tar and coreutils (including
sha256sum). Install as your ordinary user, without sudo. There is no curl 8.4
minimum and no need to install Python or PySide6 separately for quick install.

The installer reuses Python 3.12 through 3.14 with venv support when available.
Otherwise it downloads a pinned CPython 3.12 runtime from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone),
checks its SHA-256 before extraction and keeps it privately in your account.
The fallback adds about 33 MB of downloads. Python packages and Qt are installed
inside the managed environment. System Python and distribution packages are unchanged.

Native desktop libraries and Wine's system dependencies are still needed. An old
distribution may lack libraries required by current Qt wheels. Automatic Python
provisioning does not make every old Linux distribution supported.

The command requires a [published release](https://github.com/sdwoodard/logos-desktop/releases).
For development before a release is available, use [source setup](../CONTRIBUTING.md#source-setup).

The shell script downloads the Python bootstrap into a private temporary directory.
It checks download errors before executing that file. The bootstrap selects the
stable release, verifies the wheel's recorded size and SHA-256, installs a dedicated
environment and configures updates and launchers. This trusts executable code from
the project's release channel. Checksums from that channel are not an independent
publisher signature. The outer curl-to-Bash pipeline does not provide such a guarantee.

## Installed locations and PATH

| Item | Default location |
| --- | --- |
| Controller environment | `~/.local/lib/logos-desktop` |
| Python fallback, when needed | `~/.local/lib/logos-desktop-python.*` |
| Command | `~/.local/bin/logos-desktop` |
| Application-menu entry | `~/.local/share/applications/logos-desktop.desktop` |
| Logos installation proposal | `~/.local/share/logos-desktop/installation` |

XDG data settings are honored for the menu entry and Logos data. The menu entry
uses an absolute launcher, so it does not depend on your shell's PATH.

When needed, setup adds a marked PATH block for Bash, Zsh or Fish. Existing shell
content and permissions are preserved, and rerunning setup does not duplicate the
block. Bash uses .bashrc and its existing login-file choice. Zsh honors an absolute
ZDOTDIR inside your home. Fish uses its user conf.d directory. A child installer
cannot change the environment of the terminal that launched it.

Use `--no-path` if you manage your own PATH. Setup preserves conflicting commands,
desktop files, symlinks and edited managed blocks rather than replacing them.

## Download first

Download install.sh and SHA256SUMS from the chosen release into a new directory.
Compare the script's hash with the release notes and inspect it before running:

```bash
sha256sum install.sh
bash ./install.sh
```

For a different fresh controller root inside your home, or to keep shell files unchanged:

```bash
bash ./install.sh --root "$HOME/apps/logos-desktop" --no-path
```

### Managed wheel environment

For an exact downloaded version, obtain install-wheel.py and the wheel from the same
release. Verify both against its recorded hashes. Replace VERSION, VERIFIED_SHA256
and VERIFIED_BYTES with that release's values. The release notes provide a filled-in
command.

```bash
python3 ./install-wheel.py \
  --root "$HOME/.local/lib/logos-desktop" \
  --wheel ./logos_desktop-VERSION-py3-none-any.whl \
  --version VERSION --sha256 VERIFIED_SHA256 --size VERIFIED_BYTES
"$HOME/.local/lib/logos-desktop/current/bin/logos-desktop-controller-install" setup
```

The verified wheel supplies the installation implementation. It creates a versioned
environment, checks dependencies/resources and publishes a stable launcher. System
Python remains package-managed. The setup step adds the provider, command and menu entry.

This direct Python command requires Python 3.12 or newer with venv support.
Use the shell quick installer for automatic Python provisioning.

### Wheel controller updates

On Home, choose **Check controller updates**, then **Update LogosDesktop**.
Confirm the update in the popup. LogosDesktop downloads the update, prepares its
environment and restarts the selected profile automatically. Progress stays in the
app. Logos keeps running through the controller restart. Previous environments are
retained for recovery. A confirmed startup failure restores and reopens the previous
controller. For a restart needing review, open LogosDesktop from your application
menu and use **Help & logs**.

Setup preserves an existing scoped provider. Do not remove a custom provider unless
you intend to replace it. Generic pip or contributor environments do not establish
managed installation ownership.

### Refresh a managed installation from a verified wheel

To apply a maintenance build directly to an existing controller environment, download
and verify its bootstrap and wheel. Close LogosDesktop, then run the following with
your existing controller root, selected profile and the release's wheel metadata:

```bash
python3 ./install-wheel.py \
  --root "$HOME/.local/lib/logos-desktop" \
  --wheel ./logos_desktop-VERSION-py3-none-any.whl \
  --version VERSION --sha256 VERIFIED_SHA256 --size VERIFIED_BYTES \
  --migrate --config /absolute/path/profile.json --frontend qt
```

This verified migration prepares a sibling environment and reopens the selected
profile. It also supports a same-version maintenance refresh. Previous environments
and the profile's Logos data remain available.

## Set up Logos

Choose **Set up Logos** on Home. The form suggests the location above or your XDG
data location. You can edit it before confirmation. Displaying a recommendation
does not create an installation. Review the Wine runtime and location, accept vendor
terms and sign in with your own account. Your content licenses are still required.
See [compatibility](COMPATIBILITY.md) and keep a separate backup destination.

Browser sign-in links return to the selected Logos profile through its desktop URL
handler. Managed setup creates this handler, and opening Logos checks its routing.
If Firefox asks to open a sign-in link with LogosDesktop, approve that handoff.

**Recommended Wine** obtains the latest available stable AppImage from the independent
[mmtrt/WINE_AppImage](https://github.com/mmtrt/WINE_AppImage) project. A compatible
installed native Wine matching that available version can be reused. The recommendation
follows the provider's stable channel. Existing saved runtime selections stay in place
when the controller is updated.

For an existing installation, open **Settings → Advanced installation settings →
Change Wine AppImage** to select a downloaded stable Wine AppImage. Close Logos
before changing its runtime. LogosDesktop preserves the installation's Windows
user so the selected runtime opens the existing account and library.

## Use a saved profile

### Select your launcher for shared commands

The command installed by setup can be used directly:

```bash
controller=logos-desktop
"$controller" -c /absolute/path/profile.json --dialog qt
```

For multiple controller environments, set `controller` to the intended root's
absolute stable launcher instead. Choose the installation's saved profile in Settings
or supply its path with `-c`, as shown above. See [configuration](CONFIGURATION.md).

## Setup and recovery

An existing controller root is not silently overwritten. Use Home for updates.
If installation succeeded but launcher setup failed, retain the environment and
run the retry command printed by the installer. You can add --no-path when shell
files are managed elsewhere. Close other controller update/setup helpers first.

Launcher setup keeps an installation-scoped journal and preserves later user edits
during recovery. Unknown or conflicting state is reported without deleting files.
For owner-update recovery, use the installed helper's recover action. Rollback is
explicit and only restores an unchanged publication. An unknown successor may still
be running, so do not delete receipts or versions to force another update.

To remove the controller, close it and its helpers, then remove only its chosen root
and its own command/menu entry after checking their paths. Keep Logos, its Wine
prefix, configuration and backups. The generic ~/.local/bin PATH entry may remain.
Do not delete broad XDG directories. See [support](SUPPORT.md) for help with errors.
If a private Python fallback was installed, keep its directory while any controller
environment refers to it in pyvenv.cfg. It can be removed after those environments
are removed.
