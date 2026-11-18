"""Complete recovery preserves generations across interrupted publications."""

from dataclasses import replace
import json
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid
import unittest
from unittest.mock import Mock, patch

from logos_desktop import artifacts, constants, maintenance, runtime_copies, runtime_filesystem
from logos_desktop import runtime_recovery as recovery
from logos_desktop import runtime_runners, runtime_stacks
from logos_desktop import config_store, runtime, wine
from logos_desktop import runtime_recipes
from logos_desktop import doctor
from logos_desktop import processes
from logos_desktop import controller_install
from logos_desktop.config import Config, EphemeralConfiguration
from tests.artifact_fixtures import appimage
from tests.test_artifacts import response


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        for name, value in (("STATE_DIR", str(self.root / "state")),
                            ("COORDINATION_DIR", self.root / "coordination")):
            setting = patch.object(constants, name, value)
            setting.start()
            self.addCleanup(setting.stop)

        def filesystem(path):
            info = Path(path).stat()
            return {"canonical_path": str(path), "filesystem_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "filesystem_root": str(path), "inode": info.st_ino, "owner_uid": info.st_uid,
                    "boot_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "device": info.st_dev,
                    "mount_id": runtime_filesystem.live_identity(path)["mount_id"]}

        setting = patch.object(runtime_filesystem, "identity", side_effect=filesystem)
        setting.start()
        self.addCleanup(setting.stop)
        self.store = runtime_runners.RunnerStore(self.root / "runners", self.root / "runner-coordination")
        content = appimage()
        runner = replace(next(iter(runtime_stacks.bundled().runners.values())), size=len(content),
                         sha256=hashlib.sha256(content).hexdigest())
        with patch.object(artifacts.requests, "get", return_value=response(content)):
            self.store.download(runner)
        self.runner = self.store.inventory()[0]
        self.install = self.root / "install"
        self.prefix = self.install / "data/wine64_bottle"
        self.prefix.mkdir(parents=True)
        (self.install / "data/bin").mkdir()
        (self.prefix / "database").write_text("old complete bytes")
        product = self.prefix / "drive_c/users/fixture/AppData/Local/Logos"
        (product / "System").mkdir(parents=True)
        (product / "Logos.exe").write_bytes(b"synthetic product identity")
        recovery.write(product / "System/Logos.deps.json", {"libraries": {"Logos/fixture-1": {}}})
        self.context = maintenance.Context(self.prefix, self.install, "Logos")
        self.profile = self.root / "profile.json"
        self.portable = self.install / constants.PORTABLE_CONFIG_NAME
        self.document = {"install_dir": str(self.install), "faithlife_product": "Logos",
                         "wine_binary": str(self.runner.path), "faithlife_product_version": "10"}
        recovery.write(self.profile, self.document)
        recovery.write(self.portable, self.document)
        self.links = [self.install / "data/bin" / name for name in
                      ("selected_wine.AppImage", *artifacts.COMPANIONS)]
        self.links[0].symlink_to(self.runner.path)
        for link in self.links[1:]:
            link.symlink_to(self.links[0].name)
        self.service = recovery.Recovery.create(self.context, self.root / "recovery")

    def capture(self):
        return self.service.capture(store=self.store, generation=self.runner.generation,
                                    profile=self.profile, portable=self.portable, links=self.links,
                                    product_build="fixture-1", effective=self.document)

    def assert_blocked(self, context=None):
        with self.assertRaises(maintenance.RecoveryRequired):
            maintenance.Lease(context or self.context).close()
        with self.assertRaises(maintenance.RecoveryRequired):
            maintenance.Lease(context or self.context, recovery=True).close()

    def test_complete_capture_and_headless_return_retain_newer_bytes_and_pins(self):
        point = self.capture()
        old = runtime_copies.manifest(self.prefix)
        (self.prefix / "database").write_text("newer edited bytes")
        (self.prefix / "new-document").write_text("new unsynced work")
        with patch("logos_desktop.runtime_boundary.inspect", side_effect=AssertionError("no display")):
            self.service.return_prefix(self.service.return_confirmation())
        self.assertEqual(runtime_copies.manifest(self.prefix), old)
        journal = self.service.status()
        self.assertEqual((journal["phase"], journal["gate"]), ("returned", "online-resume-pending"))
        newer = Path(journal["newer"])
        self.assertEqual((newer / "new-document").read_text(), "new unsynced work")
        self.assertEqual((newer / "database").read_text(), "newer edited bytes")
        self.assertEqual(self.service.verify_point(point)["id"], point)
        self.assert_blocked()
        self.assert_blocked(maintenance.Context(newer, self.install, "Logos"))
        self.assertIn("return-point", self.store.removal_reasons(self.runner.sha256))
        with (patch.object(runtime_runners, "INTEGRATED_REFERENCE_KINDS", runtime_runners.REFERENCE_KINDS),
              self.assertRaisesRegex(runtime_runners.RunnerError, "return-point")):
            self.store.remove(self.runner.generation)
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "last valid"):
            self.service.remove_point(point)
        with (patch.object(recovery, "ONLINE_RESUME_SUPPORTED", False),
              self.assertRaisesRegex(maintenance.RecoveryRequired, "Online resume is unavailable")):
            self.service.resume_online(self.service.resume_confirmation(), retained_work_acknowledged=True)

    def test_changed_home_state_still_blocks_and_owner_can_return(self):
        self.capture()
        with patch.object(constants, "STATE_DIR", str(self.root / "alternate/state")):
            self.assert_blocked()
            reopened = recovery.Recovery(self.context)
            reopened.return_prefix(reopened.return_confirmation())
            self.assertEqual(reopened.status()["phase"], "returned")
            self.assert_blocked()

    def test_corrupt_point_refuses_return_without_touching_newer_prefix(self):
        point = self.capture()
        (self.service.root / ("point-" + point) / "prefix/database").write_text("corrupted")
        before = runtime_copies.manifest(self.prefix)
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "byte verification"):
            self.service.return_confirmation()
        self.assertEqual(runtime_copies.manifest(self.prefix), before)
        self.assert_blocked()

    def test_external_documents_mapping_is_refused_before_point_publication(self):
        user = self.prefix / "drive_c/users/fixture"
        user.mkdir(parents=True, exist_ok=True)
        (user / "Documents").symlink_to(Path.home() / "Documents")
        with self.assertRaisesRegex(runtime_copies.CopyError, "external mutable"):
            self.capture()
        self.assertEqual(self.service.status()["phase"], "preparing")
        self.assert_blocked()

    def test_interruption_after_prefix_withdrawal_recovers_both_generations(self):
        from logos_desktop import commands, runtime_commands

        point = self.capture()
        (self.prefix / "database").write_text("new work")
        fired = False

        def interrupt(boundary):
            nonlocal fired
            if boundary.startswith("after-prefix-rename") and not fired:
                fired = True
                raise OSError(5, "injected rename acknowledgment failure")

        self.service.checkpoint = interrupt
        with self.assertRaises(OSError):
            self.service.return_prefix(self.service.return_confirmation())
        self.assertFalse(self.prefix.exists())
        self.assert_blocked()
        reopened = recovery.Recovery(self.context)
        conf = commands.configuration(self.profile, environment={})
        with patch.object(constants, 'STATE_DIR', str(self.root / 'other-state')):
            status = runtime_commands.execute('recovery_status', point, conf)
            self.assertIn('return', status['next_actions'])
            runtime_commands.execute('return', point, conf, confirm=lambda _: 'return ' + point)
        self.assertEqual((self.prefix / "database").read_text(), "old complete bytes")
        self.assertEqual((Path(reopened.status()["newer"]) / "database").read_text(), "new work")
        self.assert_blocked()

    def activate_fixture(self, *, interrupt=False):
        document, _ = config_store.read_document(self.profile)
        app = SimpleNamespace(_config_updated_event=threading.Event())
        app.conf = Config(EphemeralConfiguration.project(document, str(self.profile), environment={}), app)
        spec = artifacts.ArtifactSpec("local:fixture", self.runner.path.name, "appimage")
        prepared = SimpleNamespace(runner=artifacts.AcceptedArtifact(
            self.runner.path, spec, self.runner.sha256, self.runner.size, "synthetic-test-input"), check=lambda: None)
        prepared.catalog = runtime_stacks.bundled()
        prepared.stack = next(iter(prepared.catalog.stacks.values()))

        def record_selection(record):
            journal = self.service.status()
            journal["selection"] = [recovery.activation_snapshot(item) for item in record["next"]]
            journal["activation"] = {
                "path": str(runtime.selection_state(self.install) / "runtime.json"), "record": record,
            }
            self.service.phase(journal, "selection-pending")

        original = runtime._replace_link

        def replace_link(path, target):
            original(path, target)
            if interrupt:
                # Repeated write failure also prevents activation rollback.
                raise OSError(5, "injected selection write failure")

        with (self.service.admitted(), maintenance.recovery_installation(self.context),
              runtime.prepared_inputs(app, prepared), patch.object(wine, "require_compatible"),
              patch.object(runtime, "_replace_link", side_effect=replace_link)):
            journal = self.service.status()
            journal["capability"] = {"id": "00000000-0000-0000-0000-000000000001", "sha256": "a" * 64}
            journal["runners"] = [self.service.pin(self.store, self.runner.generation, "apply"),
                                  self.service.pin(self.store, self.runner.generation, "pending-journal")]
            self.service.phase(journal, "apply-intent")
            runtime.activate(app, prepared.runner, managed=True, before_publish=record_selection)

    def test_modern_only_activation_returns_and_preserves_later_preferences(self):
        self.capture()
        self.activate_fixture()
        document = recovery.read(self.profile)
        document["later_preference"] = "keep this"
        recovery.write(self.profile, document)
        self.service.return_prefix(self.service.return_confirmation())
        restored = recovery.read(self.profile)
        self.assertEqual(restored["later_preference"], "keep this")
        self.assertEqual(restored["wine_binary"], self.document["wine_binary"])
        self.assertNotIn("INSTALLDIR", restored)

    def test_interrupted_activation_returns_from_alternate_state_root(self):
        self.capture()
        with self.assertRaises(maintenance.RecoveryRequired):
            self.activate_fixture(interrupt=True)
        activation = Path(self.service.status()["activation"]["path"])
        self.assertTrue(activation.exists())
        with patch.object(constants, "STATE_DIR", str(self.root / "alternate/state")):
            reopened = recovery.Recovery(self.context)
            reopened.return_prefix(reopened.return_confirmation())
            self.assertEqual(reopened.status()["phase"], "returned")
            self.assertFalse(activation.exists())
            self.assert_blocked()

    def test_effective_runtime_divergence_refuses_capture(self):
        foreign = self.root / "native-wine"
        foreign.write_bytes(b"unmodeled native executable")
        effective = dict(self.document, wine_binary=str(foreign))
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "Effective runtime differs"):
            self.service.capture(store=self.store, generation=self.runner.generation,
                                 profile=self.profile, portable=self.portable, links=self.links,
                                 product_build="fixture-1", effective=effective)
        self.assertEqual(self.service.status()["phase"], "preparing")

    def test_foreign_config_target_is_preserved_on_reconciliation(self):
        self.capture()
        canary = self.root / "unrelated.json"
        recovery.write(canary, {"valuable": "data"})

        def interrupt(boundary):
            if boundary == "after-phase:config-pending":
                raise OSError(5, "interrupted config settlement")

        self.service.checkpoint = interrupt
        with self.assertRaises(OSError):
            self.service.return_prefix(self.service.return_confirmation())
        journal = recovery.read(self.service.root / "journal.json")
        original = recovery.snapshot(canary)
        journal["configs"] = [{"previous": original,
                               "next": dict(original, content="e30=")}]
        recovery.write(self.service.root / "journal.json", journal)
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "Foreign or duplicate"):
            recovery.Recovery(self.context).reconcile_return()
        self.assertEqual(recovery.snapshot(canary), original)

    def test_return_ready_interruption_can_resume_headlessly(self):
        self.capture()
        (self.prefix / "new-work").write_text("retained")

        def interrupt(boundary):
            if boundary == "after-phase:return-ready":
                raise OSError(5, "interrupted before pending gate")

        self.service.checkpoint = interrupt
        with self.assertRaises(OSError):
            self.service.return_prefix(self.service.return_confirmation())
        reopened = recovery.Recovery(self.context)
        reopened.reconcile_return()
        self.assertEqual(reopened.status()["phase"], "returned")
        self.assertEqual((Path(reopened.status()["newer"]) / "new-work").read_text(), "retained")
        self.assert_blocked()

    def test_foreign_journal_blocks_even_repair_admission(self):
        journal = self.service.status()
        journal["id"] = "00000000-0000-0000-0000-000000000000"
        recovery.write(self.service.root / "journal.json", journal)
        self.assert_blocked()

    def test_lost_registered_store_is_unknown_not_empty(self):
        self.capture()
        self.service.root.rename(self.root / "displaced")
        with self.assertRaises((OSError, maintenance.MaintenanceError)):
            maintenance.Lease(self.context).close()

    def case_service(self, name, *, product_build="fixture-1", settings=None, registry_lower_case=False,
                     capture_point=True):
        root = self.root / name
        root.mkdir(mode=0o700)
        install = root / "install"
        shutil.copytree(self.install, install, symlinks=True)
        context = maintenance.Context(install / "data/wine64_bottle", install, "Logos")
        document = dict(self.document, install_dir=str(install))
        if product_build != "fixture-1":
            product = context.prefix / "drive_c/users/fixture/AppData/Local/Logos"
            recovery.write(product / "System/Logos.deps.json", {"libraries": {"Logos/" + product_build: {}}})
        if settings is not None:
            self.write_registry(context.prefix, settings, lower_case=registry_lower_case)
        recovery.write(root / "profile.json", document)
        recovery.write(install / constants.PORTABLE_CONFIG_NAME, document)
        service = recovery.Recovery.create(context, root / "complete")
        if capture_point:
            service.capture(store=self.store, generation=self.runner.generation, profile=root / "profile.json",
                            portable=install / constants.PORTABLE_CONFIG_NAME,
                            links=[install / "data/bin" / link.name for link in self.links],
                            product_build=product_build, effective=document)
        return service

    def write_registry(self, prefix, settings, *, lower_case=False):
        sections = {}
        for key, value in settings.items():
            for path, name, kind, expected in runtime_recipes._setting_entries(key, value):
                if expected is None:
                    continue
                section = path[5:].replace("\\", "\\\\")
                if lower_case:
                    section, name = section.lower(), name.upper()
                encoded = f"dword:{expected:08x}" if kind == "REG_DWORD" else json.dumps(expected)
                sections.setdefault(section, []).append(json.dumps(name) + "=" + encoded)
        content = "WINE REGISTRY Version 2\n\n" + "\n\n".join(
            "[" + section + "] 123\n" + "\n".join(values) for section, values in sections.items()
        ) + "\n"
        (prefix / "user.reg").write_text(content)

    def test_default_store_rebinds_both_profiles_after_interrupted_cli_return(self):
        self.enterContext(patch.object(constants, 'DATA_HOME', self.root / 'shared-data'))
        original = self.runner
        old_runner = replace(next(iter(runtime_stacks.bundled().runners.values())),
                             sha256=original.sha256, size=original.size)
        self.store = runtime_runners.RunnerStore()
        self.store.download(old_runner, candidates=(original.path,))
        self.runner = self.store.inventory()[0]
        self.document['wine_binary'] = str(self.runner.path)
        self.links[0].unlink()
        self.links[0].symlink_to(self.runner.path)
        prepared, target = self.distinct_prepared_fixture()
        service = self.case_service('default-return', product_build='54.3.0.5', settings=dict(prepared.recipe.settings))
        profile = service.root.parent / 'profile.json'
        portable = service.context.install / constants.PORTABLE_CONFIG_NAME
        self.store.discover_profiles(profile)
        journal = service.status()
        point = journal['point']
        journal['capability'] = {'id': '00000000-0000-0000-0000-000000000001', 'sha256': 'a' * 64}
        service.phase(journal, 'point-verified', result='succeeded')
        with patch.object(service, 'verify_capability'), patch.object(service, '_run_recipe'):
            service.apply(self.app_fixture(service), prepared, store=self.store, generation=target.generation,
                          host={'architecture': 'x86_64', 'font-arial': 'present'}, without_successful_trial=True)
        for path in (profile, portable):
            self.assertIn(target.sha256, self.store._registry()['profiles'][str(path)]['digests'])
        (service.context.prefix / 'newer-work').write_text('preserve')

        def interrupt(boundary):
            if boundary == 'after-config:' + str(profile):
                raise InterruptedError

        service.checkpoint = interrupt
        with self.assertRaises(InterruptedError):
            service.return_prefix(service.return_confirmation())
        for path in (profile, portable):
            binding = self.store._registry()['profiles'][str(path)]
            self.assertEqual(binding['state'], 'reserved')
            self.assertEqual(set(binding['digests']), {target.sha256, self.runner.sha256})

        program = '''import os, sys
from pathlib import Path
from logos_desktop import constants, main, runtime_filesystem
root, profile, point = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
constants.COORDINATION_DIR = root / 'coordination'
constants.DATA_HOME = root / 'shared-data'
def filesystem(path):
    return dict(runtime_filesystem.live_identity(path), filesystem_uuid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                filesystem_root=str(path))
runtime_filesystem.identity = filesystem
sys.argv = ['logos-desktop', '-c', profile, '--runtime-return', point]
try:
    main.main()
finally:
    assert not any(module.startswith('PySide') for module in sys.modules)
'''
        env = dict(os.environ, XDG_STATE_HOME=str(self.root / 'other-state'))
        env.pop('DISPLAY', None)
        env.pop('WAYLAND_DISPLAY', None)
        result = subprocess.run([sys.executable, '-B', '-c', program, str(self.root), str(profile), point],
                                input=f'return {point}\n', env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (profile, portable):
            self.store.register_existing_profile(path)
            binding = self.store._registry()['profiles'][str(path)]
            self.assertEqual(binding['state'], 'published')
            self.assertEqual(binding['bytes'], artifacts.regular_hash(path))
            self.assertEqual(binding['digests'], [self.runner.sha256])
        self.assertEqual(service.status()['gate'], 'online-resume-pending')
        self.assertEqual((Path(service.status()['newer']) / 'newer-work').read_text(), 'preserve')

    def prepared_fixture(self):
        catalog = runtime_stacks.bundled()
        stack = next(iter(catalog.stacks.values()))
        original = catalog.runners[stack.runner_id]
        runner = replace(original, sha256=self.runner.sha256, size=self.runner.size)
        recipe = replace(catalog.recipes[stack.recipe_id], dependencies=())
        catalog = replace(catalog, runners={runner.id: runner}, recipes={recipe.id: recipe},
                          stacks={stack.id: stack}, evidence={}, sha256="a" * 64)
        accepted = artifacts.AcceptedArtifact(self.runner.path, runner.spec(), self.runner.sha256,
                                              self.runner.size, "synthetic-integration-fixture")
        return runtime_recipes.PreparedStack(catalog.sha256, stack, recipe, accepted, (), catalog)

    def app_fixture(self, service, *, environment=None):
        profile = service.root.parent / "profile.json"
        document, _ = config_store.read_document(profile)
        app = SimpleNamespace(_config_updated_event=threading.Event())
        app.conf = Config(EphemeralConfiguration.project(document, str(profile), environment=environment or {}), app)
        return app

    def distinct_prepared_fixture(self):
        catalog = runtime_stacks.bundled()
        stack = catalog.stacks["logos-54.3-mmtrt-11.21-candidate"]
        old = replace(catalog.runners["mmtrt-stable-11.0-697401628"],
                      sha256=self.runner.sha256, size=self.runner.size)
        content = appimage(b"distinct M4 candidate runtime")
        runner = replace(catalog.runners[stack.runner_id], sha256=hashlib.sha256(content).hexdigest(),
                         size=len(content))
        with patch.object(artifacts.requests, "get", return_value=response(content)):
            self.store.download(runner)
        selected = next(item for item in self.store.inventory() if item.sha256 == runner.sha256)
        recipe = replace(catalog.recipes[stack.recipe_id], dependencies=())
        catalog = replace(catalog, runners={old.id: old, runner.id: runner}, recipes={recipe.id: recipe},
                          stacks={stack.id: stack}, evidence={}, sha256="a" * 64)
        accepted = artifacts.AcceptedArtifact(selected.path, runner.spec(), runner.sha256, runner.size,
                                              "distinct-synthetic-managed-runtime")
        return runtime_recipes.PreparedStack(catalog.sha256, stack, recipe, accepted, (), catalog), selected

    def test_case_varied_registry_preserves_custom_choices_in_full_apply(self):
        prepared = self.prepared_fixture()
        choices = ({"renderer": "gl"}, {"font-smoothing": "bgr"}, {"wpf-software": False},
                   {"windows-version": "win7"}, {"windows-version": "win11"},
                   {"windows-version": ""}, {"windows-version": "absent"})
        for index, changes in enumerate(choices):
            with self.subTest(changes=changes):
                settings = dict(prepared.recipe.settings, **changes)
                service = self.case_service("custom-" + str(index), product_build="54.3.0.5", settings=settings,
                                            registry_lower_case=True)
                journal = service.status()
                journal["capability"] = {"id": "00000000-0000-0000-0000-000000000001", "sha256": "a" * 64}
                service.phase(journal, "point-verified", result="succeeded")
                before = runtime_copies.manifest(service.context.prefix)
                with (patch.object(service, "verify_capability"), patch.object(runtime, "activate") as activation,
                      self.assertRaisesRegex(maintenance.RecoveryRequired, "Custom Wine settings preserved")):
                    service.apply(self.app_fixture(service), prepared, store=self.store,
                                  generation=self.runner.generation, host={}, without_successful_trial=True)
                activation.assert_not_called()
                self.assertEqual(runtime_copies.manifest(service.context.prefix), before)
                after = runtime_recipes.observe_prefix_settings(service.context.prefix)
                version = settings["windows-version"]
                self.assertEqual(after["windows-version"], version if version in ("win10", "win11") else None)

    def absent_windows_apply(self, *, fail_recipe=False):
        prepared = self.prepared_fixture()
        settings = {key: value for key, value in prepared.recipe.settings.items() if key != "windows-version"}
        service = self.case_service("absent-version", product_build="54.3.0.5", settings=settings)
        journal = service.status()
        point = journal["point"]
        journal["capability"] = {"id": "00000000-0000-0000-0000-000000000001", "sha256": "a" * 64}
        service.phase(journal, "point-verified", result="succeeded")
        original = runtime_copies.manifest(service.context.prefix)
        old_registry = (service.context.prefix / "user.reg").read_bytes()
        saved_registry = service.root / ("point-" + point) / "prefix/user.reg"
        point_hash = artifacts.regular_hash(service.root / ("point-" + point) / "point.json")
        intent_seen = False

        def checkpoint(boundary):
            nonlocal intent_seen
            if boundary == "after-phase:apply-intent":
                self.assertIn("Initialize absent", service.status()["reason"])
                self.assertEqual((service.context.prefix / "user.reg").read_bytes(), old_registry)
                intent_seen = True

        def recipe(inputs, account):
            self.assertTrue(intent_seen)
            self.assertEqual(service.status()["phase"], "recipe-pending")
            self.assertEqual(maintenance.current_context(), service.context)
            observed_version = runtime_recipes.observe_prefix_settings(service.context.prefix)["windows-version"]
            self.assertEqual(observed_version, "absent")
            self.assertEqual(saved_registry.read_bytes(), old_registry)
            self.write_registry(service.context.prefix, dict(inputs.recipe.settings))
            if fail_recipe:
                raise maintenance.RecoveryRequired("injected failure after version initialization")

        service.checkpoint = checkpoint
        with (patch.object(service, "verify_capability"), patch.object(service, "_run_recipe", side_effect=recipe),
              patch.object(runtime_stacks, "assess", wraps=runtime_stacks.assess) as assessment):
            if fail_recipe:
                with self.assertRaisesRegex(maintenance.RecoveryRequired, "after version initialization"):
                    service.apply(self.app_fixture(service), prepared, store=self.store,
                                  generation=self.runner.generation,
                                  host={"architecture": "x86_64", "font-arial": "present"},
                                  without_successful_trial=True)
                self.assertEqual(service.status()["result"], "unknown")
                self.assert_blocked(service.context)
            else:
                service.apply(self.app_fixture(service), prepared, store=self.store, generation=self.runner.generation,
                              host={"architecture": "x86_64", "font-arial": "present"}, without_successful_trial=True)
                self.assertEqual(service.status()["phase"], "applied")
                self.assertIn("Initialized absent", service.status()["reason"])
            self.assertEqual(assessment.call_args.kwargs["settings"]["windows-version"], "win10")
        self.assertEqual(runtime_recipes.observe_prefix_settings(service.context.prefix)["windows-version"], "win10")
        self.assertEqual(artifacts.regular_hash(service.root / ("point-" + point) / "point.json"), point_hash)
        self.assertEqual(saved_registry.read_bytes(), old_registry)
        service.verify_point(point)
        service.return_prefix(service.return_confirmation())
        self.assertEqual(runtime_copies.manifest(service.context.prefix), original)
        self.assertEqual(runtime_recipes.observe_prefix_settings(service.context.prefix)["windows-version"], "absent")
        newer = Path(service.status()["newer"])
        self.assertEqual(runtime_recipes.observe_prefix_settings(newer)["windows-version"], "win10")
        self.assert_blocked(service.context)

    def test_apply_initializes_only_absent_windows_version_after_intent_and_complete_return_restores_absence(self):
        self.absent_windows_apply()

    def test_initialization_failure_retains_guard_and_complete_return_restores_absence(self):
        self.absent_windows_apply(fail_recipe=True)

    def test_absence_does_not_authorize_win11_or_unknown_and_partial_observations(self):
        prepared = self.prepared_fixture()
        base = dict(prepared.recipe.settings)
        absent = dict(base, **{"windows-version": "absent"})
        partial = {key: value for key, value in base.items() if key != "windows-version"}
        cases = ((absent, "win11"), (dict(base, **{"windows-version": None}), "win10"), (partial, "win10"),
                 (dict(absent, **{"wpf-software": 1}), "win10"))
        for index, (observed, target) in enumerate(cases):
            with self.subTest(observed=observed, target=target):
                recipe = replace(prepared.recipe, settings=dict(base, **{"windows-version": target}))
                candidate = replace(prepared, recipe=recipe,
                                    catalog=replace(prepared.catalog, recipes={recipe.id: recipe}))
                service = self.case_service("absence-refusal-" + str(index), product_build="54.3.0.5", settings=partial)
                journal = service.status()
                journal["capability"] = {"id": "00000000-0000-0000-0000-000000000001", "sha256": "a" * 64}
                service.phase(journal, "point-verified", result="succeeded")
                before = runtime_copies.manifest(service.context.prefix)
                configurations = [recovery.snapshot(Path(item["path"])) for item in service.verify_point(
                    journal["point"])["preimages"]]
                with (patch.object(service, "verify_capability"), patch.object(runtime, "activate") as activation,
                      patch.object(runtime_recipes, "observe_prefix_settings", return_value=observed),
                      self.assertRaisesRegex(maintenance.RecoveryRequired, "Custom Wine settings preserved")):
                    service.apply(self.app_fixture(service), candidate, store=self.store,
                                  generation=self.runner.generation, host={}, without_successful_trial=True)
                activation.assert_not_called()
                self.assertEqual(runtime_copies.manifest(service.context.prefix), before)
                self.assertEqual([recovery.snapshot(Path(item["path"])) for item in configurations], configurations)

    def test_reopened_environment_runtime_drift_refuses_before_capability_or_activation(self):
        prepared = self.prepared_fixture()
        service = self.case_service("environment", product_build="54.3.0.5", settings=dict(prepared.recipe.settings))
        foreign = self.root / "native-wine"
        foreign.write_bytes(b"external Wine")
        before = runtime_copies.manifest(service.context.prefix)
        with (patch.object(runtime, "activate") as activation,
              self.assertRaisesRegex(maintenance.RecoveryRequired, "Effective old runtime changed")):
            service.apply(self.app_fixture(service, environment={"wine_binary": str(foreign)}), prepared,
                          store=self.store, generation=self.runner.generation, host={}, without_successful_trial=True)
        activation.assert_not_called()
        self.assertEqual(runtime_copies.manifest(service.context.prefix), before)

    def test_full_apply_records_original_context_and_returns_complete_configuration(self):
        prepared = self.prepared_fixture()
        service = self.case_service("applying", product_build="54.3.0.5", settings=dict(prepared.recipe.settings))
        journal = service.status()
        journal["capability"] = {"id": "00000000-0000-0000-0000-000000000001", "sha256": "a" * 64}
        service.phase(journal, "point-verified", result="succeeded")
        original = runtime_copies.manifest(service.context.prefix)

        def recipe(inputs, account):
            self.assertEqual(service.status()["phase"], "recipe-pending")
            self.assertEqual(maintenance.current_context(), service.context)
            self.assertEqual(account, "fixture")
            self.assertEqual(inputs, prepared)
            boundary = recovery.runtime_boundary.Boundary("/usr/bin/bwrap", self.root / "display", ())
            arguments = recovery.runtime_boundary.command(
                boundary, source=service.context.prefix, copied=service.context.prefix,
                private=service.root / "private", runner=prepared.runner.path,
                worker=Path(recovery.runtime_trials.__file__).with_name("runtime_trial_worker.py"),
                wine_user=account, mode="apply",
            )
            self.assertIn("--unshare-net", arguments)
            self.assertEqual(arguments[-1], "--apply")

        with patch.object(service, "verify_capability"), patch.object(service, "_run_recipe", side_effect=recipe):
            service.apply(self.app_fixture(service), prepared, store=self.store, generation=self.runner.generation,
                          host={"architecture": "x86_64", "font-arial": "present"}, without_successful_trial=True)
        self.assertEqual(service.status()["phase"], "applied")
        maintenance.Lease(service.context).close()
        with patch.object(controller_install, "_migration_recovery_identity", side_effect=runtime_filesystem.identity):
            controller_install._migration_recovery_clear(Path(constants.COORDINATION_DIR), service.context.install,
                                                         service.context.prefix)
        service.return_prefix(service.return_confirmation())
        self.assertEqual(runtime_copies.manifest(service.context.prefix), original)
        self.assertEqual(len(service.status()["configs"]), len(self.links) + 2)

    def test_checked_recipe_failure_persists_unknown_and_keeps_recovery_references(self):
        prepared = self.prepared_fixture()
        service = self.case_service("failed-apply", product_build="54.3.0.5", settings=dict(prepared.recipe.settings))
        journal = service.status()
        journal["capability"] = {"id": "00000000-0000-0000-0000-000000000001", "sha256": "a" * 64}
        service.phase(journal, "point-verified", result="succeeded")
        with (patch.object(service, "verify_capability"),
              patch.object(service, "_run_recipe", side_effect=maintenance.RecoveryRequired("checked recipe refusal")),
              self.assertRaisesRegex(maintenance.RecoveryRequired, "checked recipe refusal")):
            service.apply(self.app_fixture(service), prepared, store=self.store, generation=self.runner.generation,
                          host={"architecture": "x86_64", "font-arial": "present"}, without_successful_trial=True)
        result = service.status()
        self.assertEqual((result["phase"], result["result"]), ("recipe-pending", "unknown"))
        self.assertIn("pending-journal", self.store.removal_reasons(self.runner.sha256))
        service.verify_point(result["point"])
        self.assert_blocked(service.context)

    def test_original_mutation_command_refuses_an_unowned_context(self):
        boundary = recovery.runtime_boundary.Boundary("/usr/bin/bwrap", self.root / "display", ())
        with self.assertRaisesRegex(recovery.runtime_boundary.Unavailable, "complete recovery owner"):
            recovery.runtime_boundary.command(
                boundary, source=self.prefix, copied=self.prefix, private=self.root / "private",
                runner=self.runner.path, worker=Path(recovery.runtime_trials.__file__), mode="apply",
            )

    def test_explicit_skip_cannot_bypass_capability_or_known_affected_decision(self):
        prepared, target = self.distinct_prepared_fixture()
        service = self.case_service('skip-policy', product_build='54.3.0.5', settings=dict(prepared.recipe.settings))
        host = {'architecture': 'x86_64', 'font-arial': 'present'}
        inputs = dict(store=self.store, generation=target.generation, host=host, without_successful_trial=True)
        with patch.object(runtime, 'activate') as activate:
            with self.assertRaisesRegex(maintenance.RecoveryRequired, 'Inspect an unmodified'):
                service.apply(self.app_fixture(service), prepared, **inputs)
            journal = service.status()
            journal['capability'] = {'id': '00000000-0000-0000-0000-000000000001', 'sha256': 'a' * 64}
            service.phase(journal, 'point-verified', result='succeeded')
            denied = runtime_stacks.Assessment('known-affected', 'cached', 'known-affected', False)
            with (patch.object(service, 'verify_capability') as verify,
                  patch.object(runtime_stacks, 'assess', return_value=denied),
                  self.assertRaisesRegex(maintenance.RecoveryRequired, 'known-affected')):
                service.apply(self.app_fixture(service), prepared, experimental=True, **inputs)
            verify.assert_called_once()
            activate.assert_not_called()
            self.assertEqual(service.status()['phase'], 'point-verified')

    def test_replaced_original_root_refuses_apply_even_when_complete_bytes_match(self):
        prepared, target = self.distinct_prepared_fixture()
        service = self.case_service("replaced-before-apply", product_build="54.3.0.5",
                                    settings=dict(prepared.recipe.settings))
        old = runtime_copies.manifest(service.context.prefix)
        preserved = service.context.prefix.with_name("preserved-original")
        service.context.prefix.rename(preserved)
        shutil.copytree(preserved, service.context.prefix, symlinks=True)
        current = runtime_copies.manifest(service.context.prefix)
        self.assertEqual(current, old)
        self.assertNotEqual(current.observation_sha256, old.observation_sha256)
        with (patch.object(service, "verify_capability") as proof,
              patch.object(service, "_run_recipe") as recipe,
              self.assertRaisesRegex(maintenance.RecoveryRequired, "Original changed")):
            service.apply(self.app_fixture(service), prepared, store=self.store, generation=target.generation,
                          host={"architecture": "x86_64", "font-arial": "present"}, without_successful_trial=True)
        proof.assert_not_called()
        recipe.assert_not_called()
        self.assertEqual(runtime_copies.manifest(preserved), old)
        self.assertEqual(runtime_copies.manifest(service.context.prefix), old)
        self.assertEqual(service.status()["phase"], "point-verified")

    def root_replaced_after_selection(self, *, command_check):
        prepared, target = self.distinct_prepared_fixture()
        service = self.case_service("replaced-before-recipe-" + str(command_check), product_build="54.3.0.5",
                                    settings=dict(prepared.recipe.settings))
        journal = service.status()
        journal["capability"] = {"id": "00000000-0000-0000-0000-000000000001", "sha256": "a" * 64}
        service.phase(journal, "point-verified", result="succeeded")
        preserved = service.context.prefix.with_name("preserved-original")

        def recipe(inputs, account):
            service.context.prefix.rename(preserved)
            shutil.copytree(preserved, service.context.prefix, symlinks=True)
            (service.context.prefix / "foreign-canary").write_text("preserve replacement data")
            if not command_check:
                return
            boundary = recovery.runtime_boundary.Boundary("/usr/bin/bwrap", self.root / "display", ())
            recovery.runtime_boundary.command(
                boundary, source=service.context.prefix, copied=service.context.prefix,
                private=service.root / "private", runner=inputs.runner.path,
                worker=Path(recovery.runtime_trials.__file__).with_name("runtime_trial_worker.py"),
                wine_user=account, mode="apply",
            )

        with (patch.object(service, "verify_capability"), patch.object(service, "_run_recipe", side_effect=recipe),
              self.assertRaisesRegex(maintenance.MaintenanceError, "prefix.*changed")):
            service.apply(self.app_fixture(service), prepared, store=self.store, generation=target.generation,
                          host={"architecture": "x86_64", "font-arial": "present"}, without_successful_trial=True)
        self.assertEqual(service.status()["result"], "unknown")
        self.assertEqual((service.context.prefix / "foreign-canary").read_text(), "preserve replacement data")
        self.assertEqual((preserved / "database").read_text(), "old complete bytes")

    def test_original_mutation_command_refuses_root_replaced_after_selection(self):
        self.root_replaced_after_selection(command_check=True)

    def test_apply_completion_refuses_original_root_replaced_during_recipe(self):
        self.root_replaced_after_selection(command_check=False)

    def test_apply_capability_path_refuses_missing_or_changed_prerequisites_and_failed_observations(self):
        prepared, target = self.distinct_prepared_fixture()
        for kind in ("bwrap", "display", "namespace", "facts", "resource", "restart"):
            with self.subTest(kind=kind):
                service = self.case_service("capability-prerequisite-" + kind, product_build="54.3.0.5",
                                            settings=dict(prepared.recipe.settings))
                point = service.status()["point"]
                inspection_id, directory = self.synthetic_inspection(point, service=service)
                if kind in ("resource", "restart"):
                    record = recovery.read(directory / "inspection.json")
                    record["observations"][kind] = False
                    recovery.write(directory / "inspection.json", record)
                journal = service.status()
                journal["capability"] = {"id": inspection_id,
                                         "sha256": artifacts.regular_hash(directory / "inspection.json")}
                service.phase(journal, "point-verified", result="succeeded")
                original = runtime_copies.manifest(service.context.prefix)
                publications = [recovery.snapshot(item["path"]) for item in service.verify_point(point)["preimages"]]
                unavailable = recovery.runtime_boundary.Unavailable("Repair the missing " + kind + " prerequisite")
                with (patch.object(recovery.runtime_boundary, "inspect", return_value=object(),
                                   side_effect=unavailable if kind in ("bwrap", "display") else None),
                      patch.object(recovery, "inspection_facts",
                                   return_value={"changed": True} if kind == "facts" else {}),
                      patch.object(recovery.runtime_boundary, "command", return_value=["synthetic-probe"]),
                      patch.object(recovery.runtime_boundary, "probe",
                                   side_effect=unavailable if kind == "namespace" else None),
                      patch.object(service, "_run_recipe") as recipe,
                      patch.object(runtime, "activate") as activation,
                      self.assertRaises(maintenance.MaintenanceError)):
                    service.apply(self.app_fixture(service), prepared, store=self.store, generation=target.generation,
                                  host={"architecture": "x86_64", "font-arial": "present"},
                                  without_successful_trial=True)
                recipe.assert_not_called()
                activation.assert_not_called()
                self.assertEqual(service.status()["phase"], "point-verified")
                current = runtime_copies.manifest(service.context.prefix)
                self.assertEqual(current, original)
                self.assertEqual(current.observation_sha256, original.observation_sha256)
                self.assertEqual([recovery.snapshot(item["path"]) for item in publications], publications)

    def test_prerequisite_loss_after_apply_keeps_headless_return_and_online_guard(self):
        prepared, target = self.distinct_prepared_fixture()
        service = self.case_service("lost-prerequisite-after-apply", product_build="54.3.0.5",
                                    settings=dict(prepared.recipe.settings))
        point = service.status()["point"]
        inspection_id, directory = self.synthetic_inspection(point, service=service)
        journal = service.status()
        journal["capability"] = {"id": inspection_id, "sha256": artifacts.regular_hash(directory / "inspection.json")}
        service.phase(journal, "point-verified", result="succeeded")
        original = runtime_copies.manifest(service.context.prefix)
        app = self.app_fixture(service)
        with (patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
              patch.object(recovery, "inspection_facts", return_value={}),
              patch.object(recovery.runtime_boundary, "command", return_value=["synthetic-probe"]),
              patch.object(recovery.runtime_boundary, "probe"), patch.object(service, "_run_recipe")):
            service.apply(app, prepared, store=self.store, generation=target.generation,
                          host={"architecture": "x86_64", "font-arial": "present"}, without_successful_trial=True)
        (service.context.prefix / "synthetic-recipe-work").write_text("preserve newer recipe bytes")
        before_feedback = service.status()
        before_references = self.store.removal_reasons(target.sha256)
        service.record_local_feedback(point)
        saved = recovery.read(service.root / ("feedback-" + point + ".json"))
        self.assertEqual(saved["observation"], "works-for-me")
        self.assertEqual(saved["activation"], before_feedback["activation"])
        self.assertEqual(service.status(), before_feedback)
        self.assertEqual(self.store.removal_reasons(target.sha256), before_references)
        with (patch.object(recovery.runtime_boundary, "inspect", side_effect=recovery.runtime_boundary.Unavailable(
                "Repair the missing display before launch", "display-unavailable")),
              self.assertRaisesRegex(maintenance.MaintenanceError, "Repair.*display")):
            service.check_online_prerequisites()
        self.fresh_complete_return(service, original)
        self.assertEqual(Path(app.conf._raw.wine_binary), target.path)
        service.rebind_restored(app)
        self.assertEqual(Path(app.conf._raw.wine_binary), self.runner.path)
        self.assertFalse(app.conf._overrides.runtime_selection)
        with (patch.object(recovery.runtime_boundary, "inspect", side_effect=recovery.runtime_boundary.Unavailable(
                "Repair the missing display before offline inspection", "display-unavailable")),
              self.assertRaisesRegex(maintenance.MaintenanceError, "Repair.*display")):
            service.inspect_generation(observe=lambda: {})
        self.assertEqual(service.status()["gate"], "online-resume-pending")
        self.assert_blocked(service.context)

    def test_older_wine_apply_is_refused_and_only_unused_runner_bytes_can_be_removed(self):
        prepared = self.prepared_fixture()
        original = prepared.catalog.runners[prepared.stack.runner_id]
        old_model = replace(original, id="old-fixture-runner", version="11.21 fixture",
                            companions=(("wine", "wine-11.21"), ("wine64", "wine-11.21"),
                                        ("wineserver", "Wine 11.21")))
        content = appimage(b"candidate-runtime-11")
        new_model = replace(original, sha256=hashlib.sha256(content).hexdigest(), size=len(content))
        with patch.object(artifacts.requests, "get", return_value=response(content)):
            self.store.download(new_model)
        selected = next(item for item in self.store.inventory() if item.sha256 == new_model.sha256)
        catalog = replace(prepared.catalog, runners={new_model.id: new_model, old_model.id: old_model})
        prepared = replace(prepared, catalog=catalog, runner=artifacts.AcceptedArtifact(
            selected.path, new_model.spec(), selected.sha256, selected.size, "synthetic-older-target"))
        service = self.case_service("downgrade", product_build="54.3.0.5", settings=dict(prepared.recipe.settings))
        before = runtime_copies.manifest(service.context.prefix)
        with patch.object(runtime, "activate") as activation, self.assertRaisesRegex(
            maintenance.RecoveryRequired, "cannot downgrade",
        ):
            service.apply(self.app_fixture(service), prepared, store=self.store, generation=selected.generation,
                          host={}, without_successful_trial=True)
        activation.assert_not_called()
        self.assertEqual(runtime_copies.manifest(service.context.prefix), before)
        with patch.object(runtime_runners, "INTEGRATED_REFERENCE_KINDS", runtime_runners.REFERENCE_KINDS):
            self.store.remove(selected.generation)
            with self.assertRaisesRegex(runtime_runners.RunnerError, "return-point"):
                self.store.remove(self.runner.generation)
        service.verify_point(service.status()["point"])

    def test_empty_return_configuration_graph_cannot_settle(self):
        self.capture()
        self.activate_fixture()

        def interrupt(boundary):
            if boundary == "after-phase:config-pending":
                raise OSError(5, "interrupted config publication")

        self.service.checkpoint = interrupt
        with self.assertRaises(OSError):
            self.service.return_prefix(self.service.return_confirmation())
        journal = recovery.read(self.service.root / "journal.json")
        journal["configs"] = []
        recovery.write(self.service.root / "journal.json", journal)
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "Incomplete return"):
            recovery.Recovery(self.context).reconcile_return()
        self.assert_blocked()

    def test_keep_current_allows_fresh_capture_and_preserves_the_older_point(self):
        first = self.capture()
        self.service.keep_current()
        (self.prefix / "later-document").write_text("new source work")
        second = self.capture()
        self.assertNotEqual(first, second)
        self.assertEqual(set(self.service.point_index()["points"]), {first, second})
        self.service.verify_point(first)
        self.service.verify_point(second)

    def two_points(self):
        first = self.capture()
        self.service.keep_current()
        (self.prefix / "later-document").write_text("new source work")
        second = self.capture()
        return first, second

    def case_points(self, name):
        service = self.case_service(name)
        first = service.status()["point"]
        service.keep_current()
        profile = service.root.parent / "profile.json"
        second = service.capture(store=self.store, generation=self.runner.generation, profile=profile,
                                 portable=service.context.install / constants.PORTABLE_CONFIG_NAME,
                                 links=[service.context.install / "data/bin" / link.name for link in self.links],
                                 product_build="fixture-1", effective=recovery.read(profile))
        return service, first, second

    def test_eligible_old_point_cleanup_releases_only_its_references(self):
        first, second = self.two_points()
        before = runtime_copies.manifest(self.prefix)
        confirmation = self.service.point_removal_confirmation(first)
        self.service.remove_point(first, confirmation)
        self.assertFalse((self.service.root / ("point-" + first)).exists())
        self.assertEqual(set(self.service.point_index()["points"]), {second})
        self.assertIn(first, self.service.point_index()["retired"])
        self.service.verify_point(second)
        self.assertEqual(runtime_copies.manifest(self.prefix), before)
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "last valid"):
            self.service.remove_point(second)
        self.assertEqual(len(self.store._registry()["references"]), 1)

    def test_old_point_cleanup_restarts_after_withdrawal_without_deleting_last_point(self):
        first, second = self.two_points()
        confirmation = self.service.point_removal_confirmation(first)

        def interrupt(boundary):
            if boundary == "after-prefix-rename:point-" + first:
                raise OSError(5, "injected point cleanup rename acknowledgment loss")

        self.service.checkpoint = interrupt
        with self.assertRaises(OSError):
            self.service.remove_point(first, confirmation)
        self.assertTrue((self.service.root / (".removed-point-" + first)).exists())
        self.assertIn("pending-journal", self.store.removal_reasons(self.runner.sha256))
        reopened = recovery.Recovery(self.context)
        reopened.remove_point(first, confirmation)
        reopened.verify_point(second)
        self.assertFalse((self.service.root / (".removed-point-" + first)).exists())
        self.assertEqual(len(self.store._registry()["references"]), 1)

    def test_point_cleanup_does_not_count_a_corrupt_point_as_the_remaining_valid_point(self):
        first, second = self.two_points()
        (self.service.root / ("point-" + second) / "prefix/database").write_text("corrupt")
        with self.assertRaises(maintenance.RecoveryRequired):
            self.service.point_removal_confirmation(first)
        self.service.verify_point(first)
        self.assertTrue((self.service.root / ("point-" + first)).exists())

    def test_cleanup_refuses_current_and_referenced_or_unknown_inspection_material(self):
        first, second = self.two_points()
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "current operation"):
            self.service.point_removal_confirmation(second)
        inspection_id, directory = self.synthetic_inspection(first)
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "inspection work"):
            self.service.point_removal_confirmation(first)
        inspection_id, directory = self.synthetic_inspection(second)
        proof = {"id": inspection_id, "sha256": artifacts.regular_hash(directory / "inspection.json")}
        journal = self.service.status()
        journal["capability"] = proof
        self.service.phase(journal, "point-verified", result="succeeded")
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "referenced"):
            self.service.cleanup_inspection(inspection_id, discard_work_confirmed=True)
        journal["capability"] = None
        self.service.phase(journal, "point-verified", result="succeeded")
        record = recovery.read(directory / "inspection.json")
        recovery.write(directory / "inspection.json", dict(record, result="unknown"))
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "Unknown or unfinished"):
            self.service.cleanup_inspection(inspection_id, discard_work_confirmed=True)
        helper = self.service.pin(self.store, self.runner.generation, "helper")
        recovery.write(directory / "inspection.json", dict(record, runner=helper))
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "durable runner reservation"):
            self.service.cleanup_inspection(inspection_id, discard_work_confirmed=True)
        self.assertIn(helper["reference"], self.store._registry()["references"])
        self.assertIn("helper", self.store.removal_reasons(self.runner.sha256))
        self.assertTrue(directory.exists())
        self.service.verify_point(first)
        self.service.verify_point(second)

    def test_retained_storage_includes_historical_point_and_unknown_inspection_without_following_links(self):
        from logos_desktop import runtime_storage

        first, second = self.two_points()
        operation, directory = self.synthetic_inspection(first)
        record = recovery.read(directory / "inspection.json")
        recovery.write(directory / "inspection.json", dict(record, result="unknown"))
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "large").write_bytes(b"x" * 300000)
        (directory / "prefix/external").symlink_to(outside)
        rows = {r.location: r for r in runtime_storage.retained(self.context, checkpoint=lambda: None)}
        for point in (first, second):
            row = rows[str(self.service.root / ("point-" + point) / "prefix")]
            self.assertGreater(row.logical, 0)
            self.assertGreater(row.allocated, 0)
        inspection = rows[str(directory / "prefix")]
        self.assertIn("unknown", inspection.purpose)
        self.assertLess(inspection.logical, 300000)
        self.assertEqual(self.service.inspection_record(operation)["result"], "unknown")

    def test_cancellation_at_point_copy_leaves_original_unchanged_and_keep_current_available(self):
        original = runtime_copies.manifest(self.prefix)
        def cancel(phase):
            if phase == "point-copy":
                raise runtime_copies.Cancelled()

        self.service.checkpoint = cancel
        with self.assertRaises(runtime_copies.Cancelled):
            self.capture()
        journal = self.service.status()
        self.assertEqual(journal["phase"], "preparing")
        self.service.keep_current(expected_revision=journal["revision"])
        self.assertEqual(runtime_copies.manifest(self.prefix), original)
        self.assertEqual(self.profile.read_bytes(), self.portable.read_bytes())

    def test_inspection_copy_observes_close_event_before_any_wine_launch(self):
        point = self.capture()
        original = runtime_copies.manifest(self.prefix)
        stop = threading.Event()
        copy = runtime_copies.copy

        def closing(*args, **options):
            stop.set()
            return copy(*args, **options)

        with (patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
              patch.object(recovery.runtime_boundary, "check_display"),
              patch.object(recovery, "inspection_facts", return_value={}),
              patch.object(runtime_copies, "copy", side_effect=closing),
              patch.object(recovery.runtime_boundary, "command") as launch,
              self.assertRaises(runtime_copies.Cancelled)):
            self.service.inspect_generation(stop=stop, observe=lambda: self.fail("No worker started"))
        launch.assert_not_called()
        self.assertEqual(runtime_copies.manifest(self.prefix), original)
        self.assertEqual(self.service.verify_point(point)["id"], point)
        self.assertIsNone(self.service.status()["capability"])

    def test_point_cleanup_refuses_history_with_retained_generation(self):
        service, _, facts = self.resume_fixture("cleanup-retained-history")
        first = service.status()["point"]
        newer = Path(service.status()["newer"])
        with (patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
              patch.object(recovery, "inspection_facts", return_value=facts)):
            service.resume_online(service.resume_confirmation(), retained_work_acknowledged=True)
        profile = service.root.parent / "profile.json"
        second = service.capture(store=self.store, generation=self.runner.generation, profile=profile,
                                 portable=service.context.install / constants.PORTABLE_CONFIG_NAME,
                                 links=[service.context.install / "data/bin" / link.name for link in self.links],
                                 product_build="fixture-1", effective=recovery.read(profile))
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "Retained generations"):
            service.point_removal_confirmation(first)
        self.assertEqual((newer / "newer-work").read_text(), "preserve retained work")
        service.verify_point(first)
        service.verify_point(second)

    def test_cleanup_withdrawn_directory_replacement_and_nested_mount_preserve_canaries(self):
        for kind in ("replacement", "mount"):
            with self.subTest(kind=kind):
                service, first, second = self.case_points("cleanup-alias-" + kind)
                confirmation = service.point_removal_confirmation(first)

                def interrupt(boundary):
                    if boundary == "after-prefix-rename:point-" + first:
                        raise OSError(5, "interrupted cleanup withdrawal")

                service.checkpoint = interrupt
                with self.assertRaises(OSError):
                    service.remove_point(first, confirmation)
                service.checkpoint = lambda boundary: None
                tombstone = service.root / (".removed-point-" + first)
                if kind == "replacement":
                    tombstone.rename(service.root / ("preserved-point-" + first))
                    tombstone.mkdir()
                    canary = tombstone / "valuable-canary"
                    canary.write_text("do not delete replacement")
                    with self.assertRaises(maintenance.RecoveryRequired):
                        service.remove_point(first, confirmation)
                else:
                    canary = tombstone / "mounted-data/valuable-canary"
                    canary.parent.mkdir()
                    canary.write_text("do not delete mounted data")
                    with (patch.object(runtime_copies, "_mount_paths", return_value=[canary.parent]),
                          self.assertRaisesRegex(maintenance.MaintenanceError, "mount boundary")):
                        service.remove_point(first, confirmation)
                self.assertTrue(canary.read_text().startswith("do not delete"))
                service.verify_point(second)

    def test_point_cleanup_faults_retry_in_fresh_process_and_release_only_bound_references(self):
        cases = [("state", when, occurrence) for occurrence in range(1, 5)
                 for when in ("before-record:", "after-record:")]
        cases += [("index", when, occurrence) for occurrence in (1, 2)
                  for when in ("before-record:", "after-record:")]
        cases += [(kind, when, 1) for kind in ("plan", "point-owner", "journal-owner")
                  for when in ("before-record:", "after-record:")]
        cases += [("delete", "partial", 1), ("release", "acknowledgment", 1)]
        program = """import json
from pathlib import Path
import sys
from logos_desktop import constants, maintenance, runtime_filesystem, runtime_recovery, runtime_runners
root, install = Path(sys.argv[1]), Path(sys.argv[2])
constants.STATE_DIR = str(root / 'alternate-cleanup/state/logos-desktop')
constants.COORDINATION_DIR = root / 'coordination'
def filesystem(path):
    info = Path(path).stat()
    live = runtime_filesystem.live_identity(path)
    return dict(live, canonical_path=str(path), filesystem_uuid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                filesystem_root=str(path), inode=info.st_ino)
runtime_filesystem.identity = filesystem
service = runtime_recovery.Recovery(maintenance.Context(install / 'data/wine64_bottle', install, 'Logos'))
first, second = sys.argv[3:5]
service.remove_point(first, json.loads(sys.argv[5]))
service.verify_point(second)
assert service.status()['point'] == second
assert first in service.point_index()['retired']
assert first not in service.point_index()['points']
assert not (service.root / ('point-' + first)).exists()
assert not (service.root / ('.removed-point-' + first)).exists()
plan = runtime_recovery.read(service.root / ('point-cleanup-plan-' + first + '.json'))
store = runtime_runners.RunnerStore(plan['runtime']['store'], plan['runtime']['coordination'])
references = store._registry()['references']
assert plan['runtime']['reference'] not in references
assert plan['journal']['reference'] not in references
remaining = service.verify_point(second)
assert remaining['runtime']['reference'] in references
print(json.dumps({'retired': first, 'remaining_verified': second}))
"""
        delete = shutil.rmtree
        release = runtime_runners.RunnerStore.release_pin
        for index, (kind, when, occurrence) in enumerate(cases):
            with self.subTest(kind=kind, when=when, occurrence=occurrence):
                service, first, second = self.case_points("cleanup-fault-" + str(index))
                confirmation = service.point_removal_confirmation(first)
                point_reference = service.verify_point(first)["runtime"]
                original = runtime_copies.manifest(service.context.prefix)
                remaining = service.root / ("point-" + second) / "prefix"
                remaining_content = runtime_copies.manifest(remaining)
                plan = service.root / ("point-cleanup-plan-" + first + ".json")
                hits, faults = [], []

                def interrupt(boundary):
                    if kind in ("delete", "release"):
                        return
                    names = {"state": "point-cleanup-state-" + first + ".json", "index": "points.json",
                             "plan": plan.name, "point-owner": Path(point_reference["owner"]).name}
                    if plan.exists():
                        names["journal-owner"] = Path(recovery.read(plan)["journal"]["owner"]).name
                    if boundary == when + names.get(kind, ""):
                        hits.append(boundary)
                        if len(hits) == occurrence:
                            faults.append(kind)
                            raise OSError(5, "cleanup publication interrupted")

                def partial_delete(path, *arguments, **options):
                    if kind == "delete" and Path(path).name == ".removed-point-" + first:
                        (Path(path) / "prefix/database").unlink()
                        faults.append(kind)
                        raise OSError(5, "cleanup stopped after owned leaf deletion")
                    return delete(path, *arguments, **options)

                partial_delete.avoids_symlink_attacks = delete.avoids_symlink_attacks

                def lost_release(store, reference, **options):
                    result = release(store, reference, **options)
                    if kind == "release" and reference == point_reference["reference"]:
                        faults.append(kind)
                        raise OSError(5, "pin release acknowledgment lost")
                    return result

                service.checkpoint = interrupt
                with (patch.object(shutil, "rmtree", side_effect=partial_delete) as removal,
                      patch.object(runtime_runners.RunnerStore, "release_pin", new=lost_release)):
                    removal.avoids_symlink_attacks = delete.avoids_symlink_attacks
                    with self.assertRaises(OSError):
                        service.remove_point(first, confirmation)
                self.assertEqual(faults, [kind])
                self.assertEqual(runtime_copies.manifest(remaining), remaining_content)
                result = subprocess.run([sys.executable, "-B", "-c", program, str(self.root),
                                         str(service.context.install), first, second, json.dumps(confirmation)],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(runtime_copies.manifest(service.context.prefix), original)

    def test_case_insensitive_duplicates_and_unknown_relevant_types_are_refused(self):
        settings = dict(self.prepared_fixture().recipe.settings)
        self.write_registry(self.prefix, settings, lower_case=True)
        original = (self.prefix / "user.reg").read_text()
        for extra in ('\n[Control Panel\\\\Desktop] 123\n"fontSmoothing"="2"\n',
                      '\n[software\\\\wine\\\\direct3d] 123\n"renderer"=str(2):"gl"\n',
                      '\n[Software\\\\Wine\\\\AppDefaults\\\\Logos.exe\\\\Direct3D] 123\n"renderer"="gl"\n',
                      '\n[Software\\\\Wine] 123\n"vERSION"="win10"\n',
                      '\n[Software\\\\Wine] 123\n"Version"=dword:0000000a\n',
                      '\n[Software\\\\Wine\\\\AppDefaults\\\\Logos.exe] 123\n"Version"="win10"\n'):
            with self.subTest(extra=extra):
                (self.prefix / "user.reg").write_text(original + extra)
                with self.assertRaises(maintenance.MaintenanceError):
                    runtime_recipes.observe_prefix_settings(self.prefix)

    def test_windows_version_absence_is_distinct_from_present_unknown_values(self):
        settings = dict(self.prepared_fixture().recipe.settings)
        for value, expected in (("win10", "win10"), ("win11", "win11"), ("win7", None), ("", None), ("absent", None)):
            with self.subTest(value=value):
                self.write_registry(self.prefix, dict(settings, **{"windows-version": value}), lower_case=True)
                self.assertEqual(runtime_recipes.observe_prefix_settings(self.prefix)["windows-version"], expected)
        missing = {key: value for key, value in settings.items() if key != "windows-version"}
        for header in ("", '\n[sOFTWARE\\\\wINE] 123\n"Unrelated"="preserve"\n'):
            with self.subTest(header=header):
                self.write_registry(self.prefix, missing)
                with (self.prefix / "user.reg").open("a") as output:
                    output.write(header)
                self.assertEqual(runtime_recipes.observe_prefix_settings(self.prefix)["windows-version"], "absent")

    def test_reads_do_not_treat_access_time_updates_as_source_mutation(self):
        info = self.profile.stat()
        os.utime(self.profile, ns=(1, info.st_mtime_ns))
        first = recovery.snapshot(self.profile)
        self.assertEqual(first["kind"], "file")
        settings = dict(self.prepared_fixture().recipe.settings)
        self.write_registry(self.prefix, settings)
        registry = self.prefix / "user.reg"
        os.utime(registry, ns=(1, registry.stat().st_mtime_ns))
        self.assertEqual(runtime_recipes.observe_prefix_settings(self.prefix), settings)
        self.assertEqual(recovery.snapshot(self.profile), first)

    def test_shared_profile_with_relative_prefix_is_refused_before_capture(self):
        alias = self.root / "alias-profile.json"
        recovery.write(alias, dict(self.document, WINEPREFIX="data/wine64_bottle"))
        snapshot = recovery.runtime_trials.profile_snapshot(alias)
        recovery.runtime_trials.register_profile(snapshot, self.context)
        before = runtime_copies.manifest(self.prefix)
        with self.assertRaisesRegex(runtime_copies.CopyError, "shared by known saved profiles"):
            self.capture()
        self.assertEqual(runtime_copies.manifest(self.prefix), before)
        self.assertEqual(self.service.status()["phase"], "preparing")

    def test_probe_streams_keep_extraction_diagnostics_separate_from_version(self):
        processes.resume()
        program = "import os\nos.write(1, b'wine-11.0\\n')\nos.write(2, b'extraction diagnostics\\n')"
        output, errors, result = processes.capture(
            [sys.executable, "-c", program],
            env={"PATH": os.environ["PATH"]}, timeout=5, limit=1024, label="Synthetic version probe", separate=True,
        )
        self.assertEqual((output, errors, result), (b"wine-11.0\n", b"extraction diagnostics\n", 0))

    def test_probe_output_budget_covers_both_streams(self):
        processes.resume()
        with self.assertRaisesRegex(processes.CaptureError, "output limit"):
            processes.capture(
                [sys.executable, "-c", "import os\nos.write(1, b'a' * 600)\nos.write(2, b'b' * 600)"],
                env={"PATH": os.environ["PATH"]}, timeout=5, limit=1024, label="Synthetic version probe", separate=True,
            )

    def test_doctor_reports_unknown_when_alternate_root_cannot_inspect_registered_recovery(self):
        self.capture()
        self.service.root.rename(self.root / "retained-but-missing")
        with patch.object(constants, "STATE_DIR", str(self.root / "alternate/state")):
            report = doctor.inspect(self.app_fixture(self.service).conf)
        result = next(item for item in report["checks"] if item["name"] == "recovery")
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("No pending", result["summary"])

    def test_prepared_trial_receipt_cannot_authorize_the_unprepared_inspection_contract(self):
        point_id = self.capture()
        point = self.service.verify_point(point_id)
        inspection_id = str(uuid.uuid4())
        directory = self.service.root / ("inspection-" + inspection_id)
        directory.mkdir()
        record = {
            "schema": "complete-recovery-inspection-1", "id": inspection_id, "generation": "prepared-trial",
            "point": point_id, "result": "succeeded", "reason": "trial-added-icu", "boundary": {},
            "runner": point["runtime"], "source": str(self.prefix), "baseline": point["manifest"]["sha256"],
            "source_observation": point["manifest"]["observation_sha256"], "process": None, "worker": None,
            "observations": dict.fromkeys(("library", "resource", "documents", "restart"), True),
            "directory_identity": runtime_filesystem.identity(directory),
            "completed_monotonic_ns": recovery.time.monotonic_ns(), "copy": None, "restarts": 1,
        }
        recovery.write(directory / "inspection.json", record)
        proof = {"id": inspection_id, "sha256": artifacts.regular_hash(directory / "inspection.json")}
        before = runtime_copies.manifest(self.prefix)
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "inspection record"):
            self.service.verify_capability(proof, point)
        self.assertEqual(runtime_copies.manifest(self.prefix), before)

    def synthetic_inspection(self, point, *, newer=False, service=None):
        service = service or self.service
        inspection_id = str(uuid.uuid4())
        directory = service.root / ("inspection-" + inspection_id)
        directory.mkdir()
        (directory / "synthetic-export.txt").write_text("discard explicitly confirmed")
        source = Path(service.status()["newer"]) if newer else service.context.prefix
        receipt = runtime_copies.copy(source, directory / "prefix", logical_root=service.context.prefix)
        helper = service.pin(self.store, self.runner.generation, "helper")
        owner = recovery.read(helper["owner"])
        owner.update(phase="settled", revision=2, runner_generations=[])
        recovery.write(Path(helper["owner"]), owner)
        self.store.release_pin(helper["reference"], settled_owner_sha256=artifacts.regular_hash(helper["owner"]))
        identity = recovery.runtime_trials.identity(os.getpid())
        worker = {"schema": 1, "operation": inspection_id, "phase": "ended", "reason": "processes-settled",
                  "returncode": 0, "host_identity": identity,
                  "identity": {"pid": identity["pid"], "start_ticks": identity["start_ticks"],
                               "namespaces": identity["namespaces"], "prefix_device": receipt["copy"]["device"],
                               "prefix_inode": receipt["copy"]["inode"]}}
        recovery.write(directory / "inspection.json", {
            "schema": "complete-recovery-inspection-1", "id": inspection_id,
            "generation": "newer" if newer else "old-capability",
            "point": point, "result": "succeeded", "reason": "synthetic-cleanup-fixture", "boundary": {},
            "runner": helper, "source": str(source), "baseline": receipt["manifest"]["sha256"],
            "source_observation": receipt["manifest"]["observation_sha256"], "process": identity, "worker": worker,
            "observations": dict.fromkeys(("library", "resource", "documents", "restart"), True),
            "directory_identity": runtime_filesystem.identity(directory),
            "completed_monotonic_ns": recovery.time.monotonic_ns(), "copy": receipt["copy"], "restarts": 1,
        })
        return inspection_id, directory

    def resume_fixture(self, name):
        probe = patch.object(recovery.runtime_boundary, "check_display")
        probe.start()
        self.addCleanup(probe.stop)
        service = self.case_service(name)
        point = service.status()["point"]
        (service.context.prefix / "newer-work").write_text("preserve retained work")
        service.return_prefix(service.return_confirmation())
        inspection_id, directory = self.synthetic_inspection(point, service=service)
        record = recovery.read(directory / "inspection.json")
        facts = {"fixture": "synthetic-resume-binding"}
        record.update(generation="restored", boundary=facts)
        recovery.write(directory / "inspection.json", record)
        journal = service.status()
        journal["inspection"] = {"id": inspection_id, "sha256": artifacts.regular_hash(directory / "inspection.json")}
        service.phase(journal, "returned", result="succeeded", reason="offline-inspection-complete")
        return service, directory, facts

    def test_resume_requires_confirmation_and_preserves_later_unrelated_settings_and_retained_guard(self):
        service, _, facts = self.resume_fixture("resume-positive")
        profile = service.root.parent / "profile.json"
        document = recovery.read(profile)
        updated = config_store.apply_changes(document, {"faithlife_product_logging": True}, profile)
        recovery.write(profile, updated)
        saved = recovery.snapshot(profile)
        before = runtime_copies.manifest(service.context.prefix)
        newer = Path(service.status()["newer"])
        retained = runtime_copies.manifest(newer, logical_root=service.context.prefix)
        confirmation = service.resume_confirmation()
        with (patch.object(recovery, "ONLINE_RESUME_SUPPORTED", True),
              patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
              patch.object(recovery, "inspection_facts", return_value=facts)):
            with (patch.object(recovery.runtime_boundary, "check_display",
                               side_effect=maintenance.RecoveryRequired("Private compositor unavailable")),
                  self.assertRaisesRegex(maintenance.RecoveryRequired, "compositor")):
                recovery.Recovery(service.context).resume_online(confirmation, retained_work_acknowledged=True)
            self.assertEqual(service.status()["gate"], "online-resume-pending")
            self.assert_blocked(service.context)
            service.resume_online(confirmation, retained_work_acknowledged=True)
            result = service.status()
            self.assertEqual((result["phase"], result["gate"]), ("resumed", "online-resumed"))
            self.assertTrue(result["resume"]["retained_work_acknowledged"])
            self.assertEqual(result["resume"]["confirmation"], confirmation)
            self.assertFalse(service.context.pending.exists())
            maintenance.Lease(service.context).close()
            maintenance.Lease(service.context, recovery=True).close()
            self.assert_blocked(maintenance.Context(newer, service.context.install, "Logos"))
            self.assertEqual(runtime_copies.manifest(service.context.prefix), before)
            self.assertEqual(recovery.snapshot(profile), saved)
            current_retained = runtime_copies.manifest(newer, logical_root=service.context.prefix)
            self.assertEqual(current_retained, retained)
            self.assertEqual(current_retained.observation_sha256, retained.observation_sha256)
            (service.context.prefix / "post-resume-work").write_text("legitimate later online work")
            service.resume_online(confirmation, retained_work_acknowledged=True)
            self.assertEqual(service.status()["resume"], result["resume"])
            self.assertEqual((service.context.prefix / "post-resume-work").read_text(), "legitimate later online work")

    def test_resume_refuses_binding_and_launch_drift_without_mutating_either_generation(self):
        cases = ("ack", "confirmation", "missing-inspection", "receipt", "unknown", "point", "runtime",
                 "observations", "prefix", "source-observation", "profile-runtime", "portable-prefix",
                 "profile-install", "profile-version", "profile-code", "product",
                 "legacy-alias", "effective-override", "legacy-override", "runtime-link", "graph-missing",
                 "graph-next", "boundary", "prerequisite", "runner-pin")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                service, directory, facts = self.resume_fixture("resume-refusal-" + str(index))
                journal = service.status()
                profile = service.root.parent / "profile.json"
                portable = service.context.install / constants.PORTABLE_CONFIG_NAME
                original_profile = recovery.read(profile)
                foreign = self.root / ("foreign-runtime-" + str(index))
                foreign.write_bytes(b"unrelated executable")
                current_facts = facts
                if case in ("unknown", "point", "runtime", "observations"):
                    record = recovery.read(directory / "inspection.json")
                    if case == "unknown":
                        record["result"] = "unknown"
                    elif case == "point":
                        record["point"] = str(uuid.uuid4())
                    elif case == "runtime":
                        record["runner"]["generation"] = str(uuid.uuid4())
                    else:
                        record["observations"]["documents"] = False
                    recovery.write(directory / "inspection.json", record)
                    journal["inspection"]["sha256"] = artifacts.regular_hash(directory / "inspection.json")
                elif case == "receipt":
                    record = recovery.read(directory / "inspection.json")
                    recovery.write(directory / "inspection.json", dict(record, reason="changed"))
                elif case == "missing-inspection":
                    journal["inspection"] = None
                elif case == "prefix":
                    (service.context.prefix / "changed-work").write_text("changed after inspection")
                elif case == "source-observation":
                    path = service.context.prefix / "database"
                    info = path.stat()
                    content = path.read_bytes()
                    path.write_bytes(content)
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                elif case in ("profile-runtime", "portable-prefix", "profile-install", "profile-version",
                              "profile-code", "product", "legacy-alias", "effective-override", "legacy-override"):
                    path = portable if case == "portable-prefix" else profile
                    document = recovery.read(path)
                    if case == "profile-runtime":
                        document = config_store.apply_changes(document, {"wine_binary": str(foreign)}, path)
                    elif case == "portable-prefix":
                        document["WINEPREFIX"] = str(self.root / "foreign-prefix")
                    elif case == "profile-install":
                        document = config_store.apply_changes(
                            document, {"install_dir": str(self.root / "foreign")}, path,
                        )
                    elif case == "profile-version":
                        document = config_store.apply_changes(document, {"faithlife_product_version": "11"}, path)
                    elif case == "profile-code":
                        document = config_store.apply_changes(document, {"wine_binary_code": "Custom"}, path)
                    elif case == "product":
                        document["faithlife_product"] = document["FLPRODUCT"] = "Verbum"
                    elif case == "legacy-alias":
                        document["WINE_EXE"] = str(foreign)
                    elif case == "effective-override":
                        document["wine_dll_overrides"] = "d3d=n"
                    else:
                        document["WINEDLLOVERRIDES"] = "d3d=n"
                    recovery.write(path, document)
                elif case == "runtime-link":
                    link = service.context.install / "data/bin/selected_wine.AppImage"
                    link.unlink()
                    link.symlink_to(foreign)
                elif case == "graph-missing":
                    journal["configs"].pop()
                elif case == "graph-next":
                    journal["configs"][0]["next"]["content"] = runtime_copies.encoded(b"foreign configuration")
                elif case == "boundary":
                    current_facts = dict(facts, changed=True)
                elif case == "runner-pin":
                    self.runner.path.unlink()
                recovery.write(service.root / "journal.json", journal)
                confirmation = service.resume_confirmation() if case != "runner-pin" else {}
                if case == "confirmation":
                    confirmation = dict(confirmation, retained="different-generation")
                before = runtime_copies.manifest(service.context.prefix)
                newer = Path(service.status()["newer"])
                retained = runtime_copies.manifest(newer, logical_root=service.context.prefix)
                configurations = [recovery.snapshot(path) for path in (profile, portable)]
                inspection_error = recovery.runtime_boundary.Unavailable("missing private display")
                with (patch.object(recovery, "ONLINE_RESUME_SUPPORTED", True),
                      patch.object(recovery.runtime_boundary, "inspect", return_value=object(),
                                   side_effect=inspection_error if case == "prerequisite" else None),
                      patch.object(recovery, "inspection_facts", return_value=current_facts),
                      self.assertRaises((maintenance.MaintenanceError, artifacts.ArtifactError, OSError))):
                    service.resume_online(confirmation, retained_work_acknowledged=case != "ack")
                self.assertEqual(service.status()["phase"], "returned")
                self.assertEqual(runtime_copies.manifest(service.context.prefix), before)
                after_retained = runtime_copies.manifest(newer, logical_root=service.context.prefix)
                self.assertEqual(after_retained, retained)
                self.assertEqual(after_retained.observation_sha256, retained.observation_sha256)
                self.assertEqual([recovery.snapshot(Path(item["path"])) for item in configurations], configurations)
                self.assert_blocked(service.context)
                self.assert_blocked(maintenance.Context(newer, service.context.install, "Logos"))
                if case == "product":
                    recovery.write(profile, original_profile)

    def test_resume_boundaries_keep_legacy_marker_until_commit_and_retry_without_losing_work(self):
        boundaries = ("before-phase:resume-intent", "after-phase:resume-intent", "before-phase:resumed",
                      "after-phase:resumed", "before-marker-withdrawal", "after-marker-withdrawal",
                      "after-marker-directory-sync", "after-record-rename:journal.json")
        for index, target in enumerate(boundaries):
            with self.subTest(boundary=target):
                service, _, facts = self.resume_fixture("resume-boundary-" + str(index))
                confirmation = service.resume_confirmation()
                before = runtime_copies.manifest(service.context.prefix)
                newer = Path(service.status()["newer"])
                retained = runtime_copies.manifest(newer, logical_root=service.context.prefix)
                fired = False

                def interrupt(boundary):
                    nonlocal fired
                    if boundary != target or fired:
                        return
                    if target == "after-record-rename:journal.json" and service.status()["phase"] != "resumed":
                        return
                    fired = True
                    raise OSError(5, "injected resume interruption")

                service.checkpoint = interrupt
                with (patch.object(recovery, "ONLINE_RESUME_SUPPORTED", True),
                      patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
                      patch.object(recovery, "inspection_facts", return_value=facts)):
                    with self.assertRaises(OSError):
                        service.resume_online(confirmation, retained_work_acknowledged=True)
                    self.assertTrue(fired)
                    interrupted = service.status()
                    if service.context.pending.exists():
                        self.assert_blocked(service.context)
                    if interrupted["phase"] != "resumed":
                        self.assertTrue(service.context.pending.exists())
                        self.assert_blocked(service.context)
                        with (patch.object(constants, "STATE_DIR", str(self.root / "alternate/logos-desktop")),
                              self.assertRaises(maintenance.RecoveryRequired)):
                            maintenance.Lease(service.context).close()
                    self.assert_blocked(maintenance.Context(newer, service.context.install, "Logos"))
                    reopened = recovery.Recovery(service.context)
                    reopened.resume_online(confirmation, retained_work_acknowledged=True)
                    final = reopened.status()
                    self.assertEqual((final["phase"], final["gate"]), ("resumed", "online-resumed"))
                    self.assertFalse(service.context.pending.exists())
                    if interrupted["resume"] is not None:
                        self.assertEqual(final["resume"], interrupted["resume"])
                    maintenance.Lease(service.context).close()
                    maintenance.Lease(service.context, recovery=True).close()
                    self.assert_blocked(maintenance.Context(newer, service.context.install, "Logos"))
                    self.assertEqual(runtime_copies.manifest(service.context.prefix), before)
                    after = runtime_copies.manifest(newer, logical_root=service.context.prefix)
                    self.assertEqual(after, retained)
                    self.assertEqual(after.observation_sha256, retained.observation_sha256)

    def test_post_commit_marker_sync_failure_is_reconciled_without_repeating_the_acknowledgment(self):
        service, _, facts = self.resume_fixture("resume-marker-sync")
        confirmation = service.resume_confirmation()
        sync = recovery.sync

        def fail_sync(path):
            if Path(path) == service.context.state:
                raise OSError(5, "injected marker directory sync failure")
            return sync(path)

        with (patch.object(recovery, "ONLINE_RESUME_SUPPORTED", True),
              patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
              patch.object(recovery, "inspection_facts", return_value=facts)):
            with patch.object(recovery, "sync", side_effect=fail_sync), self.assertRaises(OSError):
                service.resume_online(confirmation, retained_work_acknowledged=True)
            committed = service.status()
            self.assertEqual(committed["phase"], "resumed")
            service.resume_online(confirmation, retained_work_acknowledged=True)
            self.assertEqual(service.status()["resume"], committed["resume"])
            newer = Path(service.status()["newer"])
            self.assert_blocked(maintenance.Context(newer, service.context.install, "Logos"))

    def test_resume_acknowledgment_corruption_blocks_every_admission_reader(self):
        cases = ("missing", "ack", "confirmation", "time", "unknown-field", "gate", "returned-record", "resumed-ack")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                service, _, facts = self.resume_fixture("resume-record-" + str(index))
                confirmation = service.resume_confirmation()

                def interrupt(boundary):
                    if boundary == "after-phase:resume-intent":
                        raise OSError(5, "stop after durable intent")

                service.checkpoint = interrupt
                with (patch.object(recovery, "ONLINE_RESUME_SUPPORTED", True),
                      patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
                      patch.object(recovery, "inspection_facts", return_value=facts), self.assertRaises(OSError)):
                    service.resume_online(confirmation, retained_work_acknowledged=True)
                journal = service.status()
                newer = Path(journal["newer"])
                if case == "missing":
                    journal["resume"] = None
                elif case == "ack":
                    journal["resume"]["retained_work_acknowledged"] = False
                elif case == "confirmation":
                    journal["resume"]["confirmation"]["active"] = str(self.root / "foreign")
                elif case == "time":
                    journal["resume"]["confirmed"] = "2026-11-12T00:00:00"
                elif case == "unknown-field":
                    journal["resume"]["foreign"] = True
                elif case == "gate":
                    journal["gate"] = "online-resumed"
                elif case == "returned-record":
                    journal.update(phase="returned", result="succeeded")
                else:
                    journal.update(phase="resumed", gate="online-resumed", result="succeeded")
                    journal["resume"]["retained_work_acknowledged"] = False
                recovery.write(service.root / "journal.json", journal)
                self.assert_blocked(service.context)
                self.assert_blocked(maintenance.Context(newer, service.context.install, "Logos"))
                with (patch.object(recovery, "ONLINE_RESUME_SUPPORTED", True),
                      self.assertRaises(maintenance.MaintenanceError)):
                    recovery.Recovery(service.context).resume_online(confirmation, retained_work_acknowledged=True)

    def fresh_process_resume(self, target, name, *, sync_failure=None):
        service, _, facts = self.resume_fixture(name)
        confirmation = service.resume_confirmation()

        def interrupt(boundary):
            if boundary == target:
                raise OSError(5, "committed resume before marker cleanup")

        service.checkpoint = interrupt
        original_sync = recovery.sync

        def fail_sync(path):
            journal_failure = (sync_failure == "journal" and Path(path) == service.root
                               and recovery.read(service.root / "journal.json")["phase"] == "resumed")
            marker_failure = sync_failure == "marker" and Path(path) == service.context.state
            if journal_failure or marker_failure:
                raise OSError(5, "injected resume directory durability failure")
            original_sync(path)

        with (patch.object(recovery, "ONLINE_RESUME_SUPPORTED", True),
              patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
              patch.object(recovery, "inspection_facts", return_value=facts),
              patch.object(recovery, "sync", side_effect=fail_sync), self.assertRaises(OSError)):
            service.resume_online(confirmation, retained_work_acknowledged=True)
        committed = service.status()
        self.assertIn(committed["phase"], ("resume-intent", "resumed"))
        self.assertEqual(service.context.pending.exists(), sync_failure != "marker")
        if service.context.pending.exists():
            self.assert_blocked(service.context)
        program = """import json
import os
from pathlib import Path
import sys
from logos_desktop import constants, maintenance, runtime_boundary, runtime_filesystem, runtime_recovery
root, install = Path(sys.argv[1]), Path(sys.argv[2])
constants.STATE_DIR = str(root / 'alternate/logos-desktop')
constants.COORDINATION_DIR = root / 'coordination'
def filesystem(path):
    info = Path(path).stat()
    live = runtime_filesystem.live_identity(path)
    return dict(live, canonical_path=str(path), filesystem_uuid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                filesystem_root=str(path), inode=info.st_ino)
runtime_filesystem.identity = filesystem
runtime_recovery.ONLINE_RESUME_SUPPORTED = True
runtime_boundary.inspect = lambda roots: object()
runtime_boundary.check_display = lambda boundary, source: None
runtime_recovery.inspection_facts = lambda boundary: json.loads(sys.argv[4])
context = maintenance.Context(install / 'data/wine64_bottle', install, 'Logos')
service = runtime_recovery.Recovery(context)
marker = runtime_recovery.read(service.root / 'marker.json')
marker_parent = Path(marker['path']).parent
sync = runtime_recovery.sync
events = []
def durable_sync(path):
    result = sync(path)
    if Path(path) == service.root:
        events.append('journal')
    if Path(path) == marker_parent:
        if 'journal' not in events:
            raise RuntimeError('Marker cleanup preceded durable journal reaffirmation')
        events.append('marker')
    return result
runtime_recovery.sync = durable_sync
service.resume_online(json.loads(sys.argv[3]), retained_work_acknowledged=True)
maintenance.Lease(context).close()
newer = Path(service.status()['newer'])
try:
    maintenance.Lease(maintenance.Context(newer, install, 'Logos')).close()
except maintenance.RecoveryRequired:
    pass
else:
    raise RuntimeError('Retained newer admission was released')
if 'journal' not in events or 'marker' not in events:
    raise RuntimeError('Resume retry did not synchronize both durability domains')
print(json.dumps({'phase': service.status()['phase'], 'resume': service.status()['resume'], 'durability': events}))
"""
        result = subprocess.run([sys.executable, "-B", "-c", program, str(self.root), str(service.context.install),
                                 json.dumps(confirmation), json.dumps(facts)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["resume"], committed["resume"])
        events = json.loads(result.stdout)["durability"]
        self.assertLess(events.index("journal"), events.index("marker"))
        self.assertFalse(service.context.pending.exists())
        newer = Path(service.status()["newer"])
        self.assert_blocked(maintenance.Context(newer, service.context.install, "Logos"))

    def test_fresh_process_reconciles_intent_and_committed_marker_under_alternate_state(self):
        for index, boundary in enumerate(("after-phase:resume-intent", "before-marker-withdrawal")):
            with self.subTest(boundary=boundary):
                self.fresh_process_resume(boundary, "resume-fresh-process-" + str(index))

    def test_fresh_process_reaffirms_visible_commit_before_marker_withdrawal_after_journal_fsync_failure(self):
        self.fresh_process_resume("unused-boundary", "resume-journal-fsync", sync_failure="journal")

    def test_fresh_process_synchronizes_prior_marker_removal_after_marker_fsync_failure(self):
        self.fresh_process_resume("unused-boundary", "resume-marker-fsync", sync_failure="marker")

    def test_successful_unreferenced_inspection_cleanup_is_restartable_and_preserves_last_point(self):
        point = self.capture()
        inspection_id, directory = self.synthetic_inspection(point)

        def interrupt(boundary):
            if boundary == "after-prefix-rename:" + directory.name:
                raise OSError(5, "injected cleanup acknowledgment loss")

        self.service.checkpoint = interrupt
        with self.assertRaises(OSError):
            self.service.cleanup_inspection(inspection_id, discard_work_confirmed=True)
        reopened = recovery.Recovery(self.context)
        reopened.cleanup_inspection(inspection_id, discard_work_confirmed=True)
        self.assertFalse(directory.exists())
        self.assertFalse((self.service.root / (".removed-inspection-" + inspection_id)).exists())
        reopened.verify_point(point)
        self.assertIn("return-point", self.store.removal_reasons(self.runner.sha256))

    def test_nonzero_application_outcome_cannot_be_imported_as_successful_capability(self):
        point_id = self.capture()
        inspection_id, directory = self.synthetic_inspection(point_id)
        record = recovery.read(directory / "inspection.json")
        record["worker"]["returncode"] = 23
        recovery.write(directory / "inspection.json", record)
        proof = {"id": inspection_id, "sha256": artifacts.regular_hash(directory / "inspection.json")}
        before = runtime_copies.manifest(self.prefix)
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "failed or incomplete application"):
            self.service.verify_capability(proof, self.service.verify_point(point_id))
        self.assertEqual(runtime_copies.manifest(self.prefix), before)
        self.assertIsNone(self.service.status()["capability"])
        self.assertIn("return-point", self.store.removal_reasons(self.runner.sha256))

    def test_completed_inspection_plain_text_export_preserves_generations_and_refuses_overwrite(self):
        point = self.capture()
        self.service.return_prefix(self.service.return_confirmation())
        inspection_id, directory = self.synthetic_inspection(point, newer=True)
        exports = directory / "prefix/drive_c/M4Export"
        exports.mkdir()
        (exports / "M4-newer.txt").write_text("M4 newer document\nNotes: retained body\n")
        destination = self.root / "M4-exported-newer.txt"
        before = runtime_copies.manifest(self.prefix)
        result = self.service.export_file(inspection_id, "M4-newer.txt", destination)
        self.assertEqual(result.read_text(), "M4 newer document\nNotes: retained body\n")
        with self.assertRaises(OSError):
            self.service.export_file(inspection_id, "M4-newer.txt", destination)
        self.assertEqual(destination.read_text(), "M4 newer document\nNotes: retained body\n")
        self.assertEqual(runtime_copies.manifest(self.prefix), before)
        self.service.verify_point(point)
        self.assert_blocked()

    def test_export_refuses_database_bytes_and_nontext_formats(self):
        point = self.capture()
        self.service.return_prefix(self.service.return_confirmation())
        inspection_id, directory = self.synthetic_inspection(point)
        exports = directory / "prefix/drive_c/M4Export"
        exports.mkdir()
        (exports / "disguised.txt").write_bytes(b"SQLite format 3\0database bytes")
        with self.assertRaisesRegex(maintenance.RecoveryRequired, "plain UTF-8 text"):
            self.service.export_file(inspection_id, "disguised.txt", self.root / "database.txt")
        with self.assertRaises(maintenance.RecoveryRequired):
            self.service.export_file(inspection_id, "database.db", self.root / "database.db")
        self.assertFalse((self.root / "database.txt").exists())

    def export_fixture(self):
        point = self.capture()
        (self.prefix / "new-document").write_text("newer retained document")
        self.service.return_prefix(self.service.return_confirmation())
        inspection_id, directory = self.synthetic_inspection(point, newer=True)
        exports = directory / "prefix/drive_c/M4Export"
        exports.mkdir()
        (exports / "M4-newer.txt").write_text("M4 retained text\n")
        return inspection_id, directory, exports

    def protected_export_manifests(self):
        return {path: runtime_copies.manifest(path, logical_root=self.prefix) for path in (
            self.prefix, Path(self.service.status()["newer"]),
            self.service.root / ("point-" + self.service.status()["point"]) / "prefix",
        )}

    def assert_export_preserved(self, before):
        for path, manifest in before.items():
            current = runtime_copies.manifest(path, logical_root=self.prefix)
            self.assertEqual(current, manifest)
            self.assertEqual(current.observation_sha256, manifest.observation_sha256)
        self.assert_blocked()

    def test_export_refuses_physical_alias_of_each_protected_root(self):
        inspection_id, _, _ = self.export_fixture()
        parent = self.root / "alias"
        parent.mkdir()
        original = runtime_filesystem.identity.side_effect
        before = self.protected_export_manifests()
        for protected in (self.service.root, self.install, Path(self.service.status()["newer"])):
            with self.subTest(protected=protected):
                def aliased(path):
                    facts = original(path)
                    if Path(path) == parent:
                        facts["filesystem_root"] = str(protected / "bound-subdirectory")
                    return facts

                with patch.object(runtime_filesystem, "identity", side_effect=aliased):
                    with self.assertRaisesRegex(maintenance.RecoveryRequired, "overlaps"):
                        self.service.export_file(inspection_id, "M4-newer.txt", parent / "document.txt")
                self.assertEqual(list(parent.iterdir()), [])
                self.assert_export_preserved(before)

    def test_export_refuses_direct_protected_destinations_and_linked_ancestry(self):
        inspection_id, _, _ = self.export_fixture()
        before = self.protected_export_manifests()
        link = self.root / "linked"
        link.symlink_to(self.prefix, target_is_directory=True)
        for parent in (self.service.root, self.install, Path(self.service.status()["newer"]), self.prefix, link):
            with self.subTest(parent=parent):
                with self.assertRaises(maintenance.MaintenanceError):
                    self.service.export_file(inspection_id, "M4-newer.txt", parent / "document.txt")
                self.assertFalse((parent / "document.txt").exists())
                self.assert_export_preserved(before)

    def test_export_refuses_replaced_completed_inspection_roots(self):
        inspection_id, directory, _ = self.export_fixture()
        before = self.protected_export_manifests()
        destination = self.root / "document.txt"
        for root in (directory / "prefix", directory):
            with self.subTest(root=root):
                displaced = root.with_name(root.name + "-displaced")
                root.rename(displaced)
                shutil.copytree(displaced, root)
                try:
                    with self.assertRaisesRegex(maintenance.RecoveryRequired, "identity changed"):
                        self.service.export_file(inspection_id, "M4-newer.txt", destination)
                    self.assertFalse(destination.exists())
                    self.assert_export_preserved(before)
                finally:
                    shutil.rmtree(root)
                    displaced.rename(root)

    def test_export_refuses_mount_introduced_inside_inspection(self):
        inspection_id, directory, exports = self.export_fixture()
        before = self.protected_export_manifests()
        for mount in (directory / "prefix", exports):
            with self.subTest(mount=mount):
                with patch.object(runtime_copies, "_mount_paths", return_value=[str(mount)]):
                    with self.assertRaisesRegex(runtime_copies.CopyError, "mount boundary"):
                        self.service.export_file(inspection_id, "M4-newer.txt", self.root / "document.txt")
                self.assertFalse((self.root / "document.txt").exists())
                self.assert_export_preserved(before)

    def test_export_refuses_parent_replacement_after_capacity_check(self):
        inspection_id, _, exports = self.export_fixture()
        before = self.protected_export_manifests()
        output = self.root / "output"
        output.mkdir()
        destination = output / "document.txt"
        for parent in (output, exports):
            with self.subTest(parent=parent):
                displaced = parent.with_name(parent.name + "-displaced")

                def replace_parent(*args):
                    parent.rename(displaced)
                    parent.symlink_to(self.prefix, target_is_directory=True)

                try:
                    with patch.object(runtime_copies, "require_capacity", side_effect=replace_parent):
                        with self.assertRaises(maintenance.MaintenanceError):
                            self.service.export_file(inspection_id, "M4-newer.txt", destination)
                    self.assertFalse(destination.exists())
                    self.assertFalse((self.prefix / "document.txt").exists())
                    self.assertFalse(list(displaced.glob(".export-*")))
                    self.assert_export_preserved(before)
                finally:
                    parent.unlink()
                    displaced.rename(parent)

    def test_export_refuses_unfinished_inspection_and_linked_or_oversized_file(self):
        inspection_id, directory, exports = self.export_fixture()
        before = self.protected_export_manifests()
        record = recovery.read(directory / "inspection.json")
        for result in ("pending", "unknown"):
            recovery.write(directory / "inspection.json", dict(record, result=result))
            with self.assertRaisesRegex(maintenance.RecoveryRequired, "Finish"):
                self.service.export_file(inspection_id, "M4-newer.txt", self.root / "document.txt")
        recovery.write(directory / "inspection.json", record)
        (exports / "linked.txt").symlink_to(exports / "M4-newer.txt")
        os.link(exports / "M4-newer.txt", exports / "hardlinked.txt")
        with (exports / "oversized.txt").open("wb") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        for name in ("linked.txt", "hardlinked.txt", "oversized.txt"):
            with self.subTest(name=name):
                with self.assertRaises((OSError, maintenance.RecoveryRequired)):
                    self.service.export_file(inspection_id, name, self.root / "document.txt")
                self.assertFalse((self.root / "document.txt").exists())
                self.assert_export_preserved(before)

    def test_export_cleans_held_temporary_on_fsync_and_publication_failures(self):
        inspection_id, _, _ = self.export_fixture()
        before = self.protected_export_manifests()
        output = self.root / "output"
        output.mkdir()
        for target in ("os.fsync", "paths.rename_no_replace"):
            with self.subTest(target=target):
                with patch("logos_desktop.runtime_recovery." + target, side_effect=OSError(28, "injected failure")):
                    with self.assertRaises(OSError):
                        self.service.export_file(inspection_id, "M4-newer.txt", output / "document.txt")
                self.assertEqual(list(output.iterdir()), [])
                self.assert_export_preserved(before)

    def test_export_refuses_source_mutation_during_staging(self):
        inspection_id, _, exports = self.export_fixture()
        before = self.protected_export_manifests()
        output = self.root / "output"
        output.mkdir()
        original_fsync = os.fsync

        def mutate_source(descriptor):
            original_fsync(descriptor)
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                (exports / "M4-newer.txt").write_text("M4 changed export\n")

        with patch.object(os, "fsync", side_effect=mutate_source):
            with self.assertRaisesRegex(maintenance.RecoveryRequired, "changed during copying"):
                self.service.export_file(inspection_id, "M4-newer.txt", output / "document.txt")
        self.assertEqual(list(output.iterdir()), [])
        self.assert_export_preserved(before)

    def test_export_cleans_held_temporary_when_parent_changes_after_staging(self):
        inspection_id, _, _ = self.export_fixture()
        before = self.protected_export_manifests()
        output = self.root / "output"
        output.mkdir()
        displaced = self.root / "output-displaced"
        original_fsync = os.fsync

        def replace_parent(descriptor):
            original_fsync(descriptor)
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                output.rename(displaced)
                output.symlink_to(self.prefix, target_is_directory=True)

        with patch.object(os, "fsync", side_effect=replace_parent):
            with self.assertRaises(maintenance.MaintenanceError):
                self.service.export_file(inspection_id, "M4-newer.txt", output / "document.txt")
        self.assertEqual(list(displaced.iterdir()), [])
        self.assertFalse((self.prefix / "document.txt").exists())
        self.assert_export_preserved(before)

    def test_export_directory_fsync_failure_preserves_published_output_and_refuses_retry(self):
        inspection_id, _, _ = self.export_fixture()
        before = self.protected_export_manifests()
        destination = self.root / "document.txt"
        original_fsync = os.fsync

        def fail_directory_sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(5, "injected directory durability failure")
            original_fsync(descriptor)

        with patch.object(os, "fsync", side_effect=fail_directory_sync):
            with self.assertRaises(OSError):
                self.service.export_file(inspection_id, "M4-newer.txt", destination)
        self.assertEqual(destination.read_text(), "M4 retained text\n")
        self.assertFalse(list(self.root.glob(".export-*")))
        with self.assertRaises(FileExistsError):
            self.service.export_file(inspection_id, "M4-newer.txt", destination)
        self.assertEqual(destination.read_text(), "M4 retained text\n")
        self.assert_export_preserved(before)

    def test_inspection_service_rejects_nonzero_application_even_when_namespace_exits_zero(self):
        self.capture()
        before = runtime_copies.manifest(self.prefix)
        settled = False
        events = iter(("prepared", "running", "ended"))
        child = Mock(pid=os.getpid(), returncode=0)
        child.poll.side_effect = lambda: 0 if settled else None

        def wait(timeout=None):
            nonlocal settled
            settled = True
            return 0

        child.wait.side_effect = wait

        def worker_status(private, process, logical, copied, receipt, operation):
            phase = next(events)
            identity = recovery.runtime_trials.identity(os.getpid())
            value = {"schema": 1, "operation": operation, "phase": phase,
                     "reason": "processes-settled" if phase == "ended" else "restart-1" if phase == "running" else "",
                     "returncode": 23 if phase == "ended" else None,
                     "identity": {"pid": identity["pid"], "start_ticks": identity["start_ticks"],
                                  "namespaces": identity["namespaces"], "prefix_device": copied.stat().st_dev,
                                  "prefix_inode": copied.stat().st_ino}}
            recovery.runtime_trials.write(private / "worker-status.json", value)
            return dict(value, host_identity=identity)

        boundary = recovery.runtime_boundary.Boundary("/usr/bin/bwrap", self.root / "display", ())
        observer = Mock(return_value=dict.fromkeys(("library", "resource", "documents", "restart"), True))
        with (patch.object(recovery.runtime_boundary, "inspect", return_value=boundary),
              patch.object(recovery.runtime_boundary, "probe"),
              patch.object(recovery, "inspection_facts", return_value={"fixture": True}),
              patch.object(recovery.subprocess, "Popen", return_value=child),
              patch.object(recovery.runtime_trials, "worker_status", side_effect=worker_status),
              self.assertRaisesRegex(maintenance.RecoveryRequired, "did not settle successfully")):
            self.service.inspect_generation(observe=observer)
        observer.assert_not_called()
        self.assertIsNone(self.service.status()["capability"])
        self.assertEqual(runtime_copies.manifest(self.prefix), before)
        self.assertIn("helper", self.store.removal_reasons(self.runner.sha256))

    def exercise_live_inspection_retention(self, *, timeout=3600, publication_failure=False, final_write_failure=False):
        self.capture()
        before = runtime_copies.manifest(self.prefix)
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def settle_fixture():
            child.stdin.close()
            child.wait(timeout=5)

        self.addCleanup(settle_fixture)
        phases = iter(("prepared", "running", "unknown", "ended"))
        reports = []
        failed_writes = []
        stop = threading.Event()
        original_write = recovery.write

        def publish(path, value, **options):
            if (publication_failure and path.name == "inspection.json" and value["worker"] is not None
                    and value["result"] == "pending" and not failed_writes):
                failed_writes.append(True)
                stop.set()
                raise OSError("synthetic inspection publication failure")
            if (final_write_failure and path.name == "inspection.json" and value["worker"] is not None
                    and value["worker"]["phase"] == "ended"):
                raise OSError("synthetic final receipt failure")
            return original_write(path, value, **options)

        def worker_status(private, process, logical, copied, receipt, operation):
            phase = next(phases)
            identity = recovery.runtime_trials.identity(child.pid)
            value = {"schema": 1, "operation": operation, "phase": phase,
                     "reason": {"prepared": "", "running": "restart-1",
                                "unknown": "shutdown-timeout", "ended": "processes-settled"}[phase],
                     "returncode": 0 if phase == "ended" else None,
                     "identity": {"pid": identity["pid"], "start_ticks": identity["start_ticks"],
                                  "namespaces": identity["namespaces"], "prefix_device": copied.stat().st_dev,
                                  "prefix_inode": copied.stat().st_ino}}
            recovery.runtime_trials.write(private / "worker-status.json", value)
            if phase == "ended":
                child.stdin.close()
            return dict(value, host_identity=identity)

        def status(value):
            fresh = recovery.Recovery(self.service.context).inspection_record(value["id"])
            self.assertEqual(fresh, value)
            self.assertIsNone(fresh["observations"])
            if value["worker"]["phase"] != "ended":
                self.assertIsNone(child.poll())
            if publication_failure:
                request = self.service.root / ("inspection-" + value["id"]) / "private/request.json"
                self.assertEqual(recovery.runtime_trials.read(request), {"action": "stop"})
            self.assertIn("helper", self.store.removal_reasons(self.runner.sha256))
            reports.append(fresh)

        boundary = recovery.runtime_boundary.Boundary("/usr/bin/bwrap", self.root / "display", ())
        observer = Mock()
        expected_error = OSError if publication_failure else maintenance.RecoveryRequired
        with (patch.object(recovery.runtime_boundary, "inspect", return_value=boundary),
              patch.object(recovery.runtime_boundary, "probe"),
              patch.object(recovery, "inspection_facts", return_value={"fixture": True}),
              patch.object(recovery.subprocess, "Popen", return_value=child),
              patch.object(recovery.runtime_trials, "worker_status", side_effect=worker_status),
              patch.object(recovery, "write", side_effect=publish),
              self.assertRaises(expected_error) as raised):
            self.service.inspect_generation(observe=observer, status=status, timeout=timeout, stop=stop)
        if publication_failure:
            self.assertEqual(str(raised.exception), "synthetic inspection publication failure")
        self.assertTrue(any(value["result"] == "unknown" for value in reports))
        self.assertTrue(any(value["worker"]["reason"] == "shutdown-timeout" for value in reports))
        first_unknown = next(index for index, value in enumerate(reports) if value["result"] == "unknown")
        self.assertTrue(all(value["result"] == "unknown" for value in reports[first_unknown:]))
        if not publication_failure:
            self.assertEqual(reports[-1]["restarts"], 1)
        if timeout < 1:
            self.assertTrue(any(value["reason"] == "inspection-timeout" for value in reports))
        self.assertEqual(len(failed_writes), int(publication_failure))
        observer.assert_not_called()
        self.assertIsNone(self.service.status()["capability"])
        self.assertEqual(runtime_copies.manifest(self.prefix), before)
        self.assertIn("helper", self.store.removal_reasons(self.runner.sha256))

    def test_live_unknown_worker_identity_and_restart_progress_are_durable(self):
        self.exercise_live_inspection_retention()

    def test_reporting_timeout_retains_live_child_and_cannot_later_publish_success(self):
        self.exercise_live_inspection_retention(timeout=0.1)

    def test_inspection_publication_failure_retains_child_until_verified_settlement(self):
        self.exercise_live_inspection_retention(publication_failure=True)

    def test_final_receipt_failure_preserves_original_error_after_live_retention(self):
        self.exercise_live_inspection_retention(publication_failure=True, final_write_failure=True)

    def fresh_process_return_durability(self, target):
        self.profile = self.root / "profile-durability/profile.json"
        self.profile.parent.mkdir()
        recovery.write(self.profile, self.document)
        self.capture()
        self.activate_fixture()
        (self.prefix / "database").write_text("valuable newer bytes")
        failed_parent = self.prefix.parent if target == "prefix" else self.profile.parent
        original_fsync = os.fsync
        failed = []

        def fail_publication_sync(descriptor):
            info = os.fstat(descriptor)
            parent = failed_parent.stat()
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
                journal = recovery.read(self.service.root / "journal.json")
                prefix_published = (target == "prefix" and journal["phase"] == "publish-intent"
                                    and self.prefix.exists() and not Path(journal["stage"]).exists())
                profile_published = (target == "profile" and journal["phase"] == "config-pending"
                                     and any(item["next"]["path"] == str(self.profile)
                                             and item["previous"] != item["next"]
                                             and recovery.snapshot(self.profile) == item["next"]
                                             for item in journal["configs"]))
                if prefix_published or profile_published:
                    failed.append(target)
                    raise OSError(5, "injected publication directory fsync failure")
            original_fsync(descriptor)

        with patch.object(os, "fsync", side_effect=fail_publication_sync), self.assertRaises(OSError):
            self.service.return_prefix(self.service.return_confirmation())
        self.assertEqual(failed, [target])
        self.assertNotEqual(self.service.status()["phase"], "returned")
        program = """import json
import os
from pathlib import Path
import stat
import sys
from logos_desktop import constants, maintenance, runtime_filesystem, runtime_recovery
root, install, failed_parent = (Path(value) for value in sys.argv[1:4])
constants.STATE_DIR = str(root / 'alternate/logos-desktop')
constants.COORDINATION_DIR = root / 'coordination'
def filesystem(path):
    info = Path(path).stat()
    live = runtime_filesystem.live_identity(path)
    return dict(live, canonical_path=str(path), filesystem_uuid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                filesystem_root=str(path), inode=info.st_ino)
runtime_filesystem.identity = filesystem
context = maintenance.Context(install / 'data/wine64_bottle', install, 'Logos')
service = runtime_recovery.Recovery(context)
journal = service.status()
required = {context.prefix.parent, *(Path(item['next']['path']).parent for item in journal['configs'])}
assert service.root not in required
assert failed_parent in required
events = set()
fsync = os.fsync
def directory_fsync(descriptor):
    info = os.fstat(descriptor)
    matches = [path for path in required if stat.S_ISDIR(info.st_mode)
               and (info.st_dev, info.st_ino) == (path.stat().st_dev, path.stat().st_ino)]
    if sys.argv[4] == 'fail' and failed_parent in matches:
        raise OSError(5, 'persistent publication directory fsync failure')
    fsync(descriptor)
    events.update(matches)
os.fsync = directory_fsync
phase = service.phase
def durable_phase(journal, value, **kwargs):
    if value == 'returned' and not required.issubset(events):
        raise AssertionError('Return succeeded before all publication directories synchronized')
    return phase(journal, value, **kwargs)
service.phase = durable_phase
if sys.argv[4] == 'fail':
    try:
        service.reconcile_return()
    except OSError:
        pass
    else:
        raise AssertionError('Persistent durability failure reported a completed return')
    assert service.status()['phase'] != 'returned'
else:
    service.reconcile_return()
    assert service.status()['phase'] == 'returned'
for selected in (context.prefix, Path(service.status()['newer'])):
    try:
        maintenance.Lease(maintenance.Context(selected, install, 'Logos')).close()
    except maintenance.RecoveryRequired:
        pass
    else:
        raise AssertionError('Return admitted an online generation')
assert (context.prefix / 'database').read_text() == 'old complete bytes'
assert (Path(service.status()['newer']) / 'database').read_text() == 'valuable newer bytes'
print(json.dumps({'phase': service.status()['phase'], 'synchronized': sorted(map(str, events))}))
"""
        for mode in ("fail", "succeed"):
            with self.subTest(target=target, mode=mode):
                result = subprocess.run([sys.executable, "-B", "-c", program, str(self.root), str(self.install),
                                         str(failed_parent), mode], capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["phase"] == "returned", mode == "succeed")
        self.assertEqual(self.service.status()["gate"], "online-resume-pending")

    def test_fresh_process_return_reaffirms_prefix_rename_durability(self):
        self.fresh_process_return_durability("prefix")

    def test_fresh_process_return_reaffirms_matching_profile_publication_durability(self):
        self.fresh_process_return_durability("profile")

    def test_pending_online_blocks_real_entry_points_in_fresh_process_with_alternate_user_state(self):
        self.capture()
        (self.prefix / "database").write_text("retained newer work")
        self.service.return_prefix(self.service.return_confirmation())
        program = """import io
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
from logos_desktop import artifacts, config_store, constants, logos, main, maintenance
from logos_desktop import repair, runtime, runtime_copies, runtime_filesystem, runtime_recovery, wine
from logos_desktop.config import Config, EphemeralConfiguration
root, install, profile = (Path(value) for value in sys.argv[1:4])
assert os.environ['HOME'] == str(root / 'alternate/home')
assert constants.STATE_DIR == str(root / 'alternate/state/logos-desktop')
constants.COORDINATION_DIR = root / 'coordination'
def filesystem(path):
    info = Path(path).stat()
    live = runtime_filesystem.live_identity(path)
    return dict(live, canonical_path=str(path), filesystem_uuid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                filesystem_root=str(path), inode=info.st_ino)
runtime_filesystem.identity = filesystem
context = maintenance.Context(install / 'data/wine64_bottle', install, 'Logos')
service = runtime_recovery.Recovery(context)
newer = Path(service.status()['newer'])
protected = {path: runtime_copies.manifest(path) for path in (context.prefix, newer)}
before = profile.read_bytes()
document = runtime_recovery.read(profile)
results = []
for selected in (context.prefix, newer):
    app = SimpleNamespace(ask=Mock(side_effect=AssertionError('Unexpected setup prompt')),
                          approve=Mock(return_value=True), status=Mock(),
                          _config_updated_event=threading.Event())
    configuration = EphemeralConfiguration.project(document, str(profile), environment={})
    configuration.wine_prefix = str(selected)
    configuration.app_run_as_root_permitted = True
    app.conf = Config(configuration, app)
    app.logos = logos.LogosManager(app)
    executable = app.conf.logos_exe_windows_path
    configuration.wine_args = [executable, 'logos4:synthetic-recovery-test']
    accepted = artifacts.AcceptedArtifact(Path(document['wine_binary']),
        artifacts.ArtifactSpec('local:fixture', Path(document['wine_binary']).name, 'appimage'),
        'a' * 64, 1, 'admission-only-input')
    operations = {
        'logos': app.logos.start,
        'indexer': app.logos.index,
        'url': lambda: main.run_url_request(configuration),
        'wine': lambda: wine.run_wine_process(app, document['wine_binary'], io.StringIO(), io.StringIO()),
        'wine-helper': lambda: wine.run_wine_completed_process(app, document['wine_binary'], exe='reg'),
        'winetricks': lambda: wine.run_winetricks(app, '-q', 'list-installed'),
        'repair': lambda: repair.repair_installation(app),
        'activation': lambda: runtime.activate(app, accepted),
        'configuration': lambda: app.conf._change(faithlife_product_release='54.3.0.0005'),
    }
    with (patch('subprocess.Popen', side_effect=AssertionError('Guarded route spawned')) as popen,
          patch('subprocess.run', side_effect=AssertionError('Guarded route executed')) as run):
        for name, operation in operations.items():
            try:
                operation()
            except (maintenance.RecoveryRequired, config_store.ConfigurationError) as error:
                if not any(word in str(error).lower() for word in ('recovery', 'retained', 'inspection')):
                    raise AssertionError((name, str(error)))
                results.append((selected.name, name))
            else:
                raise AssertionError(('Pending-online route was admitted', selected, name))
        popen.assert_not_called()
        run.assert_not_called()
assert profile.read_bytes() == before
for path, baseline in protected.items():
    current = runtime_copies.manifest(path)
    assert current == baseline
    assert current.observation_sha256 == baseline.observation_sha256
assert service.status()['gate'] == 'online-resume-pending'
ordinary = root / 'unregistered-native-prefix'
ordinary.mkdir(exist_ok=True)
maintenance.Lease(maintenance.Context(ordinary, root / 'unregistered-native-install', 'Logos')).close()
print(json.dumps({'refused_routes': results, 'ordinary_admission': True}))
"""
        for marker_available in (True, False):
            with self.subTest(marker_available=marker_available):
                if not marker_available:
                    for prefix in (self.prefix, Path(self.service.status()["newer"])):
                        marker = maintenance.Context(prefix, self.install, "Logos").pending
                        self.assertTrue(marker.exists())
                        marker.unlink()
                environment = dict(os.environ)
                for name, value in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                    ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state"),
                                    ("XDG_CACHE_HOME", "cache")):
                    environment[name] = str(self.root / "alternate" / value)
                result = subprocess.run([sys.executable, "-B", "-c", program, str(self.root), str(self.install),
                                         str(self.profile)], capture_output=True, text=True, env=environment,
                                        timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(json.loads(result.stdout)["refused_routes"]), 18)

    def test_controller_migration_rejects_incoherent_terminal_complete_recovery(self):
        service, _, facts = self.resume_fixture("migration-complete-recovery")
        with (patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
              patch.object(recovery, "inspection_facts", return_value=facts)):
            service.resume_online(service.resume_confirmation(), retained_work_acknowledged=True)
        context = service.context
        coordination = Path(constants.COORDINATION_DIR)
        original = recovery.read(service.root / "journal.json")
        with patch.object(controller_install, "_migration_recovery_identity", side_effect=runtime_filesystem.identity):
            controller_install._migration_recovery_clear(coordination, context.install, context.prefix)
            cases = [{"gate": "online-resume-pending"}, {"result": "unknown"}, {"resume": None},
                     {"inspection": None}, {"point": "not-a-point"}, {"newer": str(self.root / "foreign")},
                     {"revision": True}]
            for field, value in (("confirmed", "2026-11-12T12:00:00"),
                                 ("retained_work_acknowledged", False), ("confirmed", "invalid")):
                cases.append({"resume": dict(original["resume"], **{field: value})})
            confirmation = dict(original["resume"]["confirmation"], active=str(self.root / "foreign"))
            cases.append({"resume": dict(original["resume"], confirmation=confirmation)})
            for changes in cases:
                with self.subTest(changes=changes):
                    recovery.write(service.root / "journal.json", dict(original, **changes))
                    with self.assertRaises(controller_install.InstallError):
                        controller_install._migration_recovery_clear(coordination, context.install, context.prefix)
            recovery.write(service.root / "journal.json", original)
            relocated = service.root.with_name(service.root.name + "-original")
            service.root.rename(relocated)
            with self.assertRaises(controller_install.InstallError):
                controller_install._migration_recovery_clear(coordination, context.install, context.prefix)
            shutil.copytree(relocated, service.root)
            with self.assertRaises(controller_install.InstallError):
                controller_install._migration_recovery_clear(coordination, context.install, context.prefix)

    def test_migration_and_ordinary_admission_reject_the_same_ambiguous_raw_recovery_records(self):
        service, _, facts = self.resume_fixture("migration-strict-recovery-json")
        with (patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
              patch.object(recovery, "inspection_facts", return_value=facts)):
            service.resume_online(service.resume_confirmation(), retained_work_acknowledged=True)
        context = service.context
        self.assertFalse(context.pending.exists())
        coordination = Path(constants.COORDINATION_DIR)
        registration = recovery.registration_path(context)
        journal = service.root / "journal.json"
        marker = service.root / "marker.json"
        originals = {path: path.read_bytes() for path in (registration, journal, marker)}
        cases = [
            (registration, originals[registration].replace(b'"secondary":false',
                                                          b'"secondary":true,"secondary":false', 1)),
            (journal, originals[journal].replace(b'"gate":"online-resumed"',
                                                 b'"gate":"online-resume-pending","gate":"online-resumed"', 1)),
            (journal, originals[journal].replace(b'"retained_work_acknowledged":true',
                                                 b'"retained_work_acknowledged":false,'
                                                 b'"retained_work_acknowledged":true', 1)),
        ]
        reason = b'"reason":' + runtime_stacks.canonical(recovery.read(journal)["reason"])
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            cases.append((journal, originals[journal].replace(reason, b'"reason":' + constant, 1)))
        marker_path = b'"path":' + runtime_stacks.canonical(recovery.read(marker)["path"])
        cases.append((marker, originals[marker].replace(marker_path, b'"path":"/foreign",' + marker_path, 1)))
        for path, content in originals.items():
            cases.append((path, content + b" " * (recovery.MAX_RECORD + 1 - len(content))))
        with patch.object(controller_install, "_migration_recovery_identity", side_effect=runtime_filesystem.identity):
            for index, (path, content) in enumerate(cases):
                with self.subTest(index=index, path=path.name):
                    self.assertNotEqual(content, originals[path])
                    path.write_bytes(content)
                    try:
                        with self.assertRaises((maintenance.RecoveryRequired, runtime_stacks.CatalogError)):
                            maintenance.Lease(context).close()
                        with self.assertRaises(controller_install.InstallError):
                            controller_install._migration_recovery_clear(coordination, context.install, context.prefix)
                    finally:
                        path.write_bytes(originals[path])
            for path in originals:
                with self.subTest(readable_record=path.name):
                    path.chmod(0o644)
                    try:
                        with self.assertRaises(maintenance.RecoveryRequired):
                            maintenance.Lease(context).close()
                        with self.assertRaises(controller_install.InstallError):
                            controller_install._migration_recovery_clear(coordination, context.install, context.prefix)
                    finally:
                        path.chmod(0o600)
            maintenance.Lease(context).close()
            controller_install._migration_recovery_clear(coordination, context.install, context.prefix)

    def test_controller_migration_refuses_pending_and_secondary_and_allows_kept_current(self):
        self.capture()
        self.service.keep_current()
        coordination = Path(constants.COORDINATION_DIR)
        with patch.object(controller_install, "_migration_recovery_identity", side_effect=runtime_filesystem.identity):
            controller_install._migration_recovery_clear(coordination, self.install, self.prefix)
            registered = recovery.registration_path(self.context)
            previous = registered.read_bytes()
            for corrupt in (b"null", b"{"):
                with self.subTest(corrupt=corrupt):
                    registered.write_bytes(corrupt)
                    with self.assertRaises(controller_install.InstallError):
                        controller_install._migration_recovery_clear(coordination, self.install, self.prefix)
            registered.write_bytes(previous)
            self.capture()
            self.service.return_prefix(self.service.return_confirmation())
            for prefix in (self.prefix, Path(self.service.status()["newer"])):
                with self.subTest(prefix=prefix), self.assertRaises(controller_install.InstallError):
                    controller_install._migration_recovery_clear(coordination, self.install, prefix)

    def test_standalone_migration_identity_uses_stable_mount_root_and_current_inode(self):
        facts = {"filesystems": [{"uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "fstype": "ext4",
                                  "fsroot": "/retained-volume", "target": str(self.root), "id": 100}]}
        with patch.object(controller_install.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(facts))):
            observed = controller_install._migration_recovery_identity(self.service.root)
        facts["filesystems"][0]["id"] = 200
        with patch.object(controller_install.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(facts))):
            remapped = controller_install._migration_recovery_identity(self.service.root)
        self.assertEqual(remapped, observed)
        self.assertEqual(observed, {
            "canonical_path": str(self.service.root), "filesystem_uuid": facts["filesystems"][0]["uuid"],
            "filesystem_root": "/retained-volume/recovery", "inode": self.service.root.stat().st_ino,
            "owner_uid": os.geteuid(),
        })

    def test_initial_registration_faults_preserve_original_and_reopen_without_orphaning_admission(self):
        boundaries = ("before-record", "before-file-fsync", "after-file-fsync", "before-record-rename",
                      "after-record-rename", "after-record")
        program = """import json
from pathlib import Path
import sys
from logos_desktop import constants, maintenance, runtime_filesystem, runtime_recovery
root, install = Path(sys.argv[1]), Path(sys.argv[2])
constants.STATE_DIR = str(root / 'alternate/state/logos-desktop')
constants.COORDINATION_DIR = root / 'coordination'
def filesystem(path):
    info = Path(path).stat()
    live = runtime_filesystem.live_identity(path)
    return dict(live, canonical_path=str(path), filesystem_uuid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                filesystem_root=str(path), inode=info.st_ino)
runtime_filesystem.identity = filesystem
context = maintenance.Context(install / 'data/wine64_bottle', install, 'Logos')
service = runtime_recovery.Recovery(context)
if service.registered is not None:
    try:
        maintenance.Lease(context).close()
    except maintenance.RecoveryRequired:
        pass
    else:
        raise AssertionError('Incomplete registration admitted normal use')
    assert service.status()['phase'] == 'preparing'
    service.keep_current()
    assert service.status()['phase'] == 'kept-current'
maintenance.Lease(context).close()
assert (context.prefix / 'database').read_text() == 'old complete bytes'
print(json.dumps({'registered': service.registered is not None, 'admitted_unchanged_original': True}))
"""
        index = 0
        fsync = os.fsync
        for record in ("journal.json", "complete-recovery.json", "marker.json", "compatibility"):
            selected = ("file-fsync", "directory-fsync") if record == "compatibility" else boundaries
            for boundary in selected:
                with self.subTest(record=record, boundary=boundary):
                    parent = self.root / ("registration-fault-" + str(index))
                    index += 1
                    parent.mkdir()
                    install = parent / "install"
                    shutil.copytree(self.install, install, symlinks=True)
                    context = maintenance.Context(install / "data/wine64_bottle", install, "Logos")
                    before = runtime_copies.manifest(context.prefix)
                    retained = parent / "recovery"
                    hit = []

                    def interrupt(value):
                        if value == boundary + ":" + record:
                            hit.append(value)
                            raise OSError(5, "interrupted initial recovery publication")

                    def marker_fsync(descriptor):
                        guard = context.state_path / "restore.pending"
                        if (record == "compatibility" and guard.exists()
                                and recovery.registration_path(context).exists()):
                            target = guard if boundary == "file-fsync" else guard.parent
                            info, expected = os.fstat(descriptor), target.stat()
                            if (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
                                hit.append(boundary + ":" + record)
                                raise OSError(5, "compatibility guard fsync failed")
                        fsync(descriptor)

                    with patch.object(os, "fsync", side_effect=marker_fsync), self.assertRaises(OSError):
                        recovery.Recovery.create(context, retained, checkpoint=interrupt)
                    self.assertEqual(hit, [boundary + ":" + record])
                    result = subprocess.run([sys.executable, "-B", "-c", program, str(self.root), str(install)],
                                            capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    current = runtime_copies.manifest(context.prefix)
                    self.assertEqual(current, before)
                    self.assertEqual(current.observation_sha256, before.observation_sha256)
                    self.assertTrue(retained.exists())

    def test_capture_publication_fault_matrix_reopens_without_admitting_incomplete_points(self):
        record_boundaries = ("before-record", "before-file-fsync", "after-file-fsync", "before-record-rename",
                             "after-record-rename", "after-record")
        boundaries = [(boundary + ":point.json", 1) for boundary in record_boundaries]
        boundaries += [(boundary + ":points.json", occurrence) for occurrence in (1, 2)
                       for boundary in record_boundaries]
        boundaries += [(boundary + ":journal.json", 1) for boundary in record_boundaries]
        boundaries += [("point-copy", 1), ("before-phase:point-verified", 1), ("after-phase:point-verified", 1)]
        program = """import json
from pathlib import Path
import sys
from logos_desktop import constants, maintenance, runtime_filesystem, runtime_recovery
root, install = Path(sys.argv[1]), Path(sys.argv[2])
constants.STATE_DIR = str(root / 'alternate/state/logos-desktop')
constants.COORDINATION_DIR = root / 'coordination'
def filesystem(path):
    info = Path(path).stat()
    live = runtime_filesystem.live_identity(path)
    return dict(live, canonical_path=str(path), filesystem_uuid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                filesystem_root=str(path), inode=info.st_ino)
runtime_filesystem.identity = filesystem
context = maintenance.Context(install / 'data/wine64_bottle', install, 'Logos')
service = runtime_recovery.Recovery(context)
before = {path: path.read_bytes() for path in service.root.rglob('*') if path.is_file() and path.name != 'journal.json'}
journal = service.status()
assert journal['capability'] is None
if journal['phase'] == 'preparing':
    try:
        service.apply(None, None, store=None, generation=None, host={}, without_successful_trial=True)
    except maintenance.RecoveryRequired as error:
        assert 'freshly verified' in str(error)
    else:
        raise AssertionError('Incomplete point authorized apply')
else:
    assert journal['phase'] == 'point-verified'
    service.verify_point(journal['point'])
service.keep_current()
maintenance.Lease(context).close()
assert service.status()['phase'] == 'kept-current'
for path, content in before.items():
    assert path.read_bytes() == content
assert (context.prefix / 'database').read_text() == 'old complete bytes'
print(json.dumps({'phase': service.status()['phase'], 'retained_files': len(before)}))
"""
        for index, (target, occurrence) in enumerate(boundaries):
            with self.subTest(target=target, occurrence=occurrence):
                service = self.case_service("capture-fault-" + str(index), capture_point=False)
                before = runtime_copies.manifest(service.context.prefix)
                hits = []

                def interrupt(value):
                    if value == target:
                        hits.append(value)
                        if len(hits) == occurrence:
                            raise OSError(5, "interrupted point publication")

                service.checkpoint = interrupt
                profile = service.root.parent / "profile.json"
                with self.assertRaises(OSError):
                    service.capture(store=self.store, generation=self.runner.generation, profile=profile,
                                    portable=service.context.install / constants.PORTABLE_CONFIG_NAME,
                                    links=[service.context.install / "data/bin" / link.name for link in self.links],
                                    product_build="fixture-1", effective=recovery.read(profile))
                self.assertEqual(len(hits), occurrence)
                result = subprocess.run([sys.executable, "-B", "-c", program, str(self.root),
                                         str(service.context.install)], capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                current = runtime_copies.manifest(service.context.prefix)
                self.assertEqual(current, before)
                self.assertEqual(current.observation_sha256, before.observation_sha256)

    def test_spawned_recipe_publication_failure_preserves_live_owner_and_distinct_runtime_references(self):
        prepared, target = self.distinct_prepared_fixture()
        service = self.case_service("recipe-spawn-fault", product_build="54.3.0.5",
                                    settings=dict(prepared.recipe.settings))
        journal = service.status()
        journal["capability"] = {"id": "00000000-0000-0000-0000-000000000001", "sha256": "a" * 64}
        service.phase(journal, "point-verified", result="succeeded")
        children = []
        spawn = subprocess.Popen
        worker_program = """import ctypes
from pathlib import Path
import sys
import time
ctypes.CDLL(None).prctl(15, b'wine', 0, 0, 0)
(Path(sys.argv[1]) / 'synthetic-recipe-work').write_text('preserve newer recipe bytes')
Path(sys.argv[2]).write_text('ready')
while True:
    time.sleep(1)
"""

        def command(boundary, **arguments):
            return ["/usr/bin/env", "WINEPREFIX=" + str(service.context.prefix), sys.executable, "-B", "-c",
                    worker_program, str(service.context.prefix), str(arguments["private"] / "synthetic-ready")]

        def owned_spawn(arguments, **options):
            child = spawn(arguments, **options)
            children.append(child)
            deadline = time.monotonic() + 3
            while not Path(arguments[-1]).exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("Disposable recipe worker did not become ready")
                time.sleep(0.01)
            return child

        def interrupt(boundary):
            if boundary == "before-record:process.json":
                raise OSError(5, "helper identity publication failed after spawn")

        service.checkpoint = interrupt
        try:
            with (patch.object(service, "verify_capability"),
                  patch.object(recovery.runtime_boundary, "inspect", return_value=object()),
                  patch.object(recovery.runtime_boundary, "prepare_private",
                               side_effect=lambda path, **kwargs: path.mkdir(parents=True)),
                  patch.object(recovery.runtime_boundary, "expansion_budget", return_value={"bytes": 0}),
                  patch.object(recovery.runtime_boundary, "probe"),
                  patch.object(recovery.runtime_boundary, "command", side_effect=command),
                  patch.object(subprocess, "Popen", side_effect=owned_spawn), self.assertRaises(OSError)):
                service.apply(self.app_fixture(service), prepared, store=self.store, generation=target.generation,
                              host={"architecture": "x86_64", "font-arial": "present"}, without_successful_trial=True)
            self.assertEqual(len(children), 1)
            self.assertIsNone(children[0].poll())
            self.assertEqual(service.status()["result"], "unknown")
            with self.assertRaises(maintenance.MaintenanceBusy):
                service.return_prefix(service.return_confirmation())
            self.assertIsNone(children[0].poll())
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
        service.checkpoint = lambda boundary: None
        program = """import json
import os
from pathlib import Path
import sys
from logos_desktop import constants, runtime_filesystem, runtime_runners
root, store_root, coordination = (Path(value) for value in sys.argv[1:4])
constants.STATE_DIR = str(root / 'alternate-reference-state/logos-desktop')
constants.COORDINATION_DIR = root / 'coordination'
def filesystem(path):
    info = Path(path).stat()
    live = runtime_filesystem.live_identity(path)
    return dict(live, canonical_path=str(path), filesystem_uuid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                filesystem_root=str(path), inode=info.st_ino)
runtime_filesystem.identity = filesystem
runtime_runners.INTEGRATED_REFERENCE_KINDS = runtime_runners.REFERENCE_KINDS
store = runtime_runners.RunnerStore(store_root, coordination)
results = []
for generation, kind in zip(sys.argv[4:6], ('return-point', 'pending-journal')):
    runner = next(item for item in store.inventory() if item.generation == generation)
    reasons = store.removal_reasons(runner.sha256)
    assert any(kind in reason for reason in reasons)
    try:
        store.remove(generation)
    except runtime_runners.RunnerError:
        pass
    else:
        raise AssertionError('Actual recovery runtime was removed')
    assert runner.path.exists()
    results.append({'sha256': runner.sha256, 'reasons': reasons})
assert results[0]['sha256'] != results[1]['sha256']
print(json.dumps(results))
"""
        for returned in (False, True):
            with self.subTest(returned=returned):
                if returned:
                    service.return_prefix(service.return_confirmation())
                    newer = Path(service.status()["newer"])
                    self.assertEqual((newer / "synthetic-recipe-work").read_text(), "preserve newer recipe bytes")
                    self.assertFalse((service.context.prefix / "synthetic-recipe-work").exists())
                    self.assert_blocked(service.context)
                    self.assert_blocked(maintenance.Context(newer, service.context.install, "Logos"))
                result = subprocess.run([sys.executable, "-B", "-c", program, str(self.root), str(self.store.root),
                                         str(self.store.coordination), self.runner.generation, target.generation],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)

    def fresh_complete_return(self, service, original):
        canary = service.context.prefix / "synthetic-recipe-work"
        newer_work = canary.exists()
        program = """import json
from pathlib import Path
import sys
from logos_desktop import constants, maintenance, runtime_filesystem, runtime_recovery
root, install = Path(sys.argv[1]), Path(sys.argv[2])
assert constants.STATE_DIR == str(root / 'alternate-return/state/logos-desktop')
constants.COORDINATION_DIR = root / 'coordination'
def filesystem(path):
    info = Path(path).stat()
    live = runtime_filesystem.live_identity(path)
    return dict(live, canonical_path=str(path), filesystem_uuid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                filesystem_root=str(path), inode=info.st_ino)
runtime_filesystem.identity = filesystem
context = maintenance.Context(install / 'data/wine64_bottle', install, 'Logos')
service = runtime_recovery.Recovery(context)
if service.status()['phase'] in ('return-staging', 'return-ready', 'resume-pending', 'withdraw-intent',
                               'withdrawn', 'publish-intent', 'published', 'config-pending'):
    service.reconcile_return()
else:
    service.return_prefix(service.return_confirmation())
journal = service.status()
assert journal['phase'] == 'returned'
assert journal['gate'] == 'online-resume-pending'
for publication in journal['configs']:
    assert runtime_recovery.snapshot(publication['next']['path']) == publication['next']
newer = Path(journal['newer'])
assert (newer / 'synthetic-recipe-work').exists() == (sys.argv[3] == 'yes')
if sys.argv[3] == 'yes':
    assert (newer / 'synthetic-recipe-work').read_text() == 'preserve newer recipe bytes'
assert not (context.prefix / 'synthetic-recipe-work').exists()
for selected in (context.prefix, newer):
    try:
        maintenance.Lease(maintenance.Context(selected, install, 'Logos')).close()
    except maintenance.RecoveryRequired:
        pass
    else:
        raise AssertionError('Interrupted mutation admitted an online generation')
service.verify_point(journal['point'])
print(json.dumps({'phase': journal['phase'], 'point_verified': True}))
"""
        environment = dict(os.environ)
        for name, value in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                            ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
            environment[name] = str(self.root / "alternate-return" / value)
        result = subprocess.run([sys.executable, "-B", "-c", program, str(self.root), str(service.context.install),
                                 "yes" if newer_work else "no"], capture_output=True, text=True, timeout=15,
                                env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(runtime_copies.manifest(service.context.prefix), original)

    def test_apply_durable_publication_faults_return_in_fresh_process_with_distinct_runtimes(self):
        prepared, target_runner = self.distinct_prepared_fixture()
        boundaries = [("before-phase:apply-intent", 1), ("after-phase:apply-intent", 1),
                      ("before-phase:selection-pending", 1), ("after-phase:selection-pending", 1),
                      ("before-phase:selection-pending", 2), ("after-phase:selection-pending", 2),
                      ("before-phase:recipe-pending", 1), ("after-phase:recipe-pending", 1),
                      ("before-phase:applied", 1), ("after-phase:applied", 1)]
        boundaries += [(boundary + ":journal.json", occurrence) for occurrence in range(1, 6)
                       for boundary in ("before-record", "before-file-fsync", "after-file-fsync",
                                        "before-record-rename", "after-record-rename", "after-record")]
        for index, (target, occurrence) in enumerate(boundaries):
            with self.subTest(target=target, occurrence=occurrence):
                service = self.case_service("apply-fault-" + str(index), product_build="54.3.0.5",
                                            settings=dict(prepared.recipe.settings))
                journal = service.status()
                journal["capability"] = {"id": "00000000-0000-0000-0000-000000000001", "sha256": "a" * 64}
                service.phase(journal, "point-verified", result="succeeded")
                original = runtime_copies.manifest(service.context.prefix)
                hits, raised = [], []

                def interrupt(value):
                    if value == target:
                        hits.append(value)
                        if len(hits) == occurrence:
                            raised.append(value)
                            raise OSError(5, "injected apply publication failure")

                def recipe(*arguments):
                    (service.context.prefix / "synthetic-recipe-work").write_text("preserve newer recipe bytes")

                service.checkpoint = interrupt
                with (patch.object(service, "verify_capability"),
                      patch.object(service, "_run_recipe", side_effect=recipe), self.assertRaises(OSError)):
                    service.apply(self.app_fixture(service), prepared, store=self.store,
                                  generation=target_runner.generation,
                                  host={"architecture": "x86_64", "font-arial": "present"},
                                  without_successful_trial=True)
                self.assertEqual(raised, [target])
                self.assertTrue(self.runner.path.exists())
                self.assertTrue(target_runner.path.exists())
                self.fresh_complete_return(service, original)

    def test_changed_configuration_return_faults_preserve_later_preferences_and_aliases(self):
        prepared, target = self.distinct_prepared_fixture()
        for index, (kind, when) in enumerate((kind, when) for kind in ("profile", "portable", "selected")
                                             for when in ("before-config:", "after-config:")):
            with self.subTest(kind=kind, when=when):
                service = self.case_service("config-return-fault-" + str(index), product_build="54.3.0.5",
                                            settings=dict(prepared.recipe.settings))
                journal = service.status()
                journal["capability"] = {"id": "00000000-0000-0000-0000-000000000001", "sha256": "a" * 64}
                service.phase(journal, "point-verified", result="succeeded")
                original = runtime_copies.manifest(service.context.prefix)
                with patch.object(service, "verify_capability"), patch.object(service, "_run_recipe"):
                    service.apply(self.app_fixture(service), prepared, store=self.store, generation=target.generation,
                                  host={"architecture": "x86_64", "font-arial": "present"},
                                  without_successful_trial=True)
                profile = service.root.parent / "profile.json"
                portable = service.context.install / constants.PORTABLE_CONFIG_NAME
                for path in (profile, portable):
                    document = recovery.read(path)
                    recovery.write(path, dict(document, later_preference="preserve this setting"))
                selected = service.context.install / "data/bin/selected_wine.AppImage"
                publication = {"profile": profile, "portable": portable, "selected": selected}[kind]
                hits = []

                def interrupt(value):
                    if value == when + str(publication):
                        hits.append(value)
                        raise OSError(5, "interrupted changed configuration restoration")

                service.checkpoint = interrupt
                with self.assertRaises(OSError):
                    service.return_prefix(service.return_confirmation())
                self.assertEqual(hits, [when + str(publication)])
                self.fresh_complete_return(service, original)
                for path in (profile, portable):
                    self.assertEqual(recovery.read(path)["later_preference"], "preserve this setting")
                self.assertEqual(selected.resolve(), self.runner.path)

    def test_return_durable_phase_restart_matrix(self):
        boundaries = (
            "before-phase:return-staging", "after-phase:return-staging", "before-phase:return-ready",
            "after-phase:return-ready", "before-phase:resume-pending", "after-phase:resume-pending",
            "before-phase:withdraw-intent", "after-phase:withdraw-intent",
            "before-prefix-rename:wine64_bottle", "after-prefix-rename:wine64_bottle",
            "after-prefix-fsync:.runtime-newer-", "before-phase:withdrawn", "after-phase:withdrawn",
            "before-phase:publish-intent", "after-phase:publish-intent",
            "before-prefix-rename:.runtime-old-", "after-prefix-rename:.runtime-old-",
            "after-prefix-fsync:wine64_bottle", "before-phase:published", "after-phase:published",
            "before-phase:config-pending", "after-phase:config-pending", "before-phase:returned",
            "after-phase:returned",
        )
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                service = self.case_service("restart-" + str(index))
                (service.context.prefix / "newer-work").write_text(boundary)
                fired = False

                def interrupt(actual):
                    nonlocal fired
                    if not fired and actual.startswith(boundary):
                        fired = True
                        raise OSError(5, "injected durable boundary interruption")

                service.checkpoint = interrupt
                with self.assertRaises(OSError):
                    service.return_prefix(service.return_confirmation())
                reopened = recovery.Recovery(service.context)
                phase = reopened.status()["phase"]
                if phase == "point-verified":
                    reopened.return_prefix(reopened.return_confirmation())
                elif phase != "returned":
                    reopened.reconcile_return()
                self.assertEqual(reopened.status()["phase"], "returned")
                self.assertFalse((service.context.prefix / "newer-work").exists())
                self.assertEqual((Path(reopened.status()["newer"]) / "newer-work").read_text(), boundary)
                reopened.verify_point(reopened.status()["point"])
                self.assert_blocked(service.context)

    def test_fsync_enospc_during_staging_restarts_without_deleting_partial_material(self):
        self.capture()
        armed = False
        failed = False
        original = os.fsync

        def arm(boundary):
            nonlocal armed
            if boundary == "return-copy":
                armed = True

        def fsync(descriptor):
            nonlocal failed
            if armed and not failed:
                failed = True
                raise OSError(28, "injected copy fsync ENOSPC")
            return original(descriptor)

        self.service.checkpoint = arm
        with patch.object(os, "fsync", side_effect=fsync), self.assertRaises(OSError):
            self.service.return_prefix(self.service.return_confirmation())
        abandoned = self.service.status()["stage"]
        self.assertTrue(Path(abandoned + ".manifest.partial").exists())
        reopened = recovery.Recovery(self.context)
        reopened.reconcile_return()
        self.assertEqual(reopened.status()["phase"], "returned")
        self.assertTrue(Path(abandoned + ".manifest.partial").exists())
        self.assertNotEqual(reopened.status()["stage"], abandoned)


if __name__ == "__main__":
    unittest.main()
