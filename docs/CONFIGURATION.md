# Configuration and recovery

See also [backups and restores](BACKUPS.md), [diagnostics and support](SUPPORT.md)
and the [architecture map](ARCHITECTURE.md).

## Interfaces

The desktop control panel uses Qt. Install `logos-desktop[gui]` for graphical use,
or select the Textual panel with `--dialog tui`. The old `--dialog tk` value remains
an alias for Qt. CLI actions such as `--status`, backup and Stop do not initialize
a graphical application.

The Qt interface manages Logos and uses the latest checked stable release.
Product/controller channels and manual release selection are not graphical
preferences. Conflicting version, channel or custom installer-source overrides
are refused before graphical installation, update or reinstalling repair.
Wine runtime selection remains a separate advanced setting.

## Choosing an installation

`logos-desktop -c /absolute/path/profile.json` selects that profile for reads,
saves, editing, support export and installation actions. Without `-c`, an explicit
`CONFIG_FILE` environment value selects the profile. Otherwise the new
`$XDG_CONFIG_HOME/logos-desktop/config.json` default is used. Paths are expanded
and made absolute once at startup. An explicitly selected missing file starts an
empty profile in normal controller startup. Read-only CLI diagnostics require an
existing file.

Absolute XDG bases are honored. Missing, empty or relative XDG values fall back to
the standard home locations:

| Purpose | Default location |
| --- | --- |
| Configuration | `~/.config/logos-desktop/config.json` |
| State and logs | `~/.local/state/logos-desktop/` |
| Data | `~/.local/share/logos-desktop/` |
| New installation proposal | `~/.local/share/logos-desktop/installation/` |
| Cache | `~/.cache/logos-desktop/` |

Existing installations and external prefixes stay where they are. The portable
configuration name is `logos-desktop.json` in the installation directory.
Explicitly selected legacy JSON and shell-assignment files remain readable inputs.
Use an explicit profile path when needed. Startup does not import another controller's
installation or replace its files with the default profile.

When the default is missing, Home offers **Set up Logos**. The installation form
shows the recommended location and lets you edit it before confirmation. To reopen
a configured LogosDesktop installation, choose its profile in Settings. Explicit
CLI diagnostics require an existing profile and do not create one automatically.

Settings resolve in this order:

1. Explicit command-line value.
2. Explicitly supplied environment value.
3. Saved configuration.
4. Default.

Modern names win over legacy aliases within one saved file. False values and an
empty `WINEDEBUG` are real values. Empty paths and invalid setting types are errors.
Ordinary saves preserve unspecified fields. Command-line/environment overlays stay
transient. An explicit Wine runtime activation deliberately saves its runtime and
effective installation/prefix/binary-directory selection together. That activated
runtime wins for the current controller invocation. A later fresh invocation
applies its supplied environment again.

Persisted relative backup, Wine, prefix, binary-directory, server and log paths are
relative to the saved installation directory. Absolute external paths stay external.
Relative paths supplied through command-line/environment overrides are resolved
at invocation. `WINESERVER_EXE` selects both the server command and Wine environment
override. Without it, the selected runtime supplies its companion.

## Legacy files and saves

Legacy installation discovery, import and reconstruction are not supported. Existing
data is not removed. Keep any journals from earlier development builds and use the
controller that created an unfinished migration for recovery, or request help.

### Direct legacy selection and ordinary saves

Selecting a supported legacy file with `-c` keeps that file as the active profile.
An explicitly selected `.conf` remains the selected pathname, and its first save
converts it to JSON. JSON is recognized by content on restart. Shell assignments
are parsed as data, never executed. Unsupported shell expressions are rejected.

Successful uninstall ends any pending migration relationship for the removed
profile. The controller keeps that selected pathname as a coherent empty-profile
read/write target for later setup. It does not load or overwrite a competing
default destination.

The first conversion retains original shell bytes in `.<filename>.legacy-original`.
Later saves retain the previous valid bytes in `.<filename>.previous`. Both copies
are private files. Keep migration originals until satisfied with the new settings.
Conflicting source, destination or recovery-copy paths are refused.

