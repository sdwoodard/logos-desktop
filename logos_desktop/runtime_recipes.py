"""Pinned preparation and code-owned settings for admitted stack consumers."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import tarfile

from . import artifacts, maintenance, runtime_stacks


MAX_ICU_FILES = 4096
MAX_ICU_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class PreparedDependency:
    dependency: runtime_stacks.Dependency
    artifact: artifacts.AcceptedArtifact
    files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PreparedStack:
    catalog_sha256: str
    stack: runtime_stacks.Stack
    recipe: runtime_stacks.Recipe
    runner: artifacts.AcceptedArtifact
    dependencies: tuple[PreparedDependency, ...]
    catalog: runtime_stacks.Catalog

    def check(self):
        if (self.catalog.sha256 != self.catalog_sha256 or self.catalog.stacks[self.stack.id] != self.stack
                or self.catalog.recipes[self.recipe.id] != self.recipe
                or self.catalog.runners[self.stack.runner_id].sha256 != self.runner.sha256):
            raise artifacts.ArtifactError("Prepared stack model identity changed")
        self.runner.check()
        for role in ("wine", "wine64", "wineserver"):
            alias = self.runner.path.parent / role
            if not alias.is_symlink() or os.readlink(alias) != self.runner.path.name:
                raise artifacts.ArtifactError("Prepared companion identity changed")
        for item in self.dependencies:
            item.artifact.check()
            if item.dependency.role == "icu" and inspect_icu(item.artifact.path) != item.files:
                raise artifacts.ArtifactError("Prepared ICU observations changed")


def inspect_icu(path):
    """Observe the exact bounded Windows subtree without extracting archive paths."""
    observations = []
    names = set()
    roots = set()
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        for count, member in enumerate(archive):
            if count >= MAX_ICU_FILES:
                raise artifacts.ArtifactError("ICU entry budget exceeded")
            name = PurePosixPath(member.name)
            if (name.is_absolute() or ".." in name.parts or "\\" in member.name or member.name in names
                    or not (member.isfile() or member.isdir())):
                raise artifacts.ArtifactError("Unsupported ICU member")
            names.add(member.name)
            if not member.isfile():
                continue
            root = next((root for root in (PurePosixPath("windows"), PurePosixPath("icu-win/windows"))
                         if name.is_relative_to(root)), None)
            if root is None:
                continue
            relative = name.relative_to(root)
            if (not relative.parts or (relative.parts[0] not in ("system32", "syswow64")
                                       and relative.parts[:2] != ("globalization", "ICU"))):
                raise artifacts.ArtifactError("Unsupported ICU destination")
            total += member.size
            if total > MAX_ICU_BYTES:
                raise artifacts.ArtifactError("ICU expanded byte budget exceeded")
            roots.add(str(root))
            with archive.extractfile(member) as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            observations.append((str(relative), digest))
    if len(roots) != 1 or not observations:
        raise artifacts.ArtifactError("ICU requires one populated Windows subtree")
    if len({name for name, _ in observations}) != len(observations):
        raise artifacts.ArtifactError("Duplicate ICU destination")
    return tuple(sorted(observations))


def prepare(catalog, stack_id, root, *, candidates=(), checkpoint=None, status=None):
    """Acquire all recipe inputs without touching profile selection or a prefix."""
    stack = catalog.stacks[stack_id]
    recipe = catalog.recipes[stack.recipe_id]
    # Validate every handler before acquiring anything. Unsupported addons fail closed.
    specs = [(asset, asset.spec()) for asset in recipe.dependencies]
    root = Path(root)

    def cached(spec):
        # Only the exact accepted generation is considered. No basename search.
        if spec.digest is None:
            return tuple(candidates)
        path = root / ".artifacts" / spec.key / spec.digest.value / spec.name
        return (path, *candidates) if path.exists() else tuple(candidates)

    runner_spec = catalog.runners[stack.runner_id].spec()
    runner = artifacts.acquire(runner_spec, root, cached(runner_spec),
                               checkpoint=checkpoint, status=status)
    prepared = []
    for asset, spec in specs:
        accepted = artifacts.acquire(spec, root, cached(spec), checkpoint=checkpoint, status=status)
        observations = inspect_icu(accepted.path) if asset.role == "icu" else ()
        prepared.append(PreparedDependency(asset, accepted, observations))
    return PreparedStack(catalog.sha256, stack, recipe, runner, tuple(prepared), catalog)


def _setting_entries(key, value):
    if key == "font-smoothing":
        path = r"HKCU\Control Panel\Desktop"
        smoothing, orientation, kind = {
            "rgb": ("2", 1, 2), "bgr": ("2", 0, 2), "none": ("0", 1, 0), "absent": (None, None, None),
        }[value]
        return ((path, "FontSmoothing", "REG_SZ", smoothing),
                (path, "FontSmoothingOrientation", "REG_DWORD", orientation),
                (path, "FontSmoothingType", "REG_DWORD", kind),
                (path, "FontSmoothingGamma", "REG_DWORD", None if value == "absent" else 1400))
    writes = {
        "windows-version": (r"HKCU\Software\Wine", "Version", "REG_SZ", value),
        "wpf-software": (r"HKCU\Software\Microsoft\Avalon.Graphics", "DisableHWAcceleration", "REG_DWORD",
                         int(value) if type(value) is bool else value),
        "menu-builder-disabled": (r"HKCU\Software\Wine\DllOverrides", "winemenubuilder.exe", "REG_SZ",
                                  "" if value is True else "builtin"),
        "renderer": (r"HKCU\Software\Wine\Direct3D", "renderer", "REG_SZ", None if value == "absent" else value),
    }
    return (writes[key],)


def _registry_value(app, binary, path, name, kind):
    from . import wine

    result = wine.run_wine_completed_process(app, binary, exe="reg.exe", exe_args=["query", path, "/v", name])
    if result is None:
        raise maintenance.MaintenanceError("Recipe observation could not start")
    if result.returncode == 1:
        return None
    if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > 16384:
        raise maintenance.MaintenanceError("Recipe observation failed")
    values = [re.fullmatch(r"\s{4}" + re.escape(name) + r"\s{4}" + kind + r"(?:\s{4}(.*))?", line)
              for line in result.stdout.splitlines()]
    values = [item.group(1) or "" for item in values if item is not None]
    if len(values) != 1:
        raise maintenance.MaintenanceError("Recipe observation returned an unexpected type")
    try:
        return int(values[0], 0) if kind == "REG_DWORD" else values[0]
    except ValueError as error:
        raise maintenance.MaintenanceError("Recipe DWORD observation was invalid") from error


def apply_settings(app, prepared, observed):
    """Apply a prepared recipe only inside the owning installation transaction.

    Existing-installation callers must create full recovery before entering here.
    M4 owns that admission. This primitive does not create a recovery point.
    """
    from . import runtime, wine

    if not maintenance.installation_owned():
        raise maintenance.MaintenanceError("Recipe execution needs owned installation admission")
    if runtime.current_prepared() is not prepared:
        raise maintenance.MaintenanceError("Recipe execution needs its exact prepared runtime context")
    prepared.check()
    settings = prepared.recipe.settings
    if getattr(app.conf._overrides, "wineserver_binary", None) is not None:
        raise maintenance.MaintenanceError("Explicit wineserver override is a custom runtime")
    if any(key in observed and observed[key] != value for key, value in settings.items()):
        raise maintenance.MaintenanceError("Custom settings need explicit recipe-change authorization")
    binary = str(prepared.runner.path)
    for key, value in settings.items():
        app.checkpoint()
        for path, name, kind, expected in _setting_entries(key, value):
            if _registry_value(app, binary, path, name, kind) == expected:
                continue
            args = ["delete", path, "/v", name, "/f"] if expected is None else [
                "add", path, "/v", name, "/t", kind, "/d", str(expected), "/f",
            ]
            result = wine.run_wine_during_install(app, binary, exe="reg.exe", exe_args=args, timeout=60)
            if result is None or result.returncode != 0:
                raise maintenance.MaintenanceError("Recipe registry write failed")
            if _registry_value(app, binary, path, name, kind) != expected:
                raise maintenance.MaintenanceError("Recipe effective state does not match")


def install_icu(prepared_dependency, prefix, *, checkpoint=None):
    """Consume only the inspected ICU input, with no dependency discovery."""
    import tempfile
    from . import wine

    item = prepared_dependency
    if item.dependency.role != "icu" or inspect_icu(item.artifact.check()) != item.files:
        raise artifacts.ArtifactError("Prepared ICU input mismatch")
    if not maintenance.installation_owned():
        raise maintenance.MaintenanceError("ICU installation needs owned admission")
    with tempfile.TemporaryDirectory(prefix="logos-pinned-icu-") as temporary:
        root = Path(temporary)
        with tarfile.open(item.artifact.path, "r:gz") as archive:
            for member in archive:
                if checkpoint:
                    checkpoint()
                if not member.isfile():
                    continue
                path = PurePosixPath(member.name)
                base = next((base for base in (PurePosixPath("windows"), PurePosixPath("icu-win/windows"))
                             if path.is_relative_to(base)), None)
                if base is None:
                    continue
                destination = root / path.relative_to(base)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
        for name, expected in item.files:
            if artifacts.regular_hash(root / name) != expected:
                raise artifacts.ArtifactError("Extracted ICU input mismatch")
        wine.copy_icu_files(root, Path(prefix))
        for name, expected in item.files:
            if artifacts.regular_hash(Path(prefix) / "drive_c/windows" / name) != expected:
                raise artifacts.ArtifactError("Installed ICU bytes do not match the prepared input")
