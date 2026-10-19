import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Static
from textual.app import App as TextualApp

from ou_dedetai import constants, control, maintenance, processes, tui_app
from tests.test_runtime_paths import fixture_config


class PromptUI(TextualApp):
    action_quit = tui_app.TextualUI.action_quit
    on_unmount = tui_app.TextualUI.on_unmount

    def __init__(self, domain):
        super().__init__()
        self.domain = domain

    def compose(self):
        yield Static("Disposable maintenance prompt fixture")

    def on_mount(self):
        processes.resume()

    def go_to_main_menu(self):
        pass


class TestTuiMaintenance(unittest.IsolatedAsyncioTestCase):
    async def test_quit_or_pruning_cancels_actual_uninstall_wait_and_releases_lease(self):
        self.addCleanup(processes.resume)
        for shutdown in ("quit", "prune"):
            with self.subTest(shutdown=shutdown), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                install = root / "installation"
                for child in ("data/bin", "data/wine64_bottle"):
                    (install / child).mkdir(parents=True)
                portable = {"install_dir": str(install)}
                (install / f"{constants.BINARY_NAME}.json").write_text(json.dumps(portable))
                selected = root / "selected.json"
                selected.write_text(json.dumps(portable))
                domain = object.__new__(tui_app.TUI)
                domain.conf = fixture_config(install)
                domain.conf.app = domain
                domain.conf._overrides.config_path = str(selected)
                domain.conf._overrides.quiet = False
                domain.conf._overrides.assume_yes = False
                domain.conf._overrides.wine_prefix = None
                domain._prompt_lock = threading.Lock()
                domain._pending_prompts = {}
                domain._closing = threading.Event()
                domain._threads = []
                domain._last_status = None
                domain._config_updated_event = Mock()
                domain.console_log = []
                domain.logos = None
                domain.is_running = True
                domain.ui = PromptUI(domain)
                with patch.object(constants, "STATE_DIR", str(root / "state/FaithLife-Community")):
                    context = maintenance.Context.from_app(domain)
                    async with domain.ui.run_test() as pilot:
                        domain._run_operation(control.uninstall)
                        for _ in range(100):
                            if isinstance(domain.ui.screen, tui_app.MenuModal):
                                break
                            await pilot.pause(0.02)
                        self.assertIsInstance(domain.ui.screen, tui_app.MenuModal)
                        with self.assertRaises(maintenance.MaintenanceBusy), maintenance.use(context, exclusive=True):
                            pass
                        if shutdown == "quit":
                            await pilot.press("ctrl+q")
                        else:
                            domain.ui.pop_screen()
                        for thread in domain._threads:
                            await asyncio.to_thread(thread.join, 3)
                            self.assertFalse(thread.is_alive())
                    with maintenance.use(context, exclusive=True):
                        pass
                    self.assertTrue(install.exists())
                    self.assertTrue(selected.exists())