Saves serialize reload-and-patch updates, stage complete JSON beside each target
and replace files atomically. The selected profile is authoritative. A portable
copy under the saved installation is secondary. A portable-copy failure after the
primary commit is reported explicitly. The primary stays saved, and the previous
portable copy remains readable. A directory-sync failure is reported as unconfirmed
durability. These outcomes do not claim atomicity across two filesystems.

If another writer changes the selected installation context, reload before saving.
Malformed configuration is reported without silently replacing it with defaults.
Do not select a recovery-copy pathname as the active profile if it conflicts with
that installation's normal save destinations.

## Desktop integration and controller ownership

The controller installer creates a generic LogosDesktop application-menu entry and
the logos-desktop command. The generic entry opens the default selected configuration.
Its setup journal belongs to the controller root. This is separate from the
profile-specific launchers below. See [installation setup](INSTALL.md#installed-locations-and-path).

Use **Backups & tools → Repair & shortcuts** to add desktop launchers after reviewing
the selected profile. CLI `--create-shortcuts` also requests integration, which runs
as part of installation. These paths create profile-specific desktop IDs with
an exact `-c` argument. URL handlers route to that profile. Existing unrelated MIME
defaults are preserved. Uninspectable defaults or unconfirmed routing are reported
as incomplete integration.

Desktop integration has its own journal under the account's
`~/.local/state/logos-desktop/coordination/desktop/` directory. Qt Settings offers
**Undo desktop integration** under advanced installation settings. Desktop rollback
preserves later file/default changes and retains a handler when needed
by a conflicting MIME association.

Supported entry points share one controller owner per canonical Wine prefix,
even with different config aliases or HOME/XDG overrides. Plain duplicate graphical
starts request focus from the owner. Explicit actions refuse conflicting ownership.
Read-only diagnostics remain available. Known active or relevant unscopable legacy
controllers must be closed manually. Direct concurrent execution of an unmodified
legacy binary against the same prefix is unsupported.

Normal controller close waits for owned mutations to settle and detaches Logos.
Use explicit Stop or `--stop-installed-app` to stop verified selected product
processes, or `--stop-indexing` for the selected indexer.

## Inspection, updates and repair

Home has separate **Logos updates** and **LogosDesktop updates** cards. Runtime,
indexing, backup and repair tasks are grouped under **Backups & tools**.
First [select your controller launcher](INSTALL.md#select-your-launcher-for-shared-commands)
for the command examples below.

`logos-desktop -c profile.json --status` inspects the selected prefix without Wine,
installation recovery or configuration changes. Support, editing and Stop also
remain available while restore/runtime recovery records are pending. `--doctor`
performs read-only diagnostics without repairing, saving configuration, staging a
runtime or executing a configured binary. Runtime version compatibility and
inaccessible process state remain unknown when inspection cannot establish them.
See [support](SUPPORT.md) for the exact sanitized preview and export workflow.

```bash
"$controller" -c /absolute/path/profile.json --doctor
"$controller" -c /absolute/path/profile.json --update-status
"$controller" -c /absolute/path/profile.json --update-status --check-for-updates
"$controller" -c /absolute/path/profile.json --update-installed-app
```

Update presentation distinguishes unchecked, stale, failed or malformed lookup,
no stable release, unknown installed version, available update and current version.
An empty or failed check is not proof that Logos is current. Checking explicitly
refreshes stable metadata. Updating requires a usable fresh stable target newer
than the installed version. The installed version is reread under mutation
admission and conflicting effective configuration is refused before installation.
Standalone self-replacement remains disabled. Explicitly configured providers
support the separate [owner update routes](INSTALL.md). Provider setup selects
the public endpoints, whose availability depends on release/package publication.

`logos-desktop -c profile.json --repair` explicitly requests journal reconciliation
and failed-upgrade repair. Its confirmation names the selected prefix. A normal
launch does not silently reinstall a broken installation. Stop does not require
the top-level product executable and only signals processes whose user, prefix and
executable membership can be verified.

Pending recovery blocks mutating installation work. Runtime rollback preserves
unexpectedly changed files and retains its recovery record instead of overwriting
them. Repair can invoke an installer and is not a transactional rollback of all
Wine or MSI changes. Credentials and third-party terms still require user action.
