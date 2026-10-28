# Runtime compatibility

LogosDesktop is built for Linux x86_64 and uses Wine to run Logos. The managed
wheel environment is the supported controller installation route. Desktop, graphics
and dependency behavior can vary between distributions, so a successful Python
installation alone does not establish that every Logos feature will work.

## Current runtime policy

Recommended Wine selects the development AppImage currently published by
[mmtrt/WINE_AppImage](https://github.com/mmtrt/WINE_AppImage/releases/tag/continuous-devel).
This is an independent general-purpose Wine packaging project. LogosDesktop does
not download Wine images from FaithLife-Community. The selected Linux x86_64 asset
must have a SHA-256 digest and pass format and runtime compatibility checks.
Winetricks is obtained separately from its official tagged script with a project
SHA-256 pin. Setup does not assume it is bundled inside a Wine AppImage.

The downloadable build can lag Wine's latest source announcement. Setup uses the
available development build rather than requesting a nonexistent asset. A matching
installed native Wine can be reused. Malformed, missing or offline provider metadata
is reported without silently substituting stable, staging or Proton builds.
Wine updates do not automatically upgrade system packages.

Recommended is an acquisition policy, not the version most thoroughly tested with
Logos. New Wine releases can introduce regressions. Runtime version checks enforce
known requirements but do not certify every Logos feature.

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
