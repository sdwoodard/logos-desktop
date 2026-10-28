"""Repository-specific release policy, independent of transfer and installation."""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import platform
import re
import threading
import time
from urllib.parse import unquote, urlsplit

from packaging.version import Version

from . import artifacts, compatibility, constants


WINE_REPOSITORY = "mmtrt/WINE_AppImage"
ICU_REPOSITORY = "FaithLife-Community/icu"
SEMVER = re.compile(r"v?(\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?(?:\+[0-9A-Za-z.-]+)?)\Z")
WINE_ASSET = re.compile(r"wine-devel_(\d+\.\d+(?:\.\d+)?)-x86_64\.AppImage\Z")
ICU_TAG = re.compile(r"\d+\.\d+(?:-custom(?:\+\d+)?)?\Z")
CACHE_SCHEMA = 1
WINE_SOURCE_API = "https://api.github.com/repos/wine-mirror/wine"
WINE_SOURCE_RAW = "https://raw.githubusercontent.com/wine-mirror/wine"
WINE_SOURCE_POLICY = "wine-source-1"
WINE_RECOMMENDATION_POLICY = (
    "Recommended Wine uses the available development AppImage, not a Logos-tested certification."
)


@dataclass(frozen=True)
class WineTarget:
    version: str
    channel: str
    commit: str

    def validate(self):
        if (not all(isinstance(value, str) for value in (self.version, self.channel, self.commit))
                or re.fullmatch(r"\d+\.\d+(?:\.\d+)*", self.version) is None):
            raise ValueError("Wine target version must be numeric")
        facts = compatibility.parse_wine_version(f"wine-{self.version}")
        if facts.channel.value != self.channel or facts.rc is not None:
            raise ValueError("Wine target must be a released stable or development version")
        if re.fullmatch(r"[0-9a-f]{40}", self.commit) is None:
            raise ValueError("Wine release commit is invalid")
        return self


@dataclass(frozen=True)
class WineTargetLookup:
    target: WineTarget | None = None
    status: str = "unchecked"
    message: str = "Wine release information has not been checked"
    checked: float | None = None
    stale: bool = False


def fetch_wine_target(channel, request):
    """Cross-check published Wine history on its GitHub mirror, without signature claims."""
    branches = {"devel": "master", "stable": "stable"}
    if channel not in branches:
        raise artifacts.ArtifactError("Unknown Wine release channel")
    try:
        history = json.loads(request(
            f"{WINE_SOURCE_API}/commits?sha={branches[channel]}&path=ANNOUNCE.md&per_page=100"
        ))
        if not isinstance(history, list) or len(history) > 100:
            raise ValueError("Malformed Wine release history")
        candidates = []
        for item in history:
            message = item["commit"]["message"]
            match = re.fullmatch(r"Release (\d+\.\d+(?:\.\d+)*)\.", message)
            if match is None:
                continue
            facts = compatibility.parse_wine_version(f"wine-{match[1]}")
            if facts.channel.value == channel:
                candidates.append(WineTarget(match[1], channel, item["sha"]))
        if not candidates:
            raise ValueError("No released Wine target in the bounded announcement history")
        target = max(candidates, key=lambda value: Version(value.version)).validate()
        if sum(Version(value.version) == Version(target.version) for value in candidates) != 1:
            raise ValueError("Wine release commit is ambiguous")

        version = request(f"{WINE_SOURCE_RAW}/{target.commit}/VERSION").decode("utf-8").strip()
        if version != f"Wine version {target.version}":
            raise ValueError("Wine release VERSION disagrees with its release commit")
        announcement = request(f"{WINE_SOURCE_RAW}/{target.commit}/ANNOUNCE.md").decode("utf-8")
        intro = " ".join(announcement[:1024].split())
        number = re.escape(target.version)
        wording = (rf"The Wine development release {number} is now available\." if channel == "devel"
                   else rf"(?:stable|maintenance) release (?:Wine )?{number} is now available\.")
        if re.search(wording, intro) is None:
            raise ValueError("Wine announcement does not identify the published target")

        tag = f"wine-{target.version}"
        reference = json.loads(request(f"{WINE_SOURCE_API}/git/ref/tags/{tag}"))
        if reference["ref"] != f"refs/tags/{tag}":
            raise ValueError("Wine tag reference disagrees with the target")
        identity = reference["object"]
        if identity["type"] == "tag":
            tag_sha = identity["sha"]
            if re.fullmatch(r"[0-9a-f]{40}", tag_sha) is None:
                raise ValueError("Invalid Wine annotated tag identity")
            annotated = json.loads(request(f"{WINE_SOURCE_API}/git/tags/{tag_sha}"))
            if annotated["sha"] != tag_sha or annotated["tag"] != tag:
                raise ValueError("Wine annotated tag disagrees with the target")
            identity = annotated["object"]
        if identity["type"] != "commit" or identity["sha"] != target.commit:
            raise ValueError("Wine tag does not point to its release commit")
        return target
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise artifacts.ArtifactError(f"Invalid published Wine metadata: {error}") from error


