from __future__ import annotations

import logging
import os
import pwd
import sys
from pathlib import Path

from logos_desktop import paths


def get_runmode() -> str:
    """Gets the executing envoirnment
    
    Returns:
        flatpak or snap or binary (pyinstaller) or script
    """
    if os.environ.get("container") == "flatpak":
        return 'flatpak'
    elif getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return 'binary'
    elif os.environ.get('SNAP'):
        return 'snap'
    else:
        return 'script'



# Are we running from binary or src?
RUNMODE = get_runmode()
_snap = os.environ.get('SNAP')
if hasattr(sys, '_MEIPASS'):
    BUNDLE_DIR = Path(sys._MEIPASS)
elif _snap is not None:
    BUNDLE_DIR = Path(_snap)
else:
    # We are running in normal development mode
    BUNDLE_DIR = Path(__file__).resolve().parent
del(_snap)

# Now define assets and img directories.
APP_IMAGE_DIR = BUNDLE_DIR / 'img'
APP_ASSETS_DIR = BUNDLE_DIR / 'assets'

# Define app name variables.
APP_NAME = 'LogosDesktop'
BINARY_NAME = 'logos-desktop'
PACKAGE_NAME = 'logos_desktop'
REPO_NAME = 'logos-desktop'
REPOSITORY_SLUG = f'sdwoodard/{REPO_NAME}'

REPOSITORY_LINK = f"https://github.com/{REPOSITORY_SLUG}"
SUPPORT_LINK = f"{REPOSITORY_LINK}#support"
REPOSITORY_ISSUES_LINK = f"{REPOSITORY_LINK}/issues"
REPOSITORY_NEW_ISSUE_LINK = f"{REPOSITORY_LINK}/issues/new"

# Standalone replacement is deferred. Installed-owner updates use their own providers.
APP_SELF_UPDATES_ENABLED = False
APP_SELF_UPDATE_DISABLED_MESSAGE = (
    "Standalone replacement is disabled. Use the installed controller's owner update route."
)
APP_RELEASE_LINEAGE = "logos-desktop"
APP_VERSION = "1.0.0"
APP_RELEASE_VERSION = APP_VERSION
# A release build must explicitly identify its distribution before self-replacement.
CONTROLLER_DISTRIBUTION = "source"

CACHE_LIFETIME_HOURS = 12
"""How long to wait before considering our version cache invalid"""

if RUNMODE == 'snap':
    _snap_user_common = os.getenv('SNAP_USER_COMMON')
    if not _snap_user_common or not Path(_snap_user_common).is_absolute():
        raise ValueError("SNAP_USER_COMMON must be an absolute path when running a snap.")
    CACHE_BASE = Path(_snap_user_common) / '.cache'
    del _snap_user_common
else:
    CACHE_BASE = paths.xdg_base(os.getenv('XDG_CACHE_HOME'), Path.home() / '.cache')

CACHE_DIR = str(CACHE_BASE / 'logos-desktop')

XDG_DATA_HOME = str(paths.xdg_base(os.getenv('XDG_DATA_HOME'), Path.home() / '.local/share'))
DATA_HOME = str(Path(XDG_DATA_HOME) / 'logos-desktop')
CONFIG_BASE = paths.xdg_base(os.getenv('XDG_CONFIG_HOME'), Path.home() / '.config')
STATE_BASE = paths.xdg_base(os.getenv('XDG_STATE_HOME'), Path.home() / '.local/state')
CONFIG_DIR = str(CONFIG_BASE / 'logos-desktop')
STATE_DIR = str(STATE_BASE / 'logos-desktop')
PORTABLE_CONFIG_NAME = 'logos-desktop.json'
# Recovery roots already used this child before the identity migration.
RECOVERY_STATE_DIR = str(STATE_BASE / 'logos-desktop')
COORDINATION_DIR = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/logos-desktop/coordination'

# Set other run-time variables not set in the env.
DEFAULT_CONFIG_PATH = str(Path(CONFIG_DIR) / 'config.json')
DEFAULT_APP_WINE_LOG_PATH = os.path.expanduser(f"{STATE_DIR}/wine.log")
DEFAULT_APP_LOG_PATH = str(Path(STATE_DIR) / 'logos-desktop.log')
NETWORK_CACHE_PATH = f"{CACHE_DIR}/network.json"

RELATIVE_BINARY_DIR = "data/bin"

DEFAULT_WINEDEBUG = "err+all"
LEGACY_CONFIG_FILES = [
    str(CONFIG_BASE / 'FaithLife-Community/Logos_on_Linux.json'),
    str(CONFIG_BASE / 'Logos_on_Linux/Logos_on_Linux.json'),
    str(CONFIG_BASE / 'Logos_on_Linux/Logos_on_Linux.conf'),
    # If the user didn't have XDG_CONFIG_HOME set before, but now does.
    os.path.expanduser("~/.config/FaithLife-Community/Logos_on_Linux.json"),
    os.path.expanduser("~/.config/Logos_on_Linux/Logos_on_Linux.json"),
    os.path.expanduser("~/.config/Logos_on_Linux/Logos_on_Linux.conf")
]
LLI_AUTHOR = "Ferion11, John Goodman, T. H. Wright, N. Marti, N. Shaaban"
LLI_CURRENT_VERSION = APP_VERSION
# Standalone tagged script, pinned from official-source HTTPS observations.
# AppImage companions remain owned by their runtime provider.
WINETRICKS_VERSION = '20260125'
WINETRICKS_SHA256 = '431f82fc74000e6c864409f1d8fb495d696c03928808e3e8acffc45179312a7b'
DEFAULT_LOG_LEVEL = logging.WARNING
LOGOS_BLUE = '#0082FF'
LOGOS_GRAY = '#E7E7E7'
LOGOS_WHITE = '#FCFCFC'
PID_FILE = f'/tmp/{BINARY_NAME}.pid'

WINE_RECOMMENDED_SIGIL = "Recommended"
WINE_BETA_SIGIL = "Experimental (AKA Beta)"

FAITHLIFE_PRODUCTS = ["Logos", "Verbum"]
FAITHLIFE_PRODUCT_VERSIONS = ["10"] # This used to include 9

SUPPORT_MESSAGE = f"""For LogosDesktop support, see:
{SUPPORT_LINK}
Keep support archives private and review them before sharing.
"""

DEFAULT_SUPPORT_FILE_NAME = "LogosDesktopSupport.zip"

# Strings for choosing a follow up file or directory
PROMPT_OPTION_DIRECTORY = "Choose Directory"
PROMPT_OPTION_FILE = "Choose File"
PROMPT_OPTION_NEW_FILE = "Save as"

PROMPT_OPTION_SIGILS = [PROMPT_OPTION_DIRECTORY, PROMPT_OPTION_FILE, PROMPT_OPTION_NEW_FILE] 

# String for when a binary is meant to be downloaded later
DOWNLOAD = "Download"
WINDOWS_VERSIONS = ["vista", "win7", "win8", "win10", "win11", "Return to Main Menu"]
