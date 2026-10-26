# Backups and restores

Select the intended [configuration](CONFIGURATION.md) first. For read-only
diagnostics and sanitized archives, see [support](SUPPORT.md).

Close Logos and its indexer normally before backing up or restoring. The controller
refuses maintenance when it finds relevant running processes or another operation
using the selected installation. It does not stop unrelated Wine applications.

Use `logos-desktop --backup` or `logos-desktop --restore`. Choose an existing folder
outside the live product data. Decline the saved folder prompt to select another
location. A new location needs interactive confirmation on its current filesystem
before it can be used with assume-yes. Reconnect a missing backup disk rather than
recreating its mount directory.

A saved relative backup path resolves against the saved installation directory.
Qt Settings accepts an existing absolute folder. Folder-opening actions report a
missing location instead of creating it.

First [select your controller launcher](INSTALL.md#select-your-launcher-for-shared-commands).

```bash
"$controller" -c /absolute/path/profile.json --backup
"$controller" -c /absolute/path/profile.json --list-backups
"$controller" -c /absolute/path/profile.json --restore
"$controller" -c /absolute/path/profile.json --restore-snapshot /absolute/path/backups/snapshot-name
```

`--list-backups` reads the configured folder's completed snapshot metadata. The
CLI listing shows creation time, logical file-byte total, included roots and
recorded source. This size is not allocated disk usage. The Qt restore chooser
lists snapshot names. Its later confirmation shows creation time, recorded source
and target after verifying the selection.
New snapshots record the source product-data directory. Older valid manifests
without that field display an unknown source, not the current installation path.
Metadata listing is not a full payload-integrity check.

`--restore-snapshot` selects the snapshot container, not its `complete` child.
The controller fully verifies the chosen payload before asking for confirmation
with the exact snapshot, recorded source and live target. In this explicit CLI
workflow, type `restore /absolute/path/backups/snapshot-name` exactly as prompted.
Closed input or a different answer refuses restore. Assume-yes does not replace
this exact-source confirmation.

Snapshots contain the product's `Data`, `Documents` and `Users` roots, including
explicitly absent roots and empty directories. They do not contain the whole Wine
prefix, application binaries or controller configuration. Keep a separate complete
installation backup when changing Wine, installing or migrating an installation.

Each completed snapshot has a unique container with a `complete` payload and a
versioned SHA-256 manifest. Do not edit its files. Restore verifies the selected
snapshot and stages its complete replacement before moving any current data.
Existing snapshots are never merged or overwritten. Links, special files and
nested data mounts are refused with a path-specific error.

Older folders without completion metadata remain untouched and are not offered
for automatic restore. The old format could leave a partial copy looking complete.
Keep those folders for separately reviewed recovery rather than adding a completion
marker by hand.

Copy and preflight failures leave the current data in place. Replacement failures
attempt rollback. Interrupted replacement keeps a journal and original data under
a private `.logos-desktop-restore-*` directory beside the live product directory.
Startup retries recovery before installation repair. Writers remain blocked if
recovery cannot prove the correct file identities. Keep the reported journal and
recovery directories until the problem is resolved.

A committed restore can report that cleanup remains. In that case the new data is
active and startup retries cleanup. Do not delete recovery records to bypass the
guard. A support ZIP is closed and validated before the controller offers it as
ready. It contains sanitized diagnostics, not a library backup or raw logs. See
[support archive contents](SUPPORT.md#archive-contents).

Cancellation during copying, hashing or verification stops at a safe boundary.
Completed publication is not relabeled cancelled. Earlier completed steps remain,
and interrupted replacement still follows the recovery journal rules above.
