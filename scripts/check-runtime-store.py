#!/usr/bin/env python3
"""Exercise pinned downloads on disposable supported storage without running Wine."""

import argparse
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from logos_desktop import artifacts, constants, runtime_filesystem, runtime_recipes, runtime_runners, runtime_stacks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, required=True, help="Existing disposable ext4/Btrfs test parent")
    parser.add_argument("--report", type=Path, help="New private JSON result path")
    arguments = parser.parse_args()
    if not arguments.parent.is_absolute() or not arguments.parent.is_dir():
        parser.error("Select an existing absolute disposable test parent")
    catalog = runtime_stacks.bundled()
    with tempfile.TemporaryDirectory(prefix="runner-check-", dir=arguments.parent) as temporary:
        root = Path(temporary)
        constants.STATE_DIR = str(root / "state")
        store = runtime_runners.RunnerStore(root / "store", root / "coordination")
        result = {"catalog_sha256": catalog.sha256, "layout": runtime_filesystem.identity(root),
                  "downloads": [], "qualification": "not-performed"}
        for runner in catalog.runners.values():
            operation = store.download(runner)
            assert store.operation(operation)["result"] == "succeeded"
            with patch.object(artifacts.requests, "get", side_effect=AssertionError("network during cached reuse")):
                store.download(runner)
            result["downloads"].append({"id": runner.id, "sha256": runner.sha256, "size": runner.size})
        copies = store.inventory()
        stack = next(iter(catalog.stacks.values()))
        prepared = runtime_recipes.prepare(catalog, stack.id, root / "prepared",
                                           candidates=tuple(item.path for item in copies))
        prepared.check()
        with patch.object(artifacts.requests, "get", side_effect=AssertionError("network during cached preparation")):
            runtime_recipes.prepare(catalog, stack.id, root / "prepared").check()
        result["dependencies"] = [
            {"role": item.dependency.role, "sha256": item.artifact.sha256, "size": item.artifact.size,
             "file_observations": len(item.files)} for item in prepared.dependencies
        ]
        assert "reference-integration-incomplete" in store.removal_reasons(copies[0].sha256)
        # Only this empty, owned fixture models all uses. Application removal remains disabled.
        with patch.object(runtime_runners, "INTEGRATED_REFERENCE_KINDS", runtime_runners.REFERENCE_KINDS):
            for copy in copies:
                store.remove(copy.generation)
                assert not copy.path.exists()
        assert not store.inventory()
        result["unused_fixture_bytes_removed"] = sum(item.size for item in copies)
        content = json.dumps(result, indent=2) + "\n"
        if arguments.report is not None:
            with arguments.report.open("x") as output:
                output.write(content)
        print(content, end="")


if __name__ == "__main__":
    main()
