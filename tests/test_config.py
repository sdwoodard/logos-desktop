import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from logos_desktop import (
    artifacts, backup, config_store, constants, control, installer, main, maintenance, runtime, utils, wine,
)
from logos_desktop.app import App
from logos_desktop.config import Config, EphemeralConfiguration, PersistentConfiguration
from tests.artifact_fixtures import appimage


def configuration(path):
    app = SimpleNamespace(_config_updated_event=threading.Event(), status=Mock(), ask=Mock())
    app.conf = Config(EphemeralConfiguration.load_from_path(str(path)), app)
    return app.conf


class TestConfiguration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "chosen 文.json"
        self.install = self.root / "installation"
        self.install.mkdir()
        self.document = {
            "install_dir": str(self.install), "faithlife_product": "Logos", "faithlife_product_version": "10",
            "wine_binary": "data/bin/wine", "app_release_channel": "dev",
            "faithlife_product_release_channel": "beta", "WINEPREFIX": str(self.root / "custom-prefix"),
            "WINEDEBUG": "", "DEBUG": False, "SKIP_DEPENDENCIES": True,
            "future": {"value": [1, False]}, "_future": "preserved",
        }
        self.path.write_text(json.dumps(self.document))
        self.addCleanup(patch.stopall)
        patch.object(constants, "STATE_DIR", str(self.root / "state/app")).start()

    def child(self, program, *arguments):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", program, *map(str, arguments)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def close():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()

        self.addCleanup(close)
        return child

    def test_selected_profile_precedes_environment_and_default_even_during_bootstrap(self):
        other = self.root / "other.json"
        other.write_bytes(b"invalid default must not be loaded")
        parser = main.get_parser()
        with patch.dict(os.environ, {"CONFIG_FILE": str(other), "DEBUG": "true", "WINEDEBUG": "+old"}):
            with patch.object(main.msg, "update_log_path"), patch.object(main.msg, "update_log_level"):
                overrides, _ = main.setup_config(parser.parse_args(["-c", str(self.path), "--verbose"]))
            conf = Config(overrides, SimpleNamespace(_config_updated_event=threading.Event()))
            self.assertEqual(conf.config_file_path, str(self.path))
            self.assertEqual(conf.log_level, 20)
            conf._change(curses_color_scheme="Dark")
        self.assertEqual(other.read_bytes(), b"invalid default must not be loaded")
        saved = json.loads(self.path.read_bytes())
        self.assertFalse(saved["DEBUG"])
        self.assertEqual(saved["WINEDEBUG"], "")
        self.assertEqual(saved["future"], self.document["future"])
        self.assertEqual(saved["_future"], "preserved")
        self.assertNotIn("CONFIG_FILE", saved)

    def test_environment_is_transient_across_two_fresh_restarts(self):
        with patch.dict(os.environ, {"lli_release_channel": "stable", "DEBUG": "true", "SKIP_DEPENDENCIES": "false"}):
            conf = configuration(self.path)
            self.assertEqual(conf.app_release_channel, "stable")
            self.assertFalse(conf.skip_install_system_dependencies)
            conf._change(curses_color_scheme="Dark")
        program = """
import json,sys
from logos_desktop.config import EphemeralConfiguration,PersistentConfiguration
raw=PersistentConfiguration.load_from_path(sys.argv[1])
overrides=EphemeralConfiguration.load_from_path(sys.argv[1])
print(json.dumps([raw.app_release_channel,raw.faithlife_product_release_channel,
                 raw._legacy.DEBUG,overrides.install_dependencies_skip,overrides.wine_prefix,overrides.wine_debug]))
"""
        for restart in range(2):
            child = self.child(program, self.path)
            output, errors = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, errors)
            self.assertEqual(json.loads(output), ["dev", "beta", False, True, self.document["WINEPREFIX"], ""])

    def test_modern_aliases_win_and_changed_aliases_cannot_resurrect_old_values(self):
        document = dict(self.document, FLPRODUCT="OtherProduct", INSTALLDIR=str(self.root / "wrong"), WINE_EXE="wrong")
        self.path.write_text(json.dumps(document))
        conf = configuration(self.path)
        self.assertEqual(conf.faithlife_product, "Logos")
        self.assertEqual(conf.install_dir, str(self.install))
        conf._change(wine_binary="data/bin/new", faithlife_product_release=None)
        saved = json.loads(self.path.read_bytes())
        self.assertEqual(saved["WINE_EXE"], "data/bin/new")
        self.assertEqual(saved["INSTALLDIR"], str(self.install))
        self.assertNotIn("TARGET_RELEASE_VERSION", saved)
        self.assertEqual(PersistentConfiguration.load_from_path(str(self.path)).wine_binary, "data/bin/new")

    def test_boolean_types_are_validated_without_overwriting_malformed_input(self):
        for value, expected in ((True, True), (False, False), ("yes", True), ("false", False)):
            document = dict(self.document, DEBUG=value, LOGS=value)
            self.path.write_text(json.dumps(document))
            conf = configuration(self.path)
            conf._change(curses_color_scheme="Dark")
            loaded = PersistentConfiguration.load_from_path(str(self.path))
            self.assertIs(loaded._legacy.DEBUG, expected)
            self.assertIs(loaded.faithlife_product_logging, expected)
        for value in ([], 1, "perhaps"):
            original = json.dumps(dict(self.document, DEBUG=value)).encode()
            self.path.write_bytes(original)
            with self.assertRaisesRegex(config_store.ConfigurationError, "DEBUG"):
                configuration(self.path)
            self.assertEqual(self.path.read_bytes(), original)

    def test_explicit_legacy_projection_migrates_without_moving_source(self):
        legacy = self.root / "old.conf"
        original = (
            f"export FLPRODUCT='Logos'\nINSTALLDIR='{self.install}'\nTARGETVERSION=10\n"
            "WINEDEBUG='err+all,#literal=value' # comment\nDEBUG=false\nSKIP_DEPENDENCIES=true\n"
        ).encode()
        legacy.write_bytes(original)
        destination = self.root / "new.json"
        with patch.object(constants, "DEFAULT_CONFIG_PATH", str(destination)):
            with patch.dict(os.environ):
                os.environ.pop("CONFIG_FILE", None)
                document, _ = config_store.read_document(legacy)
                overrides = EphemeralConfiguration.project(document, str(legacy), destination=str(destination))
                app = SimpleNamespace(_config_updated_event=threading.Event(), status=Mock())
                conf = Config(overrides, app)
                self.assertEqual(conf.config_file_path, str(legacy))
                self.assertFalse(destination.exists())
                conf._change(curses_color_scheme="Dark")
                self.assertEqual(conf.config_file_path, str(destination))
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(EphemeralConfiguration.load_from_path(str(destination)).wine_debug, "err+all,#literal=value")
        self.assertEqual(json.loads(destination.read_bytes())["schema_version"], 1)

    def test_custom_conf_and_extensionless_json_restart_at_the_same_path(self):
        for name, content in (("custom.conf", b"FLPRODUCT=Logos\nDEBUG=false\nWINEDEBUG='a=b#c'\n"),
                              ("extensionless", json.dumps(self.document).encode())):
            path = self.root / name
            path.write_bytes(content)
            conf = configuration(path)
            conf._change(curses_color_scheme="Light")
            self.assertEqual(conf.config_file_path, str(path))
            self.assertEqual(json.loads(path.read_bytes())["curses_color_scheme"], "Light")
            self.assertEqual((self.root / f".{name}.previous").read_bytes(), content)
            self.assertEqual(configuration(path).curses_color_scheme, "Light")
            conf._change(curses_color_scheme="Dark")
            if name.endswith(".conf"):
                self.assertEqual((self.root / f".{name}.legacy-original").read_bytes(), content)

    def test_invalid_shell_and_competing_migration_destination_preserve_sources(self):
        for text in ("FLPRODUCT='Logos", "source other.conf", "DEBUG=$(touch sentinel)"):
            path = self.root / "invalid.conf"
            path.write_text(text)
            with self.assertRaises(config_store.ConfigurationError):
                configuration(path)
            self.assertEqual(path.read_text(), text)
        source = self.root / "legacy.conf"
        source.write_text("FLPRODUCT=Logos\n")
        conf = configuration(source)
        destination = self.root / "concurrent.json"
        conf._overrides.config_destination = str(destination)
        destination.write_text('{"faithlife_product":"OtherProduct"}')
        with self.assertRaisesRegex(config_store.ConfigurationError, "changed"):
            conf._change(curses_color_scheme="Dark")
        self.assertEqual(json.loads(destination.read_bytes()), {"faithlife_product": "OtherProduct"})
        self.assertEqual(source.read_text(), "FLPRODUCT=Logos\n")

    def test_stale_thread_and_process_writers_preserve_independent_fields(self):
        first = configuration(self.path)
        second = configuration(self.path)
        barrier = threading.Barrier(3)
        errors = []

        def change(conf, values):
            try:
                barrier.wait(timeout=5)
                conf._change(**values)
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=change, args=(first, {"curses_color_scheme": "Dark"})),
                   threading.Thread(target=change, args=(second, {"backup_dir": "backups"}))]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        program = """
import sys
from logos_desktop import constants
from logos_desktop.config import PersistentConfiguration
constants.STATE_DIR=sys.argv[2]
raw=PersistentConfiguration.load_from_path(sys.argv[1])
print('loaded',flush=True)
sys.stdin.readline()
setattr(raw,sys.argv[3],sys.argv[4])
raw.write_config(checked=True)
"""
        children = [self.child(program, self.path, constants.STATE_DIR, "app_release_channel", "stable"),
                    self.child(program, self.path, constants.STATE_DIR, "faithlife_product_release", "40.1.0.0")]
        for child in children:
            self.assertEqual(child.stdout.readline().strip(), "loaded")
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        for child in children:
            _, error = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, error)
        saved = json.loads(self.path.read_bytes())
        self.assertEqual(saved["backup_dir"], "backups")
        self.assertEqual(saved["curses_color_scheme"], "Dark")
        self.assertEqual(saved["app_release_channel"], "stable")
        self.assertEqual(saved["faithlife_product_release"], "40.1.0.0")

    def test_stage_and_primary_failure_preserve_files_and_memory_then_retry(self):
        conf = configuration(self.path)
        original = self.path.read_bytes()
        stage = config_store.Target.stage
        publish = config_store.Target.publish
        reached = []

        def partial(target, content):
            if target.path == self.path:
                stage(target, content[:17])
                reached.append(target.path)
                raise OSError("injected mid-stage error")
            return stage(target, content)

        def fail_primary(target):
            if target.path == self.path:
                raise OSError("injected primary error")
            return publish(target)

        for method, injected in (("stage", partial), ("publish", fail_primary)):
            with patch.object(config_store.Target, method, injected):
                with self.assertRaises(config_store.ConfigurationError):
                    conf._change(curses_color_scheme="Dark")
            self.assertEqual(self.path.read_bytes(), original)
            self.assertIsNone(conf._raw.curses_color_scheme)
            self.assertEqual(list(self.root.glob(".config-*")), [])
        conf._change(curses_color_scheme="Dark")
        self.assertEqual(reached, [self.path])
        self.assertEqual(configuration(self.path).curses_color_scheme, "Dark")

    def test_committed_portable_failure_and_sync_uncertainty_keep_committed_memory(self):
        conf = configuration(self.path)
        portable = self.install / constants.PORTABLE_CONFIG_NAME
        portable.write_text(json.dumps(self.document))
        previous = portable.read_bytes()
        publish = config_store.Target.publish

        def fail_portable(target):
            if target.path == portable:
                raise OSError("injected portable failure")
            return publish(target)

        with patch.object(config_store.Target, "publish", fail_portable):
            conf._change(curses_color_scheme="Dark")
        self.assertEqual(conf.curses_color_scheme, "Dark")
        self.assertEqual(configuration(self.path).curses_color_scheme, "Dark")
        self.assertEqual(portable.read_bytes(), previous)
        self.assertIn("portable", conf.persistence_warning)
        self.assertTrue(conf.app.status.called)
        original_sync = os.fsync

        def uncertain(fd):
            if Path(f"/proc/self/fd/{fd}").resolve() == self.root:
                raise OSError("injected primary directory sync failure")
            return original_sync(fd)

        # The previous-copy sync uses the same parent. Fail only after primary publication.
        committed = threading.Event()

        def mark(target):
            publish(target)
            if target.path == self.path:
                committed.set()

        with patch.object(config_store.Target, "publish", mark), patch.object(
            os, "fsync", side_effect=lambda fd: uncertain(fd) if committed.is_set() else original_sync(fd),
        ):
            conf._change(curses_color_scheme="Light")
        self.assertEqual(conf.curses_color_scheme, "Light")
        self.assertIn("durability", conf.persistence_warning)

    def test_interrupted_writer_and_continuous_reader_never_observe_partial_json(self):
        original = self.path.read_bytes()
        program = """
import sys,time
from unittest.mock import patch
from logos_desktop import config_store,constants
from logos_desktop.config import PersistentConfiguration
constants.STATE_DIR=sys.argv[2]
raw=PersistentConfiguration.load_from_path(sys.argv[1])
raw.curses_color_scheme='Dark'
publish=config_store.Target.publish
def pause(target):
    if str(target.path)==sys.argv[1]:
        print('staged',flush=True)
        time.sleep(30)
    publish(target)
with patch.object(config_store.Target,'publish',pause):
    raw.write_config(checked=True)
"""
        child = self.child(program, self.path, constants.STATE_DIR)
        self.assertEqual(child.stdout.readline().strip(), "staged")
        child.terminate()
        child.wait(timeout=5)
        self.assertEqual(self.path.read_bytes(), original)
        errors = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    self.assertEqual(json.loads(self.path.read_bytes())["faithlife_product"], "Logos")
                except BaseException as error:
                    errors.append(error)
                    return

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            conf = configuration(self.path)
            for index in range(12):
                conf._change(faithlife_product_release="40." + "1" * (index + 1))
        finally:
            stop.set()
            thread.join(5)
        self.assertFalse(errors, errors)

    def test_instances_context_caches_and_backup_paths_follow_selection(self):
        self.path.write_text(json.dumps(dict(self.document, WINEPREFIX="data/wine64_bottle")))
        first = configuration(self.path)
        second = configuration(self.path)
        self.assertIsNot(first, second)
        for base, user in ((self.install / "data/wine64_bottle", "alice"),
                           (self.root / "second/data/wine64_bottle", "bob")):
            (base / f"drive_c/users/{user}/AppData/Local/Logos").mkdir(parents=True)
        self.assertEqual(first.wine_user, "alice")
        first.install_dir = str(self.root / "second")
        self.assertEqual(first.wine_user, "bob")
        self.assertEqual(second.wine_user, "alice")
        first._change(backup_dir="backups")
        self.assertEqual(first.backup_dir, self.root / "second/backups")
        first.reload()
        self.assertEqual(first.backup_dir, self.root / "second/backups")

    def test_installed_version_cache_tracks_system_metadata_without_changing_target(self):
        product = Path(self.document["WINEPREFIX"]) / "drive_c/users/fixture/AppData/Local/Logos"
        system = product / "System"
        system.mkdir(parents=True)
        metadata = system / "Logos.deps.json"

        def version_file(path, version):
            path.write_text(json.dumps({"libraries": {f"Logos/{version}": {}}}))

        version_file(metadata, "46.2.0.5")
        self.path.write_text(json.dumps(dict(self.document, faithlife_product_release="46.2.0.0005")))
        before = self.path.read_bytes()
        conf = configuration(self.path)
        with patch.object(utils, "get_current_logos_version", wraps=utils.get_current_logos_version) as read:
            for _ in range(10):
                self.assertEqual(conf.installed_faithlife_product_release, "46.2.0.5")
            self.assertEqual(read.call_count, 1)

            pending = product / "Pending"
            pending.mkdir()
            version_file(pending / metadata.name, "54.3.0.5")
            self.assertEqual(conf.installed_faithlife_product_release, "46.2.0.5")
            self.assertEqual(read.call_count, 1)

            old_stat = metadata.stat()
            os.utime(pending / metadata.name, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            (pending / metadata.name).replace(metadata)
            for _ in range(10):
                self.assertEqual(conf.installed_faithlife_product_release, "54.3.0.5")
            self.assertEqual(read.call_count, 2)

            version_file(metadata, "46.2.0.5")
            self.assertEqual(conf.installed_faithlife_product_release, "46.2.0.5")
            self.assertEqual(read.call_count, 3)
            self.assertEqual(conf.refresh_installed_faithlife_product_release(), "46.2.0.5")
            self.assertEqual(read.call_count, 4)

            metadata.unlink()
            self.assertIsNone(conf.installed_faithlife_product_release)
            version_file(metadata, "54.3.0.5")
            self.assertEqual(conf.installed_faithlife_product_release, "54.3.0.5")
        self.assertEqual(conf.faithlife_product_release, "46.2.0.0005")
        self.assertEqual(self.path.read_bytes(), before)

    def test_conditional_restore_preserves_stored_presence_and_alias_edits(self):
        names = ("faithlife_product_release", "TARGET_RELEASE_VERSION")
        for original in ({}, {names[0]: None}, {names[1]: "46.2.0.0005"}):
            for edit in (None, "legacy", "removed_alias"):
                with self.subTest(original=original, edit=edit):
                    document = dict(self.document, **original)
                    self.path.write_text(json.dumps(document))
                    conf = configuration(self.path)
                    receipt = conf._change(faithlife_product_release="54.3.0.0005")
                    previous = {name: receipt.previous[name] for name in names if name in receipt.previous}
                    published = {name: receipt.document[name] for name in names if name in receipt.document}
                    current = json.loads(self.path.read_text())
                    current["external_sentinel"] = {"preserved": True}
                    if edit == "legacy":
                        current[names[1]] = "50.0.0.0"
                    elif edit == "removed_alias":
                        current.pop(names[1])
                    self.path.write_text(json.dumps(current))

                    conflicts = conf.restore_changes(previous, published)
                    saved = json.loads(self.path.read_text())
                    expected = current if edit else dict(current, **original)
                    if edit is None:
                        for name in names:
                            if name not in original:
                                expected.pop(name, None)
                    self.assertEqual(saved, expected)
                    self.assertEqual(conflicts, (names[0],) if edit else ())
                    self.assertEqual(conf._raw._saved, config_store.canonical(saved, self.path))
                    self.assertEqual(conf._raw.faithlife_product_release,
                                     config_store.canonical(saved, self.path).get(names[0]))

    def test_conditional_restore_rejects_edit_after_target_snapshot(self):
        conf = configuration(self.path)
        names = ("faithlife_product_release", "TARGET_RELEASE_VERSION")
        receipt = conf._change(faithlife_product_release="54.3.0.0005")
        previous = {name: receipt.previous[name] for name in names if name in receipt.previous}
        published = {name: receipt.document[name] for name in names if name in receipt.document}
        stage = config_store.Target.stage
        edited = []

        def edit_after_snapshot(target, content):
            stage(target, content)
            if target.path == self.path:
                document = json.loads(self.path.read_text())
                document["faithlife_product_release"] = "50.0.0.0"
                document["external_sentinel"] = "later edit"
                edited.append(document)
                self.path.write_text(json.dumps(document))

        with patch.object(config_store.Target, "stage", edit_after_snapshot):
            with self.assertRaisesRegex(config_store.ConfigurationError, "changed before publication"):
                conf.restore_changes(previous, published)
        self.assertEqual(json.loads(self.path.read_text()), edited[0])
        self.assertEqual(list(self.root.glob(".config-*")), [])

    def test_conditional_restore_resolves_later_relative_and_legacy_install_paths(self):
        names = ("faithlife_product_release", "TARGET_RELEASE_VERSION")
        for legacy_only in (False, True):
            with self.subTest(legacy_only=legacy_only):
                self.path.write_text(json.dumps(self.document))
                conf = configuration(self.path)
                receipt = conf._change(faithlife_product_release="54.3.0.0005")
                previous = {name: receipt.previous[name] for name in names if name in receipt.previous}
                published = {name: receipt.document[name] for name in names if name in receipt.document}
                current = json.loads(self.path.read_text())
                if legacy_only:
                    current.pop("install_dir")
                    current["INSTALLDIR"] = "installation"
                else:
                    current["install_dir"] = "installation"
                self.path.write_text(json.dumps(current))
                target_class = config_store.Target

                def owned_target(path):
                    self.assertTrue(Path(path).is_relative_to(self.root))
                    return target_class(path)

                with patch.object(config_store, "Target", side_effect=owned_target):
                    self.assertEqual(conf.restore_changes(previous, published), ())
                saved = json.loads(self.path.read_text())
                self.assertEqual((saved.get("install_dir"), saved.get("INSTALLDIR")),
                                 (current.get("install_dir"), current.get("INSTALLDIR")))
                self.assertEqual(conf.install_dir, str(self.install))
                self.assertEqual(conf._raw.install_dir, str(self.install))
                self.assertEqual(configuration(self.path).install_dir, str(self.install))
                portable = self.install / constants.PORTABLE_CONFIG_NAME
                portable_conf = configuration(portable)
                self.assertEqual(portable_conf.install_dir, conf.install_dir)
                self.assertEqual(portable_conf.wine_prefix, conf.wine_prefix)
                self.assertEqual(config_store.selection_identity(portable_conf._raw._saved),
                                 config_store.selection_identity(conf._raw._saved))
                portable_document = json.loads(portable.read_text())
                self.assertEqual("install_dir" in portable_document, "install_dir" in current)
                self.assertEqual(portable_document["INSTALLDIR"], str(self.install))

    def test_migration_resolves_relative_installation_against_source_directory(self):
        source = self.root / "legacy/old.conf"
        source.parent.mkdir()
        install = source.parent / "relative-install"
        install.mkdir()
        source.write_text("FLPRODUCT=Logos\nINSTALLDIR=relative-install\n")
        original = source.read_bytes()
        destination = self.root / "destination/new.json"
        destination.parent.mkdir()
        conf = configuration(source)
        conf._overrides.config_destination = str(destination)
        conf._change(curses_color_scheme="Dark")
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(conf.install_dir, str(install))
        self.assertEqual(json.loads(destination.read_text())["install_dir"], str(install))
        self.assertEqual((install / constants.PORTABLE_CONFIG_NAME).read_bytes(), destination.read_bytes())

    def test_empty_explicit_path_and_symlink_target_fail_without_touching_other_profile(self):
        with self.assertRaises(config_store.ConfigurationError):
            EphemeralConfiguration.load_from_path("")
        selected = self.root / "link.json"
        selected.symlink_to(self.path)
        before = self.path.read_bytes()
        conf = configuration(selected)
        with self.assertRaises(config_store.ConfigurationError):
            conf._change(curses_color_scheme="Dark")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(selected.is_symlink())

    def test_wineserver_override_reaches_real_sync_async_and_pinned_appimage_environment(self):
        capture = self.root / "server-capture.json"
        server = self.install / "tools/server"
        server.parent.mkdir()
        server.write_text(
            f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
            f"Path({str(capture)!r}).write_text(json.dumps([sys.argv,os.environ['WINESERVER'],os.environ['WINEPREFIX']]))\n"
        )
        server.chmod(0o700)
        bindir = self.install / "data/bin"
        bindir.mkdir(parents=True)
        for name in ("wine", "wine64", "wineserver"):
            path = bindir / name
            path.write_text(f"#!{sys.executable}\n")
            path.chmod(0o700)
        document = dict(self.document, WINESERVER_EXE="tools/server")
        self.path.write_text(json.dumps(document))
        conf = configuration(self.path)
        self.assertEqual(conf.wineserver_binary, str(server))
        result = wine.run_wine_completed_process(conf.app, conf.wineserver_binary, exe_args=["-w"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(capture.read_text())[1:], [str(server), document["WINEPREFIX"]])
        with tempfile.TemporaryFile(mode="w+") as output:
            child = wine.run_wine_process(conf.app, conf.wineserver_binary, output, output, exe_args=["-k"])
            self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual(json.loads(capture.read_text())[0], [str(server), "-k"])
        source = self.root / "selected.AppImage"
        source.write_bytes(appimage())
        accepted = artifacts.import_local(
            source, bindir, artifacts.ArtifactSpec("local:fixture", source.name, "appimage"),
        )
        conf._change(wine_binary=str(accepted.path))
        _, environment = wine._wine_invocation(conf.app, conf.wine_binary, None)
        self.assertEqual(environment["WINESERVER"], str(server))
        conf._overrides.wineserver_binary = None
        self.assertEqual(conf.wineserver_binary, str(accepted.path.parent / "wineserver"))
        self.assertFalse(runtime.pending(self.install))

    def test_relative_saved_projections_rebind_together_and_match_fresh_process(self):
        self.path.write_text(json.dumps(dict(
            self.document, WINEPREFIX="prefix", APPDIR_BINDIR="tools", WINESERVER_EXE="tools/server",
        )))
        conf = configuration(self.path)
        second = self.root / "second"
        for install, user in ((self.install, "alice"), (second, "bob")):
            (install / f"prefix/drive_c/users/{user}").mkdir(parents=True)
        self.assertEqual(conf.wine_user, "alice")
        conf.install_dir = second
        self.assertEqual(conf.wine_user, "bob")
        expected = [str(second / "prefix"), str(second / "tools"), str(second / "tools/server")]
        self.assertEqual([conf.wine_prefix, conf.installer_binary_dir, conf.wineserver_binary], expected)
        conf.reload()
        self.assertEqual([conf.wine_prefix, conf.installer_binary_dir, conf.wineserver_binary], expected)
        program = """
import json,sys
from threading import Event
from types import SimpleNamespace
from logos_desktop.config import Config,EphemeralConfiguration
conf=Config(EphemeralConfiguration.load_from_path(sys.argv[1]),SimpleNamespace(_config_updated_event=Event()))
print(json.dumps([conf.wine_prefix,conf.installer_binary_dir,conf.wineserver_binary]))
"""
        child = self.child(program, self.path)
        output, error = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, error)
        self.assertEqual(json.loads(output), expected)

    def test_fresh_pending_context_and_relative_restore_block_stale_config_save(self):
        stale = configuration(self.path)
        second = self.root / "second"
        latest = dict(self.document, install_dir=str(second), WINEPREFIX="prefix")
        self.path.write_text(json.dumps(latest))
        previous = self.path.read_bytes()
        selected = config_store.context(latest)
        journal = runtime.selection_state(second) / "runtime.json"
        journal.write_text("pending runtime")
        with self.assertRaises(maintenance.RecoveryRequired):
            stale._change(curses_color_scheme="Dark")
        self.assertEqual(self.path.read_bytes(), previous)
        journal.unlink()
        current = configuration(self.path)
        selected.pending.write_text("pending restore")
        with self.assertRaises(maintenance.RecoveryRequired):
            current._change(curses_color_scheme="Dark")
        self.assertEqual(self.path.read_bytes(), previous)
        selected.pending.unlink()
        with self.assertRaisesRegex(config_store.ConfigurationError, "changed on disk"):
            stale._change(curses_color_scheme="Dark")
        self.assertEqual(self.path.read_bytes(), previous)

    def test_non_wine_data_writers_refuse_pending_runtime(self):
        conf = configuration(self.path)
        conf.app.logos = None
        journal = runtime.selection_state(self.install) / "runtime.json"
        journal.write_text("preserved runtime recovery")
        for operation in (lambda: control.remove_library_catalog(conf.app),
                          lambda: backup.RestoreTask(conf.app).run()):
            with self.assertRaises(maintenance.RecoveryRequired):
                operation()
        self.assertEqual(journal.read_text(), "preserved runtime recovery")
        self.assertFalse(Path(conf.wine_prefix).exists())

    def test_previous_copy_role_collision_preserves_primary_and_portable(self):
        portable = self.install / constants.PORTABLE_CONFIG_NAME
        selected = portable.with_name(f".{portable.name}.previous")
        portable.write_text(json.dumps(dict(self.document, future="portable")))
        selected.write_text(json.dumps(dict(self.document, future="selected")))
        before = selected.read_bytes(), portable.read_bytes()
        conf = configuration(selected)
        with self.assertRaisesRegex(config_store.ConfigurationError, "conflict"):
            conf._change(curses_color_scheme="Dark")
        self.assertEqual((selected.read_bytes(), portable.read_bytes()), before)

    def test_warning_delivery_failure_cannot_undo_committed_memory_or_baseline(self):
        conf = configuration(self.path)
        portable = self.install / constants.PORTABLE_CONFIG_NAME
        portable.write_text(json.dumps(self.document))
        publish = config_store.Target.publish

        def fail_portable(target):
            if target.path == portable:
                raise OSError("portable publication failed")
            return publish(target)

        conf.app.status.side_effect = RuntimeError("closed frontend")
        with patch.object(config_store.Target, "publish", fail_portable):
            conf._change(curses_color_scheme="Dark")
        self.assertEqual(conf._raw.curses_color_scheme, "Dark")
        self.assertEqual(conf._raw._baseline["curses_color_scheme"], "Dark")
        self.assertEqual(json.loads(self.path.read_bytes())["curses_color_scheme"], "Dark")
        conf._change(backup_dir="backups")
        self.assertEqual(configuration(self.path).curses_color_scheme, "Dark")

    def test_fresh_system_install_preparation_persists_version_and_discovers_installed_paths(self):
        document = dict(self.document)
        document.pop("faithlife_product_version")
        self.path.write_text(json.dumps(document))
        live = Path(document["WINEPREFIX"]) / "drive_c/users/fixture/AppData/Local/Logos"
        live.mkdir(parents=True)
        executable = live / "Logos.exe"
        executable.write_text("harmless installed fixture")
        executable.chmod(0o700)
        conf = configuration(self.path)
        self.assertEqual(conf.faithlife_product_version, "10")
        self.assertNotIn("faithlife_product_version", json.loads(self.path.read_bytes()))
        with patch.object(installer, "_install") as install:
            installer.install(conf.app)
        install.assert_called_once_with(conf.app)
        self.assertEqual(Path(configuration(self.path).logos_exe), executable)
        self.assertTrue(App.is_installed(configuration(self.path).app))
        self.assertEqual(json.loads(self.path.read_bytes())["faithlife_product_version"], "10")

    def test_fresh_main_startup_attaches_selected_file_handler_and_debug_level(self):
        log = self.root / "selected.log"
        self.path.write_text(json.dumps(dict(self.document, LOGOS_LOG=str(log))))
        program = """
import logging,sys
from logos_desktop import main
def action(configuration,operation):
    logging.info('M07 file handler marker')
    logging.debug('M07 debug marker')
main.run=action
sys.argv=['controller','-c',sys.argv[1],'--debug','--force-root']
main.main()
"""
        child = self.child(program, self.path)
        _, error = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, error)
        self.assertIn("M07 file handler marker", log.read_text())
        self.assertIn("M07 debug marker", log.read_text())

    def test_first_runtime_switch_preserves_selected_source_and_custom_conf_original(self):
        source = self.root / "old.conf"
        source.write_text(
            f"FLPRODUCT=Logos\nTARGETVERSION=10\nINSTALLDIR='{self.install}'\nWINE_EXE=data/bin/old.AppImage\n"
        )
        original = source.read_bytes()
        candidate = self.root / "new.AppImage"
        candidate.write_bytes(appimage())
        accepted = artifacts.import_local(
            candidate, self.install / "data/bin", artifacts.ArtifactSpec("local:fixture", candidate.name, "appimage"),
        )
        destination = self.root / "migrated.json"
        with patch.object(constants, "DEFAULT_CONFIG_PATH", str(destination)):
            with patch.dict(os.environ):
                os.environ.pop("CONFIG_FILE", None)
                document, _ = config_store.read_document(source)
                overrides = EphemeralConfiguration.project(document, str(source), destination=str(destination))
                app = SimpleNamespace(_config_updated_event=threading.Event())
                app.conf = Config(overrides, app)
                with patch.object(wine, "require_compatible"):
                    runtime.activate(app, accepted)
                self.assertEqual(app.conf.config_file_path, str(destination))
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(configuration(destination).wine_binary, str(accepted.path))
        custom = self.root / "custom.conf"
        custom.write_bytes(original)
        conf = configuration(custom)
        with patch.object(wine, "require_compatible"):
            runtime.activate(conf.app, accepted)
        self.assertEqual((self.root / ".custom.conf.legacy-original").read_bytes(), original)
        conf._change(curses_color_scheme="Dark")
        self.assertEqual((self.root / ".custom.conf.legacy-original").read_bytes(), original)

    def test_runtime_migration_refuses_a_competing_destination(self):
        source = self.root / "legacy.conf"
        source.write_text(f"FLPRODUCT=Logos\nTARGETVERSION=10\nINSTALLDIR='{self.install}'\n")
        conf = configuration(source)
        destination = self.root / "new.json"
        conf._overrides.config_destination = str(destination)
        destination.write_text('{"faithlife_product":"OtherProduct"}')
        candidate = self.root / "candidate.AppImage"
        candidate.write_bytes(appimage())
        accepted = artifacts.import_local(
            candidate, self.install / "data/bin", artifacts.ArtifactSpec("local:fixture", candidate.name, "appimage"),
        )
        before = source.read_bytes(), destination.read_bytes()
        with patch.object(wine, "require_compatible"):
            with self.assertRaisesRegex(config_store.ConfigurationError, "changed"):
                runtime.activate(conf.app, accepted)
        self.assertEqual((source.read_bytes(), destination.read_bytes()), before)

    def test_runtime_recovery_rebinds_restored_prefix_and_companion_projections(self):
        self.path.write_text(json.dumps(dict(
            self.document, WINEPREFIX="prefix-old", APPDIR_BINDIR="tools", WINESERVER_EXE="tools/server",
        )))
        candidate = self.root / "new.AppImage"
        candidate.write_bytes(appimage())
        accepted = artifacts.import_local(
            candidate, self.install / "data/bin", artifacts.ArtifactSpec("local:fixture", candidate.name, "appimage"),
        )

        def interrupted():
            replace = runtime._replace_bytes

            def stop_after_selected(path, content, mode=0o600):
                replace(path, content, mode)
                if path == self.path:
                    os._exit(32)

            with patch.dict(os.environ, {"WINEPREFIX": str(self.root / "prefix-new")}):
                conf = configuration(self.path)
                with (
                    patch.object(wine, "require_compatible"),
                    patch.object(runtime, "_replace_bytes", stop_after_selected),
                ):
                    runtime.activate(conf.app, accepted)

        process = multiprocessing.get_context("fork").Process(target=interrupted)
        process.start()
        process.join(5)
        self.assertEqual(process.exitcode, 32)
        conf = configuration(self.path)
        self.assertEqual(conf.wine_prefix, str(self.root / "prefix-new"))
        runtime.recover(conf.app)
        self.assertEqual(conf.wine_prefix, str(self.install / "prefix-old"))
        self.assertEqual(conf.installer_binary_dir, str(self.install / "tools"))
        self.assertEqual(conf.wineserver_binary, str(self.install / "tools/server"))
        self.assertFalse(runtime.pending(self.install))

    def test_runtime_failure_preserves_a_competing_configuration_and_recovery_record(self):
        conf = configuration(self.path)
        source = self.root / "new.AppImage"
        source.write_bytes(appimage())
        accepted = artifacts.import_local(
            source, self.install / "data/bin", artifacts.ArtifactSpec("local:fixture", source.name, "appimage"),
        )
        replace = runtime._replace_bytes
        foreign = b'{"future":"concurrently edited configuration"}'

        def interfere(path, content, mode=0o600):
            replace(path, content, mode)
            if path.name == "runtime.json":
                self.path.write_bytes(foreign)

        with patch.object(wine, "require_compatible"), patch.object(runtime, "_replace_bytes", interfere):
            with self.assertRaises(maintenance.RecoveryRequired):
                runtime.activate(conf.app, accepted)
        self.assertEqual(self.path.read_bytes(), foreign)
        self.assertTrue(runtime.pending(self.install))

    def test_activation_refuses_pending_durable_installation_hidden_by_environment(self):
        second = self.root / "second"
        source = self.root / "candidate.AppImage"
        source.write_bytes(appimage())
        accepted = artifacts.import_local(
            source, second / "data/bin", artifacts.ArtifactSpec("local:fixture", source.name, "appimage"),
        )
        journal = runtime.selection_state(self.install) / "runtime.json"
        journal.write_text("durable installation pending")
        before = self.path.read_bytes()
        with patch.dict(os.environ, {"INSTALLDIR": str(second), "WINEPREFIX": str(second / "prefix")}):
            conf = configuration(self.path)
            with patch.object(wine, "require_compatible"):
                with self.assertRaises(maintenance.RecoveryRequired):
                    runtime.activate(conf.app, accepted)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(journal.read_text(), "durable installation pending")
        self.assertFalse(runtime.pending(second))
        self.assertFalse((second / "data/bin/selected_wine.AppImage").exists())
        self.assertFalse((second / constants.PORTABLE_CONFIG_NAME).exists())

    def test_relative_cli_appimage_uses_invocation_file_not_installation_decoy(self):
        invocation = self.root / "invocation"
        invocation.mkdir()
        requested = invocation / "candidate.AppImage"
        requested.write_bytes(appimage(b"invocation selection"))
        (self.install / requested.name).write_bytes(appimage(b"installation decoy"))
        previous = Path.cwd()
        try:
            os.chdir(invocation)
            parser = main.get_parser()
            with patch.dict(os.environ, {"WINE_EXE": str(self.install / requested.name)}):
                overrides, _ = main.parse_args(parser.parse_args([
                    "-c", str(self.path), "--set-appimage", "./candidate.AppImage",
                ]), parser)
            self.assertEqual(overrides.wine_appimage_path, str(requested))
            app = SimpleNamespace(_config_updated_event=threading.Event(), status=Mock())
            app.conf = Config(overrides, app)
            with patch.object(wine, "require_compatible"):
                utils.set_appimage_symlink(app)
            self.assertEqual(Path(app.conf.wine_binary).read_bytes(), requested.read_bytes())
            app.conf._change(curses_color_scheme="Dark")
            self.assertNotIn("wine_appimage_path", app.conf._overrides.cli_overrides)
            self.assertEqual(Path(app.conf.wine_binary).read_bytes(), requested.read_bytes())
        finally:
            os.chdir(previous)

    def test_saved_appimage_selection_cannot_return_after_selecting_system_wine(self):
        old = self.root / "old.AppImage"
        old.write_bytes(appimage(b"obsolete selection"))
        selected = self.install / "tools/wine"
        self.path.write_text(json.dumps(dict(self.document, SELECTED_APPIMAGE_FILENAME=str(old))))
        conf = configuration(self.path)
        conf.wine_binary = str(selected)
        with patch.object(utils, "get_winebin_code_and_desc", return_value=("System", "fixture")):
            self.assertEqual(conf.wine_binary_code, "System")
        conf._change(curses_color_scheme="Dark")
        conf.reload()
        self.assertNotIn("SELECTED_APPIMAGE_FILENAME", json.loads(self.path.read_bytes()))
        self.assertIsNone(conf.wine_appimage_path)
        with patch.object(runtime, "activate") as activate:
            installer.create_wine_appimage_symlinks(conf.app)
        activate.assert_not_called()
        self.assertEqual(configuration(self.path).wine_binary, str(selected))

    def test_initial_config_rebinds_saved_projections_from_one_current_snapshot(self):
        overrides = EphemeralConfiguration.load_from_path(str(self.path))
        second = self.root / "second"
        self.path.write_text(json.dumps(dict(
            self.document, install_dir=str(second), WINEPREFIX="prefix", APPDIR_BINDIR="tools",
            WINESERVER_EXE="tools/server", faithlife_product="Logos",
        )))
        app = SimpleNamespace(_config_updated_event=threading.Event())
        conf = Config(overrides, app)
        self.assertEqual(conf.install_dir, str(second))
        self.assertEqual(conf.wine_prefix, str(second / "prefix"))
        self.assertEqual(conf.installer_binary_dir, str(second / "tools"))
        self.assertEqual(conf.wineserver_binary, str(second / "tools/server"))
        self.assertEqual(conf.faithlife_product, "Logos")

    def test_reload_waits_for_ordinary_save_and_publishes_the_committed_snapshot(self):
        conf = configuration(self.path)
        staged, release_writer, read_old, release_reader, finished = [threading.Event() for _ in range(5)]
        errors = []
        publish = config_store.Target.publish
        load = PersistentConfiguration.load_from_path
        reads = []

        def pause(target):
            if target.path == self.path:
                staged.set()
                if not release_writer.wait(5):
                    raise RuntimeError("writer deadline")
            return publish(target)

        def read(*arguments, **kwargs):
            loaded = load(*arguments, **kwargs)
            if threading.current_thread().name == "m07-reloader":
                reads.append(loaded)
                if len(reads) == 1:
                    read_old.set()
                    if not release_reader.wait(5):
                        raise RuntimeError("reload deadline")
            return loaded

        def write():
            try:
                conf._change(curses_color_scheme="Dark")
            except BaseException as error:
                errors.append(error)

        def reload():
            try:
                conf.reload()
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        with patch.object(config_store.Target, "publish", pause), patch.object(
            PersistentConfiguration, "load_from_path", side_effect=read,
        ):
            writer = threading.Thread(target=write)
            reloader = threading.Thread(target=reload, name="m07-reloader")
            writer.start()
            try:
                self.assertTrue(staged.wait(5))
                reloader.start()
                self.assertTrue(read_old.wait(5))
                release_reader.set()
                self.assertFalse(finished.wait(0.15))
            finally:
                release_reader.set()
                release_writer.set()
                writer.join(5)
                if reloader.ident is not None:
                    reloader.join(5)
        self.assertFalse(writer.is_alive() or reloader.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(conf.curses_color_scheme, "Dark")
        self.assertEqual(json.loads(self.path.read_bytes())["curses_color_scheme"], "Dark")

    def test_set_appimage_startup_validation_still_attaches_the_selected_log(self):
        source = self.root / "candidate.AppImage"
        source.write_bytes(appimage())
        log = self.root / "switch.log"
        self.path.write_text(json.dumps(dict(self.document, LOGOS_LOG=str(log))))
        program = """
import logging,sys
from logos_desktop import main
main.run=lambda configuration,action: logging.info('M07 runtime-switch log marker')
sys.argv=['controller','-c',sys.argv[1],'--set-appimage',sys.argv[2],'--force-root']
main.main()
"""
        child = self.child(program, self.path, source)
        _, error = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, error)
        self.assertIn("M07 runtime-switch log marker", log.read_text())
