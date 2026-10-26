# Release notes

## 1.0.0 candidate, unreleased

The first LogosDesktop release is being prepared privately. No public release
or AUR submission is available yet.

### Daily use

- Qt control panel with system-following, light and dark themes.
- Explicit existing-install adoption, profile selection and desktop integration.
- Separate controller, Logos and Wine update operations.
- Doctor, previewable local support export and clearer failure outcomes.
- CLI and terminal interface through the same installation services.

### Data and installation handling

- Downloads are checked before installer execution, with existing good files preserved on transfer failure.
- Backup/restore preflight and recoverable replacement failures.
- Profile-bound process ownership and bounded controller cleanup.
- Managed wheel environments and Arch source package update handoffs.
- The `logos_desktop` namespace and one `logos-desktop` controller command.

### Before upgrading

Read [installation and migration](INSTALL.md), [backup guidance](BACKUPS.md) and
[runtime compatibility](COMPATIBILITY.md). Older private managed wheel builds
need explicit verified migration. Controller removal preserves the separate
library and configuration. Flatpak and standalone binaries remain deferred.

Known limits include variable scrolling delay, graphics/font issues and
unvalidated print/export. No universal Wine or distro compatibility is claimed.
Earlier development history and contributor authorship remain in Git.
