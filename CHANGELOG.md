# Release notes

## 1.0.0

Prepared for the first LogosDesktop release. Availability is recorded on the
[release page](https://github.com/sdwoodard/logos-desktop/releases).
AUR packaging is deferred beyond the first-release work.

### Daily use

- Qt control panel with system-following, light and dark themes.
- Editable recommended installation location, saved-profile selection and desktop integration.
- Home update cards and grouped backup, repair, indexing and runtime tools.
- Separate controller, Logos and Wine update operations.
- Doctor, previewable local support export and clearer failure outcomes.
- CLI and terminal interface through the same installation services.

### Data and installation handling

- Downloads are checked before installer execution, with existing good files preserved on transfer failure.
- Backup/restore preflight and recoverable replacement failures.
- Profile-bound process ownership and bounded controller cleanup.
- Managed wheel environments with installation-aware update handoffs.
- Explicit installation-scoped public provider setup that preserves existing configuration.
- The `logos_desktop` namespace and one `logos-desktop` controller command.

### Before upgrading

Read [installation](docs/INSTALL.md), [backup guidance](docs/BACKUPS.md) and
[runtime compatibility](docs/COMPATIBILITY.md). Older private managed wheel builds
need explicit verified migration. Controller removal preserves the separate
library and configuration. Flatpak and standalone binaries remain deferred.

Known limits include variable scrolling delay, graphics/font issues and
unvalidated print/export. No universal Wine or distro compatibility is claimed.
Earlier development history and contributor authorship remain in Git. Legacy
installation discovery and adoption are retired. Existing user data is not removed.
