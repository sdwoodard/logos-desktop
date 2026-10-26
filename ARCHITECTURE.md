# Architecture

## Frontends and dispatch

`main.py` selects the graphical control panel on Wayland or X11 and the Textual
panel in a headless terminal. `commands.py` handles discovery, adoption, rollback,
doctor, update status, snapshot listing/selection and support preview/export before
normal configuration startup, logging and recovery. Other explicit actions use
`cli.py`, with graphical install and repair routed through Qt when selected.
`--dialog qt` and `--dialog tui` select an interface. The retained
`tk` spelling is an alias for Qt and does not load a Tk implementation.

Install the `gui` extra for graphical use. PySide6 Essentials supplies the Qt
Widgets, GUI, Core and D-Bus components. A missing optional GUI dependency produces
an actionable message. CLI help, status and recovery remain usable without it.

## Qt boundary

`qt/startup.py` owns the application, theme and window lifetime. `qt/app.py`
adapts the shared `App` interactions to owned workers. `qt/bridge.py` delivers
queued results, bounded progress and independent process observations.
`qt/dialogs.py` resolves each prompt once on confirmation, rejection or close.

Pages render immutable installation snapshots. `qt/services.py` performs discovery,
inspection and operations away from the GUI thread. Installation controls remain
draft values until confirmed. Qt exposes Logos and latest stable releases, with
explicit refusal of conflicting invocation and custom-installer overrides.
Maintenance and settings use the same backend services as the terminal frontends.

`qt/theme.py` persists System, Light or Dark independently of installation profiles.
System follows Qt palette/scheme events and the asynchronous desktop appearance
portal. Semantic colors update without polling desktop configuration. Most controls
retain native styling. The primary action uses a palette-derived fill and text
contrast, with explicit disabled and focus states.

## Domain services

| Module | Responsibility |
| --- | --- |
| `app.py` | Interaction contract, owned workers, subscriptions and shutdown |
| `config.py`, `config_store.py` | Precedence, profile projection, migration and atomic merged saves |
| `adoption.py` | Bounded read-first discovery, explicit separate-profile publication and conflict-preserving rollback |
| `instance.py` | Canonical-prefix controller ownership, profile transfer and bounded local activation |
| `desktop.py` | Profile-bound desktop entries, serialized MIME integration and separate rollback |
| `doctor.py`, `support.py` | Read-only structured diagnostics, immutable sanitized snapshots and explicit archive publication |
| `maintenance.py` | Prefix admission, role leases, quiescence and recovery exclusions |
| `logos.py`, `processes.py` | Selected process identity, observations and bounded helper execution |
| `installer.py`, `wine.py`, `runtime.py` | Checked installation, Wine commands and recoverable runtime selection |
| `artifacts.py`, `network.py`, `metadata.py`, `releases.py`, `updates.py` | Accepted artifacts, bounded metadata and controller updates |
| `backup.py`, `backup_store.py` | Verified snapshots and recoverable user-data replacement |
| `control.py`, `repair.py`, `database.py` | Maintenance, explicit recovery and owned database policies |

Configuration writes reload and patch the selected file. Operation-scoped validators
inspect the merged document before publication. Runtime activation adopts the exact
document it validated and published rather than rereading a mutable file. Recovery
preserves unexpected changes instead of guessing which data may be overwritten.

## Configuration, adoption and admission

New config, state, data and cache children use `logos-desktop` under absolute XDG
bases or standard home fallbacks. The Python namespace is `logos_desktop` and
the controller command is `logos-desktop`. Explicitly selected legacy files remain
readable inputs. Existing prefixes are not relocated. See
[configuration and adoption](CONFIGURATION.md) for paths and CLI examples.

Adoption discovery does not execute configuration or activate Wine. Explicit
selection and confirmation precede separate no-clobber configuration publication.
The versioned state journal records intent, original bytes and publication identity.
It is not a transaction over the library. Ordinary profile/portable saves and
desktop/MIME integration have separate publication boundaries and failure outcomes.
Rollback preserves later edits and retains conflict records.

Controller coordination is anchored to the actual account home at
`.local/state/logos-desktop/coordination`, independent of invocation HOME and XDG
overrides. Config aliases resolving to one canonical prefix share ownership.
Unconfigured controllers use a config identity. Profile transfer reserves the
destination before releasing the source. Controller ownership precedes prefix
admission, artifact/runtime admission and configuration writer locks. Shared desktop
integration uses a user-wide lock and must not acquire prefix ownership while
holding it. Existing recovery roots remain readable. Unknown historical XDG roots
are not found through a filesystem-wide scan.

A plain graphical duplicate can request focus over a private bounded local socket.
Explicit commands refuse conflicting ownership rather than becoming focus requests.
Read-only diagnostics bypass controller ownership. Scoped URL callbacks use
operation admission without creating a second controller or logging the URL.
Unmodified legacy binaries do not participate in these locks. Known active or
relevant unscopable legacy controllers require manual closure. Direct concurrent
legacy-binary use of the same prefix is unsupported.

## Diagnostics and operation outcomes

Doctor inspects stored paths, architecture, runtime headers, a vetted system Wine
command preview, free space, backup-folder availability, process and recovery state.
It does not repair or execute configured runtimes. A valid ELF header does not prove
Wine version compatibility. Unavailable facts remain unknown. Adoption refuses
ambiguous Wine-user layouts instead of choosing an arbitrary user.

Support freezes allowlisted structured diagnostics. Preview renders every archive
member's exact text. Export validates the closed ZIP against that same snapshot
before private atomic publication. Raw config, logs, environment, cache and library
content are omitted. See [support](SUPPORT.md) for output-path and consent rules.

Snapshot listing reads metadata rather than hashing every payload. Restore verifies
the selected snapshot fully before exact source/target confirmation and recoverable
replacement. Missing historical source metadata remains unknown. See
[backups and restores](BACKUPS.md).

Stable product update state keeps stale, unchecked, failed, empty and unknown
installed-version results distinct from current or available. Update admission
refreshes installed metadata and validates effective configuration against the
checked newer stable target. Public controller feeds and standalone replacement
remain disabled. Explicit private providers use separate wheel-environment and
Arch owner transactions. Cancellation propagates through long reads, hashes and scans, while
safe commit boundaries preserve completed outcomes and pending recovery work.

Normal controller close cancels prompts and observations, waits for owned mutations
to settle and detaches Logos. Explicit Stop validates selected process identities.
An indexer launch is distinct from its observed completion. Unknown worker exit
status is retained rather than inferred from a successful launcher exit.
