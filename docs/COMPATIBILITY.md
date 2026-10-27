# Runtime compatibility

LogosDesktop is built for Linux x86_64 and uses Wine to run Logos. The managed
wheel environment is the supported controller installation route. Desktop, graphics
and dependency behavior can vary between distributions, so a successful Python
installation alone does not establish that every Logos feature will work.

## Current runtime policy

Recommended Wine follows the latest published development release. Wine's stable
release is identified separately. A provider's GitHub prerelease flag does not
determine either Wine channel.

The recommendation must also be available from its runtime provider. If a matching
AppImage is unavailable, the app reports that instead of silently choosing an older
build. A matching installed native Wine runtime can be selected separately. Wine
updates do not automatically upgrade your system packages.

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
