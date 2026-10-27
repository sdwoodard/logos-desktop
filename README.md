<p align="center">
  <img src="logos_desktop/img/logos-desktop.svg" alt="" width="112" height="112">
</p>

<h1 align="center">LogosDesktop</h1>

<p align="center"><strong>Install and manage Logos on Linux, from one desktop app.</strong></p>

<p align="center">
  <a href="docs/INSTALL.md"><img src="docs/media/linux-badge.svg" alt="For Linux"></a>
  <a href="docs/INSTALL.md"><img src="docs/media/python-badge.svg" alt="Python 3.12 or newer"></a>
  <a href="LICENSE"><img src="docs/media/license-badge.svg" alt="MIT and ISC licensed"></a>
</p>

<p align="center">
  <strong><a href="#install">Install</a></strong> ·
  <a href="docs/README.md">User guide</a> ·
  <a href="CONTRIBUTING.md">Contribute</a>
</p>

LogosDesktop brings setup, updates, backups and everyday maintenance into a Qt
interface. It runs Logos through Wine and keeps your controller and library separate.
This is an independent community project, not affiliated with or endorsed by Logos.

## Install

**Linux x86_64 · Python 3.12+ · curl 8.4+**

Copy this into your terminal to install the latest stable release:

```bash
bash -o pipefail -c '
  curl -fsSL --proto "=https" --proto-redir "=https" --max-time 60 --max-filesize 262144 \
    https://github.com/sdwoodard/logos-desktop/releases/latest/download/install-wheel.py |
    python3 - --latest
'
```

The installer creates a dedicated environment in `~/.local/lib/logos-desktop` and
configures updates. It runs bootstrap code from this project's GitHub release.
The command requires a published release. For unreleased builds, use the source route.

**[Download first](docs/INSTALL.md#download-first)** ·
**[Install from source](CONTRIBUTING.md#source-setup)** ·
**[Requirements and options](docs/INSTALL.md)**

Open the app:

```bash
"$HOME/.local/lib/logos-desktop/logos-desktop" --dialog qt
```

Choose **Set up Logos** on Home. Your Logos account and content licenses are still required.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/media/home-dark.png">
  <img src="docs/media/home-light.png" alt="LogosDesktop Home with setup and update controls" width="960">
</picture>

## Start with your library

Follow the [installation guide](docs/INSTALL.md), then choose **Set up Logos** on
Home. The app suggests a location you can change before installing. Your Logos
account and content licenses are still required.

Release downloads and verified commands appear on
[Releases](https://github.com/sdwoodard/logos-desktop/releases).
Check [requirements and compatibility](docs/COMPATIBILITY.md) before choosing a
Wine runtime. Building from source? Start with [Contributing](CONTRIBUTING.md).

## Keep everyday tasks close

- **Home:** open or stop Logos and check updates for Logos and LogosDesktop.
- **Backups & tools:** protect your data, manage indexing and find repair tools.
- **Help & logs:** run diagnostics and preview a support report before sharing it.
- **Settings:** choose your appearance, backup folder and installation profile.

Controller updates use the installation's managed Python environment. Logos and
Wine updates are separate. Follow the terminal's confirmation and restart steps
before treating an update as complete. See [update guidance](docs/INSTALL.md#wheel-controller-updates)
and [backup and recovery](docs/BACKUPS.md).

## Support

Start with [troubleshooting](docs/SUPPORT.md) and the [known limitations](docs/COMPATIBILITY.md).
For a bug report, include the action, expected result and what happened instead.
Support reports are created locally. Review them before sharing, and keep account
links, passwords and private library content out of public issues.

## Help shape the project

Clear bug reports, useful documentation and focused fixes are welcome.
[Contributing](CONTRIBUTING.md) explains setup, checks and small first tasks.

[User guide](docs/README.md) · [Project direction](docs/ROADMAP.md) · [Release notes](CHANGELOG.md)
