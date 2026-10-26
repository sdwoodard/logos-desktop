"""Read-only failed-upgrade inspection and explicitly requested repair."""

from enum import Enum, auto
import logging
from pathlib import Path
import sqlite3
from typing import Callable, Optional
from logos_desktop import maintenance

import logos_desktop
from logos_desktop.app import App
import logos_desktop.cli
from logos_desktop.config import EphemeralConfiguration
import logos_desktop.config
import logos_desktop.constants
import logos_desktop.database
import logos_desktop.installer
import logos_desktop.msg
import logos_desktop.system
import logos_desktop.utils

class FailureType(Enum):
    FailedUpgrade = auto()


def detect_broken_install(
    logos_appdata_dir: Optional[str],
    faithlife_product: Optional[str]
) -> Optional[FailureType]:
    if (
        not logos_appdata_dir
        or not Path(logos_appdata_dir).exists()
        or not faithlife_product
    ):
        logging.debug("Application not installed, no need to attempt repairs")
        return None

    logos_app_dir = Path(logos_appdata_dir)
    # Check to see if there is a Logos.exe in the System dir but not in the top-level
    # This is a symptom of a failed in-app upgrade
    if (
        (logos_app_dir / "System" / (faithlife_product + ".exe")).exists()
        and not (logos_app_dir / (faithlife_product + ".exe")).exists()
    ):
        return FailureType.FailedUpgrade
    
    logos_user_id = logos_desktop.config.get_logos_user_id(logos_appdata_dir)
    if not logos_user_id:
        return None
    preferences = logos_app_dir / "Documents" / logos_user_id / "LocalUserPreferences/PreferencesManager.db"
    if not preferences.is_file():
        return None
    connection = None
    try:
        connection = sqlite3.connect(preferences.as_uri() + "?mode=ro&immutable=1", uri=True)
        row = connection.execute("SELECT Data FROM Preferences WHERE Type='AppLocalPreferences' LIMIT 1").fetchone()
        if row and 'FirstRunDialogWizardState="ResourceBundleSelection"' in row[0]:
            logging.warning("Detected a failed resource download. %s", logos_desktop.constants.SUPPORT_MESSAGE)
    except sqlite3.Error:
        logging.debug("Could not inspect existing preferences", exc_info=True)
    finally:
        if connection is not None:
            connection.close()
    return None


# FIXME: This logic doesn't belong here, but it's not used anywhere else
# As running the control panel in addition to the base python app logic
# are distinct operations
# It's possible to add a control panel function to app and make this generic
def run_under_app(ephemeral_config: EphemeralConfiguration, func: Callable[[App], None]): 
    dialog = ephemeral_config.dialog or logos_desktop.system.get_dialog()
    if dialog in ('qt', 'tk'):
        from logos_desktop.main import start_graphical
        return start_graphical(ephemeral_config, function=func)
    else:
        app = logos_desktop.cli.CLI(ephemeral_config)
        func(app)

def inspect(context):
    """Inspect the chosen prefix without leases, Wine calls or journal recovery."""
    wine_user = logos_desktop.config.get_wine_user(str(context.prefix))
    if wine_user is None:
        return None
    appdata = logos_desktop.config.get_logos_appdata_dir(str(context.prefix), wine_user, context.product)
    return detect_broken_install(appdata, context.product)


def repair_installation(app, *, configuration_changes=None, require_target=False):
    context = maintenance.Context.from_app(app, prompt=False)
    if not app.approve(
        f"Repair {context.product} in {context.prefix}?",
        "This reconciles pending restore/runtime records and reinstalls after a detected failed upgrade.",
    ):
        return False
    with maintenance.installing(app) as selected:
        if selected != context:
            raise maintenance.MaintenanceError("Recovery selection changed. Inspect it before retrying repair")
        failure = inspect(selected)
        if failure != FailureType.FailedUpgrade:
            app.status("Pending records reconciled. No failed upgrade requires reinstallation")
            return
        if require_target and not configuration_changes:
            raise maintenance.MaintenanceError("Refresh stable release information before reinstalling this product")
        changes = configuration_changes or {"faithlife_product_release": None}
        previous = {name: getattr(app.conf._raw, name) for name in changes}
        app.conf._change(**changes)
        try:
            app.status(f"Recovering {selected.product} after failed upgrade")
            logos_desktop.installer.install(app)
        except BaseException:
            if require_target:
                with app.conf.validated_writes(None):
                    app.conf._change(**previous)
            else:
                app.conf._change(**previous)
            raise
        app.status(f"Recovery of {selected.product} complete")


def detect_and_recover(ephemeral_config: EphemeralConfiguration):
    """Compatibility entry point for an explicit repair request."""
    return run_under_app(ephemeral_config, repair_installation)