@dataclass(frozen=True)
class SoftwareReleaseInfo:
    version: str
    download_url: str
    digest: artifacts.Digest | None = None
    name: str = ""
    size: int | None = None
    repository: str = ""
    tag: str = ""
    prerelease: bool = False
    error: str = ""

    def spec(self, kind):
        if self.error or not self.name:
            raise artifacts.ArtifactError(self.error or "Release asset identity is missing")
        return artifacts.ArtifactSpec(self.download_url, self.name, kind, self.digest, self.size)


@dataclass(frozen=True)
class GithubSoftwareReleasesInfo:
    latest: SoftwareReleaseInfo | None
    pre_release: SoftwareReleaseInfo | None
    status: str = "ok"
    message: str = ""
    retry_after: float = 0


@dataclass(frozen=True)
class ReleaseLookup:
    release: SoftwareReleaseInfo | None = None
    status: str = "unchecked"
    message: str = "Release information has not been checked"
    checked: float | None = None
    stale: bool = False


def channel_name(channel):
    if channel == "dev":
        return "beta"
    if channel not in ("stable", "beta"):
        raise artifacts.ArtifactError("Unknown controller release channel")
    return channel


def version_key(version):
    # SemVer build labels do not affect precedence.
    return Version(version.split("+", 1)[0])


def choose(catalog, channel):
    channel = channel_name(channel)
    candidates = [catalog.latest]
    if channel == "beta":
        candidates.append(catalog.pre_release)
    candidates = [release for release in candidates if release is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda release: version_key(release.version))


def policy_key(repository):
    role = constants.APP_RELEASE_LINEAGE if repository == constants.REPOSITORY_SLUG else repository.rsplit("/", 1)[-1]
    return f"{CACHE_SCHEMA}:{repository}:{role}:linux:x86_64"


def release_digest(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise artifacts.ArtifactError("Unsupported release digest metadata")
    digest = artifacts.Digest("sha256", value[7:], "selected GitHub release asset digest")
    digest.validate()
    return digest


def _asset(item, repository, version, name):
    assets = item.get("assets")
    if not isinstance(assets, list):
        raise artifacts.ArtifactError("Release assets are malformed")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matches) != 1:
        raise artifacts.ArtifactError(f"Release {version} needs exactly one compatible {name} asset")
    asset = matches[0]
    url = asset.get("browser_download_url")
    parsed = urlsplit(url) if isinstance(url, str) else None
    expected_path = f"/{repository}/releases/download/{item['tag_name']}/{name}"
    if (parsed is None or parsed.scheme != "https" or parsed.netloc != "github.com"
            or unquote(parsed.path) != expected_path or parsed.query or parsed.fragment):
        raise artifacts.ArtifactError("Release asset URL does not match its repository and tag")
    size = asset.get("size")
    if type(size) is not int or size <= 0:
        raise artifacts.ArtifactError("Release asset size is missing or invalid")
    digest = release_digest(asset.get("digest"))
    if repository in (constants.REPOSITORY_SLUG, WINE_REPOSITORY) and digest is None:
        raise artifacts.ArtifactError("Release has no required SHA-256 digest")
    return SoftwareReleaseInfo(version, url, digest, name, size, repository, item["tag_name"], item["prerelease"])


def _publication(item):
    published = item.get("published_at")
    identity = item.get("id")
    if not isinstance(published, str) or type(identity) is not int:
        raise artifacts.ArtifactError("Release publication identity is missing")
    instant = datetime.fromisoformat(published)
    if instant.tzinfo is None:
        raise artifacts.ArtifactError("Release publication time has no timezone")
    return instant.timestamp(), identity


