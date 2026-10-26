import json
from contextlib import contextmanager
import hashlib
import multiprocessing
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psutil

from logos_desktop import constants, instance, main, maintenance, runtime
from logos_desktop.app import App
from logos_desktop.config import Config, EphemeralConfiguration


def owner_process(root, config, connection):
    constants.COORDINATION_DIR = Path(root)
    session = instance.Session.for_config(EphemeralConfiguration.load_from_path(config))
    try:
        session.acquire()
        session.listen()
        connection.send(('owned', session.key))
        while True:
            command = connection.recv()
            if command == 'crash':
                os._exit(0)
            if command == 'close':
                break
            destination = instance.Session.for_config(EphemeralConfiguration.load_from_path(command))
            try:
                destination.acquire()
            except instance.InstanceBusy:
                connection.send('busy')
            else:
                destination.close()
                connection.send('unexpected acquisition')
    finally:
        session.close()


class FixtureApp(App):
    def _ask(self, *args):
        raise AssertionError('Unexpected prompt')

    def _info(self, *args):
        pass

    def _status(self, *args, **kwargs):
        pass

    def _pop_up(self, *args, **kwargs):
        pass


class TestInstance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='inst-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.coordination = self.root / 'coord'
        self.addCleanup(patch.stopall)
        patch.object(constants, 'COORDINATION_DIR', self.coordination).start()
        patch.object(constants, 'STATE_DIR', str(self.root / 'state')).start()
        self.first = self.profile('first', self.root / 'prefix')
        self.second = self.profile('second', self.root / 'other-prefix')

    def profile(self, name, prefix):
        path = self.root / f'{name}.json'
        path.write_text(json.dumps({'install_dir': str(self.root / name), 'WINEPREFIX': str(prefix),
                                    'faithlife_product': 'Logos', 'faithlife_product_version': '10'}))
        return path

    def session(self, path):
        session = instance.Session.for_config(EphemeralConfiguration.load_from_path(str(path)))
        self.addCleanup(session.close)
        return session

    def owner(self, path):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=owner_process, args=(str(self.coordination), str(path), child))
        process.start()
        child.close()

        def settle():
            if process.is_alive():
                parent.send('close')
            process.join(5)
            self.assertFalse(process.is_alive())
            parent.close()
            process.close()

        self.addCleanup(settle)
        self.assertTrue(parent.poll(5), 'Owner did not start')
        self.assertEqual(parent.recv()[0], 'owned')
        return process, parent

    @contextmanager
    def operation_owner(self, *, exclusive=True, role=None, pending=None, nested_locks=False):
        program = '''
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from logos_desktop import constants, maintenance, runtime
from logos_desktop.config import Config, EphemeralConfiguration
constants.COORDINATION_DIR = Path(sys.argv[1])
context = maintenance.Context(Path(sys.argv[2]), Path(sys.argv[3]), 'Logos')
with ExitStack() as stack:
    stack.enter_context(maintenance.Lease(context, exclusive=sys.argv[4] == 'yes',
                                         role=sys.argv[5] or None, recovery=bool(sys.argv[6])).entered())
    if sys.argv[7]:
        app = SimpleNamespace()
        app.conf = Config(EphemeralConfiguration.load_from_path(sys.argv[7]), app)
        stack.enter_context(runtime.activation_guard(app, exclusive=True))
        stack.enter_context(runtime.configuration_lock())
    if sys.argv[6] == 'restore':
        context.pending.write_text('preserved restore journal')
    elif sys.argv[6] == 'runtime':
        (runtime.selection_state(context.install) / 'runtime.json').write_text('preserved runtime journal')
    print('ready', flush=True)
    sys.stdin.readline()
'''
        environment = dict(os.environ, XDG_STATE_HOME=str(self.root / 'xdg-owner'))
        process = subprocess.Popen(
            [sys.executable, '-B', '-c', program, str(self.coordination), str(self.root / 'prefix'),
             str(self.root / 'first'), 'yes' if exclusive else 'no', role or '', pending or '',
             str(self.first) if nested_locks else ''],
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            yield process
        finally:
            if process.poll() is None:
                process.stdin.write('\n')
                process.stdin.flush()
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)

    def callback_process(self, *, state_home='xdg-callback'):
        product = self.root / 'prefix/drive_c/users/fixture/AppData/Local/Logos/Logos.exe'
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_bytes(b'non-executable URL fixture')
        program = '''
import sys
from pathlib import Path
from unittest.mock import patch
from logos_desktop import constants, main
constants.COORDINATION_DIR = Path(sys.argv[1])
sys.argv = ['controller', '-c', sys.argv[2], '--force-root', '--wine',
            r'C:\\users\\fixture\\AppData\\Local\\Logos\\Logos.exe', 'logos4:synthetic-private-token']
with patch('logos_desktop.wine._wine_invocation') as wine, patch('subprocess.Popen') as spawn:
    try:
        main.main()
    except SystemExit as error:
        wine.assert_not_called()
        spawn.assert_not_called()
        raise SystemExit(error.code)
    raise AssertionError('Callback was admitted')
'''
        return subprocess.run([sys.executable, '-B', '-c', program, str(self.coordination), str(self.first)],
                              env=dict(os.environ, XDG_STATE_HOME=str(self.root / state_home)),
                              capture_output=True, text=True, timeout=10)

    def test_cross_xdg_exclusive_lease_refuses_real_cli_callback_before_wine(self):
        with self.operation_owner() as owner:
            alias = self.root / 'prefix-parent-alias'
            alias.symlink_to(self.root, target_is_directory=True)
            with self.assertRaises(maintenance.MaintenanceBusy):
                maintenance.Lease(maintenance.Context(alias / 'prefix'))
            result = self.callback_process()
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('Another controller', result.stderr)
            self.assertIsNone(owner.poll())
            self.assertFalse((self.root / 'xdg-callback').exists())

    def test_runtime_and_configuration_locks_do_not_make_callback_wait_on_owner(self):
        with self.operation_owner(nested_locks=True):
            for state in ('xdg-owner', 'xdg-callback'):
                result = self.callback_process(state_home=state)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('Another controller', result.stderr)
            # Reversed acquisition also refuses immediately, rather than waiting
            # for a stable prefix lock while holding the configuration lock.
            with runtime.configuration_lock():
                with self.assertRaises(maintenance.MaintenanceBusy):
                    maintenance.Lease(maintenance.Context(self.root / 'prefix'))

    def test_cross_xdg_role_lease_is_stable_but_unroled_use_remains_shared(self):
        with self.operation_owner(exclusive=False, role='logos'):
            context = maintenance.Context(self.root / 'prefix', self.root / 'first', 'Logos')
            with self.assertRaises(maintenance.MaintenanceBusy):
                maintenance.Lease(context, role='logos')
            with maintenance.Lease(context).entered(), maintenance.Lease(context, role='index').entered():
                pass
            with self.assertRaises(maintenance.MaintenanceBusy):
                maintenance.Lease(context, exclusive=True)

    def test_known_alternate_restore_and_runtime_journals_block_after_owner_exits(self):
        for marker in ('restore', 'runtime'):
            with self.subTest(marker=marker):
                with self.operation_owner(pending=marker):
                    pass
                result = self.callback_process()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('recovery is required in another state location', result.stderr)
                base = self.root / 'xdg-owner/logos-desktop'
                records = list(base.rglob('restore.pending' if marker == 'restore' else 'runtime.json'))
                self.assertEqual(len(records), 1)
                self.assertIn('preserved', records[0].read_text())
                # Recovery admission must not silently repair the wrong root.
                with self.assertRaises(maintenance.RecoveryRequired):
                    maintenance.Lease(maintenance.Context(self.root / 'prefix', self.root / 'first'),
                                      exclusive=True, recovery=True)
                records[0].unlink()

    def test_default_root_pending_record_is_detected_without_prior_registration(self):
        context = maintenance.Context(self.root / 'prefix', self.root / 'first')
        default = self.root / 'historical-default/logos-desktop'
        with patch.object(constants, 'STATE_DIR', str(default)):
            pending = context.pending
            pending.write_text('original default-root pending record')
        with patch.object(constants, 'RECOVERY_STATE_DIR', str(default)):
            with self.assertRaises(maintenance.RecoveryRequired):
                maintenance.Lease(context)
        self.assertEqual(pending.read_text(), 'original default-root pending record')

    def test_operation_lock_failure_releases_stable_lease_and_preserves_unsafe_target(self):
        context = maintenance.Context(self.root / 'prefix')
        directory = maintenance._operation_directory(context)
        os.close(directory)
        key = hashlib.sha256(os.fsencode(context.prefix)).hexdigest()
        lock = self.coordination / 'operations' / key / 'usage.lock'
        sentinel = self.root / 'sentinel'
        sentinel.write_text('preserve')
        lock.symlink_to(sentinel)
        with self.assertRaises(OSError):
            maintenance.Lease(context)
        self.assertEqual(sentinel.read_text(), 'preserve')
        lock.unlink()
        with patch.object(maintenance, 'artifact_use', side_effect=OSError('artifact admission failed')):
            with self.assertRaises(OSError):
                maintenance.Lease(context)
        with maintenance.Lease(context, exclusive=True).entered():
            pass

    def test_corrupt_known_location_fails_closed_and_releases_operation_locks(self):
        context = maintenance.Context(self.root / 'prefix')
        directory = maintenance._operation_directory(context)
        os.close(directory)
        key = hashlib.sha256(os.fsencode(context.prefix)).hexdigest()
        record = self.coordination / 'operations' / key / 'state-broken.json'
        record.write_bytes(b'not a recovery location')
        record.chmod(0o600)
        with self.assertRaises(maintenance.RecoveryRequired):
            maintenance.Lease(context)
        self.assertEqual(record.read_bytes(), b'not a recovery location')
        record.unlink()
        with maintenance.Lease(context, exclusive=True).entered():
            pass

    def test_same_prefix_aliases_and_different_xdg_are_one_owner(self):
        process, connection = self.owner(self.first)
        alias = self.root / 'alias'
        # Canonicalization must also work before the prefix has been created.
        alias.symlink_to(self.root, target_is_directory=True)
        other_config = self.profile('alias-config', alias / 'prefix')
        program = '''
import sys
from pathlib import Path
from logos_desktop import constants, instance
from logos_desktop.config import EphemeralConfiguration
constants.COORDINATION_DIR = Path(sys.argv[1])
try:
    instance.Session.for_config(EphemeralConfiguration.load_from_path(sys.argv[2])).acquire()
except instance.InstanceBusy:
    raise SystemExit(23)
raise SystemExit(99)
'''
        result = subprocess.run([sys.executable, '-B', '-c', program, str(self.coordination), str(other_config)],
                                env=dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'config-other'),
                                         XDG_STATE_HOME=str(self.root / 'state-other')),
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr.decode())
        with self.session(self.second):
            self.assertTrue(process.is_alive())
        connection.send('close')
        process.join(5)
        with self.session(other_config):
            pass

    def test_coordination_namespace_uses_account_home_not_home_or_xdg_overrides(self):
        expected = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/logos-desktop/coordination'
        for name in ('a', 'b'):
            environment = dict(os.environ, HOME=str(self.root / f'home-{name}'),
                               XDG_STATE_HOME=str(self.root / f'state-{name}'),
                               XDG_CONFIG_HOME=str(self.root / f'config-{name}'))
            result = subprocess.run(
                [sys.executable, '-B', '-c', 'from logos_desktop.constants import COORDINATION_DIR\n'
                 'print(COORDINATION_DIR)'], env=environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(expected))

    def test_crash_stale_socket_is_replaced_without_signals(self):
        process, connection = self.owner(self.first)
        session = self.session(self.first)
        self.assertTrue(session.focus())
        connection.send('crash')
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertTrue(session.endpoint.exists())
        with patch('os.kill', side_effect=AssertionError('No PID signals')):
            session.acquire()
            session.listen()
            self.assertTrue(session.focus('activation-token'))
            received = []
            session.drain_focus(received.append)
            self.assertEqual(received, ['activation-token'])
            session.close()
        self.assertFalse(session.endpoint.exists())

    def test_two_owners_reserving_each_others_prefix_do_not_deadlock(self):
        first, first_connection = self.owner(self.first)
        second, second_connection = self.owner(self.second)
        first_connection.send(str(self.second))
        second_connection.send(str(self.first))
        for connection in (first_connection, second_connection):
            self.assertTrue(connection.poll(2))
            self.assertEqual(connection.recv(), 'busy')
        self.assertTrue(first.is_alive())
        self.assertTrue(second.is_alive())

    def test_malformed_oversized_and_slow_peers_cannot_execute_or_block_focus(self):
        with self.session(self.first) as session:
            session.listen()
            for request in (b'[]', b'{', b'{"action":"exec","token":"secret"}',
                            b'{"action":"focus","token":2}', b'x' * (instance.MAX_REQUEST + 1)):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                    peer.settimeout(1)
                    peer.connect(str(session.endpoint))
                    peer.sendall(request)
                    peer.shutdown(socket.SHUT_WR)
                    self.assertNotEqual(peer.recv(16), b'ok')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.connect(str(session.endpoint))
                peer.sendall(b'{')
                time.sleep(instance.IPC_TIMEOUT + .05)
            self.assertTrue(session.focus('good'))
            received = []
            session.drain_focus(received.append)
            self.assertEqual(received, ['good'])

    def test_unsafe_lock_and_endpoint_are_preserved(self):
        session = self.session(self.first)
        self.coordination.mkdir(mode=0o700)
        target = self.root / 'sentinel'
        target.write_text('preserve')
        lock = self.coordination / (session.key + '.lock')
        lock.symlink_to(target)
        with self.assertRaises(OSError):
            session.acquire()
        lock.unlink()
        session.acquire()
        session.endpoint.symlink_to(target)
        with self.assertRaises(maintenance.MaintenanceError):
            session.listen()
        self.assertEqual(target.read_text(), 'preserve')
        self.assertTrue(session.endpoint.is_symlink())

    def test_ownership_cannot_be_acquired_from_configuration_lock(self):
        with runtime.configuration_lock():
            with self.assertRaisesRegex(maintenance.MaintenanceError, 'before taking'):
                self.session(self.first).acquire()
        self.assertFalse(self.coordination.exists())

    def app(self):
        overrides = EphemeralConfiguration.load_from_path(str(self.first))
        overrides._instance_session = self.session(self.first).acquire()
        with patch.object(App, 'start_thread'):
            app = FixtureApp(overrides)
        self.addCleanup(app.close)
        return app

    def candidate(self, app):
        return Config(EphemeralConfiguration.load_from_path(str(self.second)), app)

    def duplicate_process(self, path):
        program = '''
import sys
from pathlib import Path
from unittest.mock import patch
from logos_desktop import constants, main
constants.COORDINATION_DIR = Path(sys.argv[1])
sys.argv = ['controller', '-c', sys.argv[2], '--dialog', 'qt', '--force-root']
with patch.object(main, '_run_owned', side_effect=AssertionError('Duplicate started a controller')):
    main.main()
'''
        return subprocess.run([sys.executable, '-B', '-c', program, str(self.coordination), str(path)],
                              capture_output=True, text=True, timeout=10)

    def test_graphical_transfer_accepts_focus_mid_body_and_drains_after_success(self):
        app = self.app()
        old = app._instance_session
        old.listen()
        peer = self.session(self.second)
        received = []
        self.assertTrue(old.focus('old-before'))
        with app.profile_selection(self.candidate(app)):
            self.assertTrue(app._profile_lock.locked())
            self.assertTrue(peer.focus('destination-during'))
            self.assertTrue(old.focus('old-during'))
            result = self.duplicate_process(self.second)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(received)

        current = app._instance_session
        self.assertTrue(current.listening)
        self.assertFalse(old.listening)
        self.assertFalse(old.endpoint.exists())
        current.drain_focus(received.append)
        self.assertEqual(received, ['old-before', 'destination-during', 'old-during', ''])

        # Reopening the retired Session must not retain the new owner's queue.
        old.acquire()
        old.listen()
        self.assertTrue(current.focus('current-only'))
        retired = []
        old.drain_focus(retired.append)
        self.assertFalse(retired)
        current.drain_focus(received.append)
        self.assertEqual(received[-1], 'current-only')
        old.close()
        self.assertTrue(app.close())
        self.assertFalse(list(self.coordination.glob('*.sock')))

    def test_failed_graphical_transfer_routes_destination_focus_to_retained_owner(self):
        app = self.app()
        old_conf, old = app.conf, app._instance_session
        old.listen()
        peer = self.session(self.second)
        self.assertTrue(old.focus('old-before'))
        with self.assertRaisesRegex(ValueError, 'publication failed'):
            with app.profile_selection(self.candidate(app)):
                self.assertTrue(peer.focus('destination-during'))
                result = self.duplicate_process(self.second)
                self.assertEqual(result.returncode, 0, result.stderr)
                raise ValueError('publication failed')
        self.assertIs(app.conf, old_conf)
        self.assertIs(app._instance_session, old)
        self.assertTrue(old.listening)
        self.assertFalse(peer.endpoint.exists())
        self.assertFalse(peer.focus('closed-destination'))
        received = []
        old.drain_focus(received.append)
        self.assertEqual(received, ['old-before', 'destination-during', ''])
        with peer:
            pass
        self.assertTrue(app.close())
        self.assertFalse(list(self.coordination.glob('*.sock')))

    def test_graphical_rollback_to_absent_profile_accepts_focus_while_held(self):
        app = self.app()
        old = app._instance_session
        old.listen()
        selected = self.root / 'rolled-back.json'
        selected.write_text('{}')
        candidate = Config(EphemeralConfiguration.load_from_path(str(selected)), app)
        peer = instance.Session.for_config(candidate)
        with app.profile_selection(candidate):
            selected.unlink()
            self.assertTrue(peer.focus('rollback-during'))
            result = self.duplicate_process(selected)
            self.assertEqual(result.returncode, 0, result.stderr)
        current = app._instance_session
        self.assertIsNone(current.prefix)
        self.assertTrue(current.listening)
        received = []
        current.drain_focus(received.append)
        self.assertEqual(received, ['rollback-during', ''])
        self.assertFalse(old.endpoint.exists())
        self.assertTrue(app.close())
        self.assertFalse(list(self.coordination.glob('*.sock')))

    def test_cli_profile_transfer_never_starts_activation_listener(self):
        app = self.app()
        peer = self.session(self.second)
        with patch.object(instance.Session, 'listen', side_effect=AssertionError('CLI started a GUI listener')):
            with app.profile_selection(self.candidate(app)):
                self.assertFalse(app._instance_session.listening)
                self.assertFalse(peer.focus('not-graphical'))
            self.assertFalse(app._instance_session.listening)
            self.assertFalse(list(self.coordination.glob('*.sock')))

    def test_graphical_transfer_shares_one_bounded_queue_between_both_endpoints(self):
        app = self.app()
        old = app._instance_session
        old.listen()
        peer = self.session(self.second)
        expected = [f'request-{number}' for number in range(instance.MAX_FOCUS_REQUESTS)]
        with app.profile_selection(self.candidate(app)):
            for number, token in enumerate(expected):
                sender = old if number % 2 else peer
                self.assertTrue(sender.focus(token))
            self.assertFalse(peer.focus('queue-full'))
            self.assertFalse(old.focus('also-full'))
        received = []
        app._instance_session.drain_focus(received.append)
        self.assertEqual(received, expected)
        self.assertTrue(app._instance_session.focus('after-drain'))
        app._instance_session.drain_focus(received.append)
        self.assertEqual(received[-1], 'after-drain')

    def test_destination_listener_start_failure_cleans_endpoint_without_settling_old_manager(self):
        app = self.app()
        old = app._instance_session
        old.listen()
        peer = self.session(self.second)
        with (patch.object(threading.Thread, 'start', side_effect=RuntimeError('activation worker unavailable')),
              patch.object(app.logos, 'close') as settle):
            with self.assertRaisesRegex(RuntimeError, 'activation worker unavailable'):
                with app.profile_selection(self.candidate(app)):
                    self.fail('Destination listener failure entered the body')
            settle.assert_not_called()
        self.assertIs(app._instance_session, old)
        self.assertTrue(old.listening)
        self.assertFalse(peer.endpoint.exists())
        self.assertTrue(old.focus('retained-owner'))
        received = []
        old.drain_focus(received.append)
        self.assertEqual(received, ['retained-owner'])
        with peer:
            pass
        self.assertTrue(app.close())
        self.assertFalse(list(self.coordination.glob('*.sock')))

    def test_startup_rejects_changed_effective_prefix_before_manager_or_workers(self):
        peer, _ = self.owner(self.second)
        domain = object.__new__(FixtureApp)
        held = []

        def construct_after_change(overrides, action, preserve_logs):
            session = overrides._instance_session
            held.append(session)
            self.assertEqual(session.prefix, self.root / 'prefix')
            self.first.write_bytes(self.second.read_bytes())
            try:
                domain.__init__(overrides)
            except maintenance.MaintenanceError:
                # Failed initialization leaves A held until main's finally.
                with self.assertRaises(instance.InstanceBusy):
                    instance.Session(self.root / 'prefix').acquire()
                self.assertIsNone(getattr(session, '_application', None))
                raise

        with (patch.object(sys, 'argv', ['controller', '-c', str(self.first), '--dialog', 'qt']),
              patch.object(main, '_run_owned', side_effect=construct_after_change),
              patch.object(main.updates, 'recover_startup'),
              patch('logos_desktop.logos.LogosManager') as manager,
              patch.object(App, 'start_thread') as start,
              patch.object(App, 'dispatch_config') as dispatch):
            with self.assertRaises(SystemExit) as error:
                main.main()
            self.assertEqual(error.exception.code, 1)
            manager.assert_not_called()
            start.assert_not_called()
            dispatch.assert_not_called()

        self.assertIsInstance(domain.conf, Config)
        self.assertEqual(instance.Session.for_config(domain.conf).prefix, self.root / 'other-prefix')
        self.assertEqual(domain.config_updated_hooks, [])
        self.assertEqual(domain._threads, [])
        self.assertNotIn('logos', domain.__dict__)
        self.assertEqual(len(held), 1)
        self.assertIs(domain._instance_session, held[0])
        with instance.Session(self.root / 'prefix'):
            pass
        with self.assertRaises(instance.InstanceBusy):
            self.session(self.second).acquire()
        self.assertTrue(peer.is_alive())

    def test_startup_uses_effective_prefix_after_applying_invocation_overrides(self):
        document = json.loads(self.first.read_bytes())
        overrides = EphemeralConfiguration.project(
            document, str(self.first), environment={'WINEPREFIX': str(self.root / 'prefix')})
        session = instance.Session.for_config(overrides).acquire()
        self.addCleanup(session.close)
        overrides._instance_session = session
        self.first.write_bytes(self.second.read_bytes())
        with patch.object(App, 'start_thread'):
            app = FixtureApp(overrides)
        self.addCleanup(app.close)
        self.assertIs(app._instance_session, session)
        self.assertEqual(instance.Session.for_config(app.conf).key, session.key)
        self.assertEqual(app.conf._raw.install_dir, str(self.root / 'second'))

    def test_same_prefix_profile_replacement_reopens_unmonitored_manager(self):
        app = self.app()
        selected = self.profile('same-prefix', self.root / 'prefix')
        candidate = Config(EphemeralConfiguration.load_from_path(str(selected)), app)
        original_manager, session = app.logos, app._instance_session
        with app.profile_selection(candidate):
            self.assertFalse(app._config_updated_event.is_set())
        self.assertIs(app.conf, candidate)
        self.assertIs(app._instance_session, session)
        self.assertIs(app.logos, original_manager)
        self.assertFalse(app.logos._closed.is_set())
        self.assertIsNone(app.logos._monitor_thread)
        with self.assertRaises(instance.InstanceBusy):
            self.session(selected).acquire()
        app.logos.start_monitoring()
        self.assertTrue(app.logos._monitor_thread.is_alive())

    def test_same_prefix_monitored_selection_and_failed_body_keep_monitoring_live(self):
        app = self.app()
        app.logos.start_monitoring()
        original_manager, session = app.logos, app._instance_session
        selected = self.profile('same-prefix', self.root / 'prefix')
        candidate = Config(EphemeralConfiguration.load_from_path(str(selected)), app)
        with app.profile_selection(candidate):
            self.assertTrue(original_manager._closed.is_set())
            self.assertFalse(original_manager._monitor_thread.is_alive())
        self.assertIsNot(app.logos, original_manager)
        self.assertIs(app.conf, candidate)
        self.assertIs(app._instance_session, session)
        self.assertFalse(app.logos._closed.is_set())
        self.assertTrue(app.logos._monitor_thread.is_alive())

        current_manager = app.logos
        with self.assertRaisesRegex(ValueError, 'same-prefix publication failed'):
            with app.profile_selection(Config(EphemeralConfiguration.load_from_path(str(self.first)), app)):
                raise ValueError('same-prefix publication failed')
        self.assertIs(app.conf, candidate)
        self.assertIs(app.logos, current_manager)
        self.assertIs(app._instance_session, session)
        self.assertFalse(current_manager._closed.is_set())
        self.assertTrue(current_manager._monitor_thread.is_alive())
        with self.assertRaises(instance.InstanceBusy):
            self.session(selected).acquire()

    def test_profile_body_failure_retains_config_manager_and_both_leases_until_unwind(self):
        app = self.app()
        old_conf, old_manager = app.conf, app.logos
        with self.assertRaisesRegex(ValueError, 'publication failed'):
            with app.profile_selection(self.candidate(app)):
                self.assertIs(app.conf, old_conf)
                for path in (self.first, self.second):
                    with self.assertRaises(instance.InstanceBusy):
                        self.session(path).acquire()
                self.assertFalse(app.close(timeout=0))
                raise ValueError('publication failed')
        self.assertIs(app.conf, old_conf)
        self.assertIs(app.logos, old_manager)
        with self.session(self.second):
            pass
        with self.assertRaises(instance.InstanceBusy):
            self.session(self.first).acquire()
        self.assertTrue(app.close())
        with self.session(self.first):
            pass

    def test_profile_selection_rejects_reloaded_prefix_before_publishing_candidate(self):
        peer, _ = self.owner(self.second)
        app = self.app()
        app.logos.start_monitoring()
        old_conf, old_manager, old_session = app.conf, app.logos, app._instance_session
        selected = self.profile('candidate-a', self.root / 'candidate-prefix')
        candidate = Config(EphemeralConfiguration.load_from_path(str(selected)), app)
        replacement = self.second.read_bytes()
        with patch('logos_desktop.logos.LogosManager') as manager:
            with self.assertRaisesRegex(maintenance.MaintenanceError, 'changed during profile selection'):
                with app.profile_selection(candidate):
                    selected.write_bytes(replacement)
                    candidate.reload()
                    self.assertEqual(instance.Session.for_config(candidate).prefix, self.root / 'other-prefix')
                    self.assertIs(app.conf, old_conf)
            manager.assert_not_called()
        self.assertIs(app.conf, old_conf)
        self.assertIs(app.logos, old_manager)
        self.assertIs(app._instance_session, old_session)
        self.assertFalse(old_manager._closed.is_set())
        self.assertTrue(old_manager._monitor_thread.is_alive())
        self.assertEqual(selected.read_bytes(), replacement)
        with instance.Session(self.root / 'candidate-prefix'):
            pass
        for prefix in (self.root / 'prefix', self.root / 'other-prefix'):
            with self.assertRaises(instance.InstanceBusy):
                instance.Session(prefix).acquire()
        self.assertTrue(peer.is_alive())

    def test_adoption_later_edit_refuses_selection_and_preserves_committed_journal(self):
        try:
            from logos_desktop.qt.services import Services
        except ModuleNotFoundError as error:
            if error.name and error.name.startswith(('PySide6', 'shiboken6')):
                self.skipTest('Optional Qt services are not installed')
            raise
        from logos_desktop import adoption

        peer, _ = self.owner(self.second)
        app = self.app()
        old_conf, old_manager, old_session = app.conf, app.logos, app._instance_session
        install = self.root / 'adoption-a'
        prefix = install / 'data/wine64_bottle'
        prefix.mkdir(parents=True)
        sentinel = prefix / 'library-sentinel'
        sentinel.write_bytes(b'preserve library')
        source = self.root / 'legacy-a.json'
        source.write_text(json.dumps({'install_dir': str(install), 'faithlife_product': 'Logos',
                                      'WINEPREFIX': str(prefix)}))
        original = source.read_bytes()
        destination = self.root / 'adopted.json'
        later_edit = self.second.read_bytes()
        published = []
        adopt = adoption.adopt

        def replace_after_publication(plan):
            result = adopt(plan)
            self.assertEqual(destination.read_bytes(), plan.content)
            published.append(plan.content)
            replacement = self.root / 'external-edit.json'
            replacement.write_bytes(later_edit)
            replacement.replace(destination)
            return result

        with (patch.object(app, 'approve', return_value=True),
              patch.object(adoption, 'adopt', side_effect=replace_after_publication),
              patch.object(adoption, 'rollback') as rollback,
              patch.object(Config, 'reload', side_effect=AssertionError('No unchecked reload')),
              patch('logos_desktop.logos.LogosManager') as manager,
              patch.object(app, 'start_thread') as start):
            with self.assertRaisesRegex(maintenance.MaintenanceError, 'Adoption was published') as error:
                Services().adopt_profile(source, destination=destination)(app, threading.Event())
            manager.assert_not_called()
            start.assert_not_called()
            rollback.assert_not_called()

        self.assertIs(app.conf, old_conf)
        self.assertIs(app.logos, old_manager)
        self.assertIs(app._instance_session, old_session)
        self.assertFalse(old_manager._closed.is_set())
        self.assertEqual(destination.read_bytes(), later_edit)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(sentinel.read_bytes(), b'preserve library')
        journal = adoption._journal_path(destination)
        self.assertIn(str(journal), str(error.exception))
        self.assertEqual(json.loads(journal.read_bytes())['state'], 'committed')
        self.assertEqual(len(published), 1)
        self.assertNotEqual(published[0], later_edit)
        with instance.Session(prefix):
            pass
        for retained in (self.root / 'prefix', self.root / 'other-prefix'):
            with self.assertRaises(instance.InstanceBusy):
                instance.Session(retained).acquire()
        self.assertTrue(peer.is_alive())

    def test_busy_destination_does_not_settle_old_manager(self):
        app = self.app()
        with self.session(self.second), patch.object(app.logos, 'close') as close:
            with self.assertRaises(instance.InstanceBusy):
                with app.profile_selection(self.candidate(app)):
                    self.fail('Busy profile entered')
            close.assert_not_called()

    def test_shutdown_retains_ownership_until_workers_settle(self):
        app = self.app()
        release = threading.Event()
        worker = app.start_thread(release.wait)
        try:
            self.assertFalse(app.close(timeout=0))
            with self.assertRaises(instance.InstanceBusy):
                self.session(self.first).acquire()
        finally:
            release.set()
            worker.join(2)
        self.assertTrue(app.close())
        with self.session(self.first):
            pass

    def test_initial_configuration_change_reserves_destination_before_write(self):
        self.first.write_text('{}')
        app = self.app()
        document = {}
        target = self.root / 'new-install'
        prefix = target / 'data/wine64_bottle'
        with instance.Session(prefix):
            with self.assertRaises(instance.InstanceBusy):
                app.conf._change(install_dir=str(target))
        self.assertIsNone(app.conf._raw.install_dir)
        self.assertEqual(json.loads(self.first.read_bytes()), document)
        app.conf._change(install_dir=str(target))
        self.assertEqual(app._instance_session.prefix, prefix)
        with self.assertRaises(instance.InstanceBusy):
            instance.Session(prefix).acquire()

    def test_external_candidate_retains_destination_guard_for_later_configuration(self):
        app = self.app()
        empty = self.root / 'empty.json'
        empty.write_text('{}')
        candidate = Config(EphemeralConfiguration.load_from_path(str(empty)), app)
        with app.profile_selection(candidate):
            pass
        self.assertIs(app.conf, candidate)
        target = self.root / 'later-install'
        with instance.Session(target / 'data/wine64_bottle'):
            with self.assertRaises(instance.InstanceBusy):
                app.conf._change(install_dir=str(target))
        self.assertEqual(empty.read_text(), '{}')
        app.conf._change(install_dir=str(target))
        self.assertEqual(app._instance_session.prefix, target / 'data/wine64_bottle')

    def test_configuration_write_failure_preserves_original_owner_and_manager(self):
        self.first.write_text('{}')
        app = self.app()
        original, manager = app.conf, app.logos
        target = self.root / 'failed-install'
        with patch.object(app.conf, '_write', side_effect=OSError('disk full')):
            with self.assertRaises(maintenance.MaintenanceError):
                app.conf._change(install_dir=str(target))
        self.assertIs(app.conf, original)
        self.assertIs(app.logos, manager)
        self.assertIsNone(app.conf._raw.install_dir)
        self.assertEqual(self.first.read_text(), '{}')
        with self.assertRaises(instance.InstanceBusy):
            self.session(self.first).acquire()
        with instance.Session(target / 'data/wine64_bottle'):
            pass

    def test_successful_profile_transfer_does_not_start_unrequested_monitoring(self):
        app = self.app()
        candidate = self.candidate(app)
        old_manager = app.logos
        with patch('logos_desktop.logos.LogosManager') as manager:
            with app.profile_selection(candidate):
                self.assertIsNot(app.conf, candidate)
            manager.assert_not_called()
        self.assertIs(app.conf, candidate)
        self.assertIs(app.logos, old_manager)
        with self.session(self.first):
            pass
        with self.assertRaises(instance.InstanceBusy):
            self.session(self.second).acquire()
        app.close()
        with self.session(self.second):
            pass

    def test_monitored_transfer_restarts_only_after_success(self):
        app = self.app()
        app.logos._monitor_thread = Mock(is_alive=Mock(side_effect=[True, False, True, False]))
        old_manager = app.logos
        with patch.object(old_manager, 'start_monitoring') as restart:
            with self.assertRaises(ValueError):
                with app.profile_selection(self.candidate(app)):
                    raise ValueError('failed')
            restart.assert_called_once()
        with patch('logos_desktop.logos.LogosManager') as manager:
            manager.return_value.close.return_value = True
            with app.profile_selection(self.candidate(app)):
                manager.assert_not_called()
            manager.return_value.start_monitoring.assert_called_once()

    def test_monitor_startup_failure_does_not_release_published_profile(self):
        app = self.app()
        app.logos._monitor_thread = Mock(is_alive=Mock(side_effect=[True, False, False]))
        candidate = self.candidate(app)
        with patch('logos_desktop.logos.LogosManager', side_effect=RuntimeError('cannot start monitor')):
            with self.assertRaisesRegex(RuntimeError, 'cannot start monitor'):
                with app.profile_selection(candidate):
                    pass
        self.assertIs(app.conf, candidate)
        with self.assertRaises(instance.InstanceBusy):
            self.session(self.second).acquire()
        with self.session(self.first):
            pass

    def test_legacy_detection_exact_identity_same_user_and_scope(self):
        selected = self.root / 'previous-controller.py'
        selected.write_text(f'#!{sys.executable}\nfrom previous_app.main import main\nmain()\n')
        selected.chmod(0o700)
        instance.select_previous_controller(selected)
        process = Mock(pid=98765)
        process.uids.return_value = SimpleNamespace(real=os.getuid())
        process.exe.return_value = '/usr/bin/python3'
        process.open_files.return_value = []
        process.cmdline.return_value = ['/usr/bin/python3', str(selected)]
        with patch.object(psutil, 'process_iter', return_value=[process]):
            process.environ.return_value = {'WINEPREFIX': str(self.root / 'unrelated')}
            instance.check_legacy(self.root / 'prefix')
            process.environ.return_value = {'WINEPREFIX': str(self.root / 'prefix')}
            with self.assertRaises(instance.InstanceBusy):
                instance.check_legacy(self.root / 'prefix')
            process.environ.return_value = {}
            with self.assertRaises(instance.InstanceBusy):
                instance.check_legacy(self.root / 'prefix')
            process.cmdline.return_value = ['python3', '-B', str(selected), '-c', str(self.second)]
            instance.check_legacy(self.root / 'prefix')
            process.cmdline.return_value[-1] = str(self.first)
            with self.assertRaises(instance.InstanceBusy):
                instance.check_legacy(self.root / 'prefix')
            process.cmdline.return_value = ['python3', '/opt/unrelated.py']
            instance.check_legacy(self.root / 'prefix')
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_selected_wrapper_and_module_admission_ignore_unrelated_protected_process(self):
        selected = self.root / 'previous.py'
        selected.write_text(f'#!{sys.executable}\nfrom previous_app.main import main\nmain()\n')
        selected.chmod(0o700)
        wrapper = self.root / 'previous-launcher'
        wrapper.write_text(f'#!/bin/sh\nexec "{selected}" "$@"\n')
        wrapper.chmod(0o700)
        instance.select_previous_controller(wrapper)
        process = Mock(pid=98765)
        process.uids.return_value = SimpleNamespace(real=os.getuid())
        process.create_time.return_value = 123.0
        process.open_files.return_value = []
        process.exe.return_value = str(Path(sys.executable).resolve())
        process.environ.return_value = {'WINEPREFIX': str(self.root / 'prefix')}
        unrelated = Mock(pid=98766)
        unrelated.uids.return_value = SimpleNamespace(real=os.getuid())
        unrelated.cmdline.return_value = ['/usr/bin/credential-agent']
        unrelated.exe.side_effect = psutil.AccessDenied(98766)
        unrelated.environ.side_effect = psutil.AccessDenied(98766)
        with patch.object(psutil, 'process_iter', return_value=[unrelated, process]):
            for command in ([sys.executable, str(selected)], [sys.executable, '-m', 'previous_app.main'],
                            [sys.executable, '-X', 'dev', '-m', 'previous_app.main'],
                            [sys.executable, '-W', 'ignore', '-m', 'previous_app.main'],
                            [sys.executable, '-mprevious_app.main'],
                            [sys.executable, '-IBm', 'previous_app.main'],
                            [sys.executable, '--', str(selected)]):
                process.cmdline.return_value = command
                with self.assertRaises(instance.InstanceBusy):
                    instance.check_legacy(self.root / 'prefix')
            process.environ.return_value = {'WINEPREFIX': str(self.root / 'different')}
            instance.check_legacy(self.root / 'prefix')
            process.environ.side_effect = psutil.AccessDenied(98765)
            with self.assertRaises(instance.InstanceBusy):
                instance.check_legacy(self.root / 'prefix')
        unrelated.exe.assert_not_called()
        unrelated.environ.assert_not_called()
        wrapper.write_text('#!/bin/sh\nexec "$SOME_CONTROLLER" "$@"\n')
        with self.assertRaises(instance.InstanceBusy):
            instance.select_previous_controller(wrapper)

    def test_pip_wrapper_interpreter_and_unreadable_relative_selected_identity(self):
        selected = self.root / 'wrapped-controller'
        selected.write_text(f'#!/bin/sh\n\'\'\'exec\' "{sys.executable}" "$0" "$@"\n\' \'\'\'\n'
                            'from previous_app.main import main\nmain()\n')
        selected.chmod(0o700)
        instance.select_previous_controller(selected)
        process = Mock(pid=98765)
        process.uids.return_value = SimpleNamespace(real=os.getuid())
        process.create_time.return_value = 123.0
        process.open_files.return_value = []
        process.exe.return_value = str(Path(sys.executable).resolve())
        process.cmdline.return_value = [sys.executable, '-m', 'previous_app.main']
        process.environ.return_value = {}
        with patch.object(psutil, 'process_iter', return_value=[process]):
            with self.assertRaises(instance.InstanceBusy):
                instance.check_legacy(self.root / 'prefix')
            process.cmdline.return_value = [sys.executable, './wrapped-controller']
            process.cwd.side_effect = psutil.AccessDenied(98765)
            with self.assertRaises(instance.InstanceBusy):
                instance.check_legacy(self.root / 'prefix')

    def test_real_selected_idle_script_with_interpreter_options_refuses_without_signals(self):
        selected = self.root / 'idle-controller.py'
        selected.write_text(f'#!{sys.executable}\nimport sys\nprint("ready", flush=True)\nsys.stdin.readline()\n')
        selected.chmod(0o700)
        instance.select_previous_controller(selected)
        for options in (['-X', 'dev'], ['-W', 'ignore'], ['--']):
            child = subprocess.Popen([sys.executable, *options, str(selected)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                     env=dict(os.environ, WINEPREFIX=str(self.root / 'prefix')))
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with self.assertRaises(instance.InstanceBusy):
                    instance.check_legacy(self.root / 'prefix')
                instance.check_legacy(self.root / 'other-prefix')
            finally:
                child.communicate('\n', timeout=5)
            self.assertEqual(child.returncode, 0)

    def test_real_attached_module_is_admitted_by_selected_interpreter_and_prefix(self):
        selected = self.root / 'previous-entry'
        selected.write_text(f'#!{sys.executable}\nfrom previous_app.main import main\nmain()\n')
        selected.chmod(0o700)
        package = self.root / 'previous_app'
        package.mkdir()
        (package / '__init__.py').write_text('')
        (package / 'main.py').write_text('import sys\nprint("ready", flush=True)\nsys.stdin.readline()\n')
        instance.select_previous_controller(selected)
        for options in (['-mprevious_app.main'], ['-Bm', 'previous_app.main']):
            child = subprocess.Popen([sys.executable, *options], cwd=self.root,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                     env=dict(os.environ, WINEPREFIX=str(self.root / 'prefix')))
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with self.assertRaises(instance.InstanceBusy):
                    instance.check_legacy(self.root / 'prefix')
                instance.check_legacy(self.root / 'other-prefix')
            finally:
                child.communicate('\n', timeout=5)
            self.assertEqual(child.returncode, 0)

    def test_selected_script_with_nonstandard_interpreter_alias(self):
        interpreter = self.root / 'runtime'
        interpreter.symlink_to(sys.executable)
        selected = self.root / 'previous-entry'
        selected.write_text(f'#!{interpreter}\nimport sys\nprint("ready", flush=True)\nsys.stdin.readline()\n')
        selected.chmod(0o700)
        instance.select_previous_controller(selected)
        child = subprocess.Popen([str(interpreter), str(selected)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True,
                                 env=dict(os.environ, WINEPREFIX=str(self.root / 'prefix')))
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            with self.assertRaises(instance.InstanceBusy):
                instance.check_legacy(self.root / 'prefix')
            instance.check_legacy(self.root / 'other-prefix')
        finally:
            child.communicate('\n', timeout=5)
        self.assertEqual(child.returncode, 0)

    def test_selected_native_controller_absolute_relative_and_alias(self):
        import shutil

        selected = self.root / 'previous-native'
        shutil.copyfile(shutil.which('cat'), selected)
        selected.chmod(0o700)
        alias = self.root / 'different-name'
        alias.symlink_to(selected)
        instance.select_previous_controller(selected)
        for invocation in (str(selected), './previous-native', str(alias)):
            child = subprocess.Popen([invocation], cwd=self.root, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True,
                                     env=dict(os.environ, WINEPREFIX=str(self.root / 'prefix')))
            try:
                child.stdin.write('ready\n')
                child.stdin.flush()
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with self.assertRaises(instance.InstanceBusy):
                    instance.check_legacy(self.root / 'prefix')
                instance.check_legacy(self.root / 'other-prefix')
            finally:
                child.communicate('', timeout=5)
            self.assertEqual(child.returncode, 0)

    def dispatch(self, *arguments):
        with (patch.object(sys, 'argv', ['controller', '-c', str(self.first), '--force-root', *arguments]),
              patch.object(main, '_run_owned') as action,
              patch.object(main.updates, 'recover_startup') as recover,
              patch.object(main.msg, 'initialize_logging') as logging):
            main.main()
            return action, recover, logging

    def test_duplicate_gui_focuses_but_explicit_mutation_and_tui_are_busy(self):
        with self.session(self.first) as owner:
            owner.listen()
            action, recover, logging = self.dispatch('--dialog', 'qt')
            action.assert_not_called()
            recover.assert_not_called()
            logging.assert_not_called()
            received = []
            owner.drain_focus(received.append)
            self.assertEqual(len(received), 1)
            for arguments in (('--stop-installed-app',), ('--dialog', 'tui'), ('--wine', 'cmd.exe')):
                with self.assertRaises(SystemExit) as error:
                    self.dispatch(*arguments)
                self.assertEqual(error.exception.code, 1)
            received.clear()
            owner.drain_focus(received.append)
            self.assertFalse(received)

    def test_status_exempt_and_mutation_owned_before_recovery_or_action(self):
        with self.session(self.first):
            action, recover, _ = self.dispatch('--status')
            action.assert_called_once()
            recover.assert_not_called()

        def check_owned(*args):
            with self.assertRaises(instance.InstanceBusy):
                self.session(self.first).acquire()

        with (patch.object(main.updates, 'recover_startup', side_effect=check_owned),
              patch.object(main, '_run_owned', side_effect=check_owned),
              patch.object(sys, 'argv', ['controller', '-c', str(self.first), '--stop-installed-app'])):
            main.main()

    def test_url_handler_admits_only_selected_executable_and_holds_domain_lease(self):
        overrides = EphemeralConfiguration.load_from_path(str(self.first))
        overrides.app_run_as_root_permitted = True
        executable = 'C:\\users\\fixture\\AppData\\Local\\Logos\\Logos.exe'
        overrides.wine_args = [executable, 'logos4:synthetic-private-token']
        conf = SimpleNamespace(logos_exe_windows_path=executable, wine64_binary='/fixture/wine',
                               _raw=SimpleNamespace(install_dir=str(self.root), faithlife_product='Logos'),
                               _overrides=overrides)
        process = Mock()

        def wait():
            self.assertIsNotNone(maintenance.current_context())
            return 0

        process.wait.side_effect = wait
        with (patch('logos_desktop.config.Config', return_value=conf),
              patch('logos_desktop.logos.LogosManager', side_effect=AssertionError('No second manager')),
              patch('logos_desktop.wine._wine_invocation', return_value=(['/fixture/wine'], {})),
              patch('logos_desktop.runtime.activation_guard'),
              patch('subprocess.Popen') as popen):
            popen.return_value.__enter__.return_value = process
            with self.session(self.first):
                main.run_url_request(overrides)
            self.assertEqual(popen.call_args.args[0], ['/fixture/wine', *overrides.wine_args])
            overrides.wine_args = ['cmd.exe', 'logos4:synthetic-private-token']
            with self.assertRaises(maintenance.MaintenanceError):
                main.run_url_request(overrides)
            self.assertEqual(popen.call_count, 1)


if __name__ == '__main__':
    unittest.main()
