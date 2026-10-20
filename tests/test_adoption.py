import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ou_dedetai import adoption, config_store, constants, maintenance, runtime
from ou_dedetai.config import Config, EphemeralConfiguration, PersistentConfiguration


class TestAdoption(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.install = self.root / 'Logos Space 文'
        self.prefix = self.install / 'data/wine64_bottle'
        self.prefix.mkdir(parents=True)
        self.source = self.root / 'legacy.conf'
        self.original = (f'INSTALLDIR="{self.install}"\nFLPRODUCT=Logos\nTARGETVERSION=10\n'
                         'WINE_EXE=data/bin/wine\nDEBUG=false\n').encode()
        self.source.write_bytes(self.original)
        self.destination = self.root / 'config/logos-desktop/config.json'
        self.addCleanup(patch.stopall)
        patch.object(constants, 'STATE_DIR', str(self.root / 'state/logos-desktop')).start()
        patch.object(constants, 'COORDINATION_DIR', self.root / 'coordination').start()

    def plan(self):
        return adoption.preview(adoption.discover([self.source]), self.destination)

    def test_read_first_shell_adoption_restart_idempotence_and_rollback(self):
        plan = self.plan()
        self.assertFalse(self.destination.parent.exists())
        result = adoption.adopt(plan)
        self.assertEqual(result.state, 'committed')
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse((self.install / 'oudedetai.json').exists())
        self.assertFalse((self.install / constants.PORTABLE_CONFIG_NAME).exists())
        loaded = PersistentConfiguration.load_from_path(str(self.destination), environment=False)
        self.assertEqual(loaded.install_dir, str(self.install))
        self.assertEqual(loaded.wine_binary, str(self.install / 'data/bin/wine'))
        self.assertNotIn('agreed_to_faithlife_terms', json.loads(self.destination.read_bytes()))
        self.assertEqual(adoption.adopt(plan), result)

        program = """
import sys
from pathlib import Path
from ou_dedetai import adoption,constants
constants.STATE_DIR=sys.argv[1]
constants.COORDINATION_DIR=Path(sys.argv[3])
print(adoption.rollback(sys.argv[2]).state)
"""
        child = subprocess.run([sys.executable, '-B', '-c', program, constants.STATE_DIR, str(result.journal),
                                str(constants.COORDINATION_DIR)],
                               capture_output=True, text=True, check=True)
        self.assertEqual(child.stdout.strip(), 'rolled-back')
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(adoption.rollback(result.journal).state, 'rolled-back')

    def test_json_portable_and_ambiguity_require_selection(self):
        portable = self.install / 'oudedetai.json'
        portable.write_text(json.dumps({'INSTALLDIR': '.', 'FLPRODUCT': 'Logos', 'future': {'kept': True}}))
        found = adoption.discover([self.source, portable])
        self.assertEqual(len(found.candidates), 2)
        with self.assertRaises(adoption.AdoptionError):
            adoption.preview(found, self.destination)
        plan = adoption.preview(found.candidates[1], self.destination, overrides={'DEBUG': True})
        adoption.adopt(plan)
        document = json.loads(self.destination.read_bytes())
        self.assertEqual(document['future'], {'kept': True})
        self.assertTrue(document['DEBUG'])
        self.assertEqual(json.loads(portable.read_bytes())['INSTALLDIR'], '.')

    def test_reconstruct_only_chosen_conventional_installation(self):
        executable = self.prefix / 'drive_c/users/fixture/AppData/Local/Logos/Logos.exe'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fixture, never executed')
        found = adoption.discover([], installation=self.install)
        self.assertEqual(found.issues, ())
        self.assertIsNone(found.candidates[0].source)
        adoption.adopt(adoption.preview(found, self.destination))
        self.assertEqual(executable.read_bytes(), b'fixture, never executed')
        self.assertEqual(json.loads(self.destination.read_bytes())['WINEPREFIX'], str(self.prefix))
        loaded = PersistentConfiguration.load_from_path(str(self.destination), environment=False)
        self.assertIsNone(loaded.wine_binary)
        self.assertIsNone(loaded.wine_binary_code)

    def test_reconstruction_rejects_multiple_matching_users_and_data_profiles(self):
        first = self.prefix / 'drive_c/users/first/AppData/Local/Logos/Logos.exe'
        second = self.prefix / 'drive_c/users/second/AppData/Local/Logos/Logos.exe'
        for executable in (first, second):
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'not executed')
        found = adoption.discover([], installation=self.install)
        self.assertFalse(found.candidates)
        self.assertIn('Wine user', found.issues[0])
        shutil.rmtree(self.prefix / 'drive_c/users/second')
        profiles = first.parent / 'Data'
        (profiles / 'first-profile').mkdir(parents=True)
        selected = adoption.discover([], installation=self.install)
        plan = adoption.preview(selected, self.destination)
        (profiles / 'second-profile').mkdir()
        found = adoption.discover([], installation=self.install)
        self.assertFalse(found.candidates)
        self.assertIn('data profile', found.issues[0])
        with self.assertRaisesRegex(adoption.AdoptionError, 'data profile'):
            adoption.adopt(plan)
        self.assertFalse(self.destination.exists())

    def test_unrelated_first_user_refuses_reconstruction_and_configured_adoption(self):
        users = self.prefix / 'drive_c/users'
        unrelated = users / 'unrelated'
        unrelated.mkdir(parents=True)
        executable = users / 'actual/AppData/Local/Logos/Logos.exe'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fixture, never executed')
        (users / 'Public').mkdir()
        document, _ = config_store.read_document(self.source)
        overrides = EphemeralConfiguration.project(document, self.source, environment={})
        conf = Config(overrides, SimpleNamespace())
        with patch('ou_dedetai.config.os.listdir', return_value=['unrelated', 'actual', 'Public']):
            self.assertEqual(conf.wine_user, 'unrelated')
            self.assertEqual(Path(conf._logos_appdata_dir), unrelated / 'AppData/Local/Logos')
            for found in (adoption.discover([], installation=self.install), adoption.discover([self.source])):
                self.assertFalse(found.candidates)
                self.assertIn('multiple Wine users', found.issues[0])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / 'state').exists())

        unrelated.rmdir()
        selected = adoption.discover([], installation=self.install)
        self.assertEqual(selected.issues, ())
        adoption.adopt(adoption.preview(selected, self.destination))
        saved, _ = config_store.read_document(self.destination)
        conf = Config(EphemeralConfiguration.project(saved, self.destination, environment={}), SimpleNamespace())
        self.assertEqual(conf.wine_user, 'actual')
        self.assertEqual(Path(conf._logos_appdata_dir), executable.parent)

    def test_configured_custom_prefix_rechecks_user_ambiguity_before_publication(self):
        prefix = self.root / 'chosen-prefix'
        users = prefix / 'drive_c/users'
        executable = users / 'actual/AppData/Local/Logos/Logos.exe'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fixture, never executed')
        source = self.root / 'custom.json'
        document = {'install_dir': str(self.install), 'faithlife_product': 'Logos', 'WINEPREFIX': str(prefix)}
        source.write_text(json.dumps(document))
        original = source.read_bytes()
        selected = adoption.discover([source])
        self.assertEqual(selected.candidates[0].prefix, prefix)
        plan = adoption.preview(selected, self.destination)
        unrelated = users / 'unrelated'
        unrelated.mkdir()
        found = adoption.discover([source])
        self.assertFalse(found.candidates)
        self.assertIn('multiple Wine users', found.issues[0])
        with self.assertRaisesRegex(adoption.AdoptionError, 'multiple Wine users'):
            adoption.adopt(plan)
        self.assertFalse(self.destination.exists())
        self.assertEqual(source.read_bytes(), original)

        unrelated.rmdir()
        adoption.adopt(plan)
        saved, _ = config_store.read_document(self.destination)
        conf = Config(EphemeralConfiguration.project(saved, self.destination, environment={}), SimpleNamespace())
        self.assertEqual(conf.wine_prefix, str(prefix))
        self.assertEqual(conf.wine_user, 'actual')
        self.assertEqual(Path(conf._logos_appdata_dir), executable.parent)

    def test_non_directory_user_entries_cannot_bypass_layout_validation(self):
        users = self.prefix / 'drive_c/users'
        executable = users / 'actual/AppData/Local/Logos/Logos.exe'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fixture, never executed')
        unrelated = users / 'unrelated'
        for linked in (False, True):
            with self.subTest(linked=linked):
                if linked:
                    unrelated.symlink_to(users / 'actual', target_is_directory=True)
                else:
                    unrelated.write_bytes(b'unrelated file')
                for found in (adoption.discover([], installation=self.install), adoption.discover([self.source])):
                    self.assertFalse(found.candidates)
                    self.assertIn('linked or non-directory Wine user', found.issues[0])
                unrelated.unlink()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_future_schema_and_malformed_config_do_not_fall_back_to_reconstruction(self):
        portable = self.install / 'oudedetai.json'
        portable.write_text(json.dumps({'schema_version': 2, 'install_dir': str(self.install),
                                        'faithlife_product': 'Logos'}))
        found = adoption.discover([], installation=self.install)
        self.assertFalse(found.candidates)
        self.assertTrue(found.issues)
        self.assertFalse(self.destination.exists())

    def test_changed_source_competing_destination_links_and_context_overrides(self):
        plan = self.plan()
        self.source.write_bytes(self.original + b'VERBOSE=true\n')
        with self.assertRaises(adoption.AdoptionError):
            adoption.adopt(plan)
        self.source.write_bytes(self.original)
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b'unrelated')
        with self.assertRaises(adoption.AdoptionError):
            adoption.adopt(plan)
        self.assertEqual(self.destination.read_bytes(), b'unrelated')
        self.destination.unlink()
        self.destination.symlink_to(self.source)
        with self.assertRaises(OSError):
            adoption.adopt(plan)
        self.destination.unlink()
        with self.assertRaises(adoption.AdoptionError):
            adoption.preview(plan.candidate, self.destination, overrides={'WINEPREFIX': str(self.install)})

    def test_failure_after_publication_is_recoverable_and_later_edits_conflict(self):
        plan = self.plan()
        publish = config_store.Target.publish

        def fail_readback(target):
            if target.path == plan.journal and target.previous is not None:
                raise OSError(errno.ENOSPC, 'fixture full')
            return publish(target)

        with patch.object(config_store.Target, 'publish', fail_readback), self.assertRaises(OSError):
            adoption.adopt(plan)
        self.assertTrue(self.destination.is_file())
        self.assertEqual(json.loads(plan.journal.read_bytes())['state'], 'prepared')
        self.destination.write_bytes(b'later user edit')
        result = adoption.rollback(plan.journal)
        self.assertEqual(result.state, 'conflict')
        self.assertEqual(self.destination.read_bytes(), b'later user edit')
        self.destination.write_bytes(plan.content)
        self.assertEqual(adoption.rollback(plan.journal).state, 'rolled-back')
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_failure_before_intent_preserves_destination_absence(self):
        plan = self.plan()
        with patch.object(os, 'fsync', side_effect=OSError(errno.ENOSPC, 'fixture full')):
            with self.assertRaises(OSError):
                adoption.adopt(plan)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_no_clobber_preserves_racing_identical_file_and_rollback_ownership(self):
        plan = self.plan()
        validate = config_store.Target.validate

        def competing_publication(target):
            validate(target)
            if target.path == self.destination and target.temporary is not None:
                self.destination.write_bytes(plan.content)

        with patch.object(config_store.Target, 'validate', competing_publication):
            with self.assertRaises(FileExistsError):
                adoption.adopt(plan)
        self.assertEqual(self.destination.read_bytes(), plan.content)
        result = adoption.rollback(plan.journal)
        self.assertEqual(result.state, 'conflict')
        self.assertEqual(result.conflicts, (str(self.destination),))
        self.assertEqual(self.destination.read_bytes(), plan.content)

    def test_distinct_prefixes_and_xdg_states_share_adopt_and_rollback_destination_lock(self):
        other = self.root / 'other-installation'
        (other / 'data/wine64_bottle').mkdir(parents=True)
        other_source = self.root / 'other.json'
        other_source.write_text(json.dumps({'install_dir': str(other), 'faithlife_product': 'Logos'}))
        program = """
import sys
from pathlib import Path
from ou_dedetai import adoption,constants,maintenance
constants.COORDINATION_DIR=Path(sys.argv[3])
plan=adoption.preview(adoption.discover([sys.argv[1]]),sys.argv[2])
if sys.argv[4]=='first':
    publish=adoption._publish_absent
    def hold(target):
        print(str(plan.journal),flush=True)
        sys.stdin.readline()
        publish(target)
    adoption._publish_absent=hold
    assert adoption.adopt(plan).state=='committed'
else:
    for action in (lambda: adoption.adopt(plan),lambda: adoption.rollback(sys.argv[5])):
        try:
            action()
        except maintenance.MaintenanceBusy:
            print('busy')
        else:
            raise AssertionError('Competing destination operation was admitted')
    assert not plan.journal.exists()
"""
        arguments = [sys.executable, '-B', '-c', program]
        first_state = self.root / 'xdg-first'
        first = subprocess.Popen([*arguments, str(self.source), str(self.destination),
                                  str(constants.COORDINATION_DIR), 'first'],
                                 env=dict(os.environ, XDG_STATE_HOME=str(first_state)),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            journal = Path(first.stdout.readline().strip())
            self.assertTrue(journal.is_file())
            second_state = self.root / 'xdg-second'
            second = subprocess.run([*arguments, str(other_source), str(self.destination),
                                     str(constants.COORDINATION_DIR), 'second', str(journal)],
                                    env=dict(os.environ, XDG_STATE_HOME=str(second_state)),
                                    capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(second.stdout.splitlines(), ['busy', 'busy'])
            output, errors = first.communicate('\n', timeout=10)
            self.assertEqual(first.returncode, 0, output + errors)
            committed = self.destination.read_bytes()
            with patch.object(constants, 'STATE_DIR', str(second_state / 'logos-desktop')):
                with self.assertRaises(adoption.AdoptionError):
                    adoption.adopt(adoption.preview(adoption.discover([other_source]), self.destination))
            self.assertEqual(self.destination.read_bytes(), committed)
            with patch.object(constants, 'STATE_DIR', str(first_state / 'logos-desktop')):
                self.assertEqual(adoption.rollback(journal).state, 'rolled-back')
        finally:
            if first.poll() is None:
                first.kill()
            first.wait(timeout=5)
            for stream in (first.stdin, first.stdout, first.stderr):
                stream.close()

    def test_destination_lock_rejects_unsafe_coordination_and_canonical_alias(self):
        root = constants.COORDINATION_DIR
        root.mkdir(mode=0o755)
        with self.assertRaises(maintenance.MaintenanceError):
            with adoption._destination_lock(self.destination):
                self.fail('Shared coordination root admitted')
        root.chmod(0o700)
        alias = self.destination.parent / 'uncreated/..' / self.destination.name
        with adoption._destination_lock(self.destination):
            with self.assertRaises(maintenance.MaintenanceBusy):
                with adoption._destination_lock(alias):
                    self.fail('Canonical alias bypassed the destination lock')

    def test_diagnostic_loading_never_migrates_legacy_candidate(self):
        with patch.object(constants, 'DEFAULT_CONFIG_PATH', str(self.destination)):
            with patch.object(constants, 'LEGACY_CONFIG_FILES', [str(self.source)]):
                with patch.dict(os.environ, {}, clear=True):
                    selected = EphemeralConfiguration.load()
        self.assertEqual(selected.config_path, str(self.source))
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / 'state').exists())

    def test_new_portable_snapshot_preserves_old_portable_bytes(self):
        old = self.install / 'oudedetai.json'
        old.write_bytes(b'legacy launcher profile')
        with runtime.configuration_lock():
            config_store.save(self.destination, {'install_dir': str(self.install), 'faithlife_product': 'Logos'})
        self.assertEqual(old.read_bytes(), b'legacy launcher profile')
        self.assertEqual((self.install / constants.PORTABLE_CONFIG_NAME).read_bytes(), self.destination.read_bytes())

    def test_new_and_old_runtime_journal_target_sets_remain_recoverable(self):
        from tests.test_artifact_consumers import fixture_app

        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                app = fixture_app(self.root / ('old' if legacy else 'new'))
                install = Path(app.conf.install_dir)
                app.conf._overrides.config_path = str(install / 'oudedetai.json')
                links, configs = runtime._targets(app, legacy_portable=legacy)
                snapshots = [runtime._snapshot(path) for path in (*links, *configs)]
                record = {'install': str(install), 'previous': snapshots, 'next': snapshots}
                journal = runtime.selection_state(install) / 'runtime.json'
                journal.write_text(json.dumps(record))
                runtime._recover_locked(app, maintenance.Context(Path(app.conf.wine_prefix), install, 'Logos'))
                self.assertFalse(journal.exists())

    def test_identity_roots_are_absolute_and_coordination_ignores_home_and_xdg(self):
        program = """
import json,os,pwd
from pathlib import Path
from ou_dedetai import constants
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
