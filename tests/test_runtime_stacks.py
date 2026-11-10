"""Exact evidence policy and pinned preparation regressions."""

from copy import deepcopy
from dataclasses import replace
import hashlib
from importlib.resources import files
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from logos_desktop import artifacts, constants, maintenance, runtime_recipes, runtime_stacks as stacks
from tests.artifact_fixtures import appimage
from tests.test_artifacts import response


def document():
    return json.loads(files("logos_desktop").joinpath("assets/runtime-stacks.json").read_bytes())


def evidence(data, kind="tested", host=None):
    stack = data["stacks"][0]
    recipe = data["recipes"][0]
    record = {
        "schema": 1, "record": "evidence", "id": "test-evidence", "kind": kind, "stack_id": stack["id"],
        "runner_sha256": data["runners"][0]["sha256"], "recipe_sha256": stacks.fingerprint(recipe),
        "dependency_sha256": [item["sha256"] for item in recipe["dependencies"]],
        "product_build": stack["product_build"], "source_commit": "a" * 40, "dirty_sha256": None,
        "build_sha256": "b" * 64, "host": host or [], "date": "2026-11-09T00:00:00Z",
        "journeys": [{"name": name, "outcome": "passed", "reason": "synthetic test fixture"}
                     for name in sorted(stacks.JOURNEYS)],
        "limits": "Synthetic evidence for policy tests only", "supersedes": [], "reason": "fixture result",
    }
    if kind == "known-affected":
        record["journeys"][0]["outcome"] = "failed"
    stack["evidence_ids"].append(record["id"])
    data["evidence"].append(record)
    return record


