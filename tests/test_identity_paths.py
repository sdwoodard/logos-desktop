"""Preserved configuration and recovery guards after legacy adoption retirement."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from logos_desktop import config_store, constants, maintenance, runtime
from logos_desktop.config import EphemeralConfiguration


class TestIdentityPaths(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.install = self.root / 'Logos Space 文'
        self.install.mkdir()
        self.source = self.root / 'legacy.conf'
        self.source.write_text(f'INSTALLDIR="{self.install}"\nFLPRODUCT=Logos\nTARGETVERSION=10\n')
        self.destination = self.root / 'config/logos-desktop/config.json'
        for name, value in (('STATE_DIR', str(self.root / 'state/logos-desktop')),
                            ('COORDINATION_DIR', self.root / 'coordination')):
            selected = patch.object(constants, name, value)
            selected.start()
            self.addCleanup(selected.stop)

    def test_diagnostic_loading_never_migrates_legacy_candidate(self):
        with patch.object(constants, 'DEFAULT_CONFIG_PATH', str(self.destination)):
            with patch.object(constants, 'LEGACY_CONFIG_FILES', [str(self.source)]):
                with patch.dict(os.environ, {}, clear=True):
                    selected = EphemeralConfiguration.load()
        self.assertEqual(selected.config_path, str(self.source))
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / 'state').exists())

    def test_new_portable_snapshot_preserves_old_portable_bytes(self):
        old = self.install / 'previous-controller.json'
        old.write_bytes(b'legacy launcher profile')
        with runtime.configuration_lock():
            config_store.save(self.destination, {'install_dir': str(self.install), 'faithlife_product': 'Logos'})
        self.assertEqual(old.read_bytes(), b'legacy launcher profile')
        self.assertEqual((self.install / constants.PORTABLE_CONFIG_NAME).read_bytes(), self.destination.read_bytes())

    def test_runtime_recovery_refuses_unselected_historical_targets_without_mutation(self):
        from tests.test_artifact_consumers import fixture_app

        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                app = fixture_app(self.root / ('old' if legacy else 'new'))
                install = Path(app.conf.install_dir)
                app.conf._overrides.config_path = str(install / 'previous-controller.json')
                links, configs = runtime._targets(app)
                if legacy:
                    configs[0] = install / 'previous-portable.json'
                snapshots = [runtime._snapshot(path) for path in (*links, *configs)]
                record = {'install': str(install), 'previous': snapshots, 'next': snapshots}
                journal = runtime.selection_state(install) / 'runtime.json'
                journal.write_text(json.dumps(record))
                if legacy:
                    original = journal.read_bytes()
                    with self.assertRaisesRegex(maintenance.RecoveryRequired, "previous controller"):
                        runtime._recover_locked(app, maintenance.Context(Path(app.conf.wine_prefix), install, 'Logos'))
                    self.assertEqual(journal.read_bytes(), original)
                else:
                    runtime._recover_locked(app, maintenance.Context(Path(app.conf.wine_prefix), install, 'Logos'))
                    self.assertFalse(journal.exists())

    def test_identity_roots_are_absolute_and_coordination_ignores_home_and_xdg(self):
        program = """
import json,os,pwd
from pathlib import Path
from logos_desktop import constants
print(json.dumps([constants.DEFAULT_CONFIG_PATH,constants.STATE_DIR,constants.DATA_HOME,
                 str(constants.COORDINATION_DIR),pwd.getpwuid(os.getuid()).pw_dir]))
"""
        environment = dict(os.environ, HOME=str(self.root), XDG_CONFIG_HOME='relative',
                           XDG_STATE_HOME='relative', XDG_DATA_HOME='relative')
        output = subprocess.check_output([sys.executable, '-B', '-c', program], text=True, env=environment)
        config, state, data, coordination, passwd_home = json.loads(output)
        self.assertEqual(config, str(self.root / '.config/logos-desktop/config.json'))
        self.assertEqual(state, str(self.root / '.local/state/logos-desktop'))
        self.assertEqual(data, str(self.root / '.local/share/logos-desktop'))
        self.assertEqual(coordination, str(Path(passwd_home) / '.local/state/logos-desktop/coordination'))
