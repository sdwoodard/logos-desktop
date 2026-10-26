<p align="center">
  <img src="logos_desktop/img/logos-desktop.svg" alt="" width="72" height="72">
</p>

# LogosDesktop

**Install and manage Logos on Linux through Wine, from one desktop panel.**

Open Logos, choose its Wine runtime, back up your installation and find diagnostics
without remembering a collection of terminal commands. A terminal interface and
explicit CLI actions are available too.

**[Installation guide](INSTALL.md)** · [Get help](SUPPORT.md) · [Contribute](CONTRIBUTING.md)

An independent community project, not affiliated with or endorsed by Logos.
Your Logos account and the licenses for your content are still required.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/media/home-dark.png">
  <img src="docs/media/home-light.png" alt="LogosDesktop first-run Home with Set up Logos and explicit existing-install adoption controls" width="880">
</picture>

*Actual 1.0.0 candidate on KDE Wayland, showing a clean demonstration profile.*

## Before you install

**Private 1.0.0 candidate.** Public downloads and AUR submission are not available
yet. The instructions describe the prepared installation routes and private
candidate testing. They do not imply a published release.

- **Linux x86_64.** CachyOS with KDE Wayland is the native reference environment.
- **Two installation routes.** Arch source packaging uses pacman. The repository
  wheel route uses a dedicated user-owned Python environment.
- **Wine is a separate requirement.** Read [tested versions and known limitations](COMPATIBILITY.md)
  before choosing a runtime. Other distro source probes remain experimental.
- Flatpak, standalone binaries, Snap, Nix and ARM are not launch channels.

## What you can do

- **Start and stop Logos** with status tied to the selected installation.
- **Adopt an existing installation** without moving its library or prefix.
- **Back up and restore** with preflight checks and recoverable failed replacements.
- **Manage three distinct updates:** the controller, Logos and the Wine runtime.
- **Inspect problems** with Doctor and a previewable local support export.
- **Use your desktop theme** or choose a persistent light or dark preference.

See [configuration and existing-install adoption](CONFIGURATION.md) and
[backup and recovery](BACKUPS.md) for the details that matter to your data.

## Install, then open your installation

Start with the [installation guide](INSTALL.md). It explains prerequisites,
verification, the right update owner and removal for each route.

On first run, choose an installation location or explicitly select an existing
configuration. Review the proposed paths before confirming adoption. Handle the
vendor's terms and account sign-in yourself. LogosDesktop does not supply books,
credentials or a replacement for a Logos content license.

Existing private candidate users should read [migration](INSTALL.md#migrate-a-private-candidate)
before replacing their controller.

## Updates have different owners

| Action | What changes |
| --- | --- |
| Update LogosDesktop | The controller's own installation. Managed environments use **Update environment**. Arch source installs use **Update via AUR** |
| Update Logos | Vendor software inside the selected Wine prefix |
| Update Wine | The selected runtime, subject to its provider and compatibility checks |

An opened terminal is an update handoff, not a completed transaction. Follow its
confirmation and restart instructions, then check the running version. A provider
may lag behind a source release or be unavailable. The application reports those
states rather than silently changing installation methods.

Public update feeds remain disabled during private development. Private tests use
explicit installation-scoped providers. [Packaging details](PACKAGING.md) explain
the verified transaction and recovery boundaries.

## Support

Start with [symptoms and next steps](SUPPORT.md). Include the controller version,
distro, selected Wine/provider, Logos version and a short reproduction.

Support export is local and requires review. Do not post passwords, account links,
library databases, personal notes or an unchecked archive. No support material is
uploaded automatically.

[Compatibility notes](COMPATIBILITY.md) describe the tested runtime and the known
scrolling, graphics, font and printing limits. Successful installation is not a
promise that every Logos feature works under Wine.

## Make a useful first contribution

[Set up the source and run focused checks](CONTRIBUTING.md). Small reproducible bug
reports, clearer instructions and focused fixes are welcome. The contribution guide
includes concrete starting tasks and explains how changes are reviewed.

[Architecture](ARCHITECTURE.md) · [Current direction](ROADMAP.md) · [Release notes](CHANGELOG.md)

## Credit and licenses

This project preserves its imported Git history and contributor authorship.
Contributors include Ferion11, John Goodman, T. H. Wright, N. Marti and N. Shaaban.
The [MIT license](LICENSE), including DanielDevBR's copyright notice, is retained.

The project mark adapts Lucide's Book Open Text under ISC. See
[asset attribution](logos_desktop/img/ATTRIBUTION.md) and its
[license](logos_desktop/img/lucide-LICENSE.txt). Dependency licenses remain separate.
Logos and Verbum are third-party products.

Development uses AI assistance through OpenCode. The maintainer records scoped
validation and independent review. AI assistance is not a claim of human testing.