class CatalogTests(unittest.TestCase):
    def parse(self, data):
        return stacks.parse(json.dumps(data).encode())

    def test_bundled_candidates_have_two_exact_identities_and_no_tested_claims(self):
        catalog = stacks.bundled()
        self.assertEqual(len({item.content_key for item in catalog.runners.values()}), 2)
        self.assertFalse(catalog.evidence)
        with self.assertRaises(TypeError):
            catalog.runners["new"] = None

    def test_reject_duplicate_keys_nonfinite_utf8_and_size(self):
        for content in (b'{"schema":1,"schema":1}', b'{"x":NaN}', b"\xff", b"[" * 2000):
            with self.subTest(content=content[:30]), self.assertRaises(stacks.CatalogError):
                stacks.parse(content)
        with self.assertRaises(stacks.CatalogError):
            stacks.parse(b" " * (stacks.MAX_CATALOG_BYTES + 1))

    def test_closed_fields_and_types(self):
        cases = [("schema", True), ("schema", 2), ("runners", {}), ("evidence", [None])]
        for key, value in cases:
            data = document()
            data[key] = value
            with self.subTest(key=key), self.assertRaises(stacks.CatalogError):
                self.parse(data)
        for key, value in (("size", True), ("size", -1), ("sha256", None), ("id", "../runner"),
                           ("url", "file:///tmp/image"), ("url", "https://user:pw@example.org/a"),
                           ("url", "https://example.org/%2e%2e/a"), ("url", "https://example.org/a#fragment"),
                           ("architecture", "aarch64"), ("shell", "execute"), ("companions", [])):
            data = document()
            data["runners"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(stacks.CatalogError):
                self.parse(data)

    def test_duplicates_and_unresolved_graph_are_refused(self):
        for collection in ("runners", "recipes", "stacks"):
            data = document()
            data[collection].append(deepcopy(data[collection][0]))
            with self.subTest(collection=collection), self.assertRaises(stacks.CatalogError):
                self.parse(data)
        data = document()
        data["stacks"][0]["recipe_revision"] += 1
        with self.assertRaises(stacks.CatalogError):
            self.parse(data)

    def test_evidence_requires_complete_journeys_and_exact_bindings(self):
        for key, value in (("runner_sha256", "0" * 64), ("recipe_sha256", "0" * 64),
                           ("product_build", "99.0.0.0"), ("dependency_sha256", []), ("journeys", [])):
            data = document()
            fact = evidence(data)
            fact[key] = value
            with self.subTest(key=key), self.assertRaises(stacks.CatalogError):
                self.parse(data)

    def test_setting_types_and_companion_labels_cannot_inject_behavior(self):
        data = document()
        data["recipes"][0]["settings"][1]["value"] = 1
        with self.assertRaises(stacks.CatalogError):
            self.parse(data)
        for output in ("wine-11.0\nrun me", "wine-11.0 patched-custom", "wine-11.0 (devel)"):
            data = document()
            data["runners"][0]["companions"][0]["expected_output"] = output
            with self.subTest(output=output), self.assertRaises(stacks.CatalogError):
                self.parse(data)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.data = document()

    def assess(self, data=None, **changes):
        catalog = stacks.parse(json.dumps(data or self.data).encode())
        stack = next(iter(catalog.stacks.values()))
        recipe = catalog.recipes[stack.recipe_id]
        arguments = dict(product="Logos", product_build=stack.product_build,
                         host={"architecture": "x86_64", "font-arial": "present"},
                         settings=dict(recipe.settings),
                         dependencies={item.role: item.sha256 for item in recipe.dependencies}, availability="cached")
        arguments.update(changes)
        return stacks.assess(catalog, stack, **arguments)

    def test_offline_evidence_stays_dated_and_unavailable_is_distinct(self):
        evidence(self.data)
        self.assertEqual(self.assess().evidence, "tested")
        result = self.assess(availability="offline")
        self.assertEqual(result.reason, "tested-but-unavailable")
        self.assertFalse(result.selectable)

    def test_product_host_and_custom_mismatch(self):
        evidence(self.data, host=[{"fact": "compositor", "equals": "KWin"}])
        self.assertEqual(self.assess(product_build="55.0.0.0").reason, "product-mismatch")
        self.assertEqual(self.assess().evidence, "not-tested")
        self.assertEqual(self.assess(settings={"renderer": "gl"}).evidence, "custom")
        self.assertEqual(self.assess(dependencies={}).evidence, "custom")

    def test_experimental_and_extra_addon_observations_are_not_tested(self):
        evidence(self.data, "experimental")
        result = self.assess()
        self.assertEqual(result.evidence, "experimental")
        self.assertFalse(result.selectable)
        self.data["evidence"] = []
        self.data["stacks"][0]["evidence_ids"] = []
        evidence(self.data)
        dependencies = {item["role"]: item["sha256"] for item in self.data["recipes"][0]["dependencies"]}
        dependencies["wine-mono"] = "c" * 64
        self.assertEqual(self.assess(dependencies=dependencies).evidence, "custom")

    def test_unknown_host_keeps_known_failure_but_known_conflict_excludes_it(self):
        evidence(self.data, "known-affected", [{"fact": "gpu-vendor", "equals": "NVIDIA"}])
        self.assertEqual(self.assess().evidence, "known-affected")
        self.assertEqual(self.assess(host={"gpu-vendor": "AMD"}).evidence, "not-tested")

    def test_known_failure_vetoes_pass_and_import_with_unknown_custom_observations(self):
        passing = evidence(self.data)
        passing["id"] = "passing"
        self.data["stacks"][0]["evidence_ids"] = ["passing"]
        evidence(self.data, "known-affected")
        self.assertEqual(self.assess().evidence, "known-affected")
        self.assertEqual(self.assess(settings={}, dependencies={}).evidence, "known-affected")
        self.assertEqual(self.assess(runner_sha256=self.data["runners"][0]["sha256"]).evidence, "known-affected")

    def test_no_recommendation_preserves_target(self):
        evidence(self.data, "known-affected")
        catalog = stacks.parse(json.dumps(self.data).encode())
        recipe = next(iter(catalog.recipes.values()))
        self.assertIsNone(stacks.recommend(
            catalog, product="Logos", product_build="54.3.0.0005",
            host={"architecture": "x86_64", "font-arial": "present"}, settings=dict(recipe.settings),
            dependencies={item.role: item.sha256 for item in recipe.dependencies},
            availability={runner.id: "cached" for runner in catalog.runners.values()},
        ))
        self.assertEqual(catalog.stacks[self.data["stacks"][0]["id"]].product_build, "54.3.0.0005")


class PreparationTests(unittest.TestCase):
    def test_icu_duplicate_escape_and_expanded_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "icu.tar.gz"
            for names in (("windows/system32/a", "windows/system32/a"), ("../a",), ("windows/other/a",)):
                with tarfile.open(path, "w:gz") as archive:
                    for name in names:
                        member = tarfile.TarInfo(name)
                        member.size = 1
                        archive.addfile(member, io.BytesIO(b"x"))
                with self.subTest(names=names), self.assertRaises(artifacts.ArtifactError):
                    runtime_recipes.inspect_icu(path)

    def test_preparation_pins_runtime_without_running_it_or_using_latest(self):
        data = document()
        content = appimage()
        runner = data["runners"][0]
        runner.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
        data["recipes"][0]["dependencies"] = []
        catalog = stacks.parse(json.dumps(data).encode())
        with tempfile.TemporaryDirectory() as temporary, patch.object(constants, "STATE_DIR", temporary + "/state"):
            with patch.object(artifacts.requests, "get", return_value=response(content)):
                prepared = runtime_recipes.prepare(catalog, data["stacks"][0]["id"], Path(temporary) / "inputs")
            with patch.object(artifacts.requests, "get", side_effect=AssertionError("offline")):
                again = runtime_recipes.prepare(catalog, data["stacks"][0]["id"], Path(temporary) / "inputs")
            self.assertEqual(again.runner.sha256, prepared.runner.sha256)
            prepared.check()
            prepared.runner.path.chmod(0o600)
            prepared.runner.path.write_bytes(b"changed")
            with self.assertRaises(artifacts.ArtifactError):
                prepared.check()

    def test_recipe_requires_admission_and_preserves_custom_values(self):
        prepared = Mock(recipe=SimpleNamespace(settings={"renderer": "absent"}))
        app = Mock(conf=SimpleNamespace(_overrides=SimpleNamespace(wineserver_binary=None)))
        with self.assertRaises(maintenance.MaintenanceError):
            runtime_recipes.apply_settings(app, prepared, {})
        with (patch.object(maintenance, "installation_owned", return_value=True),
              patch("logos_desktop.runtime.current_prepared", return_value=prepared)):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "Custom"):
                runtime_recipes.apply_settings(app, prepared, {"renderer": "gl"})

    def test_recipe_idempotence_and_checked_failure(self):
        prepared = Mock(recipe=SimpleNamespace(settings={"wpf-software": True}), runner=SimpleNamespace(path="/pinned"))
        app = Mock(conf=SimpleNamespace(_overrides=SimpleNamespace(wineserver_binary=None)))
        with (patch.object(maintenance, "installation_owned", return_value=True),
              patch("logos_desktop.runtime.current_prepared", return_value=prepared),
              patch.object(runtime_recipes, "_registry_value", return_value=1) as observe,
              patch("logos_desktop.wine.run_wine_during_install") as execute):
            runtime_recipes.apply_settings(app, prepared, {"wpf-software": True})
            execute.assert_not_called()
            observe.return_value = None
            execute.return_value = SimpleNamespace(returncode=1)
            with self.assertRaises(maintenance.MaintenanceError):
                runtime_recipes.apply_settings(app, prepared, {})

    def test_smoothing_has_distinct_orientation_and_explicit_absence(self):
        rgb = runtime_recipes._setting_entries("font-smoothing", "rgb")
        bgr = runtime_recipes._setting_entries("font-smoothing", "bgr")
        self.assertEqual([value for _, name, _, value in rgb if name == "FontSmoothingOrientation"], [1])
        self.assertEqual([value for _, name, _, value in bgr if name == "FontSmoothingOrientation"], [0])
        absent = runtime_recipes._setting_entries("font-smoothing", "absent")
        self.assertTrue(all(value is None for _, _, _, value in absent))

    def test_unsupported_addon_handler_refuses_before_transfer(self):
        catalog = stacks.bundled()
        stack = next(iter(catalog.stacks.values()))
        for role in ("wine-mono", "wine-gecko"):
            asset = stacks.Dependency("addon", role, "1", "https://example.org/addon.msi", "a" * 64, 10,
                                      stacks.LAYOUTS[role])
            recipe = replace(catalog.recipes[stack.recipe_id], dependencies=(asset,))
            candidate = replace(catalog, recipes={recipe.id: recipe})
            with (self.subTest(role=role), patch.object(artifacts, "acquire") as acquire,
                  self.assertRaises(stacks.CatalogError)):
                runtime_recipes.prepare(candidate, stack.id, Path("/unused"))
            acquire.assert_not_called()

    def test_smoothing_effective_state_repeat_absence_and_partial_failure(self):
        app = Mock(conf=SimpleNamespace(_overrides=SimpleNamespace(wineserver_binary=None)))
        registry = {}
        writes = []

        def query(app, binary, *, exe, exe_args):
            _, path, _, name = exe_args
            if (path, name) not in registry:
                return SimpleNamespace(returncode=1, stdout="")
            kind, value = registry[path, name]
            text = hex(value) if kind == "REG_DWORD" else value
            return SimpleNamespace(returncode=0, stdout=f"{path}\n    {name}    {kind}    {text}\n")

        def write(app, binary, *, exe, exe_args, timeout):
            writes.append(exe_args)
            action, path, _, name = exe_args[:4]
            if action == "delete":
                del registry[path, name]
            else:
                kind = exe_args[exe_args.index("/t") + 1]
                value = exe_args[exe_args.index("/d") + 1]
                registry[path, name] = (kind, int(value) if kind == "REG_DWORD" else value)
            return SimpleNamespace(returncode=0)

        for value in ("rgb", "bgr", "none", "absent"):
            prepared = Mock(recipe=SimpleNamespace(settings={"font-smoothing": value}),
                            runner=SimpleNamespace(path="/pinned"))
            with (self.subTest(value=value), patch.object(maintenance, "installation_owned", return_value=True),
                  patch("logos_desktop.runtime.current_prepared", return_value=prepared),
                  patch("logos_desktop.wine.run_wine_completed_process", side_effect=query),
                  patch("logos_desktop.wine.run_wine_during_install", side_effect=write)):
                runtime_recipes.apply_settings(app, prepared, {})
                path = r"HKCU\Control Panel\Desktop"
                if value == "absent":
                    self.assertFalse(registry)
                else:
                    self.assertEqual(registry[path, "FontSmoothing"][1], "0" if value == "none" else "2")
                    self.assertEqual(registry[path, "FontSmoothingOrientation"][1], 0 if value == "bgr" else 1)
                count = len(writes)
                runtime_recipes.apply_settings(app, prepared, {})
                self.assertEqual(len(writes), count)
        prepared = Mock(recipe=SimpleNamespace(settings={"font-smoothing": "rgb"}),
                        runner=SimpleNamespace(path="/pinned"))
        writes.clear()

        def fail_second(*args, **kwargs):
            return SimpleNamespace(returncode=1) if len(writes) == 1 else write(*args, **kwargs)

        with (patch.object(maintenance, "installation_owned", return_value=True),
              patch("logos_desktop.runtime.current_prepared", return_value=prepared),
              patch("logos_desktop.wine.run_wine_completed_process", side_effect=query),
              patch("logos_desktop.wine.run_wine_during_install", side_effect=fail_second),
              self.assertRaises(maintenance.MaintenanceError)):
            runtime_recipes.apply_settings(app, prepared, {})
        self.assertEqual(len(registry), 1)
