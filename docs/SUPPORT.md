# Diagnostics and support

Collect diagnostics locally, review the preview and share an archive only
with the intended recipient. Creating an archive does not upload it.

See [configuration and recovery](CONFIGURATION.md), [backups](BACKUPS.md) and the
[architecture map](ARCHITECTURE.md) for related behavior. Redistribution requirements
are described in [license notices](NOTICES.md) and [LICENSE](../LICENSE).

## Start with the symptom

| Symptom | Next step |
| --- | --- |
| Command is missing or opens another installation | Use the stable launcher for your selected installation, as described below |
| Home has no installation selected | Choose Set up Logos, or select a saved LogosDesktop profile in Settings |
| An older controller blocks startup | Close that selected controller normally. Do not kill unrelated Wine processes or delete ownership locks |
| Controller update is unavailable | Check the installation provider on Home. An offline service is different from a current installation |
| Wine recommendation is unavailable | Check COMPATIBILITY.md and the actual provider result. Do not substitute an arbitrary older runtime |
| Restore or runtime recovery reports a conflict | Preserve the reported recovery journal and previous data. Inspect the conflicting paths before retrying |
| Logos opens but scrolling or rendering is poor | Read the compatibility notes and record the selected Wine, GPU/driver and reproduction |

Report a reproducible problem in the project's
[issues](https://github.com/sdwoodard/logos-desktop/issues).
Do not post unreviewed archives or credentials.

## Run a read-only check

First [select your controller launcher](INSTALL.md#select-your-launcher-for-shared-commands).

```bash
"$controller" -c /absolute/path/profile.json --status
"$controller" -c /absolute/path/profile.json --doctor
```

`--status` inspects the selected installation. `--doctor` reports architecture,
installation/prefix/backup-folder availability, runtime-header inspection, available
vetted Wine command preview, free space, recovery records and selected-prefix
process state. Qt offers the same doctor report. Neither doctor nor support
collection repairs the installation, stages Wine, saves configuration or executes
the configured runtime. A command preview is not an executed compatibility test.

Unknown runtime compatibility or inaccessible process state stays unknown. A backup
folder being available does not prove that its snapshots are valid. Use the
[verified restore workflow](BACKUPS.md) for payload validation. `--repair` is a
separate explicit action that can change files and invoke an installer.

## Preview and export

In Qt, choose **Create support archive…** on **Help & logs**. The controller presents
the exact sanitized snapshot, asks for export confirmation, then asks for an output
path and separate permission to replace an existing file. The interactive CLI
`--get-support` uses the same workflow and refuses assume-yes.

For an explicit CLI output path:

```bash
"$controller" -c /absolute/path/profile.json --support-preview
"$controller" -c /absolute/path/profile.json --support-export /absolute/private/folder/support.zip --confirm-export
```

`--support-preview` prints every member name and its text without exporting.
`--support-export PATH` prepares and prints its own exact snapshot before writing.
It requires `--confirm-export`, independently of `--assume-yes`. A separate preview
invocation is a separate inspection, so facts can change between commands. The
archive always contains the snapshot printed by its export invocation.

Existing output requires another explicit option:

```bash
"$controller" -c /absolute/path/profile.json \
  --support-export /absolute/private/folder/support.zip --confirm-export --overwrite-export
```

Choose a `.zip` path in an existing owned directory that is not writable by other
users. Output cannot overlap protected configuration, logs, installation, prefix,
backup, selected runtime, cache or state paths. Symlink directories and linked or
unowned existing outputs are refused. The archive is created privately, closed,
validated against the preview and atomically published. There is no automatic
overwrite or raw-file staging.

## Archive contents

The current sanitized archive contains only:

| Member | Contents |
| --- | --- |
| `manifest.json` | Format identity, member hashes and sizes, and explicit omissions |
| `doctor.json` | Structured doctor facts and fixed guidance |
| `summary.txt` | The same read-only diagnostic summary |

Raw configuration, logs, crash reports, environment dumps, network cache, library
databases, notes and user paths are omitted. The exporter does not attempt to make
unknown free-form logs safe by replacing a few secrets. **Open log** is a separate
local viewing action, not part of sanitized export. Review any manually shared raw
log separately.

When requesting help, describe the action, expected result, observed result and
whether the problem repeats. Include the LogosDesktop build/version, Linux
distribution and relevant doctor outcome when known. Keep credentials, login URLs
and private library content out of the report. Preserve historical migration, runtime
or restore journals when an operation reports a conflict or pending recovery.
