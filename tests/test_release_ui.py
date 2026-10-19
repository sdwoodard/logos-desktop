import asyncio
from pathlib import Path
import tempfile
from threading import Event, RLock, Timer
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ou_dedetai import constants, network, processes, releases, tui_app
from ou_dedetai.config import Config, PersistentConfiguration
from ou_dedetai.logos import State
from tests.test_releases import release_item


class TestReleaseRendering(unittest.IsolatedAsyncioTestCase):
    async def test_textual_input_and_quit_remain_live_during_metadata_request(self):
        self.addCleanup(processes.resume)
        entered, finish = Event(), Event()
        catalog = releases.select([release_item()], constants.REPOSITORY_SLUG)

        def load(repository):
            entered.set()
            finish.wait(5)
            return catalog

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (patch.multiple(constants, APP_SELF_UPDATES_ENABLED=True, CACHE_BASE=root,
                                 CACHE_DIR=str(root / "logos-desktop"),
                                 NETWORK_CACHE_PATH=str(root / "logos-desktop/network.json")),
                  patch.object(network, "_get_release_data", side_effect=load),
                  patch.object(network, "_fetch_faithlife_product_releases", return_value=[])):
                client = network.NetworkRequests()
                config = object.__new__(Config)
                config._raw = PersistentConfiguration(faithlife_product="Logos", faithlife_product_version="10")
                config._network = client
                config._overrides = SimpleNamespace(quiet=False)
                domain = object.__new__(tui_app.TUI)
                domain.conf, domain.is_running, domain._last_console_message = config, True, None
                domain._closing = Event()
                domain._closed = False
                domain._thread_lock = RLock()
                domain._hook_lock = RLock()
                domain._threads = []
                domain.config_updated_hooks = []
                domain._config_updated_event = Event()
                domain.cancel_pending_prompts = Mock()
                domain.is_installed = lambda: True
                domain.logos = SimpleNamespace(
                    logos_state=State.STOPPED, indexing_state=State.STOPPED,
                    start_monitoring=Mock(), close=Mock(return_value=True),
                )
                domain._controller_checks = releases.ControllerChecks(client)
                domain._controller_rendered = None
                domain.ui = tui_app.TextualUI(domain)
                watchdog = Timer(4, finish.set)
                watchdog.start()
                try:
                    async with domain.ui.run_test(size=(90, 28)) as pilot:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        started = time.monotonic()
                        await pilot.press("down")
                        await pilot.press("q")
                        self.assertLess(time.monotonic() - started, 2)
                        self.assertFalse(domain.is_running)
                finally:
                    finish.set()
                    watchdog.cancel()
                    domain._controller_checks.close()
                    await asyncio.to_thread(domain._controller_checks._worker.join, 3)
                    self.assertFalse(domain._controller_checks._worker.is_alive())

    async def test_tui_does_not_restart_when_the_previous_check_is_not_quiescent(self):
        domain = object.__new__(tui_app.TUI)
        domain._closing = Event()
        domain._controller_checks = Mock()
        domain._controller_checks.close.side_effect = RuntimeError("not stopped")
        prepared = Mock()
        with self.assertRaisesRegex(RuntimeError, "not stopped"):
            domain._restart_controller(prepared, [])
        prepared.restart.assert_not_called()
