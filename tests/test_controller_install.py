from contextlib import ExitStack
import base64
import csv
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import Mock, patch
import venv
import zipfile

from logos_desktop import constants, controller_install as install
import logos_desktop


class ControllerInstallTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.home))
        self.stack.enter_context(patch.object(install.pwd, "getpwuid", return_value=Mock(pw_dir=str(self.home))))
        self.root = self.home / "controller"
        self.root.mkdir(mode=0o700)
        (self.root / "versions").mkdir(mode=0o700)
        self.a = "versions/1.0.0-" + "a" * 64
        self.b = "versions/1.1.0-" + "b" * 64
        self.c = "versions/1.2.0-" + "c" * 64
        for relative in (self.a, self.b, self.c):
            (self.root / relative).mkdir(mode=0o700)
        install._publish(self.root, self.a)
        (self.root / "logos-desktop").write_text(install._launcher(self.root))
        (self.root / "logos-desktop").chmod(0o700)
        self.identity = install.Identity("venv", "1.0.0", "1.0.0", self.root,
                                         self.root / "logos-desktop", self.root / "provider.json")

    def test_bootstrap_creates_private_parents_only_beneath_safe_home_paths(self):
        target = self.home / ".local/lib/logos-desktop"
        with self.assertRaises(FileNotFoundError):
            install._root(target)
        self.assertFalse((self.home / ".local").exists())
        self.assertEqual(install._root(target, create=True), target)
        for path in (self.home / ".local", self.home / ".local/lib", target):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

        unsafe = self.home / "unsafe"
        unsafe.mkdir()
        unsafe.chmod(0o777)
        alias = self.home / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for path in (unsafe / "new/controller", alias / "new/controller", self.home.parent / "outside/controller"):
            with self.subTest(path=path), self.assertRaises(install.InstallError):
                install._root(path, create=True)
        self.assertFalse((unsafe / "new").exists())
        self.assertFalse((self.root / "new").exists())

    def test_public_provider_setup_binds_owner_and_preserves_existing_configuration(self):
        self.assertEqual(install.configure_provider(self.identity).status, "configured")
        data = install._provider(self.identity)
        self.assertEqual(data, {"schema": 1, "installation": str(self.root),
                                "manifest_url": install.RELEASE_MANIFEST})
        self.assertEqual(self.identity.provider_path.stat().st_mode & 0o777, 0o600)
        original = self.identity.provider_path.read_bytes()
        with self.assertRaisesRegex(install.InstallError, "already exists"):
            install.configure_provider(self.identity)
        self.assertEqual(self.identity.provider_path.read_bytes(), original)
        self.assertFalse(list(self.root.glob(".provider-*")))

        with self.assertRaisesRegex(install.InstallError, "identified"):
            install.configure_provider(install.Identity("unsupported"))
        wrong = install.Identity("venv", root=self.root, provider_path=self.home / "provider.json")
        with self.assertRaisesRegex(install.InstallError, "path"):
            install.configure_provider(wrong)
        self.assertFalse((self.home / "provider.json").exists())

    def test_public_provider_setup_refuses_symlink_and_concurrent_publication(self):
        outside = self.home / "outside"
        outside.write_bytes(b"preserved")
        self.identity.provider_path.symlink_to(outside)
        with self.assertRaisesRegex(install.InstallError, "already exists"):
            install.configure_provider(self.identity)
        self.assertEqual(outside.read_bytes(), b"preserved")
        self.assertTrue(self.identity.provider_path.is_symlink())
        self.identity.provider_path.unlink()

        link = os.link

        def publish_concurrently(*args, **kwargs):
            self.identity.provider_path.write_bytes(b"concurrent provider")
            return link(*args, **kwargs)

        with patch.object(install.os, "link", side_effect=publish_concurrently):
            with self.assertRaisesRegex(install.InstallError, "already exists"):
                install.configure_provider(self.identity)
        self.assertEqual(self.identity.provider_path.read_bytes(), b"concurrent provider")
        self.assertFalse(list(self.root.glob(".provider-*")))

    def test_public_provider_setup_refuses_exposed_parent_and_unidentified_command(self):
        self.home.chmod(0o770)
        try:
            with self.assertRaisesRegex(install.InstallError, "parent"):
                install.configure_provider(self.identity)
        finally:
            self.home.chmod(0o700)
        self.root.chmod(0o755)
        with self.assertRaises(install.InstallError):
            install.configure_provider(self.identity)
        self.assertFalse(self.identity.provider_path.exists())
        with patch.object(install, "identify", return_value=install.Identity("unsupported")):
            self.assertEqual(install.main(["configure-provider"]), 1)

    def distribution(self, relative=None):
        prefix = self.root / (relative or self.a)
        version, digest = prefix.name.split("-", 1)
        venv.EnvBuilder(with_pip=False, symlinks=True).create(prefix)
        site = next((prefix / "lib").glob("python*/site-packages"))
        package = site / "logos_desktop"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "constants.py").write_text(f"APP_VERSION = {version!r}\n")
        for name in ("assets/LogosStubFailOK.mst", "img/logos-desktop.svg"):
            resource = package / name
            resource.parent.mkdir(exist_ok=True)
            resource.write_text("fixture")
        qt = site / "PySide6"
        qt.mkdir()
        (qt / "__init__.py").write_text("")
        (qt / "QtCore.py").write_text("")
        metadata = site / f"logos_desktop-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Metadata-Version: 2.1\nName: logos-desktop\nVersion: {version}\n")
        (metadata / "entry_points.txt").write_text("[console_scripts]\nlogos-desktop = logos_desktop.main:main\n")
        (metadata / "direct_url.json").write_text(json.dumps({"archive_info": {"hashes": {"sha256": digest}}}))
        (metadata / "RECORD").write_text(
            f"logos_desktop/__init__.py,,\n{metadata.name}/METADATA,,\n"
        )
        entry = prefix / "bin/logos-desktop"
        entry.write_text(f"#!{prefix}/bin/python\n")
        entry.chmod(0o700)
        return prefix, package, importlib.metadata.Distribution.at(metadata)

    def recorded_distribution(self):
        prefix, package, distribution = self.distribution()
        (package / "main.py").write_text("def main():\n    pass\n")
        metadata = Path(distribution._path)
        paths = [*package.rglob("*"), *metadata.iterdir(), prefix / "bin/logos-desktop"]
        output = io.StringIO()
        writer = csv.writer(output)
        for path in paths:
            if not path.is_file() or path.name == "RECORD":
                continue
            content = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
            writer.writerow((os.path.relpath(path, metadata.parent), "sha256=" + digest, len(content)))
        writer.writerow((metadata.name + "/RECORD", "", ""))
        (metadata / "RECORD").write_text(output.getvalue())
        return prefix, package, metadata

    def test_migration_inventory_rejects_modified_bytes_and_escaping_record(self):
        prefix, package, metadata = self.recorded_distribution()
        evidence = install.installed_evidence(prefix)
        self.assertIn("bin/logos-desktop", evidence)
        original = (package / "main.py").read_bytes()
        (package / "main.py").write_bytes(b"changed")
        with self.assertRaisesRegex(install.InstallError, "differs"):
            install.installed_evidence(prefix)
        (package / "main.py").write_bytes(original)
        with (metadata / "RECORD").open("a") as output:
            output.write("../../../../../../outside,,\n")
        with self.assertRaisesRegex(install.InstallError, "outside"):
            install.installed_evidence(prefix)

    def test_migration_rollback_uses_recorded_bytes_without_old_namespace_probe(self):
        prefix, package, metadata = self.recorded_distribution()
        evidence = install.installed_evidence(prefix)
        record = install._activate(self.root, self.b, "1.1.0", previous_files=evidence)
        with patch.object(install, "_probe", side_effect=AssertionError("old module executed")):
            self.assertTrue(install._recover(self.root, record))
        self.assertEqual(install._target(self.root), self.a)
        record = install._activate(self.root, self.b, "1.1.0", previous_files=evidence)
        (package / "main.py").write_text("changed")
        with self.assertRaises(install.InstallError):
            install._recover(self.root, record)
        self.assertEqual(install._target(self.root), self.b)
        install._publish(self.root, self.c)
        with self.assertRaisesRegex(install.InstallError, "later publication"):
            install._recover(self.root, record)
        self.assertEqual(install._target(self.root), self.c)

    def test_explicit_migration_preserves_provider_profile_and_preparation_failure(self):
        self.recorded_distribution()
        profile = self.home / "selected profile.json"
        profile.write_text('{"install_dir": "/fixture/library"}')
        self.provider({"manifest_url": "https://example.invalid/releases"})
        before = profile.read_bytes(), self.identity.provider_path.read_bytes()
        wheel, data = self.wheel()
        with (patch.object(install, "_migration_quiet"),
              patch.object(install, "_install", side_effect=OSError("disk full"))):
            with self.assertRaises(OSError):
                install.migrate(self.root, wheel, data, str(profile), "qt")
        self.assertEqual(install._target(self.root), self.a)
        self.assertFalse((self.root / "operation.json").exists())
        with (patch.object(install, "_migration_quiet"),
              patch.object(install, "_install", return_value=self.b),
              patch.object(install, "_launch", return_value=install.Result("updated", "ready")) as launch):
            result = install.migrate(self.root, wheel, data, str(profile), "qt")
        self.assertEqual(result.status, "migrated")
        self.assertEqual(launch.call_args.args[2:], (profile, "qt"))
        self.assertTrue(install._receipt(self.root)["previous_files"])
        self.assertEqual((profile.read_bytes(), self.identity.provider_path.read_bytes()), before)

    def test_migration_refuses_actual_relative_and_alias_invocations(self):
        child_script = self.root / "idle.py"
        child_script.write_text("import sys\nprint('ready', flush=True)\nsys.stdin.readline()\n")
        alias = self.home / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for argument in ("./idle.py", str(alias / "idle.py")):
            with self.subTest(argument=argument):
                child = subprocess.Popen([sys.executable, argument], cwd=self.root, text=True,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                try:
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                    with self.assertRaisesRegex(install.InstallError, "Close controllers"):
                        install._migration_quiet(self.root)
                finally:
                    child.communicate("\n", timeout=5)
                self.assertEqual(child.returncode, 0)

    def test_migration_receipt_budget_refuses_before_any_publication(self):
        evidence = {f"file-{number}": "a" * 64 for number in range(1000)}
        with self.assertRaisesRegex(install.InstallError, "receipt size"):
            install._activate(self.root, self.b, "1.1.0", previous_files=evidence)
        self.assertEqual(install._target(self.root), self.a)
        self.assertFalse((self.root / "operation.json").exists())

    def test_migration_preserves_pending_runtime_journal_before_install_and_after_prepare(self):
        self.recorded_distribution()
        profile = self.home / "profile.json"
        library = self.home / "library"
        profile.write_text(json.dumps({"install_dir": str(library)}))
        state = self.home / ".local/state/logos-desktop"
        key = hashlib.sha256(os.fsencode(library)).hexdigest()
        journal = state / "runtime-selection" / key / "runtime.json"
        journal.parent.mkdir(parents=True)
        content = b'{"previous": [{"path": "explicit historical fixture"}]}\n'
        wheel, data = self.wheel()
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(state.parent)}):
            for phase in ("preflight", "preparation"):
                def prepare(*args):
                    journal.write_bytes(content)
                    return self.b

                if phase == "preflight":
                    journal.write_bytes(content)
                with (patch.object(install, "_migration_quiet"),
                      patch.object(install, "_install", side_effect=prepare) as install_wheel):
                    with self.assertRaisesRegex(install.InstallError, "previous controller"):
                        install.migrate(self.root, wheel, data, str(profile), "qt")
                    if phase == "preflight":
                        install_wheel.assert_not_called()
                self.assertEqual(install._target(self.root), self.a)
                self.assertFalse((self.root / "operation.json").exists())
                self.assertEqual(journal.read_bytes(), content)
                journal.unlink()

    def test_migration_reservation_uses_existing_prefix_protocol_and_releases(self):
        from logos_desktop import instance

        config = self.home / "profile.json"
        prefix = self.home / "library/data/wine64_bottle"
        config.write_text(json.dumps({"install_dir": str(prefix.parent.parent)}))
        coordination = self.home / ".local/state/logos-desktop/coordination"
        with patch.object(constants, "COORDINATION_DIR", coordination):
            with install._migration_reservation(config):
                with self.assertRaises(instance.InstanceBusy):
                    instance.Session(prefix).acquire()
            with patch.object(instance, "check_legacy"), instance.Session(prefix):
                with self.assertRaises(install.InstallError):
                    with install._migration_reservation(config):
                        self.fail("Migration admitted an owned prefix")

    def test_migration_refuses_inaccessible_known_recovery_locations(self):
        self.recorded_distribution()
        profile = self.home / 'profile.json'
        library = self.home / 'library'
        prefix = library / 'data/wine64_bottle'
        profile.write_text(json.dumps({'install_dir': str(library)}))
        coordination = self.home / '.local/state/logos-desktop/coordination'
        operations = coordination / 'operations'
        directory = operations / hashlib.sha256(os.fsencode(prefix)).hexdigest()
        directory.mkdir(mode=0o700, parents=True)
        coordination.chmod(0o700)
        operations.chmod(0o700)
        state = self.home / 'alternate-state/logos-desktop'
        journal = state / 'runtime-selection' / hashlib.sha256(os.fsencode(library)).hexdigest() / 'runtime.json'
        journal.parent.mkdir(parents=True)
        journal.write_bytes(b'pending historical fixture\n')
        location = json.dumps({'schema_version': 1, 'state_dir': str(state),
                               'install': str(library), 'prefix': str(prefix)}, sort_keys=True).encode()
        record = directory / ('state-' + hashlib.sha256(location).hexdigest() + '.json')
        record.write_bytes(location)
        record.chmod(0o600)
        wheel, data = self.wheel()
        operations.chmod(0o000)
        try:
            with patch.object(install, '_install') as prepare:
                with self.assertRaises(PermissionError):
                    install.migrate(self.root, wheel, data, str(profile), 'qt')
                prepare.assert_not_called()
        finally:
            operations.chmod(0o700)
        self.assertEqual(install._target(self.root), self.a)
        self.assertFalse((self.root / 'operation.json').exists())
        self.assertEqual(journal.read_bytes(), b'pending historical fixture\n')

    def test_migration_detects_alias_to_shared_venv_interpreter(self):
        prefix, package, distribution = self.distribution()
        alias = self.home / "external-alias"
        alias.symlink_to(self.root / "current", target_is_directory=True)
        child = subprocess.Popen([str(alias / "bin/python"), "-c",
                                  "import sys\nprint('ready', flush=True)\nsys.stdin.readline()\n"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(install.InstallError, "Close controllers"):
                install._migration_quiet(self.root)
        finally:
            child.communicate("\n", timeout=5)

    def runtime(self, prefix, package, distribution):
        self.stack.enter_context(patch.object(sys, "prefix", str(prefix)))
        self.stack.enter_context(patch.object(sys, "executable", str(prefix / "bin/python")))
        self.stack.enter_context(patch.object(logos_desktop, "__file__", str(package / "__init__.py")))
        self.stack.enter_context(patch.object(constants, "APP_VERSION", distribution.version))
        self.stack.enter_context(patch.object(importlib.metadata, "distribution", return_value=distribution))
        self.stack.enter_context(patch.object(install, "_owner", return_value=[]))
        self.stack.enter_context(patch.object(install.shutil, "which", return_value=None))

    def provider(self, value):
        install._write(self.identity.provider_path, {"schema": 1, "installation": str(self.root), **value})

    def wheel(self, content=None):
        path = self.home / "logos_desktop-1.1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("logos_desktop/controller_install.py", content or "# fixture\n")
        data = {"filename": path.name, "version": "1.1.0", "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "url": "https://example.invalid/wheel"}
        return path, data

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Disposable helper did not reach the expected state")

    def test_real_isolated_interpreter_identity_and_provenance(self):
        prefix, package, distribution = self.distribution()
        self.runtime(prefix, package, distribution)
        self.assertEqual(install.identify(), self.identity)
        metadata = Path(distribution._path)
        (metadata / "direct_url.json").write_text('{"archive_info": {"hashes": {"sha256": "wrong"}}}')
        self.assertEqual(install.identify().owner, "unsupported")

    def test_marker_alone_and_wrong_entrypoint_do_not_admit_environment(self):
        prefix, package, distribution = self.distribution()
        self.runtime(prefix, package, distribution)
        (prefix / "bin/logos-desktop").write_text("#!/usr/bin/python\n")
        self.assertIn("entry point", install.identify().message)
        (prefix / "pyvenv.cfg").write_text("include-system-site-packages = true\n")
        self.assertIn("foreign system", install.identify().message)

    def test_private_prerelease_a_is_identified_but_update_feed_requires_stable(self):
        relative = "versions/1.0.0rc1-" + "e" * 64
        (self.root / relative).mkdir(mode=0o700)
        prefix, package, distribution = self.distribution(relative)
        install._publish(self.root, relative)
        self.runtime(prefix, package, distribution)
        identity = install.identify()
        self.assertEqual(identity.owner, "venv")
        self.assertEqual(identity.running_version, "1.0.0rc1")
        _, release = self.wheel()
        release.update(version="1.1.0rc1", filename="logos_desktop-1.1.0rc1-py3-none-any.whl")
        self.assertEqual(install._wheel(release, stable=False), release)
        with self.assertRaises(install.InstallError):
            install._wheel(release)

    def test_package_database_precedes_canonical_layout_and_rejects_mixed_owner(self):
        prefix, package, distribution = self.distribution()
        self.runtime(prefix, package, distribution)
        with patch.object(install, "_owner", return_value=[("pacman", "logos-desktop")]):
            identity = install.identify()
        self.assertEqual(identity.owner, "unsupported")
        self.assertIn("system package manager", identity.message)
        with patch.object(install, "_owner", side_effect=[[], [], [("rpm", "foreign")]]):
            self.assertEqual(install.identify().owner, "unsupported")

    def test_loaded_source_shadowing_is_rejected(self):
        prefix, package, distribution = self.distribution()
        self.runtime(prefix, package, distribution)
        with patch.object(logos_desktop, "__file__", str(self.home / "source/__init__.py")):
            self.assertIn("Loaded source", install.identify().message)

    def test_registered_user_command_preserves_managed_identity_and_refuses_edits(self):
        from logos_desktop import user_integration

        prefix, package, distribution = self.distribution()
        self.runtime(prefix, package, distribution)
        command = self.home / ".local/bin/logos-desktop"
        command.parent.mkdir(parents=True)
        content = user_integration.command_content(self.root)
        command.write_bytes(content)
        command.chmod(0o700)
        with patch.object(install.shutil, "which", return_value=str(command)):
            self.assertEqual(install.identify().owner, "venv")
            command.write_bytes(content + b"# user edit\n")
            self.assertEqual(install.identify().owner, "unsupported")
            command.write_bytes(content)
            command.chmod(0o755)
            self.assertEqual(install.identify().owner, "unsupported")

    def test_setup_preserves_custom_provider_and_refuses_pending_publication(self):
        from logos_desktop import user_integration

        install._write(self.identity.provider_path, {"schema": 1, "installation": str(self.root),
                                                    "manifest_url": "https://example.invalid/custom"})
        original = self.identity.provider_path.read_bytes()
        with (patch.object(install, "identify", return_value=self.identity),
              patch.object(user_integration, "install", return_value="fixture setup") as setup):
            self.assertEqual(install.main(["setup", "--no-path"]), 0)
            setup.assert_called_once_with(self.root, modify_path=False)
            self.assertEqual(self.identity.provider_path.read_bytes(), original)
            (self.root / "operation.json").write_text("{}")
            setup.reset_mock()
            with patch.object(install, "_receipt", return_value={"status": "published"}):
                self.assertEqual(install.main(["setup"]), 1)
            setup.assert_not_called()

    def test_real_probe_rejects_missing_packaged_resource(self):
        prefix, package, _ = self.distribution()
        self.assertEqual(install._probe(prefix), "1.0.0")
        (package / "assets/LogosStubFailOK.mst").unlink()
        with self.assertRaises(install.InstallError):
            install._probe(prefix)

    def test_probe_accepts_pip_long_shebang_wrapper_but_rejects_extra_commands(self):
        prefix, _, _ = self.distribution()
        entry = prefix / "bin/logos-desktop"
        entry.write_text(f'#!/bin/sh\n\'\'\'exec\' "{prefix}/bin/python" "$0" "$@"\n\' \'\'\'\n')
        self.assertEqual(install._probe(prefix), "1.0.0")
        entry.write_text(entry.read_text().replace('"$0"', '"unexpected" "$0"'))
        with self.assertRaises(install.InstallError):
            install._probe(prefix)

    def test_same_python_inode_in_another_venv_does_not_relax_entrypoint_binding(self):
        prefix, _, _ = self.distribution(self.a)
        other, _, _ = self.distribution(self.b)
        expected = prefix / "bin/python"
        foreign = other / "bin/python"
        self.assertTrue(foreign.samefile(expected))
        (prefix / "bin/logos-desktop").write_text(f"#!{foreign}\n")
        with patch.object(install, "_owner", return_value=[("pacman", "python")]) as owners:
            with self.assertRaisesRegex(install.InstallError, "entry point"):
                install._probe(prefix)
        owners.assert_not_called()

    def test_system_probe_rejects_another_venv_even_when_python_has_the_same_inode(self):
        prefix = self.home / "system/usr"
        (prefix / "bin").mkdir(parents=True)
        expected = prefix / "bin/python"
        expected.symlink_to(Path(sys.executable).resolve())
        other, _, _ = self.distribution(self.a)
        foreign = other / "bin/python"
        self.assertTrue(foreign.samefile(expected))
        (prefix / "bin/logos-desktop").write_text(f"#!{foreign}\n")
        data = json.dumps({"prefix": str(prefix), "version": "1.0.0"})
        with (patch.object(install, "_run", return_value=data),
              patch.object(install, "_owner", return_value=[("pacman", "python")]) as owners):
            with self.assertRaisesRegex(install.InstallError, "entry point"):
                install._probe(prefix)
        owners.assert_not_called()

    def test_unregistered_path_alias_to_same_launcher_is_not_admitted(self):
        prefix, package, distribution = self.distribution()
        self.runtime(prefix, package, distribution)
        alternate = self.home / "other-bin/logos-desktop"
        alternate.parent.mkdir()
        alternate.symlink_to(self.root / "logos-desktop")
        self.assertTrue(alternate.samefile(self.root / "logos-desktop"))
        with patch.object(install.shutil, "which", return_value=str(alternate)):
            identity = install.identify()
        self.assertEqual(identity.owner, "unsupported")
        self.assertIn("PATH selects a different controller", identity.message)

    def test_nonprivate_root_and_symlink_parent_are_refused(self):
        self.root.chmod(0o755)
        with self.assertRaises(install.InstallError):
            install._root(self.root)
        linked = self.home / "link"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(install.InstallError):
            install._root(linked)

    def test_no_public_wheel_feed_and_installation_scope(self):
        self.assertEqual(install.check(self.identity).status, "unavailable")
        self.provider({"manifest_url": "https://example.invalid/manifest"})
        data = install._read(self.identity.provider_path)
        data["installation"] = "/another/controller"
        install._write(self.identity.provider_path, data)
        with patch.object(install, "_download") as download:
            self.assertEqual(install.check(self.identity).status, "unavailable")
        download.assert_not_called()

    def test_loopback_real_protocol_and_transfer_digest_rejection_before_pip(self):
        wheel, release = self.wheel()
        bodies = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = bodies[self.path]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        release["url"] = base + "/wheel"
        bodies["/manifest"] = json.dumps(release).encode()
        bodies["/wheel"] = b"x" * release["size"]
        self.provider({"manifest_url": base + "/manifest", "allow_loopback": True})
        result = install.check(self.identity)
        self.assertEqual(result.status, "available")
        with patch.object(install, "identify", return_value=self.identity), patch.object(install, "_install") as pip:
            with self.assertRaisesRegex(install.InstallError, "SHA-256"):
                install._wheel_update(self.identity, result.artifact, None, "qt")
        pip.assert_not_called()
        self.assertEqual(install._target(self.root), self.a)
        self.assertFalse((self.root / "operation.json").exists())
        with self.assertRaises(install.InstallError):
            install._url(base, {})

    def test_rejected_metadata_and_downgrade_are_not_available(self):
        self.provider({"manifest_url": "https://example.invalid/manifest"})
        _, release = self.wheel()
        for change in ({"version": "1.1.0rc1"}, {"sha256": "bad"}, {"filename": "../../wheel.whl"},
                       {"size": True}, {"url": "http://example.invalid/wheel"}):
            with self.subTest(change=change), patch.object(
                install, "_download", return_value=(json.dumps({**release, **change}).encode(), "")
            ):
                self.assertEqual(install.check(self.identity).status, "unavailable")
        release.update(version="1.0.0", filename="logos_desktop-1.0.0-py3-none-any.whl")
        with patch.object(install, "_download", return_value=(json.dumps(release).encode(), "")):
            self.assertEqual(install.check(self.identity).status, "current")

    def test_install_uses_final_path_exact_extra_and_retains_failed_environment(self):
        wheel, release = self.wheel()
        with patch.object(install, "_run", side_effect=["", install.InstallError("dependency failure")]) as run:
            with self.assertRaisesRegex(install.InstallError, "dependency failure"):
                install._install(self.root, wheel, release)
        final = self.root / "versions" / f"1.1.0-{release['sha256']}"
        self.assertTrue(final.is_dir())
        self.assertIn(str(final), run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[1].args[0][-1], f"{wheel}[gui]")
        self.assertEqual(install._target(self.root), self.a)
        with patch.object(install, "_run") as run:
            with self.assertRaisesRegex(install.InstallError, "already exists"):
                install._install(self.root, wheel, release)
        run.assert_not_called()

    def test_gui_update_prepares_before_profile_transfer_without_terminal_authorization(self):
        wheel, release = self.wheel()
        self.provider({"manifest_url": "https://example.invalid/manifest"})
        transferred = False

        def download(url, config, *, limit, destination):
            destination.write(wheel.read_bytes())
            return release['size'], release['sha256']

        def prepare(*args):
            self.assertFalse(transferred)
            self.assertEqual(install._target(self.root), self.a)
            return self.b

        def transfer():
            nonlocal transferred
            self.assertEqual(install._target(self.root), self.b)
            transferred = True

        def restart(root, record, config):
            self.assertTrue(transferred)
            install._write(root / 'operation.json', {**record, 'status': 'ready'})
            return install.Result('updated', 'Ready', release['version'])

        with (patch.object(install, 'identify', return_value=self.identity),
              patch.object(install, 'check', return_value=install.Result('available', 'B', '1.1.0', release)),
              patch.object(install, '_download', side_effect=download),
              patch.object(install, '_install', side_effect=prepare),
              patch.object(install, '_launch_gui', side_effect=restart),
              patch.object(install, 'handoff', side_effect=AssertionError('No terminal')),
              patch('builtins.input', side_effect=AssertionError('Popup already confirmed'))):
            result = install.update_gui(self.identity, '/fixture/profile.json', checkpoint=Mock(),
                                        progress=Mock(), before_restart=transfer)
        self.assertEqual(result.status, 'updated')
        self.assertEqual(install._target(self.root), self.b)
        self.assertEqual(install._receipt(self.root)['status'], 'ready')
        self.assertFalse(list(self.root.glob('.download-*')))

    def test_gui_cancel_after_preparation_keeps_a_and_records_reusable_unpublished_b(self):
        wheel, release = self.wheel()
        self.provider({"manifest_url": "https://example.invalid/manifest"})
        prepared = False

        def download(url, config, *, limit, destination):
            destination.write(wheel.read_bytes())
            return release['size'], release['sha256']

        def prepare(*args):
            nonlocal prepared
            prepared = True
            return self.b

        def checkpoint():
            if prepared:
                raise install.InstallError('Cancelled before publication')

        with (patch.object(install, 'identify', return_value=self.identity),
              patch.object(install, 'check', return_value=install.Result('available', 'B', '1.1.0', release)),
              patch.object(install, '_download', side_effect=download),
              patch.object(install, '_install', side_effect=prepare),
              patch.object(install, '_launch_gui') as launch):
            transfer = Mock()
            with self.assertRaisesRegex(install.InstallError, 'Cancelled before publication'):
                install.update_gui(self.identity, None, checkpoint=checkpoint,
                                   progress=Mock(), before_restart=transfer)
        transfer.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(install._target(self.root), self.a)
        self.assertEqual(install._receipt(self.root)['status'], 'aborted')
        with patch.object(install, '_download', return_value=(json.dumps(release).encode(), '')):
            self.assertEqual(install.check(self.identity).status, 'available')

    def test_gui_observer_protocol_uses_a_fresh_python_process_without_fork_or_input(self):
        record = install._activate(self.root, self.b, '1.1.0')
        original = subprocess.Popen
        children = []

        def start(command, **kwargs):
            self.assertEqual(command[:4], [sys.executable, '-I', '-m', 'logos_desktop.controller_install'])
            self.assertIn('observe-gui-update', command)
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertTrue(kwargs['start_new_session'])
            acceptance = {'accepted': True, 'token': record['token']}
            source = f"import json\nprint(json.dumps({acceptance!r}), flush=True)\n"
            child = original([sys.executable, '-I', '-c', source], **kwargs)
            children.append(child)
            return child

        with (patch.object(subprocess, 'Popen', side_effect=start),
              patch.object(install.os, 'fork', side_effect=AssertionError('Qt must not fork')),
              patch('builtins.input', side_effect=AssertionError('No terminal input'))):
            self.assertEqual(install._launch_gui(self.root, record, '/fixture/profile.json').status, 'handed-off')
        for child in children:
            self.assertEqual(child.wait(timeout=5), 0)

    def test_gui_observer_preserves_later_claims_and_restores_a_for_changed_b(self):
        record = install._activate(self.root, self.b, '1.1.0')
        with (patch.object(install.sys, 'prefix', str(self.root / self.a)),
              patch.object(install, '_probe', return_value='1.0.0'),
              patch.object(install, '_wait_for_predecessor'),
              patch.object(install, '_supervise') as supervise,
              patch.object(subprocess, 'Popen') as reopen):
            with self.assertRaisesRegex(install.InstallError, 'does not match'):
                install._observe_gui_update(json.dumps({'root': str(self.root), 'token': 'f' * 48,
                                                        'pid': 987654, 'started': '100'}), None)
            self.assertEqual(install._target(self.root), self.b)

            with (patch.object(install, '_probe', side_effect=['1.0.0', '9.9.9', '1.0.0']),
                  patch('sys.stdout', new_callable=io.StringIO) as accepted):
                self.assertEqual(install._observe_gui_update(
                    json.dumps({'root': str(self.root), 'token': record['token'],
                                'pid': 987654, 'started': '100'}), None), 0)
            self.assertTrue(json.loads(accepted.getvalue())['accepted'])
            result_path = self.root / f"supervision-{record['token']}.json"
            self.assertEqual(json.loads(result_path.read_text())['status'], 'failed')
            self.assertEqual(install._target(self.root), self.a)
            supervise.assert_not_called()
            reopen.assert_called_once()
            self.assertEqual(reopen.call_args.args[0],
                             [str(self.root / self.a / 'bin/logos-desktop'), '--dialog', 'qt'])

    def test_gui_waits_for_predecessor_identity_and_accepts_pid_reuse_as_exit(self):
        claim = {'pid': 987654, 'started': '100'}
        with (patch.object(install, '_process_start', side_effect=[('S', '100'), ('S', '101')]),
              patch.object(install.time, 'sleep') as wait):
            install._wait_for_predecessor(claim)
        wait.assert_called_once()

    def test_refused_observer_acceptance_restores_a_before_graphical_shutdown(self):
        original = subprocess.Popen
        for response in ({'accepted': False}, {'accepted': True, 'token': 'wrong'}, []):
            record = install._activate(self.root, self.b, '1.1.0')

            def start(command, **kwargs):
                source = f"import json\nprint(json.dumps({response!r}), flush=True)\n"
                return original([sys.executable, '-I', '-c', source], **kwargs)

            with (self.subTest(response=response), patch.object(subprocess, 'Popen', side_effect=start),
                  patch.object(install, '_probe', return_value='1.0.0')):
                result = install._launch_gui(self.root, record, None)
            self.assertEqual(result.status, 'failed')
            self.assertEqual(install._target(self.root), self.a)
            self.assertEqual(install._receipt(self.root)['status'], 'rolled-back')

    def test_pip_check_failure_and_wrong_version_never_publish(self):
        wheel, release = self.wheel()
        with patch.object(install, "_run", side_effect=["", "", install.InstallError("pip check failed")]):
            with self.assertRaisesRegex(install.InstallError, "pip check"):
                install._install(self.root, wheel, release)
        self.assertEqual(install._target(self.root), self.a)
        other = self.home / "another"
        other.mkdir(mode=0o700)
        with patch.object(install, "_run", return_value=""), patch.object(install, "_probe", return_value="9.0.0"):
            with self.assertRaisesRegex(install.InstallError, "wrong package version"):
                install._install(other, wheel, release)
        self.assertFalse((other / "current").exists())

    def test_recovery_compares_receipt_and_publication(self):
        record = install._activate(self.root, self.b, "1.1.0")
        with patch.object(install, "_probe", return_value="1.0.0"):
            self.assertTrue(install._recover(self.root, record))
        self.assertEqual(install._target(self.root), self.a)
        self.assertTrue((self.root / self.b).exists())
        record = install._activate(self.root, self.b, "1.1.0")
        install._publish(self.root, self.c)
        with self.assertRaisesRegex(install.InstallError, "later publication"):
            install._recover(self.root, record)
        self.assertEqual(install._target(self.root), self.c)
        install._publish(self.root, self.b)
        install._write(self.root / "operation.json", {**record, "token": "f" * 48})
        with self.assertRaisesRegex(install.InstallError, "later publication"):
            install._recover(self.root, record)

    def test_recovery_preserves_pointer_edited_during_previous_version_probe(self):
        record = install._activate(self.root, self.b, "1.1.0")
        with patch.object(install, "_probe", side_effect=lambda path: install._publish(self.root, self.c)):
            with self.assertRaisesRegex(install.InstallError, "changed during recovery"):
                install._recover(self.root, record)
        self.assertEqual(install._target(self.root), self.c)

    def test_pointer_changed_during_install_is_not_adopted_as_previous(self):
        install._publish(self.root, self.c)
        with self.assertRaisesRegex(install.InstallError, "changed during preparation"):
            install._activate(self.root, self.b, "1.1.0", expected_previous=self.a)
        self.assertEqual(install._target(self.root), self.c)

    def test_interrupted_publication_does_not_launch_duplicate(self):
        record = install._activate(self.root, self.b, "1.1.0")
        with self.assertRaisesRegex(install.InstallError, "unknown successor"):
            install._reconcile(self.root)
        self.assertEqual(install.check(self.identity).status, "unknown")
        install._publish(self.root, self.a)
        record["status"] = "prepared"
        install._write(self.root / "operation.json", record)
        install._reconcile(self.root)
        self.assertEqual(install._receipt(self.root)["status"], "aborted")

    def test_public_handoff_reconciles_prepared_a_before_provider_admission(self):
        record = install._activate(self.root, self.b, "1.1.0")
        install._publish(self.root, self.a)
        record["status"] = "prepared"
        install._write(self.root / "operation.json", record)
        self.assertEqual(install.check(self.identity).status, "unknown")
        available = install.Result("available", "fixture", "1.1.0", {})
        with (patch.object(install, "identify", return_value=self.identity),
              patch.object(install, "check", return_value=available),
              patch.object(install.shutil, "which", return_value=None)):
            self.assertEqual(install.handoff(self.identity, None).status, "manual")
        self.assertEqual(install._receipt(self.root)["status"], "aborted")

    def test_recover_cli_never_relaunches_ambiguous_published_successor(self):
        record = install._activate(self.root, self.b, "1.1.0")
        with (patch.object(install, "identify", return_value=self.identity),
              patch.object(subprocess, "Popen") as launch, patch("builtins.print")):
            self.assertEqual(install.main(["recover"]), 1)
            self.assertEqual(install.handoff(self.identity, None).status, "unknown")
            install._publish(self.root, self.a)
            record["status"] = "prepared"
            install._write(self.root / "operation.json", record)
            self.assertEqual(install.main(["recover"]), 0)
        launch.assert_not_called()
        self.assertEqual(install._receipt(self.root)["status"], "aborted")

    def test_reconciled_unpublished_b_can_be_revalidated_without_in_place_install(self):
        wheel, release = self.wheel()
        target = f"versions/1.1.0-{release['sha256']}"
        (self.root / target).mkdir(mode=0o700)
        self.distribution(target)
        record = install._activate(self.root, target, "1.1.0")
        install._publish(self.root, self.a)
        install._write(self.root / "operation.json", {**record, "status": "prepared"})
        with patch.object(install, "identify", return_value=self.identity):
            self.assertEqual(install.recover(self.identity).status, "recovered")
        with patch.object(install, "_run", return_value="") as commands:
            # Keep the real interpreter/resource/provenance probe while replacing pip.
            commands.side_effect = lambda command, **kw: (
                "" if "pip" in command else subprocess.check_output(command, text=True).strip()
            )
            self.assertEqual(install._install(self.root, wheel, release), target)
        pip_commands = [call.args[0] for call in commands.call_args_list if "pip" in call.args[0]]
        self.assertEqual(len(pip_commands), 1)
        self.assertEqual(pip_commands[0][-1], "check")
        self.assertEqual(install._target(self.root), self.a)

    def test_confirmed_nonzero_startup_exit_restores_a(self):
        record = install._activate(self.root, self.b, "1.1.0")
        child = Mock(poll=Mock(return_value=2))
        with (patch.object(subprocess, "Popen", return_value=child),
              patch.object(install, "_probe", return_value="1.0.0")):
            result = install._supervise(self.root, record, "/fixture/profile.json", "qt", Mock())
            self.assertEqual(result.status, "failed")
        self.assertEqual(install._target(self.root), self.a)
        child.kill.assert_not_called()

    def test_unknown_report_keeps_supervising_until_late_confirmed_failure(self):
        record = install._activate(self.root, self.b, "1.1.0")
        child = Mock(poll=Mock(side_effect=[None, 2]))
        notify = Mock()
        with (patch.object(subprocess, "Popen", return_value=child),
              patch.object(install.time, "monotonic", side_effect=[0, 31]),
              patch.object(install, "_probe", return_value="1.0.0")):
            result = install._supervise(self.root, record, None, "qt", notify)
        self.assertEqual(notify.call_args.args[0].status, "unknown")
        self.assertEqual(result.status, "failed")
        child.kill.assert_not_called()
        child.terminate.assert_not_called()
        self.assertEqual(install._target(self.root), self.a)

    def test_spawn_failure_restores_a_and_retains_b(self):
        record = install._activate(self.root, self.b, "1.1.0")
        with (patch.object(subprocess, "Popen", side_effect=OSError("fixture exec failure")),
              patch.object(install, "_probe", return_value="1.0.0")):
            result = install._supervise(self.root, record, None, "qt", Mock())
        self.assertEqual(result.status, "failed")
        self.assertEqual(install._target(self.root), self.a)
        self.assertTrue((self.root / self.b).exists())

    def test_late_readiness_or_publication_conflict_prevents_automatic_recovery(self):
        for transition in ("ready", "conflict"):
            with self.subTest(transition=transition):
                record = install._activate(self.root, self.b, "1.1.0")
                polls = 0

                def poll():
                    nonlocal polls
                    polls += 1
                    if polls == 1:
                        return None
                    if transition == "ready":
                        install._write(self.root / "operation.json", {**record, "status": "ready"})
                    else:
                        install._publish(self.root, self.c)
                    return 2

                # Change state between iterations, before the observer reads it.
                def pause(seconds):
                    poll()

                child = Mock(poll=Mock(return_value=None))
                with (patch.object(subprocess, "Popen", return_value=child),
                      patch.object(install.time, "monotonic", side_effect=[0, 31]),
                      patch.object(install.time, "sleep", side_effect=pause),
                      patch.object(install, "_recover") as rollback):
                    polls = 1
                    result = install._supervise(self.root, record, None, "qt", Mock())
                self.assertEqual(result.status, "updated" if transition == "ready" else "unknown")
                rollback.assert_not_called()

    def test_detached_supervisor_recovers_late_failure_after_helper_termination(self):
        self.distribution(self.a)
        binary = self.root / self.b / "bin/logos-desktop"
        binary.parent.mkdir()
        binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(0.8)\nraise SystemExit(2)\n")
        binary.chmod(0o700)
        record = install._activate(self.root, self.b, "1.1.0")
        source = (
            "from pathlib import Path\n"
            "from logos_desktop import controller_install as c\n"
            f"root = Path({str(self.root)!r})\n"
            "result = c._launch(root, c._receipt(root), None, 'qt', observation_seconds=0.05)\n"
            "print(result.status, flush=True)\n"
        )
        helper = subprocess.Popen([sys.executable, "-c", source], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.addCleanup(helper.stdout.close)
        self.addCleanup(helper.stderr.close)
        self.assertEqual(helper.stdout.readline().strip(), "unknown")
        helper.terminate()
        helper.wait(timeout=5)
        result_path = self.root / f"supervision-{record['token']}.json"
        self.wait_for(result_path.exists)
        self.assertEqual(install._read(result_path)["status"], "failed")
        self.assertEqual(install._target(self.root), self.a)

    def test_tui_exclusively_owns_helper_pty_until_successor_exit(self):
        binary = self.root / self.b / "bin/logos-desktop"
        binary.parent.mkdir()
        sentinel = self.home / "tui-input.json"
        finished = self.home / "tui-finished"
        binary.write_text(
            f"#!{sys.executable}\nimport json, os\nfrom pathlib import Path\n"
            "assert all(os.isatty(fd) for fd in (0, 1, 2))\n"
            "print('SUCCESSOR_READY', flush=True)\n"
            "answer = input()\n"
            f"Path({str(sentinel)!r}).write_text(json.dumps(answer))\n"
            f"receipt = Path({str(self.root / 'operation.json')!r})\n"
            "record = json.loads(receipt.read_text())\nrecord['status'] = 'ready'\n"
            "receipt.write_text(json.dumps(record))\n"
            "print('SUCCESSOR_OWNS_INPUT', flush=True)\n"
            "assert input() == 'EXIT'\n"
            f"Path({str(finished)!r}).touch()\n"
        )
        binary.chmod(0o700)
        install._activate(self.root, self.b, "1.1.0")
        source = (
            "import fcntl, os, termios\nfrom pathlib import Path\n"
            "os.setsid()\nfcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
            "from logos_desktop import controller_install as c\n"
            f"root = Path({str(self.root)!r})\n"
            "c.identify = lambda: c.Identity('venv', root=root)\n"
            "c.recover = lambda identity: c.Result('recovered', 'fixture')\n"
            "c.check = lambda identity: c.Result('available', 'fixture', '1.1.0', {})\n"
            "def update(identity, release, config, frontend, *, before_tui_launch=None):\n"
            "    before_tui_launch()\n"
            "    return c._launch(root, c._receipt(root), config, frontend, observation_seconds=0.05)\n"
            "c._wheel_update = update\n"
            "raise SystemExit(c.main(['handoff', '--frontend', 'tui']))\n"
        )
        master, slave = pty.openpty()
        helper = subprocess.Popen([sys.executable, "-c", source], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.addCleanup(os.close, master)
        output = bytearray()

        def read_until(text):
            deadline = time.monotonic() + 5
            while text not in output and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    output.extend(os.read(master, 65536))
            self.assertIn(text, output)

        try:
            read_until(b"Type UPDATE")
            os.write(master, b"UPDATE\n")
            read_until(b"SUCCESSOR_READY")
            time.sleep(0.15)
            os.write(master, b"only-the-successor\n")
            read_until(b"SUCCESSOR_OWNS_INPUT")
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                output.extend(os.read(master, 65536))
            self.assertEqual(json.loads(sentinel.read_text()), "only-the-successor")
            self.assertNotIn(b"Press Enter", output)
            self.assertNotIn(b"updated:", output)
            self.assertNotIn(b"unknown:", output)
            self.assertIsNone(helper.poll())
            os.write(master, b"EXIT\n")
            self.assertEqual(helper.wait(timeout=5), 0)
            self.assertTrue(finished.exists())
        finally:
            if helper.poll() is None:
                helper.terminate()
                helper.wait(timeout=5)

    def test_tui_digest_failure_keeps_error_visible_until_acknowledged(self):
        _, release = self.wheel()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"x" * release["size"]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        release["url"] = f"http://127.0.0.1:{server.server_port}/wheel"
        self.provider({"manifest_url": release["url"], "allow_loopback": True})
        profile = self.home / "selected profile.json"
        profile.write_bytes(b'{"keep": true}\n')
        prior = install._activate(self.root, self.b, "1.1.0")
        with patch.object(install, "_probe", return_value="1.0.0"):
            install._recover(self.root, prior)
        receipt = (self.root / "operation.json").read_bytes()
        program = textwrap.dedent(f"""
            import fcntl, os, termios
            from pathlib import Path
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            from logos_desktop import controller_install as c
            Path.home = classmethod(lambda cls: Path({str(self.home)!r}))
            root = Path({str(self.root)!r})
            c.identify = lambda: c.Identity('venv', '1.0.0', '1.0.0', root,
                                            root / 'logos-desktop', root / 'provider.json')
            c.recover = lambda identity: c.Result('recovered', 'fixture')
            c.check = lambda identity: c.Result('available', 'fixture', '1.1.0', {release!r})
            def forbidden(*args, **kwargs):
                raise AssertionError('Rejected bytes reached installation or launch')
            c._install = c._activate = c._launch = forbidden
            raise SystemExit(c.main(['handoff', '--frontend', 'tui']))
        """)
        master, slave = pty.openpty()
        helper = subprocess.Popen([sys.executable, "-c", program], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.addCleanup(os.close, master)
        output = bytearray()

        def read_until(text):
            deadline = time.monotonic() + 5
            while text not in output and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    output.extend(os.read(master, 65536))
            self.assertIn(text, output)

        try:
            read_until(b"Type UPDATE")
            os.write(master, b"UPDATE\n")
            read_until(b"Press Enter to close this helper")
            self.assertIn(b"Downloaded wheel failed size or SHA-256 validation", output)
            self.assertIsNone(helper.poll())
            self.assertEqual(install._target(self.root), self.a)
            self.assertEqual((self.root / "operation.json").read_bytes(), receipt)
            self.assertEqual(profile.read_bytes(), b'{"keep": true}\n')
            os.write(master, b"\n")
            self.assertEqual(helper.wait(timeout=5), 1)
        finally:
            if helper.poll() is None:
                helper.terminate()
                helper.wait(timeout=5)

    def test_tui_launch_callback_follows_preparation_cleanup_and_lock_release(self):
        wheel, release = self.wheel()
        self.provider({"manifest_url": "https://example.invalid/release"})

        def download(url, config, *, limit, destination):
            destination.write(wheel.read_bytes())
            return release["size"], release["sha256"]

        for phase in ("download", "install", "activate", "cleanup"):
            callback = Mock()
            with self.subTest(phase=phase), ExitStack() as stack:
                stack.enter_context(patch.object(install, "identify", return_value=self.identity))
                stack.enter_context(patch.object(install, "_tui_terminal"))
                stack.enter_context(patch.object(install, "_download", side_effect=download))
                stack.enter_context(patch.object(install, "_install", return_value=self.b))
                stack.enter_context(patch.object(install, "_activate", return_value={}))
                launch = stack.enter_context(patch.object(install, "_launch"))
                if phase == "cleanup":
                    unlink = Path.unlink

                    def fail_cleanup(path, *args, **kwargs):
                        if path.parent.name.startswith(".download-"):
                            raise OSError("cleanup failure")
                        return unlink(path, *args, **kwargs)

                    stack.enter_context(patch.object(Path, "unlink", fail_cleanup))
                else:
                    method = {"download": "_download", "install": "_install", "activate": "_activate"}[phase]
                    stack.enter_context(patch.object(install, method, side_effect=OSError(phase)))
                with self.assertRaises(OSError):
                    install._wheel_update(self.identity, release, None, "tui", before_tui_launch=callback)
                callback.assert_not_called()
                launch.assert_not_called()
            for directory in self.root.glob(".download-*"):
                for path in directory.iterdir():
                    path.unlink()
                directory.rmdir()

        for frontend in ("qt", "tui"):
            order = []

            def transfer():
                with install._locked(self.root):
                    self.assertFalse(list(self.root.glob(".download-*")))
                order.append("transfer")

            def launch(*args):
                order.append("launch")
                return install.Result("updated", "fixture")

            with (patch.object(install, "identify", return_value=self.identity),
                  patch.object(install, "_tui_terminal"), patch.object(install, "_download", side_effect=download),
                  patch.object(install, "_install", return_value=self.b),
                  patch.object(install, "_activate", return_value={}),
                  patch.object(install, "_launch", side_effect=launch)):
                install._wheel_update(self.identity, release, None, frontend, before_tui_launch=transfer)
            self.assertEqual(order, ["transfer", "launch"] if frontend == "tui" else ["launch"])

    def test_tui_main_reclaims_input_only_after_confirmed_failed_launch(self):
        for outcome in ("failed", "updated", "unknown", "exception"):
            def update(*args, before_tui_launch):
                before_tui_launch()
                if outcome == "exception":
                    raise install.InstallError("launch-stage observation failure")
                return install.Result(outcome, "fixture")

            with (self.subTest(outcome=outcome),
                  patch.object(install, "identify", return_value=self.identity),
                  patch.object(install, "recover", return_value=install.Result("recovered", "fixture")),
                  patch.object(install, "check", return_value=install.Result("available", "fixture", "1.1.0", {})),
                  patch.object(install, "_wheel_update", side_effect=update),
                  patch.object(sys.stdin, "isatty", return_value=True),
                  patch("builtins.input", side_effect=["UPDATE", ""]) as answers,
                  patch("builtins.print")):
                result = install.main(["handoff", "--frontend", "tui"])
            self.assertEqual(result, 0 if outcome == "updated" else 1)
            self.assertEqual(answers.call_count, 2 if outcome == "failed" else 1)

    def test_tui_unknown_supervisor_does_not_read_live_successor_input(self):
        binary = self.root / self.b / "bin/logos-desktop"
        binary.parent.mkdir()
        received = self.home / "successor-answer"
        binary.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            "print('LIVE_SUCCESSOR', flush=True)\n"
            f"Path({str(received)!r}).write_text(input())\n"
        )
        binary.chmod(0o700)
        record = install._activate(self.root, self.b, "1.1.0")
        program = textwrap.dedent(f"""
            import fcntl, os, termios, time
            from pathlib import Path
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            from logos_desktop import controller_install as c
            root = Path({str(self.root)!r})
            c.identify = lambda: c.Identity('venv', root=root)
            c.recover = lambda identity: c.Result('recovered', 'fixture')
            c.check = lambda identity: c.Result('available', 'fixture', '1.1.0', {{}})
            def broken_observer(*args):
                raise RuntimeError('supervisor fixture failure')
            def update(*args, before_tui_launch):
                before_tui_launch()
                c._receipt = broken_observer
                return c._launch(root, {record!r}, None, 'tui')
            c._wheel_update = update
            code = c.main(['handoff', '--frontend', 'tui'])
            print('HELPER_RETURNED', flush=True)
            deadline = time.monotonic() + 5
            while not Path({str(received)!r}).exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            raise SystemExit(code)
        """)
        master, slave = pty.openpty()
        helper = subprocess.Popen([sys.executable, "-c", program], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.addCleanup(os.close, master)
        output = bytearray()

        def read_until(text):
            deadline = time.monotonic() + 5
            while text not in output and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    output.extend(os.read(master, 65536))
            self.assertIn(text, output)

        try:
            read_until(b"Type UPDATE")
            os.write(master, b"UPDATE\n")
            read_until(b"LIVE_SUCCESSOR")
            read_until(b"HELPER_RETURNED")
            self.assertIn(b"unknown:", output)
            self.assertNotIn(b"Press Enter", output)
            os.write(master, b"successor-only\n")
            self.assertEqual(helper.wait(timeout=5), 1)
            self.assertEqual(received.read_text(), "successor-only")
            self.assertEqual(install._target(self.root), self.b)
            self.assertEqual(install._receipt(self.root)["status"], "published")
        finally:
            if helper.poll() is None:
                helper.terminate()
                helper.wait(timeout=5)

    def test_nonterminal_tui_is_refused_before_install_or_activation(self):
        with (patch.object(os, "isatty", return_value=False), patch.object(install, "_install") as pip):
            with self.assertRaisesRegex(install.InstallError, "exclusive terminal"):
                install._wheel_update(self.identity, {}, None, "tui")
        pip.assert_not_called()
        self.assertEqual(install._target(self.root), self.a)

    def test_exit_failure_with_later_pointer_edit_reports_unknown(self):
        record = install._activate(self.root, self.b, "1.1.0")

        def exit_after_edit():
            install._publish(self.root, self.c)
            return 2

        child = Mock(poll=Mock(side_effect=exit_after_edit))
        with patch.object(subprocess, "Popen", return_value=child):
            result = install._supervise(self.root, record, None, "qt", Mock())
        self.assertEqual(result.status, "unknown")
        self.assertEqual(install._target(self.root), self.c)

    def test_startup_confirmation_is_bound_to_runtime_token_and_one_use(self):
        record = install._activate(self.root, self.b, "1.1.0")
        identity = install.Identity("venv", "1.1.0", "1.1.0", self.root)
        claim = json.dumps({"root": str(self.root), "token": record["token"]})
        with (patch.dict(os.environ, {install.TOKEN: claim}),
              patch.object(install, "identify", return_value=identity),
              patch.object(sys, "prefix", str(self.root / self.b))):
            self.assertTrue(install.confirm_startup())
            self.assertFalse(install.confirm_startup())
        self.assertEqual(install._receipt(self.root)["status"], "ready")
        with patch.object(install, "_probe"):
            self.assertFalse(install._recover(self.root, install._receipt(self.root)))
            self.assertTrue(install._recover(self.root, install._receipt(self.root), explicit=True))
        self.assertEqual(install._target(self.root), self.a)

    def test_wrong_runtime_cannot_confirm_or_clear_receipt(self):
        record = install._activate(self.root, self.b, "1.1.0")
        claim = json.dumps({"root": str(self.root), "token": record["token"]})
        with (patch.dict(os.environ, {install.TOKEN: claim}),
              patch.object(install, "identify", return_value=self.identity)):
            with self.assertRaises(install.InstallError):
                install.confirm_startup()
        self.assertEqual(install._receipt(self.root), record)

    def test_real_separate_successor_consumes_token_and_confirms_exact_b(self):
        prefix, package, _ = self.distribution(self.b)
        (package / "controller_install.py").write_text(Path(install.__file__).read_text())
        entry = prefix / "bin/logos-desktop"
        entry.write_text(f"#!{prefix}/bin/python\n"
                         "from logos_desktop.controller_install import confirm_startup\n"
                         "confirm_startup()\n")
        record = install._activate(self.root, self.b, "1.1.0")
        with patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.home / "no-package-tools")}):
            result = install._launch(self.root, record, self.home / "profile.json", "qt")
        self.assertEqual(result.status, "updated")
        self.assertEqual(install._receipt(self.root)["status"], "ready")
        self.assertEqual(install._target(self.root), self.b)
        self.assertTrue((self.root / self.a).is_dir())

    def test_concurrent_helper_is_refused(self):
        with install._locked(self.root):
            with self.assertRaisesRegex(install.InstallError, "Another controller helper"):
                with install._locked(self.root):
                    self.fail("Second helper acquired the same installation")

    def test_handoff_has_independent_lifetime_and_manual_command_fallback(self):
        available = install.Result("available", "fixture", "1.1.0", {})
        with patch.object(install, "identify", return_value=self.identity), patch.object(
            install, "check", return_value=available
        ), patch.object(install.shutil, "which", return_value=None):
            result = install.handoff(self.identity, self.home / "profile space.json", "qt")
            self.assertEqual(result.status, "manual")
            self.assertIn(str(self.home / "profile space.json"), result.command)
        with (patch.object(install, "identify", return_value=self.identity),
              patch.object(install, "check", return_value=available),
              patch.object(install.shutil, "which", return_value="/fixture/konsole"),
              patch.object(subprocess, "Popen") as launch):
            self.assertEqual(install.handoff(self.identity, None).status, "handed-off")
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_terminal_wrapper_hides_kde_options_and_executes_equivalent_helper_arguments(self):
        prefix, package, _ = self.distribution(self.a)
        (package / "controller_install.py").write_text(textwrap.dedent("""\
            import json
            import os
            from pathlib import Path
            import sys

            def main(argv=None):
                arguments = sys.argv[1:] if argv is None else argv
                Path(os.environ['LOGOS_DESKTOP_TEST_CAPTURE']).write_text(json.dumps(arguments))
                return 0

            if __name__ == '__main__':
                raise SystemExit(main())
            """))
        profile = self.home / ("selected --config --profile 'quotes' \"double\" 日本語\n"
                               "raise RuntimeError('injected').json")
        profile.write_text("{}\n")
        capture = self.home / "captured-helper-arguments.json"

        def kde_global_options(argv):
            # Model KDE's global options being consumed even after the command flag.
            options = {}
            index = 1
            while index < len(argv):
                name, equals, value = argv[index].partition("=")
                if name in ("--config", "--profile", "--workdir", "--tabs-from-file"):
                    if not equals:
                        index += 1
                        value = argv[index]
                    options[name] = value
                index += 1
            return options

        for terminal, flag in (("konsole", "-e"), ("xterm", "-e"), ("gnome-terminal", "--")):
            for frontend in ("qt", "tui"):
                with self.subTest(terminal=terminal, frontend=frontend):
                    executable = f"/fixture/{terminal}"
                    with (patch.object(sys, "executable", str(prefix / "bin/python")),
                          patch.object(install, "identify", return_value=self.identity),
                          patch.object(install, "recover", return_value=install.Result("recovered", "fixture")),
                          patch.object(install, "check", return_value=install.Result("available", "fixture")),
                          patch.object(install.shutil, "which", side_effect=lambda name: (
                              executable if name == terminal else None
                          )), patch.dict(os.environ, {
                              "LOGOS_DESKTOP_TEST_CAPTURE": str(capture), "DISPLAY": ":fixture",
                          }),
                          patch.object(subprocess, "Popen") as launch):
                        result = install.handoff(self.identity, profile, frontend)

                    self.assertEqual(result.status, "handed-off")
                    terminal_argv = launch.call_args.args[0]
                    self.assertEqual(terminal_argv[:2], [executable, flag])
                    self.assertEqual(kde_global_options(terminal_argv), {})
                    self.assertNotIn("--frontend", terminal_argv)
                    self.assertNotIn(str(profile), terminal_argv)
                    self.assertTrue(launch.call_args.kwargs["start_new_session"])
                    expected = ["handoff", "--frontend", frontend, "--config", str(profile)]
                    self.assertEqual(result.command, (str(prefix / "bin/python"), "-I", "-m",
                                                      "logos_desktop.controller_install", *expected))
                    env = launch.call_args.kwargs["env"]
                    wrapped = subprocess.run(terminal_argv[2:], env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(wrapped.returncode, 0, wrapped.stderr)
                    self.assertEqual(json.loads(capture.read_text()), expected)
                    capture.unlink()
                    direct = subprocess.run(result.command, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(direct.returncode, 0, direct.stderr)
                    self.assertEqual(json.loads(capture.read_text()), expected)
                    self.assertEqual(profile.read_text(), "{}\n")

    def test_wayland_without_x11_does_not_handoff_to_unusable_xterm(self):
        with (patch.object(install, "identify", return_value=self.identity),
              patch.object(install, "check", return_value=install.Result("available", "fixture")),
              patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": "wayland-fixture"}),
              patch.object(install.shutil, "which", side_effect=lambda name: (
                  "/usr/bin/xterm" if name == "xterm" else None
              )),
              patch.object(subprocess, "Popen") as launch):
            result = install.handoff(self.identity, self.home / "selected profile.json")
        self.assertEqual(result.status, "manual")
        self.assertIn(str(self.home / "selected profile.json"), result.command)
        launch.assert_not_called()

    def test_cancellation_during_each_prep_phase_propagates_without_terminal_launch(self):
        for phase in ("identify", "recover", "check", "terminal", "environment"):
            with self.subTest(phase=phase):
                cancelled = False

                def finish(name, result):
                    nonlocal cancelled
                    if phase == name:
                        cancelled = True
                    return result

                def checkpoint():
                    if cancelled:
                        # OSError must not become a manual-terminal fallback result.
                        raise OSError("fixture cancellation")

                with (patch.object(install, "identify", side_effect=lambda: finish("identify", self.identity)),
                      patch.object(install, "recover", side_effect=lambda identity: finish(
                          "recover", install.Result("recovered", "fixture")
                      )), patch.object(install, "check", side_effect=lambda identity: finish(
                          "check", install.Result("available", "fixture", "1.1.0", {})
                      )), patch.object(install.shutil, "which", side_effect=lambda name: finish(
                          "terminal", "/fixture/konsole"
                      )), patch.object(install, "_environment", side_effect=lambda: finish("environment", {})),
                      patch.object(subprocess, "Popen") as launch):
                    with self.assertRaisesRegex(OSError, "fixture cancellation"):
                        install.handoff(self.identity, self.home / "profile space.json", checkpoint=checkpoint)
                launch.assert_not_called()

    def test_final_checkpoint_precedes_popen_and_no_checkpoint_follows_acceptance(self):
        for frontend in ("qt", "tui"):
            with self.subTest(frontend=frontend):
                accepted = False
                sequence = []

                def checkpoint():
                    self.assertFalse(accepted, "Accepted handoffs no longer belong to the cancellable caller")
                    sequence.append("checkpoint")

                def accept(*args, **kwargs):
                    nonlocal accepted
                    self.assertEqual(sequence[-1], "checkpoint")
                    accepted = True
                    sequence.append("popen")
                    return Mock()

                with (patch.object(install, "identify", return_value=self.identity),
                      patch.object(install, "recover", return_value=install.Result("recovered", "fixture")),
                      patch.object(install, "check", return_value=install.Result("available", "fixture")),
                      patch.object(install.shutil, "which", return_value="/fixture/konsole"),
                      patch.object(subprocess, "Popen", side_effect=accept)):
                    result = install.handoff(self.identity, None, frontend, checkpoint=checkpoint)
                self.assertEqual(result.status, "handed-off")
                self.assertEqual(sequence[-1], "popen")

    def test_bootstrap_script_checks_hash_before_executing_packaged_implementation(self):
        marker = self.home / "must-not-execute"
        wheel, data = self.wheel(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        script = Path(__file__).resolve().parents[1] / "scripts/install-wheel.py"
        command = [sys.executable, str(script), "--root", str(self.root), "--wheel", str(wheel),
                   "--version", "1.1.0", "--sha256", "0" * 64, "--size", str(data["size"])]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256", result.stderr)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
