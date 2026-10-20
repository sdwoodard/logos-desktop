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

from ou_dedetai import adoption, config_store, constants, desktop


class TestDesktopIntegration(unittest.TestCase):
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
                self.assertIn('"/usr/bin/env" "--"', text)
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
        with self.assertRaises(adoption.AdoptionError):
            self.plan('another.json', url=False)

    def test_mime_change_after_preview_and_after_commit_preserved(self):
        plan = self.plan()
        self.defaults[desktop.SCHEMES[0]] = 'other.desktop'
        with self.assertRaises(adoption.AdoptionError):
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
from ou_dedetai import constants,desktop
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
        probe = subprocess.run(['/usr/bin/python', '-c', 'from gi.repository import Gio'], capture_output=True)
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
        subprocess.run(['/usr/bin/python', '-c', program, str(handler)], capture_output=True, check=True)
        deadline = time.monotonic() + 5
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(json.loads(output.read_text()),
                         ['literal%u', '-c', str(config), '--wine', r'C:\Logos Space\Logos.exe',
                          'logos4:fixture-only'])

    def test_proven_legacy_handler_handoff_records_and_restores_prior_default(self):
        from ou_dedetai.installer import desktop_exec

        install = self.root / 'installation'
        config = self.root / 'selected.json'
        config.write_text(json.dumps({'install_dir': str(install), 'faithlife_product': 'Logos'}))
        launcher = [str(install / constants.BINARY_NAME)]
        entries = self.root / 'share/applications'
        entries.mkdir(parents=True)
        legacy = entries / 'Logos-url-handler.desktop'
        command = desktop_exec([*launcher, '--wine', r'C:\Logos.exe'], accepts_url=True)
        legacy.write_text('[Desktop Entry]\nType=Application\nName=Logos URL Handler\nExec=' + command + '\n')
        original = legacy.read_bytes()
        self.defaults.update(dict.fromkeys(desktop.SCHEMES, legacy.name))
        plan = desktop.preview(config, launcher, windows_executable=r'C:\Logos.exe',
                               data_home=self.root / 'share', config_home=self.root / 'config')
        self.assertEqual(len(plan.files), 4)

        def query(scheme):
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(self.root / 'config/mimeapps.list')
            return parser.get('Default Applications', scheme, fallback=legacy.name).rstrip(';')

        with patch.object(desktop, '_default', side_effect=query):
            desktop.adopt(plan)
            self.assertEqual(legacy.read_bytes(), original)
            result = desktop.rollback(plan.journal)
        self.assertEqual(result.state, 'rolled-back')
        self.assertFalse((self.root / 'config/mimeapps.list').exists())
        self.assertEqual(legacy.read_bytes(), original)
