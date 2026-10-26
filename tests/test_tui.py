import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Input, Log, OptionList

from logos_desktop import constants, processes, releases, tui_app
from logos_desktop.config import EphemeralConfiguration
from logos_desktop.logos import State


class TestTextualOperations(unittest.IsolatedAsyncioTestCase):
    async def test_close_waits_for_startup_inspection_and_discards_its_late_result(self):
        from logos_desktop import controller_install

        entered, release = threading.Event(), threading.Event()
        closed = asyncio.Event()

        def identify():
            entered.set()
            if not release.wait(5):
                raise AssertionError("Startup inspection was not released")
            return controller_install.Identity("venv", "1.0.0", "1.0.0")

        with patch.object(controller_install, "identify", side_effect=identify):
            domain = self.domain()
            domain._controller_identified = Mock(wraps=domain._controller_identified)
            original_close = domain.close

            def close(timeout=5):
                result = original_close(timeout)
                if result:
                    closed.set()
                return result

            domain.close = close
            async with domain.ui.run_test(size=(100, 35)):
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    domain.ui.action_quit()
                    self.assertFalse(domain._closed)
                finally:
                    release.set()
                await asyncio.wait_for(closed.wait(), 3)
                self.assertTrue(domain._closed)
                domain._controller_identified.assert_not_called()
                self.assertIsNone(domain._controller_identity)
                self.assertFalse(domain._threads)

    async def test_explicit_check_wins_over_late_startup_identity_without_dismissing_modal(self):
        from logos_desktop import controller_install

        entered, release = threading.Event(), threading.Event()
        published = asyncio.Event()
        initial = controller_install.Identity("unsupported", "1.0.0", "", message="Earlier inspection")
        current = controller_install.Identity("aur", "1.0.0", "1.0.0-1")

        def identify():
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Startup inspection was not released")
                return initial
            return current

        with (patch.object(controller_install, "identify", side_effect=identify),
              patch.object(controller_install, "check", return_value=controller_install.Result("available", "B"))):
            domain = self.domain()
            original_identified = domain._controller_identified

            def identified(identity, standalone):
                original_identified(identity, standalone)
                published.set()

            domain._controller_identified = identified
            async with domain.ui.run_test(size=(100, 35)) as pilot:
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    self.assertIsNotNone(domain._run_operation(domain._check_controller))
                    prompt = await self.screen(pilot, tui_app.InfoModal)
                    release.set()
                    await asyncio.wait_for(published.wait(), 3)
                    self.assertIs(domain.ui.screen, prompt)
                    self.assertIs(domain._controller_identity, current)
                    self.assertEqual(domain._controller_result.status, "available")
                    self.assertIn("Update via AUR", domain.set_tui_menu_options())
                    await pilot.press("escape")
                    await self.finished(pilot, domain)
                finally:
                    release.set()

    async def test_delayed_startup_inspection_admits_first_action_and_preserves_its_prompt(self):
        from logos_desktop import controller_install

        entered, release = threading.Event(), threading.Event()
        published = asyncio.Event()
        identity = controller_install.Identity("venv", "1.0.0", "1.0.0")
        readiness_threads = []

        def identify():
            entered.set()
            if not release.wait(5):
                raise AssertionError("Startup inspection was not released")
            return identity

        with (patch.object(controller_install, "identify", side_effect=identify),
              patch.object(controller_install, "confirm_startup", side_effect=lambda: readiness_threads.append(
                  threading.get_ident())),
              patch.object(controller_install, "check") as check):
            domain = self.domain()
            original_poll = domain.poll_controller_releases

            def poll():
                original_poll()
                if domain._controller_identity is not None:
                    published.set()

            domain.poll_controller_releases = poll
            destination = self.root / "First action directory"
            destination.mkdir()
            answers = []
            async with domain.ui.run_test(size=(100, 35)) as pilot:
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    self.assertFalse(domain._operation_active)
                    self.assertNotIn(threading.get_ident(), readiness_threads)
                    worker = domain._run_operation(lambda app: answers.extend([
                        app.conf.faithlife_product, app.conf.install_dir,
                    ]))
                    self.assertIsNotNone(worker)
                    prompt = await self.screen(pilot, tui_app.MenuModal)
                    release.set()
                    await asyncio.wait_for(published.wait(), 3)
                    self.assertIs(domain.ui.screen, prompt)
                    self.assertTrue(domain._operation_active)
                    await self.choose(pilot, "Logos")
                    await self.screen(pilot, tui_app.MenuModal)
                    await self.choose(pilot, constants.PROMPT_OPTION_DIRECTORY)
                    directory = await self.screen(pilot, tui_app.InputModal)
                    directory.query_one(Input).value = str(destination)
                    await pilot.press("enter")
                    await self.finished(pilot, domain)
                    self.assertEqual(answers, ["Logos", str(destination)])
                    check.assert_not_called()
                finally:
                    release.set()

    async def test_checkpoint_cancellation_during_real_handoff_keeps_tui_and_restores_lease(self):
        from logos_desktop import controller_install, instance
        from logos_desktop.app import UserExitedFromAsk

        entered, finish, cancelled = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        identity = controller_install.Identity("aur", "1.0.0", "1.0.0-1")

        def provider(owner):
            entered.set()
            if not finish.wait(5):
                raise AssertionError("Delayed provider was not released")
            return controller_install.Result("available", "Private provider has B")

        with (patch.object(constants, "COORDINATION_DIR", self.root / "coordination"),
              patch.object(instance, "check_legacy"),
              patch.object(controller_install, "identify", return_value=identity),
              patch.object(controller_install, "check", side_effect=provider),
              patch.object(controller_install.shutil, "which", return_value="/fixture/konsole"),
              patch.object(controller_install.subprocess, "Popen") as popen):
            domain = self.domain()
            domain.approve = lambda *args: True
            original_checkpoint = domain.checkpoint

            def checkpoint():
                original_checkpoint()
                if cancelled.is_set():
                    raise UserExitedFromAsk

            domain.checkpoint = checkpoint
            session = instance.Session(profile=self.selected)
            self.addCleanup(session.close)
            session.acquire()
            session.listen()
            domain._instance_session = session
            async with domain.ui.run_test(size=(100, 35)) as pilot:
                await self.finished(pilot, domain)
                domain._run_operation(domain._handoff_controller)
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    self.assertIsNone(domain._instance_session)
                    cancelled.set()
                finally:
                    finish.set()
                await self.finished(pilot, domain)
                popen.assert_not_called()
                self.assertIs(domain._instance_session, session)
                self.assertTrue(session.listening)
                with self.assertRaises(instance.InstanceBusy):
                    instance.Session(profile=self.selected).acquire()
                self.assertTrue(domain.is_running)
                self.assertFalse(domain._closing.is_set())
                self.assertIsNone(domain._controller_result)

    async def test_managed_check_is_explicit_and_handoff_closes_old_tui(self):
        from logos_desktop import controller_install, utils

        identity = controller_install.Identity("venv", "1.0.0", "1.0.0")
        with (patch.object(controller_install, "identify", return_value=identity),
              patch.object(controller_install, "check", return_value=controller_install.Result(
                  "available", "Private installation provider has B")) as check,
              patch.object(controller_install, "confirm_startup") as readiness,
              patch.object(utils, "handoff_controller", return_value=controller_install.Result(
                  "handed-off", "Terminal owns the transaction")) as handoff):
            domain = self.domain()
            domain.approve = lambda *args: True
            async with domain.ui.run_test(size=(100, 35)) as pilot:
                await self.finished(pilot, domain)
                check.assert_not_called()
                readiness.assert_called_once()
                self.assertIn("Check controller updates", domain.set_tui_menu_options())
                await self.choose(pilot, "Check controller updates", "#main-menu")
                await self.screen(pilot, tui_app.InfoModal)
                await pilot.press("escape")
                await self.finished(pilot, domain)
                check.assert_called_once_with(identity)
                self.assertIn("Update environment", domain.set_tui_menu_options())
                await self.choose(pilot, "Update environment", "#main-menu")
                for _ in range(100):
                    if not domain.is_running:
                        break
                    await pilot.pause(.02)
                self.assertFalse(domain.is_running)
                handoff.assert_called_once_with(domain, identity, "tui")

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
