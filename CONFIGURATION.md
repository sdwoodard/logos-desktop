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

Existing installations and external prefixes stay where they are. The new portable
configuration name is `logos-desktop.json` in the installation directory.
Legacy `oudedetai.json` and FaithLife-Community/Logos_on_Linux configuration names
remain readable inputs. Startup does not replace them with the new default.

When the default is missing, normal graphical startup opens an unconfigured Home.
Discover legacy configurations, explicitly select a result, or choose a legacy
configuration file or installation directory to adopt. Discovery does not select
the first result. Confirm a separate durable destination before adoption. Explicit
CLI actions with a missing default report candidates and require a selection.

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

### Explicit adoption

Adoption reads JSON, supported shell assignments or a portable configuration as
data. It never executes shell input. A supplied installation directory is checked
for portable configuration first. Without one, reconstruction supports the
conventional `data/wine64_bottle` layout with one identifiable product and Wine
user. Multiple Wine users or ambiguous data profiles require resolution rather
than a guessed selection. Reconstruction does not establish Wine compatibility or
invent a runtime selection. Review unknown diagnostics and select a runtime
separately when needed.

Examples below use absolute placeholder paths. Run one action per invocation:

```bash
logos-desktop --discover-installations
logos-desktop -c /absolute/path/legacy.json --discover-installations
logos-desktop --adopt-installation /absolute/path/legacy.json --adoption-destination /absolute/path/profile.json --confirm-adoption
logos-desktop --adopt-installation /absolute/path/installation --adoption-destination /absolute/path/profile.json --confirm-adoption
logos-desktop -c /absolute/path/profile.json --dialog qt
```

`--adoption-destination` selects the new file. For adoption, `-c` can also supply
the destination if that option is absent. The default destination is the new
default configuration path. Adoption refuses an existing destination unless it
is an unchanged publication with a matching committed adoption journal. It leaves
the original configuration and library in place and does not install, download,
move data or accept third-party terms. Invocation overrides are not silently saved
as the adopted selection.

The private, versioned journal is under
`$XDG_STATE_HOME/logos-desktop/adoption/<destination-hash>.json`, with the normal
state fallback above. The operation prints its exact pathname. It records original
configuration bytes and the published file identity before no-clobber publication.
Keep this journal for interrupted adoption and rollback:

```bash
logos-desktop --rollback-adoption /absolute/path/to/adoption-journal.json --confirm-adoption
```

Rollback removes only the unchanged controller-owned publication. Later edits or
replacement files are preserved and reported as conflicts, with the journal
retained. Resolve those conflicts before retrying adoption. Rollback never rewinds
the library, removes the external prefix or undoes Wine/MSI changes. Reverting
source code does not roll back on-disk adoption.

### Direct legacy selection and ordinary saves

Selecting a legacy file with `-c` is different from adopting it to a separate file.
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
Adoption itself publishes only the separate selected profile, not a portable copy.

If another writer changes the selected installation context, reload before saving.
Malformed configuration is reported without silently replacing it with defaults.
Do not select a recovery-copy pathname as the active profile if it conflicts with
that installation's normal save destinations.

## Desktop integration and controller ownership

Adoption does not add desktop launchers. Qt offers a separate confirmed integration
action. CLI `--create-shortcuts` explicitly requests integration, which also runs
as part of installation. These paths create profile-specific desktop IDs with
an exact `-c` argument. URL handlers route to that profile. Existing unrelated MIME
defaults are preserved. Uninspectable defaults or unconfirmed routing are reported
as incomplete integration, independently of configuration adoption success.

Desktop integration has its own journal under the account's
`~/.local/state/logos-desktop/coordination/desktop/` directory. Qt Settings offers
separate adoption and desktop-integration rollback actions. The CLI
`--rollback-adoption` accepts adoption journals, not desktop journals. Desktop
rollback preserves later file/default changes and retains a handler when needed
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

`logos-desktop -c profile.json --status` inspects the selected prefix without Wine,
installation recovery or configuration changes. Support, editing and Stop also
remain available while restore/runtime recovery records are pending. `--doctor`
performs read-only diagnostics without repairing, saving configuration, staging a
runtime or executing a configured binary. Runtime version compatibility and
inaccessible process state remain unknown when inspection cannot establish them.
See [support](SUPPORT.md) for the exact sanitized preview and export workflow.

```bash
logos-desktop -c /absolute/path/profile.json --doctor
logos-desktop -c /absolute/path/profile.json --update-status
logos-desktop -c /absolute/path/profile.json --update-status --check-for-updates
logos-desktop -c /absolute/path/profile.json --update-installed-app
```

Update presentation distinguishes unchecked, stale, failed or malformed lookup,
no stable release, unknown installed version, available update and current version.
An empty or failed check is not proof that Logos is current. Checking explicitly
refreshes stable metadata. Updating requires a usable fresh stable target newer
than the installed version. The installed version is reread under mutation
admission and conflicting effective configuration is refused before installation.
LogosDesktop self-updates remain disabled during private development.

`logos-desktop -c profile.json --repair` explicitly requests journal reconciliation
and failed-upgrade repair. Its confirmation names the selected prefix. A normal
launch does not silently reinstall a broken installation. Stop does not require
the top-level product executable and only signals processes whose user, prefix and
executable membership can be verified.

Pending recovery blocks mutating installation work. Runtime rollback preserves
unexpectedly changed files and retains its recovery record instead of overwriting
them. Repair can invoke an installer and is not a transactional rollback of all
Wine or MSI changes. Credentials and third-party terms still require user action.
