# Release notes

## 1.0.3

- Logos' System theme follows the desktop preference during graphical setup and launch.
- Logos taskbar entries match the Windows application and use its locally installed icon.
- Fresh setup enables WPF software rendering for startup and sign-in windows under Wine.
- Browser sign-in returns use the selected Logos profile and the controller's stable launcher.
- Controller updates show progress in the app and restart automatically after the confirmation popup.
- Failed controller startup restores and reopens the previous environment.
- Wine launches preserve the installation's Windows user when changing runtimes.
- Direct Proton Wine launches find their packaged graphics dependencies.
- Desktop-integration upgrades preserve later user edits and recover interrupted publication.

## 1.0.2

- Recommended Wine selects the latest available stable AppImage from mmtrt/WINE_AppImage.
- Stable runtime metadata has a separate cache identity from earlier development recommendations.
- Existing runtime selections are preserved when updating the controller.
- Runtime updates preserve a selected Wine version newer than the stable recommendation.

## 1.0.1

- Recommended Wine uses the available development AppImage from mmtrt/WINE_AppImage.
  Setup no longer requests an unavailable version from the inherited provider.
- Wine downloads require SHA-256 release metadata. Provider lag no longer makes a
  newer Wine source announcement block setup.
- Quick install provisions a private verified Python runtime when needed and
  installs Qt dependencies automatically. curl 8.4 is no longer required.

## 1.0.0

Prepared for the first LogosDesktop release. Availability is recorded on the
[release page](https://github.com/sdwoodard/logos-desktop/releases).

### Daily use

- Qt control panel with system-following, light and dark themes.
- Single-line shell installation with command, PATH and application-menu setup.
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

### Before changing an installation

Read [installation](docs/INSTALL.md), [backup guidance](docs/BACKUPS.md) and
[runtime compatibility](docs/COMPATIBILITY.md). Controller removal preserves the
separate library and configuration.

Known limits include variable scrolling delay, graphics/font issues and
unvalidated print/export. No universal Wine or distro compatibility is claimed.