def select(data, repository):
    """Select the newest release before checking that release's intended asset."""
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise artifacts.ArtifactError("Malformed release metadata")
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        return GithubSoftwareReleasesInfo(None, None, "incompatible", "Only Linux x86_64 assets are supported")

    groups = {False: [], True: []}
    for item in data:
        if type(item.get("draft")) is not bool or type(item.get("prerelease")) is not bool:
            raise artifacts.ArtifactError("Release publication flags are malformed")
        if item["draft"]:
            continue
        tag = item.get("tag_name")
        if not isinstance(tag, str):
            raise artifacts.ArtifactError("Release tag is missing")

        if repository == constants.REPOSITORY_SLUG:
            match = SEMVER.fullmatch(tag)
            if match is None:
                continue
            version = match[1]
            key = version_key(version)
            if key.is_prerelease != item["prerelease"]:
                raise artifacts.ArtifactError("Controller prerelease flag disagrees with its version")
            name = f"logos-desktop-{version}-linux-x86_64"
            ordering = (key,)
        elif repository == WINE_REPOSITORY:
            if tag != "continuous-devel":
                continue
            assets = item.get("assets")
            if not isinstance(assets, list):
                raise artifacts.ArtifactError("Wine release assets are malformed")
            names = [asset.get("name") for asset in assets if isinstance(asset, dict)]
            matches = [(name, WINE_ASSET.fullmatch(name)) for name in names if isinstance(name, str)]
            matches = [(name, match) for name, match in matches if match is not None]
            if len(matches) != 1:
                raise artifacts.ArtifactError("Wine development release needs exactly one Linux x86_64 AppImage")
            name, match = matches[0]
            version = match[1]
            if compatibility.parse_wine_version(f"wine-{version}").channel.value != "devel":
                raise artifacts.ArtifactError("Wine development asset does not identify a development release")
            if item["prerelease"]:
                raise artifacts.ArtifactError("Wine development provider release is marked prerelease")
            ordering = (Version(version), *_publication(item))
        elif repository == ICU_REPOSITORY:
            if ICU_TAG.fullmatch(tag) is None:
                continue
            version, name = tag, "icu-win.tar.gz"
            ordering = _publication(item)
        else:
            raise artifacts.ArtifactError("No release policy is defined for this repository")
        groups[item["prerelease"]].append((ordering, version, name, item))

    def newest(candidates):
        if not candidates:
            return None
        candidates.sort(key=lambda value: value[0], reverse=True)
        ordering, version, name, item = candidates[0]
        if len(candidates) > 1 and candidates[1][0] == ordering:
            return SoftwareReleaseInfo(version, "", repository=repository, tag=item["tag_name"],
                                       prerelease=item["prerelease"], error="Release version identity is ambiguous")
        try:
            return _asset(item, repository, version, name)
        except artifacts.ArtifactError as error:
            return SoftwareReleaseInfo(version, "", repository=repository, tag=item["tag_name"],
                                       prerelease=item["prerelease"], error=str(error))

    stable, beta = newest(groups[False]), newest(groups[True])
    return GithubSoftwareReleasesInfo(stable, beta, "ok" if stable or beta else "empty")


def fetch(repository, request):
    try:
        data = []
        for page in range(1, 6):
            content = request(f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}")
            batch = json.loads(content)
            if not isinstance(batch, list) or len(batch) > 100:
                raise artifacts.ArtifactError("Malformed release page")
            data.extend(batch)
            if len(batch) < 100:
                return select(data, repository)
        return GithubSoftwareReleasesInfo(None, None, "unavailable", "Release scan exceeded its page budget")
    except artifacts.MetadataError as error:
        return GithubSoftwareReleasesInfo(None, None, error.status, str(error), error.retry_after)
    except artifacts.ArtifactError as error:
        return GithubSoftwareReleasesInfo(None, None, "unavailable", str(error))
    except (ValueError, KeyError, TypeError) as error:
        raise artifacts.ArtifactError("Malformed release metadata") from error


def serialize(catalog):
    return {"latest": asdict(catalog.latest) if catalog.latest else None,
            "pre_release": asdict(catalog.pre_release) if catalog.pre_release else None,
            "status": catalog.status, "message": catalog.message}


