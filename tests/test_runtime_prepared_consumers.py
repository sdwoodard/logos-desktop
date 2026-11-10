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
