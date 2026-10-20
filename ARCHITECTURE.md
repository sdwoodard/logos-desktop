# Architecture

## Frontends and dispatch

`main.py` selects the graphical control panel on Wayland or X11 and the Textual
panel in a headless terminal. Explicit command actions use `cli.py` without
importing Qt. `--dialog qt` and `--dialog tui` select an interface. The retained
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

Normal controller close cancels prompts and observations, waits for owned mutations
to settle and detaches Logos. Explicit Stop validates selected process identities.
An indexer launch is distinct from its observed completion. Unknown worker exit
status is retained rather than inferred from a successful launcher exit.
