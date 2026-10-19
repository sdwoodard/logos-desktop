import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Input, Log, OptionList

from ou_dedetai import constants, processes, releases, tui_app
from ou_dedetai.config import EphemeralConfiguration
from ou_dedetai.logos import State


class TestTextualOperations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(processes.resume)
        self.root = Path(temporary.name)
        self.selected = self.root / "selected.json"
        self.selected.write_text("{}")
        self.install = self.root / "Installation 文"
        patches = [
            patch.multiple(constants, CACHE_BASE=self.root / "cache", CACHE_DIR=str(self.root / "cache/app"),
                           NETWORK_CACHE_PATH=str(self.root / "cache/app/network.json"),
                           STATE_DIR=str(self.root / "state/app")),
            patch.object(releases.ControllerChecks, "request"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def domain(self, installed=False):
        if installed:
            live = self.install / "data/wine64_bottle/drive_c/users/fixture/AppData/Local/Logos"
            live.mkdir(parents=True)
            (live / "Logos.exe").touch()
            (live / "Logos.exe").chmod(0o700)
            (self.install / "data/bin").mkdir()
            (live / "System").mkdir()
            (live / "System/Logos.deps.json").write_text('{"libraries":{"Logos/40.0.0.0":null}}')
            document = {"install_dir": str(self.install), "faithlife_product": "Logos",
                        "faithlife_product_version": "10",
                        "faithlife_product_release": "40.0.0.0"}
            self.selected.write_text(json.dumps(document))
            (self.install / f"{constants.BINARY_NAME}.json").write_text(json.dumps(document))
        domain = tui_app.TUI(EphemeralConfiguration.load_from_path(str(self.selected)))
        self.addCleanup(domain.close)
        return domain

    async def screen(self, pilot, kind):
        for _ in range(150):
            if isinstance(pilot.app.screen, kind):
                return pilot.app.screen
            await pilot.pause(.02)
        self.fail(f"Expected screen {kind.__name__}, got {type(pilot.app.screen).__name__}")

    async def finished(self, pilot, domain):
        for _ in range(150):
            if not domain._operation_active:
                await pilot.pause(.03)
                return
            await pilot.pause(.02)
        self.fail("TUI operation did not finish")

    async def choose(self, pilot, label, selector="#menu-list"):
        menu = pilot.app.screen.query_one(selector, OptionList)
        labels = [str(option.prompt) for option in menu.options]
        menu.highlighted = labels.index(label)
        menu.focus()
        await pilot.press("enter")

    async def test_full_constructor_initial_product_and_mixed_directory_prompt(self):
        domain = self.domain()
        destination = self.root / "Custom directory 文"
        destination.mkdir()
        answers = []
        async with domain.ui.run_test(size=(100, 35)) as pilot:
            domain._run_operation(lambda app: answers.extend([app.conf.faithlife_product, app.conf.install_dir]))
            await self.screen(pilot, tui_app.MenuModal)
            await self.choose(pilot, "Logos")
            await self.screen(pilot, tui_app.MenuModal)
            await self.choose(pilot, constants.PROMPT_OPTION_DIRECTORY)
            screen = await self.screen(pilot, tui_app.InputModal)
            screen.query_one(Input).value = str(destination)
            await pilot.press("enter")
            await self.finished(pilot, domain)
            self.assertEqual(answers, ["Logos", str(destination)])
            self.assertEqual(domain.conf.install_dir, str(destination))
            self.assertFalse(domain._pending_prompts)
            self.assertEqual(len(domain.config_updated_hooks), 1)
            self.assertEqual(domain.ui.focused.id, "main-menu")

    async def test_real_utilities_uninstall_cancel_preserves_installation(self):
        domain = self.domain(installed=True)
        async with domain.ui.run_test(size=(100, 35)) as pilot:
            await self.choose(pilot, "Utilities →", "#main-menu")
            await self.screen(pilot, tui_app.MenuModal)
            await self.choose(pilot, "Uninstall")
            await self.screen(pilot, tui_app.MenuModal)
            await pilot.press("escape")
            await self.finished(pilot, domain)
            self.assertTrue(self.install.exists())
            self.assertTrue(self.selected.exists())
            self.assertFalse(domain._pending_prompts)

    async def test_delayed_install_rejects_overlap_restores_override_and_retains_failure(self):
        domain = self.domain()
        entered, release = threading.Event(), threading.Event()

        def install(app):
            self.assertTrue(app.conf._overrides.assume_yes)
            app.conf.rebind(app.conf._raw)
            entered.set()
            release.wait(3)
            raise ValueError("Injected install failure")

        async with domain.ui.run_test(size=(100, 35)) as pilot:
            with patch.object(tui_app.installer, "install", side_effect=install):
                await self.choose(pilot, "Install", "#main-menu")
                for _ in range(100):
                    if entered.is_set():
                        break
                    await pilot.pause(.02)
                self.assertTrue(entered.is_set())
                called = Mock()
                self.assertIsNone(domain._run_operation(called))
                called.assert_not_called()
                tick = []
                domain.ui.set_timer(.01, lambda: tick.append(True))
                await pilot.pause(.05)
                self.assertTrue(tick)
                release.set()
                await self.finished(pilot, domain)
            screen = await self.screen(pilot, tui_app.InfoModal)
            self.assertIn("Injected install failure", screen.message)
            self.assertFalse(domain.conf._overrides.assume_yes)
            await pilot.press("enter")
            self.assertEqual(domain.ui.focused.id, "main-menu")

    async def test_index_stop_logging_and_retained_log_state(self):
        domain = self.domain(installed=True)
        domain.logos.start_monitoring = Mock()
        async with domain.ui.run_test(size=(100, 35)) as pilot:
            domain.logos.indexing_state = State.RUNNING
            domain.ui.action_refresh()
            with patch.object(domain.logos, "stop_indexing") as stop:
                await self.choose(pilot, "Stop Indexing", "#main-menu")
                await self.finished(pilot, domain)
                stop.assert_called_once_with()
            self.assertEqual(domain.logos.indexing_state, State.RUNNING)
            domain.logos.indexing_state = State.STOPPING
            domain.ui.action_refresh()
            menu = domain.ui.query_one("#main-menu", OptionList)
            stopping = next(option for option in menu.options if str(option.prompt) == "Stopping Indexing")
            self.assertTrue(stopping.disabled)
            domain.logos.indexing_state = State.STOPPED
            with patch.object(domain.logos, "switch_logging") as switch:
                domain.utilities_menu_select("Enable Logging")
                await self.finished(pilot, domain)
                switch.assert_called_once_with("enable")
            domain._run_operation(lambda app: [app.status(f"line {number}") for number in range(350)])
            await self.finished(pilot, domain)
            self.assertLessEqual(domain.ui.query_one(Log).line_count, 300)
            self.assertFalse(hasattr(domain, "console_log"))
            self.assertEqual(len(domain.config_updated_hooks), 1)

    async def test_quit_interrupts_input_modal_and_close_waits_for_mutation(self):
        domain = self.domain()
        async with domain.ui.run_test(size=(100, 35)) as pilot:
            domain._run_operation(lambda app: app.ask("Directory", [constants.PROMPT_OPTION_DIRECTORY]))
            await self.screen(pilot, tui_app.InputModal)
            await pilot.press("ctrl+c")
            await self.finished(pilot, domain)
            self.assertTrue(domain._closing.is_set())
            self.assertFalse(domain._pending_prompts)

        domain = self.domain()
        entered, release = threading.Event(), threading.Event()

        def mutation(app):
            entered.set()
            release.wait(3)
            (self.root / "mutation-complete").write_text("complete")

        async with domain.ui.run_test(size=(100, 35)) as pilot:
            domain._run_operation(mutation)
            await asyncio.to_thread(entered.wait, 2)
            domain.ui.action_quit()
            await pilot.pause(.05)
            self.assertFalse(domain._closed)
            release.set()
            await self.finished(pilot, domain)
        self.assertTrue((self.root / "mutation-complete").exists())
        self.assertTrue(domain.close())
