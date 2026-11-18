"""Prepared inputs reach consumers without selection or dependency discovery."""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from logos_desktop import artifacts, constants, installer, maintenance, runtime, runtime_recipes, runtime_stacks, wine
from tests.artifact_fixtures import appimage
from tests.test_artifacts import response


class PreparedConsumers(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = patch.multiple(constants, STATE_DIR=str(self.root / "state"),
                                    COORDINATION_DIR=self.root / "coordination")
        self.state.start()
        self.addCleanup(self.state.stop)
        self.app = Mock(conf=SimpleNamespace(
            _raw=SimpleNamespace(install_dir=str(self.root / "install"), faithlife_product="Logos",
                                 faithlife_product_release="54.3.0.0005"),
            _overrides=SimpleNamespace(install_dir=None, wine_prefix=None, wineserver_binary=None,
                                       wine_dll_overrides=None),
            wine_prefix=str(self.root / "install/data/wine64_bottle"),
        ))
        self.context = maintenance.Context.from_app(self.app, prompt=False)

    def prepared(self):
        catalog = runtime_stacks.bundled()
        stack = next(iter(catalog.stacks.values()))
        recipe = catalog.recipes[stack.recipe_id]
        image = appimage()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as stream:
            member = tarfile.TarInfo("windows/globalization/ICU/icudtl.dat")
            member.size = 4
            stream.addfile(member, io.BytesIO(b"data"))
        content = {"icu": archive.getvalue(), "winetricks": b"#!/bin/sh\nexit 0\n"}
        dependencies = tuple(replace(item, size=len(content[item.role]),
                                     sha256=hashlib.sha256(content[item.role]).hexdigest())
                             for item in recipe.dependencies)
        recipe = replace(recipe, dependencies=dependencies)
        runner = replace(catalog.runners[stack.runner_id], size=len(image), sha256=hashlib.sha256(image).hexdigest())
        catalog = replace(catalog, runners={runner.id: runner}, recipes={recipe.id: recipe})

        def transfer(url, **kwargs):
            if url == runner.url:
                return response(image)
            return response(next(content[item.role] for item in dependencies if item.url == url))

        with patch.object(artifacts.requests, "get", side_effect=transfer):
            return runtime_recipes.prepare(catalog, stack.id, self.root / "prepared")

    @contextmanager
    def owned(self):
        with (patch.object(maintenance, "installation_owned", return_value=True),
              patch.object(maintenance, "current_context", return_value=self.context)):
            yield

    def test_prefix_only_admission_leaves_artifacts_for_later_ordered_acquisition(self):
        with patch.object(maintenance, "artifact_use") as use:
            lease = maintenance.Lease(self.context, prefix_only=True)
            lease.close()
            use.assert_not_called()
            lease = maintenance.Lease(self.context)
            lease.close()
            self.assertEqual(use.call_count, 1)
        with self.assertRaises(ValueError):
            maintenance.Lease(self.context, role="logos", prefix_only=True)

    def test_prepared_invocation_keeps_exact_bytes_and_denies_addon_fetches(self):
        prepared = self.prepared()
        with self.owned(), runtime.prepared_inputs(self.app, prepared):
            with patch.object(artifacts, "import_local", side_effect=AssertionError("unexpected import")):
                command, environment = wine._wine_invocation(self.app, str(prepared.runner.path), "mscoree=")
            self.assertEqual(command, [str(prepared.runner.path)])
            self.assertEqual(environment["WINESERVER"], str(prepared.runner.path.parent / "wineserver"))
            self.assertIn("mshtml=", environment["WINEDLLOVERRIDES"])
            with self.assertRaises(artifacts.ArtifactError):
                runtime.executable("/arbitrary/runtime", self.app)
        self.assertIsNone(runtime.current_prepared())

    def test_prepared_installer_consumes_exact_icu_and_winetricks_without_latest(self):
        prepared = self.prepared()

        def initialize(binary, app):
            self.assertEqual(binary, str(prepared.runner.path))
            (self.context.prefix / "drive_c/windows").mkdir(parents=True)
            (self.context.prefix / "system.reg").write_bytes(b"initialized")

        with (self.owned(), patch.object(wine, "initializeWineBottle", side_effect=initialize),
              patch.object(runtime_recipes, "apply_settings") as settings,
              patch.object(artifacts.requests, "get", side_effect=AssertionError("late fetch"))):
            helper = installer.configure_prepared_stack(
                self.app, prepared, observed_settings={},
                host_facts={"architecture": "x86_64", "font-arial": "present"},
            )
        self.assertEqual(Path(helper), next(item.artifact.path for item in prepared.dependencies
                                           if item.dependency.role == "winetricks"))
        self.assertEqual((self.context.prefix / "drive_c/windows/globalization/ICU/icudtl.dat").read_bytes(), b"data")
        settings.assert_called_once_with(self.app, prepared, {})

    def test_cold_tested_setup_reassesses_download_before_binding_prepared_inputs(self):
        try:
            from logos_desktop.qt import runner_services as gui
            from logos_desktop.qt.window import Installation, InstallDraft
        except ModuleNotFoundError as error:
            if error.name == "PySide6":
                self.skipTest("Optional Qt frontend is not installed")
            raise
        prepared = self.prepared()
        runner = prepared.catalog.runners[prepared.stack.runner_id]
        choice = gui.RecipeChoice(prepared.stack.id, prepared.recipe.id, "tested", False, False, "unchecked")
        row = gui.Version(runner.id, runner.family, runner.version, runner.sha256, runner.size, "", "", False,
                          (), choice.stack, "Tested", False, (choice,), True)
        snapshot = Installation("Logos", "", "", "", installed=False)
        release = prepared.stack.product_build
        cold = gui.Library("", release, (row,), "", gui.RecoveryView())
        cached = replace(cold, rows=(replace(row, generation="bound-generation", path=str(prepared.runner.path),
                                              choices=(replace(choice, selectable=True),)),))
        draft = InstallDraft("Logos", str(self.context.install), constants.WINE_RECOMMENDED_SIGIL, True, release)
        store = Mock()
        store.inventory.return_value = (SimpleNamespace(generation="bound-generation", state="published",
                                                        path=prepared.runner.path, sha256=runner.sha256,
                                                        size=runner.size),)
        self.app.conf._overrides.assume_yes = False
        with (patch.object(gui, "library", side_effect=[cold, cached]),
              patch.object(gui.runtime_runners, "RunnerStore", return_value=store),
              patch.object(runtime_stacks, "bundled", return_value=prepared.catalog),
              patch.object(gui.runtime_commands, "managed_candidate",
                           return_value=(store, "bound-generation", prepared)) as bind):
            selected, setup = gui.setup_runtime(self.app, None, draft, snapshot)
        store.download.assert_called_once()
        bind.assert_called_once_with(prepared.catalog, choice.stack, checkpoint=self.app.checkpoint)
        self.assertIs(setup.prepared, prepared)
        self.assertEqual(selected.runtime, str(prepared.runner.path))

    def test_setup_selector_and_service_refuse_experimental_sibling_of_tested_recipe(self):
        import json
        from tests.test_runtime_stacks import document, evidence
        try:
            from PySide6.QtWidgets import QApplication
            from logos_desktop.qt import runner_services as gui
            from logos_desktop.qt.runner_page import WineRunners
            from logos_desktop.qt.window import Installation, InstallDraft
        except ModuleNotFoundError as error:
            if error.name == "PySide6":
                self.skipTest("Optional Qt frontend is not installed")
            raise
        data = document()
        data["recipes"][0]["required_host_facts"] = []
        fact = evidence(data)
        recipe = dict(data["recipes"][0], id="experimental-recipe")
        stack = dict(data["stacks"][0], id="experimental-stack", recipe_id=recipe["id"], evidence_ids=["experiment"])
        data["recipes"].append(recipe)
        data["stacks"].append(stack)
        data["evidence"].append(dict(fact, id="experiment", kind="experimental", stack_id=stack["id"],
                                     recipe_sha256=runtime_stacks.fingerprint(recipe)))
        catalog = runtime_stacks.parse(json.dumps(data).encode())
        tested = data["stacks"][0]
        runner = catalog.runners[tested["runner_id"]]
        store = Mock(root=self.root, coordination=self.root)
        copy = SimpleNamespace(path=self.root / "wine", generation="selected", size=runner.size, sha256=runner.sha256,
                               state="published", content_key=runner.content_key)
        store.inventory.return_value = (copy,)
        store.download_states.return_value = store.removal_reasons.return_value = ()
        self.app.conf.config_file_path = self.root / "conf"
        self.app.conf._network = Mock()
        self.app.conf._overrides.assume_yes = False
        snapshot = Installation("Logos", "", "", "", installed=False)
        draft = InstallDraft("Logos", str(self.context.install), str(copy.path), True, tested["product_build"],
                             copy.generation, tested["id"])
        qt = QApplication.instance() or QApplication([])
        page = WineRunners()
        page.setup = True
        with (patch.object(runtime_stacks, "bundled", return_value=catalog),
              patch.object(gui.runtime_runners, "RunnerStore", return_value=store),
              patch.object(gui.network, "product_update_state",
                           return_value=Mock(target=draft.release, actionable=True)),
              patch.object(gui.runtime_commands, "managed_candidate") as bind):
            page.load(snapshot, gui.library(self.app, snapshot), ())
            self.assertTrue(page.buttons["draft"].isEnabled())
            bind.return_value = (store, copy.generation, object())
            _, setup = gui.setup_runtime(self.app, None, draft, snapshot)
            self.assertIs(setup.prepared, bind.return_value[2])
            bind.assert_called_once_with(catalog, tested["id"], checkpoint=self.app.checkpoint)
            page.recipe.setCurrentIndex(page.recipe.findData(stack["id"]))
            self.assertEqual(page.row.stack, stack["id"])
            self.assertFalse(page.buttons["draft"].isEnabled())
            with self.assertRaisesRegex(maintenance.RecoveryRequired, "selected settings"):
                gui.setup_runtime(self.app, None, replace(draft, stack=page.row.stack), snapshot)
            self.assertEqual(bind.call_count, 1)
            data["stacks"].remove(stack)
            data["evidence"].pop()
            with patch.object(runtime_stacks, "bundled", return_value=runtime_stacks.parse(json.dumps(data).encode())):
                page.load(snapshot, gui.library(self.app, snapshot), ())
                page.recipe.setCurrentIndex(page.recipe.findData(""))
                self.assertTrue(page.buttons["draft"].isEnabled())
                self.assertEqual(page.row.stack, "")
                _, fallback = gui.setup_runtime(self.app, None, replace(draft, stack=""), snapshot)
                self.assertIsNone(fallback.prepared)
                self.assertEqual(bind.call_count, 1)
        page.close()
        qt.processEvents()

    def test_existing_prefix_and_custom_override_are_refused_before_mutation(self):
        prepared = self.prepared()
        self.context.prefix.mkdir(parents=True)
        canary = self.context.prefix / "data"
        canary.write_bytes(b"preserve")
        with self.owned(), self.assertRaisesRegex(maintenance.MaintenanceError, "complete recovery"):
            installer.configure_prepared_stack(self.app, prepared, observed_settings={}, host_facts={})
        self.assertEqual(canary.read_bytes(), b"preserve")
        self.app.conf._overrides.wineserver_binary = "/custom/server"
        with self.owned(), self.assertRaisesRegex(maintenance.MaintenanceError, "custom"):
            with runtime.prepared_inputs(self.app, prepared):
                self.fail("custom context admitted")

    def test_implicit_companion_tampering_is_refused_before_invocation(self):
        prepared = self.prepared()
        alias = prepared.runner.path.parent / "wineserver"
        for kind in ("missing", "file", "wrong-link"):
            with self.subTest(kind=kind):
                alias.unlink(missing_ok=True)
                if kind == "file":
                    alias.write_bytes(b"foreign executable")
                elif kind == "wrong-link":
                    alias.symlink_to("/foreign/server")
                with self.owned(), self.assertRaisesRegex(artifacts.ArtifactError, "companion"):
                    with runtime.prepared_inputs(self.app, prepared):
                        wine._wine_invocation(self.app, str(prepared.runner.path), None)
        alias.unlink()
        alias.symlink_to(prepared.runner.path.name)
        prepared.check()

    def test_known_affected_prepared_setup_refuses_before_prefix_initialization(self):
        prepared = self.prepared()
        fact = runtime_stacks.Evidence(
            "affected", "known-affected", prepared.stack.id, prepared.runner.sha256, prepared.recipe.sha256,
            tuple(item.dependency.sha256 for item in prepared.dependencies), prepared.stack.product_build,
            {}, "2026-11-10T00:00:00Z", "affected fixture", {"library": "failed"},
        )
        prepared = replace(prepared, catalog=replace(prepared.catalog, evidence={fact.id: fact}))
        with (self.owned(), patch.object(wine, "initializeWineBottle") as initialize,
              self.assertRaisesRegex(maintenance.MaintenanceError, "affected fixture")):
            installer.configure_prepared_stack(
                self.app, prepared, observed_settings={},
                host_facts={"architecture": "x86_64", "font-arial": "present"}, advanced=True,
            )
        initialize.assert_not_called()
        self.assertFalse(self.context.prefix.exists())
