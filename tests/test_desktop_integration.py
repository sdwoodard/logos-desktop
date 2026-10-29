import errno
import configparser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from logos_desktop import config_store, constants, desktop, file_transactions


class TestMimeEnvironment(unittest.TestCase):
    def test_default_query_excludes_stale_token_and_preserves_other_environment(self):
        environment = {"XDG_ACTIVATION_TOKEN": "synthetic-stale-token", "LANG": "C.UTF-8"}
        result = subprocess.CompletedProcess([], 0, b"fixture.desktop\n")
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(desktop.shutil, "which", return_value="/fixture/xdg-mime"),
              patch.object(desktop.subprocess, "run", return_value=result) as run):
            self.assertEqual(desktop._default("x-scheme-handler/synthetic"), "fixture.desktop")
        run.assert_called_once_with(
            ["/fixture/xdg-mime", "query", "default", "x-scheme-handler/synthetic"],
            env={"LANG": "C.UTF-8"}, capture_output=True, timeout=5, check=True,
        )


class TestDesktopIntegration(unittest.TestCase):
    def test_managed_entries_use_stable_launcher_and_preserve_profile_arguments(self):
        from logos_desktop import controller_install

        launcher = self.root / "controller/logos-desktop"
        identity = controller_install.Identity("venv", "1.0.0", "1.0.0", launcher=launcher)
        plan = desktop.preview(self.root / "selected profile.json", desktop.controller_launcher(identity),
                               windows_executable=r"C:\Logos\Logos.exe", data_home=self.root / "venv",
                               config_home=self.root / "config")
        for entry in plan.files:
            if entry.path.suffix == ".desktop":
                text = entry.after.decode()
                self.assertIn(str(launcher), text)
                self.assertIn("selected profile.json", text)
                self.assertNotIn(sys.executable, text)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(constants, 'COORDINATION_DIR', self.root / 'coordination').start()
        self.defaults = {scheme: '' for scheme in desktop.SCHEMES}
        patch.object(desktop, '_default', side_effect=self.default).start()

    def default(self, scheme):
        if self.defaults[scheme]:
            return self.defaults[scheme]
        mime = self.root / 'config/mimeapps.list'
        if not mime.exists():
            return ''
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(mime)
        return parser.get('Default Applications', scheme, fallback='').rstrip(';')

    def plan(self, name='selected Space 文%u.json', *, url=True):
        return desktop.preview(self.root / name, ['/fixture/100% controller', 'literal%u'],
                               windows_executable=r'C:\Logos Space\Logos.exe' if url else None,
                               data_home=self.root / 'share', config_home=self.root / 'config')

    def test_profile_routing_absent_mime_and_conflict_safe_rollback(self):
        plan = self.plan()
        self.assertFalse((self.root / 'coordination').exists())
        self.assertFalse((self.root / 'share').exists())
        result = desktop.adopt(plan)
        self.assertEqual(result.state, 'committed')
        handler = f'logos-desktop-{plan.profile_id}-url.desktop'
        for item in plan.files:
            self.assertEqual(item.path.read_bytes(), item.after)
            if item.path.suffix == '.desktop':
                text = item.after.decode()
                self.assertIn('"-c"', text)
                self.assertIn('selected Space 文%%u.json', text)
                self.assertIn('Exec=/usr/bin/env "--"', text)
        mime = self.root / 'config/mimeapps.list'
        self.assertIn(handler, mime.read_text())
        edited = plan.files[0].path
        edited.write_bytes(b'later edit')
        rolled = desktop.rollback(plan.journal)
        self.assertEqual(rolled.conflicts, (str(edited),))
        self.assertEqual(edited.read_bytes(), b'later edit')
        self.assertFalse(mime.exists())
        self.assertTrue(plan.journal.exists())

    def test_unrelated_defaults_and_unowned_ids_are_preserved(self):
        self.defaults.update(dict.fromkeys(desktop.SCHEMES, 'unrelated.desktop'))
        plan = self.plan()
        self.assertEqual(len(plan.files), 3)
        self.assertEqual(len(plan.issues), 2)
        self.assertFalse((self.root / 'config').exists())
        desktop.adopt(plan)
        collision = self.plan('another.json', url=False)
        collision.files[0].path.write_bytes(b'[Desktop Entry]\nName=Unrelated\n')
        with self.assertRaises(file_transactions.FileTransactionError):
            self.plan('another.json', url=False)

    def test_callback_readiness_checks_the_exact_profile_and_executable_binding(self):
        plan = self.plan()
        desktop.adopt(plan)
        launcher = ['/fixture/100% controller', 'literal%u']
        with patch.object(constants, 'XDG_DATA_HOME', self.root / 'share'):
            self.assertTrue(desktop.url_handler_current(plan.config, launcher, r'C:\Logos Space\Logos.exe'))
            self.assertFalse(desktop.url_handler_current(plan.config, launcher, r'C:\Other\Logos.exe'))
            self.assertFalse(desktop.url_handler_current(
                self.root / 'other.json', launcher, r'C:\Logos Space\Logos.exe',
            ))

    def test_mime_change_after_preview_and_after_commit_preserved(self):
        plan = self.plan()
        self.defaults[desktop.SCHEMES[0]] = 'other.desktop'
        with self.assertRaises(file_transactions.FileTransactionError):
            desktop.adopt(plan)
        self.assertFalse(plan.files[0].path.exists())
        self.defaults.update(dict.fromkeys(desktop.SCHEMES, ''))
        plan = self.plan()
        desktop.adopt(plan)
        mime = plan.files[-1].path
        before = mime.read_bytes()
        self.defaults[desktop.SCHEMES[0]] = 'later.desktop'
        result = desktop.rollback(plan.journal)
        self.assertIn(str(mime), result.conflicts)
        self.assertEqual(mime.read_bytes(), before)
        handler = next(item.path for item in plan.files if item.path.name.endswith('-url.desktop'))
        self.assertTrue(handler.exists())
        self.assertIn(str(handler), result.conflicts)

    def test_unrelated_mime_edit_keeps_referenced_handler_until_conflict_resolved(self):
        mime = self.root / 'config/mimeapps.list'
        mime.parent.mkdir()
        prior = b'[Added Associations]\napplication/pdf=existing-reader.desktop;\n'
        mime.write_bytes(prior)
        plan = self.plan()
        desktop.adopt(plan)
        original = mime.read_bytes()
        mime.write_bytes(original + b'text/plain=later-editor.desktop;\n')
        edited = mime.read_bytes()
        handler = next(item.path for item in plan.files if item.path.name.endswith('-url.desktop'))
        handler_content = handler.read_bytes()
        for attempt in range(2):
            result = desktop.rollback(plan.journal)
            self.assertEqual(result.state, 'conflict')
            self.assertEqual(set(result.conflicts), {str(mime), str(handler)})
            self.assertEqual(mime.read_bytes(), edited)
            self.assertEqual(handler.read_bytes(), handler_content)
            self.assertIn(handler.name, mime.read_text())
            record = json.loads(plan.journal.read_bytes())
            self.assertEqual(set(record['conflicts']), {str(mime), str(handler)})
        mime.write_bytes(original)
        self.assertEqual(desktop.rollback(plan.journal).state, 'rolled-back')
        self.assertEqual(mime.read_bytes(), prior)
        self.assertFalse(handler.exists())

    def test_actual_partial_replace_failure_rolls_back_prior_mime_bytes(self):
        mime = self.root / 'config/mimeapps.list'
        mime.parent.mkdir()
        mime.write_bytes(b'[Added Associations]\ntext/plain=fixture.desktop;\n')
        original = mime.read_bytes()
        plan = self.plan()
        publish = config_store.Target.publish

        def fail_mime(target):
            if target.path == mime:
                raise OSError(errno.ENOSPC, 'fixture full')
            return publish(target)

        with patch.object(config_store.Target, 'publish', fail_mime), self.assertRaises(OSError):
            desktop.adopt(plan)
        self.assertEqual(json.loads(plan.journal.read_bytes())['state'], 'prepared')
        # No MIME change was published, so its absent readback is not a conflict.
        result = desktop.rollback(plan.journal)
        self.assertEqual(mime.read_bytes(), original)
        self.assertFalse(plan.files[0].path.exists())
        self.assertEqual(result.state, 'rolled-back')

    def test_later_external_mime_reference_retains_new_handler(self):
        self.defaults.update(dict.fromkeys(desktop.SCHEMES, 'existing.desktop'))
        plan = self.plan()
        self.assertFalse(any(item.path.name == 'mimeapps.list' for item in plan.files))
        desktop.adopt(plan)
        handler = next(item.path for item in plan.files if item.path.name.endswith('-url.desktop'))
        mime = self.root / 'config/mimeapps.list'
        mime.parent.mkdir()
        mime.write_text(f'[Default Applications]\n{desktop.SCHEMES[0]}={handler.name};\n')
        original = mime.read_bytes()
        self.defaults.update(dict.fromkeys(desktop.SCHEMES, ''))
        result = desktop.rollback(plan.journal)
        self.assertEqual(result.conflicts, (str(handler),))
        self.assertTrue(handler.is_file())
        self.assertEqual(mime.read_bytes(), original)

    def test_private_coordination_rejects_links_and_shared_permissions(self):
        root = Path(constants.COORDINATION_DIR)
        root.mkdir(mode=0o755)
        with self.assertRaises(Exception):
            with desktop.integration_lock():
                self.fail('Shared coordination directory admitted')
        root.rmdir()
        root.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(Exception):
            with desktop.integration_lock():
                self.fail('Linked coordination directory admitted')

    def test_integration_lock_serializes_real_processes_with_different_xdg_roots(self):
        program = """
import sys
from pathlib import Path
from logos_desktop import constants,desktop
constants.COORDINATION_DIR=Path(sys.argv[1])
with desktop.integration_lock():
    print('acquired',flush=True)
    sys.stdin.readline()
"""
        processes = []
        try:
            for name in ('first', 'second'):
                environment = dict(os.environ, XDG_STATE_HOME=str(self.root / name))
                child = subprocess.Popen([sys.executable, '-B', '-c', program, str(constants.COORDINATION_DIR)],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, env=environment)
                processes.append(child)
                if name == 'first':
                    self.assertEqual(child.stdout.readline().strip(), 'acquired')
            with self.assertRaises(subprocess.TimeoutExpired):
                processes[1].communicate('\n', timeout=0.2)
            processes[0].communicate('\n', timeout=5)
            output, errors = processes[1].communicate(timeout=5)
            self.assertEqual(output.strip(), 'acquired', errors)
        finally:
            for child in processes:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()

    def test_real_gio_launch_preserves_profile_unicode_percent_and_url_arguments(self):
        system_python = next((path for path in ('/usr/bin/python', '/usr/bin/python3') if Path(path).is_file()), None)
        if system_python is None:
            self.skipTest('System Python GIO bindings are unavailable')
        probe = subprocess.run([system_python, '-c', 'from gi.repository import Gio'], capture_output=True)
        if probe.returncode:
            self.skipTest('System Python GIO bindings are unavailable')
        script = self.root / 'capture Space 文.py'
        output = self.root / 'received.json'
        script.write_text('import json,sys\nfrom pathlib import Path\n'
                          'Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n')
        config = self.root / 'profile Space 文%u.json'
        plan = desktop.preview(config, [sys.executable, str(script), str(output), 'literal%u'],
                               windows_executable=r'C:\Logos Space\Logos.exe',
                               data_home=self.root / 'share', config_home=self.root / 'config')
        desktop.adopt(plan)
        handler = next(item.path for item in plan.files if item.path.name.endswith('-url.desktop'))
        subprocess.run(['desktop-file-validate', str(handler)], capture_output=True, check=True)
        program = """
import sys
from gi.repository import Gio
entry=Gio.DesktopAppInfo.new_from_filename(sys.argv[1])
assert entry is not None
entry.launch_uris(['logos4:fixture-only'],None)
"""
        subprocess.run([system_python, '-c', program, str(handler)], capture_output=True, check=True)
        deadline = time.monotonic() + 5
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(json.loads(output.read_text()),
                         ['literal%u', '-c', str(config), '--wine', r'C:\Logos Space\Logos.exe',
                          'logos4:fixture-only'])

    def assert_legacy_handoff(self, directory, command_template):
        install = self.root / directory
        config = self.root / 'selected.json'
        config.write_text(json.dumps({'install_dir': str(install), 'faithlife_product': 'Logos'}))
        original_config = config.read_bytes()
        launcher = [str(install / constants.BINARY_NAME)]
        entries = self.root / 'share/applications'
        entries.mkdir(parents=True)
        legacy = entries / 'Logos-url-handler.desktop'
        command = command_template.format(root=self.root, binary=constants.BINARY_NAME)
        legacy.write_bytes(('[Desktop Entry]\r\nType=Application\r\nName=Logos URL Handler\r\n'
                            'Exec=' + command + '\r\n# Preserve original bytes\r\n').encode())
        original = legacy.read_bytes()
        mime = self.root / 'config/mimeapps.list'
        mime.parent.mkdir()
        prior_mime = ('# Original associations\r\n[Default Applications]\r\n'
                      + ''.join(f'{scheme}={legacy.name};\r\n' for scheme in desktop.SCHEMES)
                      + '[Added Associations]\r\ntext/plain=fixture-editor.desktop;\r\n').encode()
        mime.write_bytes(prior_mime)

        def query(scheme):
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(mime)
            return parser.get('Default Applications', scheme, fallback='').rstrip(';')

        with patch.object(desktop, '_default', side_effect=query):
            plan = desktop.preview(config, launcher, windows_executable=r'C:\Logos.exe',
                                   data_home=self.root / 'share', config_home=self.root / 'config')
            self.assertEqual(len(plan.files), 4)
            self.assertEqual(plan.issues, ())
            self.assertEqual(dict(plan.checks), {config: original_config, legacy: original})
            self.assertEqual(desktop.adopt(plan).state, 'committed')
            handler = next(item.path.name for item in plan.files if item.path.name.endswith('-url.desktop'))
            self.assertTrue(all(query(scheme) == handler for scheme in desktop.SCHEMES))
            self.assertEqual(legacy.read_bytes(), original)
            result = desktop.rollback(plan.journal)
            self.assertTrue(all(query(scheme) == legacy.name for scheme in desktop.SCHEMES))
        self.assertEqual(result.state, 'rolled-back')
        self.assertEqual(result.conflicts, ())
        self.assertEqual(mime.read_bytes(), prior_mime)
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(config.read_bytes(), original_config)

    def test_proven_legacy_handler_handoff_records_and_restores_prior_default(self):
        self.assert_legacy_handoff(
            'installation', r'{root}/installation/{binary} "--wine" "C:\\\\Logos.exe" %u',
        )

    def test_historical_safe_launcher_handoff_restores_exact_bytes_and_defaults(self):
        self.assert_legacy_handoff(
            'installation', r'"{root}/installation/{binary}" "--wine" "C:\\\\Logos.exe" %u',
        )

    def test_historical_complex_launcher_handoff_restores_exact_bytes_and_defaults(self):
        self.assert_legacy_handoff(
            'Space 文 $`', r'"{root}/Space 文 \\$\\`/{binary}" "--wine" "C:\\\\Logos.exe" %u',
        )

    def test_historical_percent_env_handoff_restores_exact_bytes_and_defaults(self):
        self.assert_legacy_handoff(
            '100% launcher',
            r'"/usr/bin/env" "--" "{root}/100%% launcher/{binary}" "--wine" "C:\\\\Logos.exe" %u',
        )

    def test_legacy_recognition_rejects_mismatched_profile_and_nonexact_entries(self):
        install = self.root / 'installation'
        config = self.root / 'selected.json'
        config.write_text(json.dumps({'install_dir': str(install), 'faithlife_product': 'Logos'}))
        launcher = [str(install / constants.BINARY_NAME)]
        entries = self.root / 'share/applications'
        entries.mkdir(parents=True)
        legacy = entries / 'Logos-url-handler.desktop'
        # Both accepted representations are frozen templates, independent of the formatter.
        commands = (
            r'{root}/installation/{binary} "--wine" "C:\\\\Logos.exe" %u',
            r'"{root}/installation/{binary}" "--wine" "C:\\\\Logos.exe" %u',
        )
        for template in commands:
            command = template.format(root=self.root, binary=constants.BINARY_NAME)
            for name, mutation, selected, entry_type, entry_name in (
                ('other-profile', command, [str(self.root / 'other' / constants.BINARY_NAME)],
                 'Application', 'Logos URL Handler'),
                ('wrong-executable', command.replace('/installation/', '/other/'), launcher,
                 'Application', 'Logos URL Handler'),
                ('extra-argument', command + ' "--extra"', launcher, 'Application', 'Logos URL Handler'),
                ('different-type', command, launcher, 'Link', 'Logos URL Handler'),
                ('different-name', command, launcher, 'Application', 'Other URL Handler'),
                ('different-filename', command, launcher, 'Application', 'Logos URL Handler'),
            ):
                with self.subTest(template=template, case=name):
                    content = (f'[Desktop Entry]\nType={entry_type}\nName={entry_name}\n'
                               f'Exec={mutation}\n').encode()
                    legacy.write_bytes(content)
                    previous = 'other.desktop' if name == 'different-filename' else legacy.name
                    self.defaults.update(dict.fromkeys(desktop.SCHEMES, previous))
                    plan = desktop.preview(config, selected, windows_executable=r'C:\Logos.exe',
                                           data_home=self.root / 'share', config_home=self.root / 'config')
                    self.assertEqual(len(plan.files), 3)
                    self.assertEqual(len(plan.issues), 2)
                    self.assertEqual(plan.checks, ())
                    self.assertFalse((self.root / 'config').exists())
                    self.assertEqual(legacy.read_bytes(), content)
