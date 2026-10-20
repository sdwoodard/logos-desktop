# LogosDesktop

LogosDesktop is an independent community installer and manager for Logos on Linux
through Wine. It is not affiliated with or endorsed by Logos.
A valid Logos license is still required.

## Development status

This repository is private development source. There is no supported LogosDesktop
release or installation package yet. Inherited distribution recipes remain
unvalidated and do not establish platform support.

Controller self-update discovery and replacement are disabled during private
development. Updates to Logos and the selected Wine runtime are separate operations.
Native journeys and distribution validation are still in progress. Use disposable
configurations and prefixes for development.

The current source retains the `ou_dedetai` import namespace and legacy runtime
paths while migration is prepared. Both `logos-desktop` and the transitional
`oudedetai` console command call the same entry point. The graphical frontend uses
Qt Widgets through PySide6. Install the `gui` extra for desktop use, or choose
`--dialog tui` for the terminal panel. Explicit CLI actions remain available
without a graphical application. See [the architecture](ARCHITECTURE.md) for the
frontend and shared-service boundaries.

See [CONTRIBUTING.md](CONTRIBUTING.md) for source setup and focused checks.
See [CONFIGURATION.md](CONFIGURATION.md) for profile selection, setting precedence,
legacy files, atomic saves and explicit recovery commands.

## Support

Use this private repository's [issues](https://github.com/sdwoodard/logos-desktop/issues)
for development feedback. The Get Support operation creates a local archive.
Keep it private and review its contents before sharing. It can contain configuration,
paths and logs. Nothing is uploaded automatically.

## Attribution and license

LogosDesktop derives from [OuDedetai](https://github.com/FaithLife-Community/OuDedetai)
and preserves its Git history and contributor credit. The imported baseline is
`e72fd5f9a236aed009c8e444a954774da5750f8d`.

Upstream credits include Ferion11, John Goodman, T. H. Wright, N. Marti and
N. Shaaban. The [MIT license](LICENSE), including DanielDevBR's copyright notice,
is retained. Logos and Verbum are third-party products.

The Qt application icon adapts Lucide's Book Open Text glyph under the ISC license.
See [icon attribution and source](ou_dedetai/img/ATTRIBUTION.md) and the included
[Lucide license](ou_dedetai/img/lucide-LICENSE.txt).

Development of this baseline used AI assistance through OpenCode, with the scoped
validation and independent review recorded privately by the maintainer.
