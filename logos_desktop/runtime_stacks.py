"""Bundled runtime facts and exact stack policy, independent of release caches."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
from importlib.resources import files
import json
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from packaging.version import InvalidVersion, Version

from . import artifacts, compatibility, constants


MAX_CATALOG_BYTES = 4 * 1024 * 1024
SETTING_VALUES = {
    "windows-version": ("win10", "win11"),
    "wpf-software": (False, True),
    "menu-builder-disabled": (False, True),
    "renderer": ("absent", "gdi", "gl"),
    "font-smoothing": ("absent", "rgb", "bgr", "none"),
}
HOST_FACTS = frozenset((
    "distribution", "distribution-release", "kernel", "compositor", "compositor-version",
    "session-protocol", "gpu-vendor", "gpu-renderer", "driver-version", "architecture",
    "font-arial", "font-fallback",
))
JOURNEYS = frozenset((
    "sign-in", "download", "index", "library", "search", "typing", "scroll", "restart",
    "trial", "apply", "return", "network-denial", "retained-work", "online-resume",
))
LAYOUTS = {
    "icu": "icu-win-v1", "winetricks": "winetricks-script-v1", "font-input": "arial-cab-v1",
    "wine-mono": "wine-mono-msi-v1", "wine-gecko": "wine-gecko-msi-v1",
}


class CatalogError(ValueError):
    """Bundled facts are invalid or unsupported by this controller."""


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("Duplicate JSON key")
        result[key] = value
    return result


def decode(content, limit=MAX_CATALOG_BYTES):
    if not isinstance(content, bytes) or len(content) > limit:
        raise CatalogError("JSON byte budget exceeded")
    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=_object,
                          parse_constant=lambda value: _invalid("Nonfinite JSON number"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise CatalogError("Invalid bounded JSON") from error


def _invalid(message):
    raise CatalogError(message)


def fields(value, expected):
    if type(value) is not dict or set(value) != set(expected.split()):
        raise CatalogError("Unsupported or missing record fields")


def text(value, pattern=None, limit=4096):
    if (not isinstance(value, str) or len(value.encode("utf-8")) > limit
            or (pattern is not None and re.fullmatch(pattern, value, re.ASCII) is None)):
        raise CatalogError("Invalid bounded text")
    return value


def identifier(value):
    return text(value, r"[a-z0-9][a-z0-9._-]{0,95}")


def digest(value):
    return text(value, r"[a-f0-9]{64}")


def positive(value):
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise CatalogError("Invalid positive integer")
    return value


def sequence(value, maximum):
    if type(value) is not list or len(value) > maximum:
        raise CatalogError("Invalid bounded array")
    return value


def https(value):
    text(value)
    try:
        parsed = urlsplit(value)
        decoded = unquote(parsed.path)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.query or parsed.port is not None
                or not parsed.path.startswith("/") or "\\" in value
                or any(ord(char) <= 32 or ord(char) == 127 for char in value)
                or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
                or "\\" in decoded or re.search(r"%(?:2f|5c)", parsed.path, re.IGNORECASE)
                or any(part in (".", "..") for part in decoded.split("/"))):
            raise CatalogError("Invalid HTTPS asset identity")
    except ValueError as error:
        raise CatalogError("Invalid HTTPS asset identity") from error
    return value


def _record(value, kind, keys):
    fields(value, "schema record id " + keys)
    if type(value["schema"]) is not int or value["schema"] != 1 or value["record"] != kind:
        raise CatalogError("Unsupported runtime record schema")
    identifier(value["id"])


def _predicates(value):
    result = {}
    for predicate in sequence(value, 16):
        fields(predicate, "fact equals")
        if predicate["fact"] not in HOST_FACTS or predicate["fact"] in result:
            raise CatalogError("Unsupported or duplicate host predicate")
        result[predicate["fact"]] = text(predicate["equals"])
    return MappingProxyType(result)


@dataclass(frozen=True)
class Runner:
    id: str
    provider: str
    family: str
    version: str
    size: int
    sha256: str
    url: str
    companions: tuple

    @property
    def content_key(self):
        return (self.provider, "x86_64", "appimage2", self.sha256)

    def spec(self):
        name = unquote(PurePosixPath(urlsplit(self.url).path).name)
        return artifacts.ArtifactSpec(self.url, name, "appimage",
                                      artifacts.Digest("sha256", self.sha256, "bundled runtime catalog"), self.size)


@dataclass(frozen=True)
class Dependency:
    id: str
    role: str
    version: str
    url: str
    sha256: str
    size: int
    layout: str

    def spec(self):
        kinds = {"icu": "icu", "winetricks": "winetricks", "wine-mono": "msi", "wine-gecko": "msi"}
        if self.role not in kinds:
            raise CatalogError("Unsupported pinned dependency handler")
        if self.role in ("wine-mono", "wine-gecko"):
            # Addons need product-specific MSI attestation, never generic MSI acceptance.
            raise CatalogError("Addon MSI handler is unavailable until product attestation is implemented")
        return artifacts.ArtifactSpec(self.url, PurePosixPath(urlsplit(self.url).path).name, kinds[self.role],
                                      artifacts.Digest("sha256", self.sha256, "bundled recipe"), self.size,
                                      None)


@dataclass(frozen=True)
class Recipe:
    id: str
    revision: int
    sha256: str
    settings: object
    dependencies: tuple[Dependency, ...]
    host: object


@dataclass(frozen=True)
class Stack:
    id: str
    runner_id: str
    recipe_id: str
    recipe_revision: int
    product_build: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: str
    stack_id: str
    runner_sha256: str
    recipe_sha256: str
    dependencies: tuple[str, ...]
    product_build: str
    host: object
    date: str
    reason: str
    journeys: object


@dataclass(frozen=True)
class Catalog:
    runners: object
    recipes: object
    stacks: object
    evidence: object
    sha256: str


def _runner(value):
    _record(value, "runner", "revision provider family version_display architecture format size sha256 url "
            "source_url source_revision packaging_revision companions")
    positive(value["revision"])
    identifier(value["provider"])
    identifier(value["family"])
    text(value["version_display"])
    text(value["packaging_revision"])
    if value["source_revision"] is not None:
        text(value["source_revision"], limit=96)
    if value["architecture"] != "x86_64" or value["format"] != "appimage2":
        raise CatalogError("Unsupported runner format")
    https(value["source_url"])
    companions = []
    roles = set()
    for companion in sequence(value["companions"], 3):
        fields(companion, "role alias parser expected_output")
        role = companion["role"]
        if role not in ("wine", "wine64", "wineserver") or role in roles or companion["alias"] != role:
            raise CatalogError("Invalid companion role")
        parser = "wineserver-numeric-v1" if role == "wineserver" else "wine-numeric-v1"
        if companion["parser"] != parser:
            raise CatalogError("Unsupported companion parser")
        output = text(companion["expected_output"])
        try:
            if role == "wineserver":
                compatibility.parse_wineserver_version(output)
            else:
                compatibility.parse_wine_version(output)
        except ValueError as error:
            raise CatalogError("Invalid expected companion version") from error
        companions.append((role, output))
        roles.add(role)
    if not {"wine", "wineserver"}.issubset(roles):
        raise CatalogError("Required companion identity is missing")
    wine_version = compatibility.parse_wine_version(dict(companions)["wine"]).version
    if any((compatibility.parse_wineserver_version(output) if role == "wineserver"
            else compatibility.parse_wine_version(output).version) != wine_version for role, output in companions):
        raise CatalogError("Companion versions disagree")
    return Runner(value["id"], value["provider"], value["family"], value["version_display"],
                  positive(value["size"]), digest(value["sha256"]), https(value["url"]), tuple(companions))


def _recipe(value):
    _record(value, "recipe", "revision controller_min settings dependencies required_host_facts")
    try:
        if Version(text(value["controller_min"])) > Version(constants.APP_VERSION):
            raise CatalogError("Recipe needs a newer controller")
    except InvalidVersion as error:
        raise CatalogError("Invalid recipe controller version") from error
    settings = {}
    for setting in sequence(value["settings"], 5):
        fields(setting, "key value")
        key, item = setting["key"], setting["value"]
        if not isinstance(key, str) or key not in SETTING_VALUES or key in settings:
            raise CatalogError("Unsupported or duplicate recipe setting")
        if not any(type(item) is type(option) and item == option for option in SETTING_VALUES[key]):
            raise CatalogError("Invalid recipe setting type or value")
        settings[key] = item
    dependencies = []
    ids = set()
    roles = set()
    for asset in sequence(value["dependencies"], 16):
        fields(asset, "id role version url sha256 size layout")
        identifier(asset["id"])
        role = asset["role"]
        if (not isinstance(role, str) or role not in LAYOUTS or asset["layout"] != LAYOUTS[role]
                or asset["id"] in ids or role in roles):
            raise CatalogError("Unsupported or duplicate dependency")
        dependencies.append(Dependency(asset["id"], role, text(asset["version"]), https(asset["url"]),
                                       digest(asset["sha256"]), positive(asset["size"]), asset["layout"]))
        ids.add(asset["id"])
        roles.add(role)
    return Recipe(value["id"], positive(value["revision"]), fingerprint(value), MappingProxyType(settings),
                  tuple(dependencies), _predicates(value["required_host_facts"]))


def _stack(value):
    _record(value, "stack", "revision runner_id recipe_id recipe_revision product product_build "
            "controller_schema evidence_ids")
    positive(value["revision"])
    if value["product"] != "Logos" or type(value["controller_schema"]) is not int or value["controller_schema"] != 1:
        raise CatalogError("Unsupported stack product or controller schema")
    evidence = tuple(identifier(item) for item in sequence(value["evidence_ids"], 64))
    if len(set(evidence)) != len(evidence):
        raise CatalogError("Duplicate evidence reference")
    return Stack(value["id"], identifier(value["runner_id"]), identifier(value["recipe_id"]),
                 positive(value["recipe_revision"]), text(value["product_build"], r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+"),
                 evidence)


def _evidence(value):
    _record(value, "evidence", "kind stack_id runner_sha256 recipe_sha256 dependency_sha256 product_build "
            "source_commit dirty_sha256 build_sha256 host date journeys limits supersedes reason")
    if value["kind"] not in ("tested", "experimental", "known-affected"):
        raise CatalogError("Unsupported evidence kind")
    text(value["source_commit"], r"[a-f0-9]{40}")
    if value["dirty_sha256"] is not None:
        digest(value["dirty_sha256"])
    digest(value["build_sha256"])
    text(value["limits"], limit=8192)
    supersedes = sequence(value["supersedes"], 64)
    if len(set(identifier(item) for item in supersedes)) != len(supersedes):
        raise CatalogError("Duplicate supersession")
    date = text(value["date"])
    try:
        if not date.endswith("Z") or datetime.fromisoformat(date).tzinfo is None:
            raise ValueError
    except ValueError as error:
        raise CatalogError("Invalid evidence UTC date") from error
    journeys = {}
    for journey in sequence(value["journeys"], 14):
        fields(journey, "name outcome reason")
        name = journey["name"]
        if name not in JOURNEYS or name in journeys or journey["outcome"] not in (
            "passed", "failed", "unknown", "not-run",
        ):
            raise CatalogError("Unsupported or duplicate journey")
        text(journey["reason"])
        journeys[name] = journey["outcome"]
    if value["kind"] == "tested" and (set(journeys) != JOURNEYS or set(journeys.values()) != {"passed"}):
        raise CatalogError("Tested evidence requires every core journey")
    if value["kind"] == "known-affected" and "failed" not in journeys.values():
        raise CatalogError("Affected evidence needs a failed journey")
    dependencies = tuple(digest(item) for item in sequence(value["dependency_sha256"], 16))
    if len(set(dependencies)) != len(dependencies):
        raise CatalogError("Duplicate dependency digest")
    return Evidence(value["id"], value["kind"], identifier(value["stack_id"]), digest(value["runner_sha256"]),
                    digest(value["recipe_sha256"]), dependencies,
                    text(value["product_build"], r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+"),
                    _predicates(value["host"]), date, text(value["reason"]), MappingProxyType(journeys))


def _parse(content):
    document = decode(content)
    fields(document, "schema runners recipes stacks evidence")
    if type(document["schema"]) is not int or document["schema"] != 1:
        raise CatalogError("Unsupported catalog schema")
    collections = []
    for kind, parser, maximum in (("runners", _runner, 512), ("recipes", _recipe, 1024),
                                  ("stacks", _stack, 1024), ("evidence", _evidence, 4096)):
        records = {}
        for value in sequence(document[kind], maximum):
            item = parser(value)
            if item.id in records:
                raise CatalogError("Duplicate runtime identity")
            records[item.id] = item
        collections.append(records)
    runners, recipes, stacks, evidence = collections
    for stack in stacks.values():
        if (stack.runner_id not in runners or stack.recipe_id not in recipes
                or recipes[stack.recipe_id].revision != stack.recipe_revision
                or any(key not in evidence for key in stack.evidence_ids)):
            raise CatalogError("Unresolved stack reference")
        if any(evidence[key].stack_id != stack.id for key in stack.evidence_ids):
            raise CatalogError("Evidence belongs to another stack")
    for fact in evidence.values():
        if fact.stack_id not in stacks:
            raise CatalogError("Unresolved evidence stack")
        stack = stacks[fact.stack_id]
        recipe = recipes[stack.recipe_id]
        if (fact.id not in stack.evidence_ids or fact.runner_sha256 != runners[stack.runner_id].sha256
                or fact.recipe_sha256 != recipe.sha256 or fact.product_build != stack.product_build
                or sorted(fact.dependencies) != sorted(asset.sha256 for asset in recipe.dependencies)):
            raise CatalogError("Contradictory evidence identity")
    for raw in document["evidence"]:
        if any(key not in evidence or key == raw["id"] for key in raw["supersedes"]):
            raise CatalogError("Unresolved evidence supersession")
    # Supersession is provenance, not permission to erase a negative fact.
    def visit(key, active, finished):
        if key in active:
            raise CatalogError("Cyclic evidence supersession")
        if key in finished:
            return
        active.add(key)
        for previous in supersession[key]:
            visit(previous, active, finished)
        active.remove(key)
        finished.add(key)

    supersession = {raw["id"]: raw["supersedes"] for raw in document["evidence"]}
    finished = set()
    for key in supersession:
        visit(key, set(), finished)
    return Catalog(*(MappingProxyType(items) for items in collections), fingerprint(document))


def parse(content):
    try:
        return _parse(content)
    except (KeyError, TypeError, UnicodeError, OverflowError, RecursionError) as error:
        raise CatalogError("Invalid runtime field type") from error


def bundled():
    resource = files("logos_desktop").joinpath("assets/runtime-stacks.json")
    with resource.open("rb") as source:
        return parse(source.read(MAX_CATALOG_BYTES + 1))


def matches(predicates, observed, *, negative=False):
    """Unknown facts cannot pass positive evidence or exclude a known failure."""
    return all((key not in observed or observed[key] is None) and negative or observed.get(key) == expected
               for key, expected in predicates.items())


@dataclass(frozen=True)
class Assessment:
    evidence: str
    availability: str
    reason: str
    selectable: bool


def assess(catalog, stack, *, product, product_build, host, settings, dependencies, availability,
           runner_sha256=None):
    runner = catalog.runners[stack.runner_id]
    actual_digest = runner.sha256 if runner_sha256 is None else digest(runner_sha256)
    recipe = catalog.recipes[stack.recipe_id]
    if product != "Logos" or product_build != stack.product_build:
        return Assessment("not-tested", availability, "product-mismatch", False)

    for fact in catalog.evidence.values():
        if (fact.kind != "known-affected" or fact.runner_sha256 != actual_digest
                or fact.product_build != product_build or not matches(fact.host, host, negative=True)):
            continue
        affected_recipe = catalog.recipes[catalog.stacks[fact.stack_id].recipe_id]
        settings_conflict = any(key in settings and settings[key] is not None and settings[key] != value
                                for key, value in affected_recipe.settings.items())
        dependencies_conflict = any(asset.role in dependencies and dependencies[asset.role] is not None
                                    and dependencies[asset.role] != asset.sha256
                                    for asset in affected_recipe.dependencies)
        if not settings_conflict and not dependencies_conflict:
            return Assessment("known-affected", availability, fact.reason, False)

    settings_match = all(key in settings and type(settings[key]) is type(value) and settings[key] == value
                         for key, value in recipe.settings.items())
    extra_override = any(key not in recipe.settings for key in settings)
    dependencies_match = dependencies == {asset.role: asset.sha256 for asset in recipe.dependencies}
    if not settings_match or extra_override or not dependencies_match or actual_digest != runner.sha256:
        return Assessment("custom", availability, "recipe-or-dependency-mismatch", False)
    if not matches(recipe.host, host):
        return Assessment("not-tested", availability, "host-mismatch-or-unknown", False)
    for key in stack.evidence_ids:
        fact = catalog.evidence[key]
        if fact.kind == "tested" and matches(fact.host, host):
            usable = availability in ("cached", "available")
            return Assessment("tested", availability, "dated-bundled-evidence" if usable else "tested-but-unavailable",
                              usable)
    if any(catalog.evidence[key].kind == "experimental" for key in stack.evidence_ids):
        return Assessment("experimental", availability, "advanced-choice-required", False)
    return Assessment("not-tested", availability, "no-matching-project-evidence",
                      availability in ("cached", "available"))


def recommend(catalog, *, product, product_build, host, settings, dependencies, availability):
    """Resolve the current product target without changing it or the current selection."""
    candidates = []
    for stack in catalog.stacks.values():
        runner = catalog.runners[stack.runner_id]
        result = assess(catalog, stack, product=product, product_build=product_build, host=host,
                        settings=settings, dependencies=dependencies,
                        availability=availability.get(runner.id, "unchecked"))
        if not result.selectable:
            continue
        experimental = any(catalog.evidence[key].kind == "experimental" for key in stack.evidence_ids)
        output = dict(runner.companions)["wine"]
        version = compatibility.parse_wine_version(output)
        eligible, _ = compatibility.wine_eligibility(version, Version(product_build), "10")
        if not eligible or (result.evidence != "tested" and (experimental or version.channel.value != "stable")):
            continue
        candidates.append((result.evidence == "tested", version.version, stack.id))
    return max(candidates)[2] if candidates else None
