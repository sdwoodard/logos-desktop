from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import pty
import select
import shlex
import socket
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

from ou_dedetai import constants, controller_install as install
import ou_dedetai


class ControllerInstallTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.home))
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

    def distribution(self, relative=None):
        prefix = self.root / (relative or self.a)
        version, digest = prefix.name.split("-", 1)
        venv.EnvBuilder(with_pip=False, symlinks=True).create(prefix)
        site = next((prefix / "lib").glob("python*/site-packages"))
        package = site / "ou_dedetai"
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
        (metadata / "entry_points.txt").write_text("[console_scripts]\nlogos-desktop = ou_dedetai.main:main\n")
        (metadata / "direct_url.json").write_text(json.dumps({"archive_info": {"hashes": {"sha256": digest}}}))
        (metadata / "RECORD").write_text(
            f"ou_dedetai/__init__.py,,\n{metadata.name}/METADATA,,\n"
        )
        entry = prefix / "bin/logos-desktop"
        entry.write_text(f"#!{prefix}/bin/python\n")
        entry.chmod(0o700)
        return prefix, package, importlib.metadata.Distribution.at(metadata)

    def runtime(self, prefix, package, distribution):
        self.stack.enter_context(patch.object(sys, "prefix", str(prefix)))
        self.stack.enter_context(patch.object(sys, "executable", str(prefix / "bin/python")))
        self.stack.enter_context(patch.object(ou_dedetai, "__file__", str(package / "__init__.py")))
        self.stack.enter_context(patch.object(constants, "APP_VERSION", distribution.version))
        self.stack.enter_context(patch.object(importlib.metadata, "distribution", return_value=distribution))
        self.stack.enter_context(patch.object(install, "_owner", return_value=[]))
        self.stack.enter_context(patch.object(install.shutil, "which", return_value=None))

    def provider(self, value):
        install._write(self.identity.provider_path, {"schema": 1, "installation": str(self.root), **value})

    def wheel(self, content=None):
        path = self.home / "logos_desktop-1.1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ou_dedetai/controller_install.py", content or "# fixture\n")
        data = {"filename": path.name, "version": "1.1.0", "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "url": "https://example.invalid/wheel"}
        return path, data

    def recipe_tree(self, build):
        entries = []
        for path in sorted(build.iterdir()):
            content = path.read_bytes()
            digest = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            entries.append(f"100644 blob {digest}\t{path.name}\0")
        return "".join(entries)

    def aur_metadata_provider(self):
        bodies = {}
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                status, body = bodies.get(self.path, (404, b"missing fixture"))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        identity = install.Identity("aur", "1.0.0", "1.0.0-1", launcher=Path("/usr/bin/logos-desktop"),
                                    provider_path=self.home / "aur/provider.json")
        identity.provider_path.parent.mkdir(mode=0o700)
        config = {"schema": 1, "installation": "/usr/bin/logos-desktop", "allow_loopback": True,
                  "rpc_url": base + "/rpc", "git_url": base + "/recipe.git",
                  "release_manifest_url": base + "/source"}
        install._write(identity.provider_path, config)
        rpc = {"type": "multiinfo", "resultcount": 1,
               "results": [{"Name": "logos-desktop", "PackageBase": "logos-desktop", "Version": "1.0.0-1"}]}
        source = {"version": "1.1.0", "filename": "logos_desktop-1.1.0-py3-none-any.whl",
                  "url": base + "/source.whl", "size": 12345, "sha256": "b" * 64}
        bodies["/rpc"] = (200, json.dumps(rpc).encode())
        bodies["/source"] = (200, json.dumps(source).encode())
        return identity, config, bodies, requests, rpc, source

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
        self.assertIn("mixed package owner", identity.message)
        with patch.object(install, "_owner", side_effect=[[], [], [("rpm", "foreign")]]):
            self.assertEqual(install.identify().owner, "unsupported")

    def test_loaded_source_shadowing_is_rejected(self):
        prefix, package, distribution = self.distribution()
        self.runtime(prefix, package, distribution)
        with patch.object(ou_dedetai, "__file__", str(self.home / "source/__init__.py")):
            self.assertIn("Loaded source", install.identify().message)

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

    def test_system_probe_accepts_real_binusr_aliases_only_with_consistent_package_owner(self):
        system = self.home / "system"
        prefix = system / "usr"
        (prefix / "bin").mkdir(parents=True)
        expected = prefix / "bin/python"
        expected.symlink_to(Path(sys.executable).resolve())
        (prefix / "sbin").symlink_to("bin", target_is_directory=True)
        (system / "bin").symlink_to("usr/bin", target_is_directory=True)
        (system / "sbin").symlink_to("usr/bin", target_is_directory=True)
        entry = prefix / "bin/logos-desktop"
        data = json.dumps({"prefix": str(prefix), "version": "1.0.0"})

        for candidate in (prefix / "sbin/python", system / "bin/python", system / "sbin/python"):
            with self.subTest(candidate=candidate):
                self.assertTrue(candidate.samefile(expected))
                entry.write_text(f"#!{candidate}\n")
                with (patch.object(install, "_run", return_value=data),
                      patch.object(install, "_owner", return_value=[("pacman", "python")]) as owners):
                    with self.assertRaisesRegex(install.InstallError, "entry point"):
                        install._probe(prefix)
                    owners.assert_not_called()
                    self.assertEqual(install._probe(prefix, system_aliases=True), "1.0.0")
                queried = {call.args[0] for call in owners.call_args_list}
                self.assertEqual(queried, {expected, candidate, expected.resolve()})

        entry.write_text(f"#!{prefix / 'sbin/python'}\n")
        with (patch.object(install, "_run", return_value=data),
              patch.object(install, "_owner", side_effect=[[("pacman", "python")],
                                                         [("pacman", "python")], []])):
            self.assertEqual(install._probe(prefix, system_aliases=True), "1.0.0")
        for owners in ([], [("rpm", "python")], [("pacman", "python"), ("dpkg-query", "python")]):
            with self.subTest(owners=owners), patch.object(install, "_run", return_value=data), patch.object(
                install, "_owner", return_value=owners
            ):
                with self.assertRaisesRegex(install.InstallError, "entry point"):
                    install._probe(prefix, system_aliases=True)
        with (patch.object(install, "_run", return_value=data),
              patch.object(install, "_owner", side_effect=[[("pacman", "python")],
                                                         [("pacman", "python")], [("pacman", "foreign")]])):
            with self.assertRaisesRegex(install.InstallError, "entry point"):
                install._probe(prefix, system_aliases=True)

    def test_same_python_inode_in_another_venv_does_not_relax_entrypoint_binding(self):
        prefix, _, _ = self.distribution(self.a)
        other, _, _ = self.distribution(self.b)
        expected = prefix / "bin/python"
        foreign = other / "bin/python"
        self.assertTrue(foreign.samefile(expected))
        (prefix / "bin/logos-desktop").write_text(f"#!{foreign}\n")
        with patch.object(install, "_owner", return_value=[("pacman", "python")]) as owners:
            for aliases in (False, True):
                with self.subTest(aliases=aliases), self.assertRaisesRegex(install.InstallError, "entry point"):
                    install._probe(prefix, system_aliases=aliases)
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
                install._probe(prefix, system_aliases=True)
        owners.assert_not_called()

    def test_identify_enables_system_aliases_only_after_consistent_aur_ownership(self):
        prefix, package, distribution = self.distribution()
        self.runtime(prefix, package, distribution)
        with (patch.object(sys, "prefix", "/usr"),
              patch.object(install, "_owner", return_value=[("pacman", "logos-desktop")]) as owners,
              patch.object(install, "_probe", return_value="1.0.0") as probe,
              patch.object(install, "_run", return_value="logos-desktop 1.0.0-1")):
            self.assertEqual(install.identify().owner, "aur")
            self.assertEqual([call.args[0] for call in owners.call_args_list],
                             [package / "__init__.py", Path("/usr/bin/logos-desktop"),
                              Path(distribution._path) / "METADATA"])
            probe.assert_called_once_with(Path("/usr"), system_aliases=True)
        with (patch.object(sys, "prefix", "/usr"),
              patch.object(install, "_owner", side_effect=[[("pacman", "logos-desktop")],
                                                         [("pacman", "logos-desktop")], [("pacman", "foreign")]]),
              patch.object(install, "_probe") as probe):
            self.assertEqual(install.identify().owner, "unsupported")
        probe.assert_not_called()

    def test_identify_accepts_binusr_python_and_path_launcher_but_rejects_foreign_aliases(self):
        prefix, package, distribution = self.distribution()
        self.runtime(prefix, package, distribution)
        system = self.home / "system"
        system_prefix = system / "usr"
        (system_prefix / "bin").mkdir(parents=True)
        (system_prefix / "sbin").symlink_to("bin", target_is_directory=True)
        python = system_prefix / "bin/python"
        python.symlink_to(Path(sys.executable).resolve())
        entry = system_prefix / "bin/logos-desktop"
        entry.write_text("#!/usr/sbin/python\n")
        foreign = system / "opt/foreign/bin/logos-desktop"
        foreign.parent.mkdir(parents=True)
        foreign.symlink_to(entry)
        self.assertTrue((system_prefix / "sbin/logos-desktop").samefile(entry))
        self.assertTrue(foreign.samefile(entry))
        real_probe = install._probe
        real_alias = install._system_alias

        def physical(path):
            path = Path(path)
            if path.is_relative_to("/usr") or path.is_relative_to("/opt"):
                return system / path.relative_to("/")
            return path

        def alias(prefix, candidate, expected, **kwargs):
            return real_alias(physical(prefix), physical(candidate), physical(expected), **kwargs)

        def run(command, **kwargs):
            if command[0] == str(python):
                return json.dumps({"prefix": str(system_prefix), "version": "1.0.0"})
            self.assertEqual(command, ["pacman", "-Q", "logos-desktop"])
            return "logos-desktop 1.0.0-1"

        cases = (
            ("/usr/sbin/logos-desktop", "logos-desktop", "logos-desktop", "aur"),
            ("/opt/foreign/bin/logos-desktop", "logos-desktop", "logos-desktop", "unsupported"),
            ("/usr/sbin/logos-desktop", "foreign", "logos-desktop", "unsupported"),
            ("/usr/sbin/logos-desktop", "logos-desktop", "foreign", "unsupported"),
        )
        for selected, alias_package, canonical_package, expected_owner in cases:
            with self.subTest(selected=selected, alias_package=alias_package, canonical_package=canonical_package):
                def owner(path):
                    path = Path(path)
                    if path in (python, python.resolve(), system_prefix / "sbin/python"):
                        return [("pacman", "python")]
                    if path == system_prefix / "sbin/logos-desktop":
                        return [("pacman", alias_package)]
                    if path == entry:
                        return [("pacman", canonical_package)]
                    return [("pacman", "logos-desktop")]

                # Map only the disposable root's namespace. Alias decisions still
                # use real resolve/samefile calls for both interpreter and launcher.
                with (patch.object(sys, "prefix", "/usr"),
                      patch.object(install, "_owner", side_effect=owner),
                      patch.object(install, "_run", side_effect=run),
                      patch.object(install, "_probe", side_effect=lambda path, **kw: real_probe(physical(path), **kw)),
                      patch.object(install, "_system_alias", side_effect=alias) as aliases,
                      patch.object(install.shutil, "which", return_value=selected) as which):
                    identity = install.identify()
                which.assert_called_once_with("logos-desktop")
                self.assertEqual(identity.owner, expected_owner, identity.message)
                candidates = [str(call.args[1]) for call in aliases.call_args_list]
                self.assertEqual(candidates, ["/usr/sbin/python", selected])
                self.assertEqual(aliases.call_args.kwargs, {"package": "logos-desktop"})
                if expected_owner == "aur":
                    self.assertEqual(identity.launcher, Path("/usr/bin/logos-desktop"))
                else:
                    self.assertIn("PATH selects a different controller", identity.message)

    def test_venv_path_alias_to_same_launcher_is_not_admitted_as_system_alias(self):
        prefix, package, distribution = self.distribution()
        self.runtime(prefix, package, distribution)
        alternate = self.home / "other-bin/logos-desktop"
        alternate.parent.mkdir()
        alternate.symlink_to(self.root / "logos-desktop")
        self.assertTrue(alternate.samefile(self.root / "logos-desktop"))
        with (patch.object(install.shutil, "which", return_value=str(alternate)),
              patch.object(install, "_system_alias") as system_alias):
            identity = install.identify()
        self.assertEqual(identity.owner, "unsupported")
        self.assertIn("PATH selects a different controller", identity.message)
        system_alias.assert_not_called()

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
            "from ou_dedetai import controller_install as c\n"
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
            "from ou_dedetai import controller_install as c\n"
            f"root = Path({str(self.root)!r})\n"
            "c.identify = lambda: c.Identity('venv', root=root)\n"
            "c.recover = lambda identity: c.Result('recovered', 'fixture')\n"
            "c.check = lambda identity: c.Result('available', 'fixture', '1.1.0', {})\n"
            "c._wheel_update = lambda identity, release, config, frontend: "
            "c._launch(root, c._receipt(root), config, frontend, observation_seconds=0.05)\n"
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
                         "from ou_dedetai.controller_install import confirm_startup\n"
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
                          )), patch.dict(os.environ, {"LOGOS_DESKTOP_TEST_CAPTURE": str(capture)}),
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
                                                      "ou_dedetai.controller_install", *expected))
                    env = launch.call_args.kwargs["env"]
                    wrapped = subprocess.run(terminal_argv[2:], env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(wrapped.returncode, 0, wrapped.stderr)
                    self.assertEqual(json.loads(capture.read_text()), expected)
                    capture.unlink()
                    direct = subprocess.run(result.command, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(direct.returncode, 0, direct.stderr)
                    self.assertEqual(json.loads(capture.read_text()), expected)
                    self.assertEqual(profile.read_text(), "{}\n")

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

    def test_aur_rpc_version_and_recipe_pin_are_provider_specific(self):
        identity = install.Identity("aur", "1.0.0", "1.0.0-2", launcher=Path("/usr/bin/logos-desktop"),
                                    provider_path=self.home / "aur/provider.json")
        self.assertEqual(install.check(identity).status, "unavailable")
        identity.provider_path.parent.mkdir(mode=0o700)
        install._write(identity.provider_path, {
            "schema": 1, "installation": "/usr/bin/logos-desktop",
            "rpc_url": "https://aur.archlinux.org/rpc/v5/info?arg[]=logos-desktop",
            "git_url": "https://aur.archlinux.org/logos-desktop.git",
        })
        rpc = {"type": "multiinfo", "resultcount": 1,
               "results": [{"Name": "logos-desktop", "PackageBase": "logos-desktop", "Version": "1.0.0-3"}]}
        with patch.object(install, "_download", return_value=(json.dumps(rpc).encode(), "")), patch.object(
            install, "_run", side_effect=["1", "d" * 40 + "\tHEAD"]
        ) as run:
            result = install.check(identity)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.artifact["commit"], "d" * 40)
        self.assertEqual(run.call_args_list[0].args[0], ["vercmp", "1.0.0-3", "1.0.0-2"])
        rpc["results"][0]["Name"] = "logos-desktop-bin"
        with patch.object(install, "_download", return_value=(json.dumps(rpc).encode(), "")):
            self.assertEqual(install.check(identity).status, "unavailable")

    def test_aur_source_ahead_and_empty_rpc_disable_handoff_without_wheel_conversion(self):
        identity, config, bodies, requests, rpc, source = self.aur_metadata_provider()
        for package_version in ("1.0.0-1", "2:1.0.0-3", None):
            with self.subTest(package_version=package_version):
                if package_version is None:
                    rpc.update(resultcount=0, results=[])
                else:
                    rpc["results"][0]["Version"] = package_version
                bodies["/rpc"] = (200, json.dumps(rpc).encode())
                requests.clear()
                current = install.Identity("aur", "1.0.0", package_version or "1.0.0-1",
                                           launcher=identity.launcher, provider_path=identity.provider_path)
                with (patch.object(install, "_run", return_value="0") as commands,
                      patch.object(install, "identify", return_value=current),
                      patch.object(subprocess, "Popen") as launch,
                      patch.object(install, "_wheel_update") as wheel):
                    result = install.handoff(current, self.home / "profile space.json", "qt")
                self.assertEqual(result.status, "provider-lag")
                self.assertEqual(result.version, "1.1.0")
                self.assertIn("Source release 1.1.0 is not yet available from AUR", result.message)
                self.assertIsNone(result.artifact)
                self.assertEqual(result.command, ())
                self.assertEqual(requests, ["/rpc", "/source"])
                self.assertFalse(any(call.args[0][0] == "git" for call in commands.call_args_list))
                launch.assert_not_called()
                wheel.assert_not_called()

    def test_aur_current_source_is_optional_and_newer_aur_recipe_keeps_its_owner(self):
        identity, config, bodies, requests, rpc, source = self.aur_metadata_provider()
        source.update(version="1.0.0", filename="logos_desktop-1.0.0-py3-none-any.whl")
        bodies["/source"] = (200, json.dumps(source).encode())
        with patch.object(install, "_run", return_value="0") as commands:
            self.assertEqual(install.check(identity).status, "current")
        self.assertEqual(requests, ["/rpc", "/source"])
        self.assertEqual(commands.call_args.args[0], ["vercmp", "1.0.0-1", "1.0.0-1"])

        del config["release_manifest_url"]
        install._write(identity.provider_path, config)
        requests.clear()
        with patch.object(install, "_run", return_value="0"):
            result = install.check(identity)
        self.assertEqual(result.status, "current")
        self.assertIn("from AUR", result.message)
        self.assertIn("Source release availability was not checked", result.message)
        self.assertEqual(requests, ["/rpc"])

        # An actual newer AUR recipe remains usable even if the optional source feed is down.
        config["release_manifest_url"] = config["rpc_url"].replace("/rpc", "/missing-source")
        install._write(identity.provider_path, config)
        rpc["results"][0]["Version"] = "1.1.0-1"
        bodies["/rpc"] = (200, json.dumps(rpc).encode())
        requests.clear()
        with patch.object(install, "_run", side_effect=["1", "d" * 40 + "\tHEAD"]):
            result = install.check(identity)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.artifact, {"version": "1.1.0-1", "commit": "d" * 40,
                                          "git_url": config["git_url"]})
        self.assertEqual(requests, ["/rpc"])

    def test_aur_configured_source_failure_never_silently_reports_current(self):
        identity, config, bodies, requests, rpc, source = self.aur_metadata_provider()
        unreachable = socket.socket()
        self.addCleanup(unreachable.close)
        unreachable.bind(("127.0.0.1", 0))
        closed_url = f"http://127.0.0.1:{unreachable.getsockname()[1]}/source"
        normal_url = config["release_manifest_url"]
        cases = (
            (closed_url, (200, json.dumps(source).encode()), "offline"),
            (normal_url, (503, b"source unavailable"), "unavailable"),
            (normal_url, (200, json.dumps({**source, "sha256": "bad"}).encode()), "unavailable"),
            (normal_url, (200, b"[]"), "unavailable"),
            (normal_url, (200, json.dumps({**source, "url": []}).encode()), "unavailable"),
        )
        for url, response, expected_status in cases:
            with self.subTest(url=url, response=response):
                config["release_manifest_url"] = url
                install._write(identity.provider_path, config)
                bodies["/source"] = response
                requests.clear()
                with patch.object(install, "_run", return_value="0") as commands:
                    result = install.check(identity)
                self.assertEqual(result.status, expected_status)
                self.assertIsNone(result.artifact)
                self.assertEqual(result.command, ())
                self.assertNotIn("/source.whl", requests)
                self.assertFalse(any(call.args[0][0] == "git" for call in commands.call_args_list))

    def test_aur_review_decline_and_makepkg_failure_use_pinned_recipe(self):
        identity = install.Identity("aur", "1.0.0", "1.0.0-1", launcher=Path("/usr/bin/logos-desktop"),
                                    provider_path=self.home / "aur/provider.json")
        release = {"version": "1.1.0-1", "commit": "d" * 40, "git_url": "https://example.invalid/aur.git"}

        def run(command, **kwargs):
            if "clone" in command:
                build = Path(command[-1])
                build.mkdir()
                (build / "PKGBUILD").write_text("# disposable recipe fixture\n")
                (build / ".SRCINFO").write_text(
                    "pkgbase = logos-desktop\n pkgver = 1.1.0\n pkgrel = 1\n arch = x86_64\npkgname = logos-desktop"
                )
            if "rev-parse" in command:
                return release["commit"]
            if "ls-tree" in command:
                return self.recipe_tree(kwargs["cwd"])
            if command == ["makepkg", "--printsrcinfo"]:
                return "pkgbase = logos-desktop\n pkgver = 1.1.0\n pkgrel = 1\n arch = x86_64\npkgname = logos-desktop"
            return "fixture recipe diff"

        with (patch.object(install, "identify", return_value=identity),
              patch.object(install, "_run", side_effect=run) as commands,
              patch("builtins.input", return_value="NO"), patch("builtins.print"),
              patch.object(subprocess, "run") as makepkg):
            self.assertEqual(install._aur_update(identity, release).status, "cancelled")
        makepkg.assert_not_called()
        self.assertTrue(any(call.args[0] == ["git", "checkout", "--detach", release["commit"]]
                            for call in commands.call_args_list))
        results = [subprocess.CompletedProcess([], 1, "", ""), subprocess.CompletedProcess([], 1)]
        with (patch.object(install, "identify", return_value=identity),
              patch.object(install, "_run", side_effect=run), patch("builtins.input", return_value="REVIEWED"),
              patch("builtins.print"), patch.object(subprocess, "run", side_effect=results) as makepkg):
            self.assertEqual(install._aur_update(identity, release).status, "failed")
        self.assertEqual(makepkg.call_args.args[0], ["makepkg", "-si"])
        self.assertNotIn("-Syu", str(makepkg.call_args_list))
        self.assertEqual(install._target(self.root), self.a)

    def test_aur_refuses_nested_and_symlink_recipes_before_any_makepkg(self):
        identity = install.Identity("aur", "1.0.0", "1.0.0-1", launcher=Path("/usr/bin/logos-desktop"),
                                    provider_path=self.home / "aur/provider.json")
        release = {"version": "1.1.0-1", "commit": "d" * 40, "git_url": "https://example.invalid/aur.git"}
        srcinfo = "pkgbase = logos-desktop\n pkgver = 1.1.0\n pkgrel = 1\n arch = x86_64\npkgname = logos-desktop"
        for layout in ("nested", "symlink-helper", "symlink-PKGBUILD", "untracked-directory"):
            with self.subTest(layout=layout):
                tree = []

                def run(command, **kwargs):
                    if "clone" in command:
                        build = Path(command[-1])
                        build.mkdir()
                        contents = {"PKGBUILD": b"source helpers/build.sh\n", ".SRCINFO": srcinfo.encode()}
                        if layout == "nested":
                            contents["helpers/build.sh"] = b"echo undisplayed\n"
                        if layout == "symlink-helper":
                            contents["helper.sh"] = b"external-helper"
                        for name, content in contents.items():
                            path = build / name
                            path.parent.mkdir(exist_ok=True)
                            mode = "100644"
                            if name == "helper.sh" or (layout == "symlink-PKGBUILD" and name == "PKGBUILD"):
                                path.symlink_to("external-helper")
                                mode = "120000"
                            else:
                                path.write_bytes(content)
                            digest = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
                            tree.append(f"{mode} blob {digest}\t{name}\0")
                        if layout == "untracked-directory":
                            (build / "helpers").mkdir()
                    if "rev-parse" in command:
                        return release["commit"]
                    if "ls-tree" in command:
                        return "".join(tree)
                    return "fixture diff"

                with (patch.object(install, "identify", return_value=identity),
                      patch.object(install, "_run", side_effect=run) as commands,
                      patch.object(subprocess, "run") as packages, patch("builtins.input") as approve):
                    with self.assertRaises(install.InstallError):
                        install._aur_update(identity, release)
                packages.assert_not_called()
                approve.assert_not_called()
                self.assertFalse(any(call.args[0][0] == "makepkg" for call in commands.call_args_list))

    def test_successful_aur_cli_preserves_frontend_and_spaced_config_in_restart_command(self):
        identity = install.Identity("aur", "1.0.0", "1.0.0-1", launcher=Path("/usr/bin/logos-desktop"),
                                    provider_path=self.home / "aur/provider.json")
        release = {"version": "1.1.0-1", "commit": "d" * 40, "git_url": "https://example.invalid/aur.git"}
        srcinfo = "pkgbase = logos-desktop\n pkgver = 1.1.0\n pkgrel = 1\n arch = x86_64\npkgname = logos-desktop"
        profile = self.home / "profile with spaces" / "config file.json"
        update = install._aur_update

        def run(command, **kwargs):
            if "rev-parse" in command:
                return release["commit"]
            if command == ["makepkg", "--printsrcinfo"]:
                return srcinfo
            if command == ["pacman", "-Q", "logos-desktop"]:
                return "logos-desktop 1.1.0-1"
            return "fixture recipe review"

        for frontend in ("qt", "tui"):
            with self.subTest(frontend=frontend):
                outcomes = []

                def perform(*args):
                    result = update(*args)
                    outcomes.append(result)
                    return result

                results = [subprocess.CompletedProcess([], 1, "", ""), subprocess.CompletedProcess([], 0)]
                with (patch.object(install, "identify", return_value=identity),
                      patch.object(install, "check", return_value=install.Result("available", "fixture", "1.1.0-1",
                                                                               release)),
                      patch.object(install, "_aur_update", side_effect=perform) as updater,
                      patch.object(install, "_run", side_effect=run),
                      patch.object(install, "_recipe_files", return_value={"PKGBUILD": "fixture", ".SRCINFO": srcinfo}),
                      patch.object(install, "_owner", return_value=[("pacman", "logos-desktop")]),
                      patch.object(subprocess, "run", side_effect=results),
                      patch.object(subprocess, "Popen") as restart,
                      patch.object(sys.stdin, "isatty", return_value=False),
                      patch("builtins.input", return_value="REVIEWED"), patch("builtins.print") as output):
                    self.assertEqual(install.main(["handoff", "--config", str(profile), "--frontend", frontend]), 0)
                updater.assert_called_once_with(identity, release, str(profile), frontend)
                expected = ("/usr/bin/logos-desktop", "-c", str(profile), "--dialog", frontend)
                self.assertEqual(outcomes[0].status, "restart-required")
                self.assertEqual(outcomes[0].command, expected)
                output.assert_any_call("Restart command: " + shlex.join(expected))
                self.assertIn("not been verified", outcomes[0].message)
                restart.assert_not_called()

    def test_aur_tui_pty_keeps_restart_command_and_errors_visible_until_acknowledged(self):
        profile = self.home / "profile with spaces" / "config file.json"
        srcinfo = "pkgbase = logos-desktop\n pkgver = 1.1.0\n pkgrel = 1\n arch = x86_64\npkgname = logos-desktop"
        expected = ("/usr/bin/logos-desktop", "-c", str(profile), "--dialog", "tui")
        for transaction_status in (0, 1):
            with self.subTest(transaction_status=transaction_status):
                source = textwrap.dedent(f"""\
                    import fcntl
                    import os
                    from pathlib import Path
                    import subprocess
                    import termios
                    from ou_dedetai import controller_install as c

                    os.setsid()
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                    identity = c.Identity('aur', '1.0.0', '1.0.0-1',
                        launcher=Path('/usr/bin/logos-desktop'),
                        provider_path=Path({str(self.home / 'aur/provider.json')!r}))
                    release = {{'version': '1.1.0-1', 'commit': 'd' * 40,
                               'git_url': 'https://example.invalid/aur.git'}}
                    srcinfo = {srcinfo!r}
                    c.identify = lambda: identity
                    c.check = lambda identity: c.Result('available', 'fixture', '1.1.0-1', release)
                    c._recipe_files = lambda build, commit: {{'PKGBUILD': 'fixture', '.SRCINFO': srcinfo}}
                    c._owner = lambda path: [('pacman', 'logos-desktop')]

                    def run(command, **kwargs):
                        if 'rev-parse' in command:
                            return release['commit']
                        if command == ['makepkg', '--printsrcinfo']:
                            return srcinfo
                        if command == ['pacman', '-Q', 'logos-desktop']:
                            return 'logos-desktop 1.1.0-1'
                        return 'fixture recipe review'

                    def transaction(command, **kwargs):
                        code = 1 if command == ['pacman', '-Qu'] else {transaction_status}
                        return subprocess.CompletedProcess(command, code, '', '')

                    c._run = run
                    subprocess.run = transaction
                    raise SystemExit(c.main(['handoff', '--frontend', 'tui', '--config', {str(profile)!r}]))
                    """)
                master, slave = pty.openpty()
                env = {**os.environ, "HOME": str(self.home)}
                helper = subprocess.Popen([sys.executable, "-c", source], stdin=slave, stdout=slave,
                                          stderr=slave, env=env)
                os.close(slave)
                output = bytearray()

                def read_until(text):
                    deadline = time.monotonic() + 5
                    while text not in output and time.monotonic() < deadline:
                        ready, _, _ = select.select([master], [], [], 0.1)
                        if ready:
                            try:
                                output.extend(os.read(master, 65536))
                            except OSError:
                                break
                    self.assertIn(text, output)

                try:
                    read_until(b"Type REVIEWED")
                    os.write(master, b"REVIEWED\n")
                    read_until(b"Press Enter to close this helper:")
                    if transaction_status == 0:
                        self.assertIn(b"restart-required:", output)
                        self.assertIn(("Restart command: " + shlex.join(expected)).encode(), output)
                        self.assertIn(b"not been verified", output)
                    else:
                        self.assertIn(b"failed: makepkg or package authorization failed", output)
                    time.sleep(0.15)
                    self.assertIsNone(helper.poll(), "AUR result must remain visible until acknowledgement")
                    os.write(master, b"\n")
                    self.assertEqual(helper.wait(timeout=5), transaction_status)
                finally:
                    if helper.poll() is None:
                        helper.terminate()
                        helper.wait(timeout=5)
                    os.close(master)

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