def deserialize(value, repository):
    if not isinstance(value, dict) or value.get("status") not in ("ok", "empty"):
        raise ValueError("Invalid cached release catalog")

    def release(raw):
        if raw is None:
            return None
        raw = dict(raw)
        if raw.get("digest") is not None:
            digest = artifacts.Digest(**raw["digest"])
            digest.validate()
            raw["digest"] = digest
        result = SoftwareReleaseInfo(**raw)
        if result.repository != repository or not result.tag:
            raise ValueError("Cached release identity is incomplete")
        if result.error:
            if not isinstance(result.error, str) or len(result.error) > 500:
                raise ValueError("Invalid cached release failure")
            if repository == constants.REPOSITORY_SLUG and SEMVER.fullmatch(result.version) is None:
                raise ValueError("Invalid cached controller version")
            return result
        # Reapply the repository policy to cached asset identity before reuse.
        item = {"tag_name": result.tag, "draft": False, "prerelease": result.prerelease,
                "id": 1, "published_at": "2000-01-01T00:00:00+00:00", "assets": [{
                    "name": result.name, "size": result.size, "browser_download_url": result.download_url,
                    "digest": f"{result.digest.algorithm}:{result.digest.value}" if result.digest else None,
                }]}
        catalog = select([item], repository)
        checked = catalog.pre_release if result.prerelease else catalog.latest
        if checked != result:
            raise ValueError("Cached release identity disagrees with its policy")
        return result

    return GithubSoftwareReleasesInfo(release(value.get("latest")), release(value.get("pre_release")),
                                     value["status"], value.get("message", ""))


def controller_state(lookup):
    """Return display state without network or executable inspection."""
    if lookup.status != "ok" or lookup.stale or lookup.release is None:
        return "stale" if lookup.stale else lookup.status
    if constants.APP_RELEASE_VERSION is None:
        return "unversioned"
    current, available = version_key(constants.APP_RELEASE_VERSION), version_key(lookup.release.version)
    if current < available:
        return "available"
    return "current" if current == available else "development"


class ControllerChecks:
    """Own one finite refresh worker and discard results after close or channel change."""

    def __init__(self, network, admission=None, refresh_product=None):
        self.network = network
        self.admission = admission
        self.refresh_product = refresh_product
        self.revision = 0
        self.eligibility_message = "Checking controller installation method"
        self.result = ReleaseLookup()
        self.channel = None
        self._lock = threading.Lock()
        self._closed = False
        self._busy = False
        self._force_pending = False
        self._cancel = threading.Event()
        self._next = 0

    def request(self, channel, *, force=False):
        with self._lock:
            if self._closed:
                return
            changed = channel != self.channel
            self.channel = channel
            if changed:
                self.result = ReleaseLookup(status="checking", message="Checking controller releases")
            if self._busy:
                self._force_pending = self._force_pending or force
                return
            if not changed and not force and time.monotonic() < self._next:
                return
            self._busy = True
            self._next = time.monotonic() + 60

        def refresh():
            from . import processes

            selected_channel, forced = channel, force
            while True:
                eligibility = ""
                result = ReleaseLookup(status="disabled", message=constants.APP_SELF_UPDATE_DISABLED_MESSAGE)
                try:
                    with processes.owned(self._cancel):
                        if constants.APP_SELF_UPDATES_ENABLED and self.admission is not None:
                            try:
                                self.admission()
                            except (RuntimeError, OSError, ValueError) as error:
                                eligibility = str(error)
                        if not self._cancel.is_set() and constants.APP_SELF_UPDATES_ENABLED:
                            result = self.network.controller_release(selected_channel, force=forced)
                        if not self._cancel.is_set() and self.refresh_product is not None:
                            self.refresh_product(force=forced)
                except Exception as error:
                    result = ReleaseLookup(status="unavailable", message=str(error))
                with self._lock:
                    if not self._closed and self.channel == selected_channel:
                        self.result = result
                        self.eligibility_message = eligibility
                        self.revision += 1
                    if not self._closed and (self._force_pending or self.channel != selected_channel):
                        selected_channel, forced = self.channel, self._force_pending
                        self._force_pending = False
                        continue
                    self._busy = False
                    return

        self._worker = threading.Thread(target=refresh, name="Controller release check", daemon=False)
        self._worker.start()

    def close(self):
        with self._lock:
            self._closed = True
            self._cancel.set()
            worker = getattr(self, "_worker", None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(7)
            if worker.is_alive():
                raise RuntimeError("Release check did not stop. Wait before restarting the controller")
