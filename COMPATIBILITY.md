# Runtime compatibility during private development

These observations are from native CachyOS testing on 2026-10-26. They are not a
supported distribution or hardware matrix. LogosDesktop remains private.
The managed wheel environment is the launch installation route. Earlier private
Arch source-package checks do not establish an available AUR package. AUR is
deferred beyond launch. Final artifacts retain their own migration/update validation.

## Current runtime policy

Recommended Wine follows the latest published development release. Wine's stable
release is identified separately. A provider's GitHub prerelease flag does not
determine either Wine channel.

The tested targets are Wine development **11.20**, DXVK **3.1.1** and Logos
**54.3.0.5** with its bundled .NET/WPF **10.0.12**. The standalone Winetricks
default is **20260125**. An AppImage's embedded Winetricks belongs to that provider
and can differ from the standalone default.

The community AppImage provider did not have an exact Wine 11.20 build at the
last check. LogosDesktop reports that route as unavailable instead of silently
substituting an older build. An existing matching native Wine installation can
be selected. LogosDesktop does not automatically upgrade system packages or
search private development directories for a runtime.

Installations no longer force Wine's GDI renderer. They leave an existing renderer
selection untouched and use Wine's default when no selection exists. Updating
LogosDesktop does not automatically remove GDI from an older prefix.

## Observed functionality

On the tested Wine 11.20 setup, Logos 54 opened its Dashboard, Library and a local
Bible. A disposable Logos installation updated from 46.2.0.5 to 54.3.0.5. The same
Logos process also survived controller closure, was detected by a new controller
without a duplicate launch, and stopped through the controller.

These are bounded journeys, not certification of every Logos feature, library,
printer, graphics driver or Wine build. Preserve a recoverable backup before
upgrading an existing prefix. Do not downgrade a prefix that a newer Wine has
already upgraded.

## Graphics and scrolling limitations

DXVK is optional and is not bundled or installed automatically. The DXVK 3.1.1
comparison used its official AMD64 D3D9 component in a disposable prefix. Follow
the [DXVK project's setup and requirements](https://github.com/doitsujin/dxvk)
if evaluating it separately. Do not replace system graphics libraries.

- Scrolling delay remains variable. The latest DXVK comparison did not show a
  repeatable improvement over WineD3D across cold starts. No general latency or
  acceleration guarantee is made.
- Some settings popups show a black lower fringe with accelerated rendering. The
  tested choices remained readable and selectable, including Escape and reopening.
  The fringe also occurred with default WineD3D on the current tested stack.
- On the tested NVIDIA/AMD dual-GPU machine, unfiltered DXVK produced black public
  WPF content and Vulkan presenter errors. A process-local DXVK device filter
  restored that public rendering. This is not a universal NVIDIA preference or a
  reason to change global GPU settings. Consult the upstream device-filter
  documentation and identify the appropriate device on the actual machine.
- Print/export is not validated. A candidate window disappeared during a print
  check, without an established cause.
- Public font controls retained missing-glyph and catalog errors. Successful Bible
  rendering does not certify every font or writing system.
- A shared-texture diagnostic under Wine 11.20 and DXVK 3.1.1 reopened the resource
  but returned incorrect consumer pixels. Functional texture sharing is unproven.

These residual tool/application compatibility defects remain known limitations.
They do not permit reporting a failed installation as successful, bypassing
artifact validation, or changing another installation's files or processes.

## Scope and troubleshooting

Record the selected Wine and wineserver versions, Wine provider, Logos version,
DXVK version if used, GPU/driver, kernel, distribution and reproduction steps.
Keep account data and library content out of public reports. Use LogosDesktop's
local support archive for a reviewed, private diagnostic handoff.

Disabling `mscoree` globally caused a test-fixture startup failure with current
.NET. Normal LogosDesktop launches do not disable it by default. A fixture-specific
retry is not a universal Wine-version workaround. Review explicit DLL overrides
before attributing such a failure to Wine, DXVK or the kernel.

Wine 10.8 observations remain historical evidence, not current-runtime acceptance.
Container source probes are experimental and do not establish desktop support.
Flatpak is deferred. Public release availability remains separate from candidate testing.
