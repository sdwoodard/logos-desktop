import threading
import unittest
from unittest.mock import Mock, patch

from logos_desktop.app import App


class TestApp(App):
    def _ask(self, question, options):
        return None

    def _info(self, message):
        pass

    def _status(self, message, percent=None):
        pass

    def _pop_up(self, title, message):
        pass


class TestAppExit(unittest.TestCase):
    def app(self):
        with patch("logos_desktop.config.Config", return_value=Mock()):
            app = TestApp(None)
        self.addCleanup(app.close)
        return app

    def test_instances_own_callbacks_and_closed_callbacks_do_not_run(self):
        first, second = self.app(), self.app()
        seen, other = threading.Event(), threading.Event()
        unsubscribe = first.subscribe_config(seen.set)
        second.subscribe_config(other.set)
        first._config_updated_event.set()
        self.assertTrue(seen.wait(2))
        self.assertFalse(other.is_set())
        unsubscribe()
        seen.clear()
        self.assertTrue(first.close())
        first._config_updated_event.set()
        self.assertFalse(seen.wait(0.05))
        self.assertEqual(first._threads, [])

    def test_close_keeps_mutation_owned_until_it_finishes(self):
        app = self.app()
        started, release = threading.Event(), threading.Event()

        def mutation():
            started.set()
            release.wait(3)

        worker = app.start_thread(mutation)
        self.assertTrue(started.wait(2))
        try:
            self.assertFalse(app.close(timeout=0))
            self.assertTrue(worker.is_alive())
            with self.assertRaisesRegex(RuntimeError, "closing"):
                app.start_thread(lambda: None)
        finally:
            release.set()
        self.assertTrue(app.close())
        self.assertEqual(app._threads, [])
        self.assertTrue(app.close())

    def test_worker_exit_preserves_failure_for_owner_without_signaling_logos(self):
        app = self.app()
        app._schedule_exit_on_main_thread = Mock()
        app.logos.stop = Mock()
        worker = app.start_thread(lambda: app.exit("fixture failure"))
        worker.join(2)
        self.assertEqual(app._pending_exit, ("fixture failure", False))
        app._schedule_exit_on_main_thread.assert_called_once_with("fixture failure", False)
        self.assertTrue(app.close())
        app.logos.stop.assert_not_called()

    def test_main_exit_status_and_disposal_are_consistent(self):
        for intended, code in ((True, 0), (False, 1)):
            app = self.app()
            with self.assertRaises(SystemExit) as result:
                app.exit("fixture", intended=intended)
            self.assertEqual(result.exception.code, code)
            self.assertEqual(app._threads, [])
