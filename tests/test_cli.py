import io
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from logos_desktop import cli, constants, main
from logos_desktop.app import App, UserExitedFromAsk


PROGRAM = r'''
import os, sys, threading
from logos_desktop import cli, main
scenario, selected = sys.argv[1:]
def action(app):
    if scenario == 'close':
        threading.Timer(.3, app.request_close).start()
    if scenario == 'info':
        class Input:
            encoding = sys.stdin.encoding
            def fileno(self):
                return sys.stdin.fileno()
            def isatty(self):
                return True
        app._stdin = Input()
        app.info('Information before question')
    if scenario == 'error':
        os.close(0)
    if scenario == 'quiet':
        app.conf._overrides.quiet = True
    app.status('Progress fixture', 50)
    print('answer=' + app.ask('Approve?', ['Yes', 'No']))
    if scenario == 'two':
        print('answer=' + app.ask('Again?', ['Yes', 'No']))
cli.CLI.installation_status = action
sys.argv = ['logos-desktop', '--status', '-c', selected]
try:
    main.main()
finally:
    print('remaining=' + repr([t.name for t in threading.enumerate() if t is not threading.main_thread()]))
'''


class TestPromptContract(unittest.TestCase):
    def test_actual_backend_checkpoint_errors_restore_instance_before_terminal_launch(self):
        import threading
        from logos_desktop import controller_install, instance, utils

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = controller_install.Identity("aur", "1.0.0", "1.0.0-1")
            with (patch.object(constants, "COORDINATION_DIR", root / "coordination"),
                  patch.object(instance, "check_legacy"),
                  patch.object(controller_install, "identify", return_value=identity),
                  patch.object(controller_install.shutil, "which", return_value="/fixture/konsole"),
                  patch.object(controller_install.subprocess, "Popen") as popen):
                for frontend, error in (("qt", UserExitedFromAsk()), ("tui", OSError("Checkpoint failed"))):
                    with self.subTest(frontend=frontend, error=type(error).__name__):
                        session = instance.Session(profile=root / "profile.json")
                        session.acquire()
                        session.listen()
                        cancelled = threading.Event()

                        def checkpoint():
                            if cancelled.is_set():
                                raise error

                        def environment():
                            cancelled.set()
                            return {}

                        app = SimpleNamespace(_profile_lock=threading.Lock(), _instance_session=session,
                                              conf=SimpleNamespace(config_file_path=str(root / "profile.json")),
                                              checkpoint=checkpoint, exit=Mock())
                        try:
                            with (patch.object(controller_install, "check", return_value=controller_install.Result(
                                    "available", "Private provider has B")),
                                  patch.object(controller_install, "_environment", side_effect=environment)):
                                with self.assertRaises(type(error)) as raised:
                                    utils.handoff_controller(app, identity, frontend)
                            self.assertIs(raised.exception, error)
                            popen.assert_not_called()
                            self.assertIs(app._instance_session, session)
                            self.assertTrue(session.listening)
                            with self.assertRaises(instance.InstanceBusy):
                                instance.Session(profile=root / "profile.json").acquire()
                            app.exit.assert_not_called()
                        finally:
                            session.close()

    def test_handoff_releases_real_instance_before_helper_and_restores_on_refusal(self):
        import threading
        from logos_desktop import controller_install, instance, utils

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (patch.object(constants, "COORDINATION_DIR", root / "coordination"),
                  patch.object(instance, "check_legacy")):
                for state in ("handed-off", "manual", "refused", "unknown"):
                    with self.subTest(state=state):
                        session = instance.Session(profile=root / "profile.json")
                        session.acquire()
                        session.listen()
                        app = SimpleNamespace(_profile_lock=threading.Lock(), _instance_session=session,
                                              conf=SimpleNamespace(config_file_path=str(root / "profile.json")))
                        identity = controller_install.Identity("venv", "1.0.0", "1.0.0")

                        def handoff(owner, config, frontend, checkpoint=None):
                            self.assertIsNone(app._instance_session)
                            self.assertFalse(session.listening)
                            with instance.Session(profile=root / "profile.json"):
                                pass
                            return controller_install.Result(state, "Fixture outcome")

                        try:
                            with patch.object(controller_install, "handoff", side_effect=handoff):
                                result = utils.handoff_controller(app, identity, "tui")
                            self.assertEqual(result.status, state)
                            if state == "handed-off":
                                self.assertIsNone(app._instance_session)
                            else:
                                self.assertIs(app._instance_session, session)
                                self.assertTrue(session.listening)
                                with self.assertRaises(instance.InstanceBusy):
                                    instance.Session(profile=root / "profile.json").acquire()
                        finally:
                            session.close()

    def test_managed_update_preserves_config_frontend_and_bypasses_standalone(self):
        from logos_desktop import controller_install, updates

        identity = controller_install.Identity("venv", "1.0.0", "1.0.0")
        args = SimpleNamespace(config="/fixture/selected profile.json", dialog=None)
        result = controller_install.Result("handed-off", "Terminal owns the transaction")
        with (patch.object(controller_install, "identify", return_value=identity),
              patch.object(main.EphemeralConfiguration, "load_from_path", return_value=SimpleNamespace(dialog="tui")),
              patch.object(controller_install, "handoff", return_value=result) as handoff,
              patch.object(updates, "eligible") as standalone,
              patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertIs(main.run_controller_update(args), result)
            handoff.assert_called_once_with(identity, args.config, "tui")
            standalone.assert_not_called()
            self.assertIn("handed-off", output.getvalue())
            self.assertNotIn("Updated", output.getvalue())

    def test_controller_check_dispatches_before_profile_or_gui_setup(self):
        from logos_desktop import controller_install

        with (patch.object(sys, "argv", ["logos-desktop", "--check-controller-updates"]),
              patch.object(controller_install, "main", return_value=0) as check,
              patch.object(main, "setup_config") as setup):
            with self.assertRaises(SystemExit) as stopped:
                main.main()
            self.assertEqual(stopped.exception.code, 0)
            check.assert_called_once_with(["check"])
            setup.assert_not_called()

    def app(self, responses):
        return SimpleNamespace(
            conf=SimpleNamespace(_overrides=SimpleNamespace(assume_yes=False)),
            _ask=Mock(side_effect=responses), _exit_option="Exit",
        )

    def test_canonical_choices_and_cancellation_on_retry(self):
        for response, expected in (("yes", "Yes"), ("NO", "No"), ("YEs", "Yes")):
            self.assertEqual(App.ask(self.app([response]), "Choose", ["Yes", "No"]), expected)
        self.assertEqual(App.ask(self.app(["YES"]), "Choose", ["Yes", "yes"]), "Yes")
        self.assertEqual(App.ask(self.app(["yes"]), "Choose", ["Yes", "yes"]), "yes")
        for cancel in (None, "Exit", "exit", "EXIT"):
            domain = self.app(["invalid", cancel])
            with self.assertRaises(UserExitedFromAsk):
                App.ask(domain, "Choose", ["Yes", "No"])
            self.assertEqual(domain._ask.call_count, 2)

    def test_blank_path_does_not_mean_current_directory(self):
        domain = self.app(["", None])
        with self.assertRaises(UserExitedFromAsk):
            App.ask(domain, "Directory", [constants.PROMPT_OPTION_DIRECTORY])


class TestCliPipes(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.selected = self.root / "selected.json"
        self.selected.write_text("{}")
        self.environment = dict(os.environ, HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / "config"),
                                XDG_CACHE_HOME=str(self.root / "cache"), XDG_STATE_HOME=str(self.root / "state"))

    def command(self, scenario):
        return [sys.executable, "-B", "-c", PROGRAM, scenario, str(self.selected)]

    def run_case(self, scenario, answer):
        result = subprocess.run(self.command(scenario), input=answer, capture_output=True, text=True,
                                env=self.environment, timeout=8)
        self.assertIn("remaining=[]", result.stdout)
        self.assertNotIn("\033", result.stdout)
        self.assertNotIn("\r", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_eof_and_input_failure_are_bounded_nonzero_and_clear(self):
        for scenario, answer in (("one", ""), ("one", "invalid\n"), ("error", "")):
            with self.subTest(scenario=scenario, answer=answer):
                result = self.run_case(scenario, answer)
                self.assertEqual(result.returncode, 1)
                self.assertIn("input", result.stderr.lower())

    def test_multiple_answers_while_writer_stays_open_and_partial_line_close(self):
        for scenario, answer in (("two", "yes\nno\n"), ("close", "partial")):
            with self.subTest(scenario=scenario):
                process = subprocess.Popen(self.command(scenario), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, env=self.environment, text=True)
                try:
                    process.stdin.write(answer)
                    process.stdin.flush()
                    process.wait(timeout=5)
                    output = process.stdout.read()
                    errors = process.stderr.read()
                    self.assertEqual(process.returncode, 0, errors)
                    self.assertIn("remaining=[]", output)
                    if scenario == "two":
                        self.assertIn("answer=Yes", output)
                        self.assertIn("answer=No", output)
                    else:
                        self.assertIn("cancelled", errors)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)

    def test_info_acknowledgement_is_not_the_next_answer_and_quiet_stays_quiet(self):
        result = self.run_case("info", "\nno\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("answer=No", result.stdout)
        result = self.run_case("quiet", "yes\n")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Progress fixture", result.stdout)

    def test_actual_redirected_file_status_and_narrow_terminal(self):
        target = self.root / "output"
        with target.open("w") as output:
            result = subprocess.run(self.command("one"), input="yes\n", stdout=output, stderr=subprocess.PIPE,
                                    text=True, env=self.environment, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Progress fixture", target.read_text())
        domain = object.__new__(cli.CLI)
        domain._last_status = None
        output = io.StringIO()
        output.isatty = lambda: True
        with (patch.object(sys, "stdout", output), patch.dict(os.environ, TERM="xterm"),
              patch.object(cli.shutil, "get_terminal_size", return_value=os.terminal_size((1, 1)))):
            domain._status("narrow", 120)
        self.assertLess(len(output.getvalue()), 100)

    def test_unimplemented_actions_have_parser_errors_and_no_frontend(self):
        for flag in ("--dirlink", "--check-resources"):
            with self.subTest(flag=flag), patch.object(main, "run_control_panel") as frontend:
                with patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
                    main.setup_config(main.get_parser().parse_args([flag]))
                self.assertEqual(error.exception.code, 2)
                frontend.assert_not_called()

    def test_winetricks_empty_args_and_spaced_wine_arguments_dispatch_once(self):
        cases = [(["--winetricks"], []), (["--wine", "a path 文", "literal"], ["a path 文", "literal"])]
        for arguments, expected in cases:
            parser = main.get_parser()
            config, action = main.parse_args(parser.parse_args(["-c", str(self.selected), *arguments]), parser)
            self.assertEqual(config.wine_args, expected)
            domain = Mock()
            with patch.object(cli, "CLI", return_value=domain):
                action(config)
            getattr(domain, "wine" if arguments[0] == "--wine" else "winetricks").assert_called_once_with()
            domain.close.assert_called_once_with()

    def test_interrupt_keeps_actual_command_owned_until_its_final_write(self):
        program = '''
import sys
from logos_desktop import main, system
selected, action, command = sys.argv[1:]
system.ensure_winetricks = lambda *args, **kwargs: command
sys.argv = ['logos-desktop', '-c', selected, action, 'fixture']
main.main()
'''
        admission = '''
import sys
from pathlib import Path
from logos_desktop import maintenance
try:
    with maintenance.use(maintenance.Context(Path(sys.argv[1])), exclusive=True):
        pass
except maintenance.MaintenanceBusy:
    sys.exit(44)
'''
        for action in ("--wine", "--winetricks"):
            with self.subTest(action=action):
                root = self.root / action.removeprefix("--")
                root.mkdir()
                command = root / "wine"
                command.write_text(f'''#!{sys.executable}
import pathlib, time
root = pathlib.Path({str(root)!r})
(root / 'entered').touch()
deadline = time.monotonic() + 5
while not (root / 'release').exists() and time.monotonic() < deadline:
    time.sleep(.02)
(root / 'final-write').write_text('complete')
''')
                command.chmod(0o700)
                server = root / "wineserver"
                server.write_text("#!/bin/sh\nprintf 'Wine 11.19\\n'\n")
                server.chmod(0o700)
                install = root / "installation"
                prefix = install / "data/wine64_bottle"
                prefix.mkdir(parents=True)
                self.selected.write_text(json.dumps({
                    "install_dir": str(install), "faithlife_product": "Logos", "faithlife_product_version": "10",
                    "faithlife_product_release": "40.0.0.0", "wine_binary": str(command),
                }))
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", program, str(self.selected), action, str(command)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environment, text=True,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not (root / "entered").exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue((root / "entered").exists())
                    process.send_signal(signal.SIGINT)
                    time.sleep(.1)
                    self.assertIsNone(process.poll())
                    self.assertFalse((root / "final-write").exists())
                    probe = subprocess.run([sys.executable, "-B", "-c", admission, str(prefix)],
                                           env=self.environment, capture_output=True, timeout=5)
                    self.assertEqual(probe.returncode, 44, probe.stderr)
                finally:
                    (root / "release").touch()
                    output, errors = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 130, errors)
                self.assertIn("Waiting for the active Wine command", output)
                self.assertEqual((root / "final-write").read_text(), "complete")
