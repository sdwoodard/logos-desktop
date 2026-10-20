# Configuration and recovery

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
`CONFIG_FILE` environment value selects the profile. Otherwise the retained
FaithLife-Community default is used. Paths are expanded and made absolute once at
startup. An explicitly selected missing file starts an empty profile.

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

Default legacy discovery reads the original file without moving it. A deliberate
setting save migrates that data to the default JSON destination while retaining
the source. A concurrently created destination causes a reload-required error.
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

## Inspection and repair

`logos-desktop -c profile.json --status` inspects the selected prefix without Wine,
installation recovery or configuration changes. Support, editing and Stop also
remain available while restore/runtime recovery records are pending. Support output
is local and can contain private configuration and logs. AppImage execution is
skipped during support collection so a probe cannot import a runtime into the prefix.

`logos-desktop -c profile.json --repair` explicitly requests journal reconciliation
and failed-upgrade repair. Its confirmation names the selected prefix. A normal
launch does not silently reinstall a broken installation. Stop does not require
the top-level product executable and only signals processes whose user, prefix and
executable membership can be verified.

Pending recovery blocks mutating installation work. Runtime rollback preserves
unexpectedly changed files and retains its recovery record instead of overwriting
them. Repair can invoke an installer and is not a transactional rollback of all
Wine or MSI changes. Credentials and third-party terms still require user action.
