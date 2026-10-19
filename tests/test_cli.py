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

from ou_dedetai import cli, constants, main
from ou_dedetai.app import App, UserExitedFromAsk


PROGRAM = r'''
import os, sys, threading
from ou_dedetai import cli, main
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
from ou_dedetai import main, system
selected, action, command = sys.argv[1:]
system.ensure_winetricks = lambda *args, **kwargs: command
sys.argv = ['logos-desktop', '-c', selected, action, 'fixture']
main.main()
'''
        admission = '''
import sys
from pathlib import Path
from ou_dedetai import maintenance
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
