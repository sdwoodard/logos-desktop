# Runtime compatibility

LogosDesktop is built for Linux x86_64 and uses Wine to run Logos. The managed
wheel environment is the supported controller installation route. Desktop, graphics
and dependency behavior can vary between distributions, so a successful Python
installation alone does not establish that every Logos feature will work.

## Current runtime policy

The graphical installer resolves the current stable Logos target and offers a
bundled Wine candidate for review. Without matching project evidence, it can
offer an eligible stable AppImage labelled **Not tested**. The separate provider
feed checks stable releases from
[mmtrt/WINE_AppImage](https://github.com/mmtrt/WINE_AppImage/releases/tag/continuous-stable).
This is an independent general-purpose Wine packaging project. The selected Linux x86_64 asset
must have a SHA-256 digest and pass format and runtime compatibility checks.
Winetricks is obtained separately from its official tagged script with a project
SHA-256 pin. Setup does not assume it is bundled inside a Wine AppImage.

Advanced setup also offers installed native Wine. Wine runtime updates are
separate from system package updates.
Existing saved runtime selections stay in place when the controller is updated.

Installations no longer force Wine's GDI renderer. They leave an existing renderer
selection untouched and use Wine's default when no selection exists. Updating
LogosDesktop does not automatically remove GDI from an older prefix.

## Changing runtimes

Preserve a recoverable backup before upgrading an existing prefix. Do not downgrade
a prefix that a newer Wine has already upgraded. Check the workflows you rely on
after changing runtime or graphics settings. Controller updates and Logos updates
are separate from changing Wine.

## Graphics and scrolling limitations

DXVK is optional and is not bundled or installed automatically. Follow
the [DXVK project's setup and requirements](https://github.com/doitsujin/dxvk)
if evaluating it separately. Do not replace system graphics libraries.

- Scrolling speed can vary. Switching to DXVK does not consistently improve it.
- Some menus and popups can show drawing artifacts.
- Multi-GPU systems may need process-local device selection. Check DXVK's guidance
  before changing settings, and avoid applying a workaround to every application.
- Some fonts or glyphs may be missing.
- Printing and export are not validated.

## Scope and troubleshooting

Record the selected Wine and wineserver versions, Wine provider, Logos version,
DXVK version if used, GPU/driver, kernel, distribution and reproduction steps.
Keep account data and library content out of public reports. Use LogosDesktop's
local support archive for a reviewed, private diagnostic handoff.

Review custom DLL overrides when investigating startup problems. A workaround for
one installation is not necessarily appropriate for another. Use
[diagnostics and support](SUPPORT.md) to gather the details without changing your data.

The wheel's Python tag does not establish Logos compatibility with other
architectures or operating systems.
## Wine runners and local recovery

Open **Backups & tools → Wine runners and recovery** to review Wine for the
selected installation. Select a version to read its details. Download prepares
verified files. It leaves the current Wine selection in place. In new setup,
**Advanced setup → Browse Wine runners** changes the installation draft only.

Close Logos and its indexer before starting a trial. The window labelled
**Offline trial copy** opens a temporary copy of your installation. Check
Library, search, typing and scrolling. Restart the trial and check your work
again. Use **End trial** on Home to release the original. If shutdown is
still running or unknown, recheck status before another normal shutdown request.

**Use for this installation** changes the original after a complete recovery
point and an offline check of the old installation. **Apply without successful
trial** skips the candidate trial checklist. It requires the same recovery
checks. Native and custom Wine remain usable under their existing launch rules.

**Return to saved point** restores the whole local installation to its point
date and retains newer work separately. Inspect the restored and retained
copies offline. To preserve a text document, export it from Logos into
`C:\M4Export` in the retained inspection and choose a separate destination
folder when that inspection finishes. **Resume online** reconnects the restored
installation to current server data. Returning local files cannot rewind cloud
changes. **This installation works for me** records a local observation and
keeps recovery available.

The private display requires a Wayland session, Bubblewrap, XWayland, system
Python with PyQt6 Qt Quick and the Qt 6 Wayland compositor QML modules. If these
are unavailable, restore the prerequisites and recheck. Complete byte-level
return works without a graphical Wine session.

On CachyOS the system bindings and compositor come from `python-pyqt6` and
`qt6-wayland`. On Ubuntu 24.04 use `python3-pyqt6.qtquick`, `qt6-wayland`,
`qml6-module-qtwayland-compositor`, `qml6-module-qtquick-window`,
`qml6-module-qtquick` and `qml6-module-qtqml-workerscript`.
