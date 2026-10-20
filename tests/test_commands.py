import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from ou_dedetai import commands, network, utils, maintenance


class TestHeadlessCommands(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.install = self.root / 'installation'
        (self.install / 'data/wine64_bottle').mkdir(parents=True)
        self.config = self.root / 'selected.json'
        self.config.write_text(json.dumps({
            'install_dir': str(self.install), 'faithlife_product': 'Logos',
            'faithlife_product_version': '10', 'backup_dir': 'backups',
        }))
        self.environment = {key: value for key, value in os.environ.items()
                            if key not in ('CONFIG_FILE', 'INSTALLDIR', 'WINEPREFIX', 'FLPRODUCT')}
        self.environment.update(HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / 'config'),
                                XDG_STATE_HOME=str(self.root / 'state'),
                                XDG_CACHE_HOME=str(self.root / 'cache'),
                                XDG_DATA_HOME=str(self.root / 'data'))

    def run_command(self, *arguments):
        program = """
import sys
from ou_dedetai.main import main
try:
    main()
finally:
    assert not any(name.startswith(('PySide6', 'ou_dedetai.qt')) for name in sys.modules)
"""
        result = subprocess.run([sys.executable, '-B', '-c', program, *map(str, arguments)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                env=self.environment, timeout=20)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('\x1b', result.stdout)
        return result

    def test_selected_doctor_does_not_create_logs_or_state(self):
        before = set(self.root.rglob('*'))
        result = self.run_command('-c', self.config, '--doctor')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('architecture:', result.stdout)
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_missing_default_discovers_without_selecting_and_explicit_config_wins(self):
        legacy = self.root / 'config/FaithLife-Community/oudedetai.json'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(self.config.read_bytes())
        result = self.run_command('--doctor')
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(legacy), result.stdout)
        self.assertIn('--adopt-installation', result.stderr)
        result = self.run_command('--discover-installations')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_command('-c', self.config, '--doctor')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(str(legacy), result.stdout)
        self.assertFalse((self.root / 'config/logos-desktop/config.json').exists())

    def test_explicit_legacy_cli_actions_refuse_missing_default_without_qt(self):
        legacy = self.root / 'config/FaithLife-Community/oudedetai.json'
        legacy.parent.mkdir(parents=True)
        original = self.config.read_bytes()
        legacy.write_bytes(original)
        for action in ('--run-installed-app', '--backup', '--repair', '--status'):
            with self.subTest(action=action):
                result = self.run_command('--dialog', 'qt', action)
                self.assertEqual(result.returncode, 1)
                self.assertIn(str(legacy), result.stdout)
                self.assertIn('Configuration is missing', result.stderr)
                self.assertEqual(legacy.read_bytes(), original)
        self.assertFalse((self.root / 'config/logos-desktop/config.json').exists())

    def test_export_prints_exact_snapshot_and_requires_explicit_confirmation(self):
        output = self.root / 'support.zip'
        result = self.run_command('-c', self.config, '--support-export', output, '-y')
        self.assertEqual(result.returncode, 1)
        self.assertIn('=== manifest.json ===', result.stdout)
        self.assertIn('--confirm-export', result.stderr)
        self.assertFalse(output.exists())
        result = self.run_command('-c', self.config, '--support-export', output, '--confirm-export')
        self.assertEqual(result.returncode, 0, result.stderr)
        with ZipFile(output) as archive:
            expected = ''.join(f'=== {name} ===\n{archive.read(name).decode()}' for name in archive.namelist())
        self.assertTrue(result.stdout.startswith(expected))
        result = self.run_command('-c', self.config, '--support-export', output, '--confirm-export')
        self.assertEqual(result.returncode, 1)
        result = self.run_command('-c', self.config, '--support-export', output,
                                  '--confirm-export', '--overwrite-export')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_confirmation_and_missing_sources_fail_safely(self):
        for arguments, message in (
            (('--adopt-installation', self.config, '-y'), '--confirm-adoption'),
            (('--rollback-adoption', self.root / 'missing', '-y'), '--confirm-adoption'),
            (('-c', self.config, '--list-backups'), 'No such file'),
            (('-c', self.root / 'missing', '--doctor'), 'Configuration is missing'),
            (('--confirm-export',), '--support-export'),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_command(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_adoption_and_rollback_round_trip_with_explicit_config_authority(self):
        destination = self.root / 'adopted.json'
        result = self.run_command('--adopt-installation', self.config,
                                  '--adoption-destination', destination, '--confirm-adoption')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(destination.is_file())
        journal = next((self.root / 'state').rglob('adoption/*.json'))
        result = self.run_command('-c', self.config, '--rollback-adoption', journal, '--confirm-adoption')
        self.assertEqual(result.returncode, 1)
        self.assertIn('does not match --config', result.stderr)
        result = self.run_command('-c', destination, '--rollback-adoption', journal, '--confirm-adoption')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(destination.exists())
        self.assertTrue(self.config.exists())

    def test_projection_uses_stored_fields_without_config_getters(self):
        with patch('ou_dedetai.config.Config', side_effect=AssertionError('No manager config')):
            conf = commands.configuration(self.config)
        self.assertEqual(conf._raw.install_dir, str(self.install))

    def test_verified_restore_requires_exact_confirmation_even_with_assume_yes(self):
        from ou_dedetai import backup, constants
        from tests.test_backup import fixture_app, hashes

        app = fixture_app(self.root)
        live = Path(app.conf._logos_appdata_dir)
        (live / 'Data').mkdir(parents=True)
        (live / 'Data/sentinel').write_text('snapshot')
        # Config resolves product data from the installed executable's directory.
        (live / 'Logos.exe').write_bytes(b'fixture')
        backup_root = self.root / 'backups'
        backup_root.mkdir()
        self.config.write_text(json.dumps({
            'install_dir': app.conf._raw.install_dir, 'faithlife_product': 'Logos',
            'faithlife_product_version': '10', 'backup_dir': str(backup_root),
        }))
        with patch.object(constants, 'STATE_DIR', str(self.root / 'state/logos-desktop')):
            snapshot = backup.BackupTask(app).run()
        (live / 'Data/sentinel').write_text('later data')
        before = hashes(live)
        result = self.run_command('-c', self.config, '--restore-snapshot', snapshot, '-y')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f'Snapshot: {snapshot}', result.stdout)
        self.assertIn(f'Target: {live}', result.stdout)
        self.assertIn('stdin reached EOF', result.stderr)
        self.assertEqual(hashes(live), before)


class TestNormalGraphicalStartup(unittest.TestCase):
    def test_actual_main_opens_missing_new_default_without_selecting_legacy(self):
        from threading import Event
        from types import SimpleNamespace
        from ou_dedetai import constants, main
        from ou_dedetai.config import Config, LegacyConfiguration

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / 'existing-installation'
            prefix = install / 'data/wine64_bottle'
            prefix.mkdir(parents=True)
            legacy = root / 'legacy.json'
            original = json.dumps({'install_dir': str(install), 'faithlife_product': 'Logos',
                                   'faithlife_product_version': '10'}).encode()
            legacy.write_bytes(original)
            destination = root / 'config/logos-desktop/config.json'
            captured = []

            def start(ephemeral, function=None, **options):
                self.assertIsNone(function)
                self.assertEqual(ephemeral.config_path, str(destination))
                self.assertIsNone(ephemeral.install_dir)
                app = SimpleNamespace(_config_updated_event=Event())
                with patch.object(Config, '_create_network_requests_with_hook', return_value=Mock()):
                    conf = Config(ephemeral, app)
                self.assertIsNone(conf._raw.install_dir)
                self.assertIsNone(conf._raw.faithlife_product)
                self.assertEqual(conf.config_file_path, str(destination))
                captured.append(conf)

            with (
                patch.dict(os.environ, {'DIALOG': 'qt'}, clear=True),
                patch.object(constants, 'DEFAULT_CONFIG_PATH', str(destination)),
                patch.object(constants, 'LEGACY_CONFIG_FILES', [str(legacy)]),
                patch.object(constants, 'COORDINATION_DIR', root / 'coordination'),
                patch.object(constants, 'STATE_DIR', str(root / 'state/logos-desktop')),
                patch.object(LegacyConfiguration, 'load', side_effect=AssertionError('No implicit legacy selection')),
                patch.dict(sys.modules, {'ou_dedetai.qt.startup': SimpleNamespace(start=start)}),
                patch.object(main.updates, 'recover_startup'),
                patch.object(main.msg, 'initialize_logging'),
                patch.object(main.system, 'check_architecture'),
            ):
                for arguments in ([], ['--dialog', 'qt']):
                    with patch.object(sys, 'argv', ['logos-desktop', *arguments]):
                        main.main()
            self.assertEqual(len(captured), 2)
            self.assertEqual(legacy.read_bytes(), original)
            self.assertFalse(destination.exists())
            self.assertEqual(list(prefix.iterdir()), [])


class TestUpdateAdmission(unittest.TestCase):
    def test_unknown_equal_and_older_versions_never_install(self):
        from types import SimpleNamespace

        for installed in (None, 'invalid', '40.0', '41.0'):
            conf = SimpleNamespace(
                faithlife_product='Logos', faithlife_product_version='10',
                refresh_installed_faithlife_product_release=lambda: installed,
                _network=SimpleNamespace(product_release=lambda *args, **kwargs:
                                         network.ProductLookup(('40.0',), 'ok')),
            )
            with patch('ou_dedetai.installer.install') as install:
                with self.assertRaises(maintenance.MaintenanceError):
                    utils.update_faithlife_product.__wrapped__(SimpleNamespace(conf=conf))
                install.assert_not_called()
