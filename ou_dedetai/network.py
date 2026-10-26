"""Release metadata and application-facing artifact acquisition."""

from base64 import b64encode
from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Callable, Optional
from xml.etree import ElementTree as ET

from . import artifacts, constants, paths, releases
from .releases import GithubSoftwareReleasesInfo, SoftwareReleaseInfo as SoftwareReleaseInfo


@dataclass(frozen=True)
class ProductLookup:
    versions: tuple[str, ...] = ()
    status: str = "unchecked"
    message: str = "Product releases have not been checked"
    checked: float | None = None
    stale: bool = False
    retry_after: float = 0


@dataclass(frozen=True)
class ProductUpdateState:
    installed: str | None
    target: str | None
    status: str
    message: str
    actionable: bool = False
    stale: bool = False


def _product_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){1,3}", value):
        raise ValueError("Product version is unknown or invalid")
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (4 - len(parts))


def is_newer_product_release(installed, target):
    """Compare the actual installed version again under mutation admission."""
    try:
        return _product_version(target) > _product_version(installed)
    except ValueError:
        return False


def product_update_state(installed, lookup: ProductLookup):
    """Project a stable product feed into truthful frontend action state."""
    target = None
    try:
        target = max(lookup.versions, key=_product_version) if lookup.versions else None
    except ValueError:
        return ProductUpdateState(installed, None, "malformed", "Product feed contains an invalid version")

    if lookup.stale:
        return ProductUpdateState(installed, target, "stale", lookup.message, stale=True)
    if lookup.status not in ("ok", "empty"):
        return ProductUpdateState(installed, target, lookup.status, lookup.message)
    if target is None:
        return ProductUpdateState(installed, None, "no-release", "No stable product release is available")
    try:
        current = _product_version(installed)
    except ValueError:
        return ProductUpdateState(installed, target, "unknown-installed", "Installed product version is unknown")
    if _product_version(target) > current:
        return ProductUpdateState(installed, target, "available", f"Stable release {target} is available", True)
    return ProductUpdateState(installed, target, "current", "The installed product is current")


class FileProps:
    def __init__(self, path):
        self.path = Path(path) if path is not None else None

    @property
    def size(self):
        return self.path.stat().st_size if self.path is not None and self.path.is_file() else None

    def _get_md5(self):
        return b64encode(bytes.fromhex(artifacts.regular_hash(self.path, "md5"))).decode()

    @property
    def md5(self):
        return self._get_md5() if self.path is not None else None


class UrlProps:
    def __init__(self, url):
        self.path = url
        self._metadata = None

    def metadata(self):
        if self._metadata is None:
            self._metadata = artifacts.head_metadata(self.path)
        return self._metadata

    @property
    def size(self):
        return self.metadata()[0]

    @property
    def md5(self):
        digest = self.metadata()[1]
        return b64encode(bytes.fromhex(digest.value)).decode() if digest is not None else None


@dataclass
class CachedRequests:
    faithlife_product_releases: dict = field(default_factory=dict)
    repository_latest_version: dict = field(default_factory=dict)
    repository_latest_url: dict = field(default_factory=dict)
    repository_latest_pre_release_version: dict = field(default_factory=dict)
    repository_latest_pre_release_url: dict = field(default_factory=dict)
    repository_digest: dict = field(default_factory=dict)
    release_entries: dict = field(default_factory=dict)
    faithlife_checked: dict = field(default_factory=dict)
    product_entries: dict = field(default_factory=dict)
    last_updated: Optional[float] = None
    _update_hook: Optional[Callable[[], None]] = None

    @classmethod
    def load(cls):
        cache = Path(constants.CACHE_DIR)
        if cache.exists() or cache.is_symlink():
            paths.validate_cache_directory(cache, constants.CACHE_BASE)
        path = Path(constants.NETWORK_CACHE_PATH)
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Invalid metadata cache file: {path}")
            try:
                with path.open() as source:
                    output = json.load(source)
            except json.JSONDecodeError:
                logging.warning("Invalid metadata cache JSON")
            else:
                if isinstance(output, dict):
                    known = cls().__dict__
                    values = {key: value for key, value in output.items() if key in known and not key.startswith("_")}
                    for key in known:
                        if key not in ("last_updated", "_update_hook") and not isinstance(values.get(key, {}), dict):
                            values.pop(key, None)
                    if type(values.get("last_updated")) not in (int, float):
                        values.pop("last_updated", None)
                    for key in values.keys() - {"last_updated", "faithlife_product_releases",
                                                 "release_entries", "faithlife_checked", "product_entries"}:
                        values[key] = {name: value for name, value in values[key].items()
                                       if isinstance(name, str) and isinstance(value, str) and value}
                    release_cache = {}
                    for product, versions in values.get("faithlife_product_releases", {}).items():
                        if not isinstance(product, str) or not isinstance(versions, dict):
                            continue
                        release_cache[product] = {}
                        for version, channels in versions.items():
                            if not isinstance(version, str) or not isinstance(channels, dict):
                                continue
                            release_cache[product][version] = {
                                channel: release_list for channel, release_list in channels.items()
                                if isinstance(channel, str) and isinstance(release_list, list)
                                and all(isinstance(release, str) for release in release_list)
                            }
                    values["faithlife_product_releases"] = release_cache
                    return cls(**values)
        return cls(last_updated=time.time())

    def _as_dict(self):
        return {key: value for key, value in self.__dict__.items() if not key.startswith("_")}

    def _write(self, content=None):
        path = Path(constants.NETWORK_CACHE_PATH)
        cache = paths.ensure_cache_directory(Path(constants.CACHE_DIR), constants.CACHE_BASE)
        if path.parent != cache:
            raise ValueError("Metadata cache must remain in the application cache")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=cache, delete=False) as output:
                temporary = Path(output.name)
                json.dump(self._as_dict() if content is None else content, output, indent=4, sort_keys=True)
                output.write("\n")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if self._update_hook:
            self._update_hook()

    def _is_fresh(self):
        return self.last_updated is not None and (
            self.last_updated + constants.CACHE_LIFETIME_HOURS * 3600 > time.time()
        )

    def ensure_fresh(self, force=False):
        # Entry freshness is checked by readers without discarding offline data.
        return self


class NetworkRequests:
    def __init__(self, force_clean=None, hook=None):
        load_error = ""
        try:
            self._cache = CachedRequests.load()
        except (OSError, ValueError) as error:
            self._cache = CachedRequests()
            load_error = f"Could not read release cache ({type(error).__name__})"
            logging.warning(load_error)
        self._cache._update_hook = hook
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._revisions = {}
        self._inflight = {}
        self._force = bool(force_clean)
        self._forced = set()
        self.persistence_error = load_error

    def faithlife_product_releases(self, product, version, channel):
        lookup = self.product_release(product, version, channel)
        if lookup.status not in ("ok", "empty") or lookup.stale:
            raise artifacts.ArtifactError(lookup.message)
        return list(lookup.versions)

    def product_release(self, product, version, channel, *, force=False, refresh=True):
        key = f"feed:{product}:{version}:{channel}"
        with self._lock:
            entry = self._cache.product_entries.get(key, {})
            entry = entry if isinstance(entry, dict) else {}
            versions = entry.get("versions", [])
            if not isinstance(versions, list) or not all(
                isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+){1,3}", value) for value in versions
            ):
                entry, versions = {}, []
            checked, fresh, retry, status = self._freshness(entry, empty=not versions)
            revision = self._revisions.get(key, 0)
            needed = (force or (self._force and key not in self._forced) or not fresh
                      or status not in ("ok", "empty", "unchecked")) and time.time() >= retry

        if refresh and needed:
            def load():
                try:
                    values = _fetch_faithlife_product_releases(product, version, channel)
                    return ProductLookup(tuple(values), "ok" if values else "empty", "No product releases available")
                except artifacts.MetadataError as error:
                    return ProductLookup(status=error.status, message=str(error), retry_after=error.retry_after)
                except artifacts.ArtifactError as error:
                    return ProductLookup(status="malformed", message=str(error))

            def publish(received):
                previous = self._cache.product_entries.get(key, {})
                previous = previous if isinstance(previous, dict) else {}
                if received.status in ("ok", "empty"):
                    previous = {"versions": list(received.versions), "checked": time.time(), "retry": 0,
                                "status": received.status, "message": received.message}
                else:
                    previous = dict(previous, status=received.status, message=received.message,
                                    retry=time.time() + max(60, received.retry_after))
                self._cache.product_entries[key] = previous

            self._refresh(key, revision, load, publish)
            return self.product_release(product, version, channel, refresh=False)
        message = entry.get("message", "Product release information is unavailable")
        return ProductLookup(tuple(versions), status, message,
                             checked, checked is not None and (not fresh or status not in ("ok", "empty")))

    def wine_appimage_versions(self, *, refresh=True):
        return self._repo_version("FaithLife-Community/wine-appimages", refresh=refresh)

    def wine_release_target(self, channel="devel", *, force=False, refresh=True):
        if channel not in ("stable", "devel"):
            raise artifacts.ArtifactError("Unknown Wine release channel")
        key = f"{releases.WINE_SOURCE_POLICY}:{channel}"
        with self._lock:
            entry = self._cache.release_entries.get(key, {})
            entry = entry if isinstance(entry, dict) else {}
            try:
                target = releases.WineTarget(**entry["target"]).validate()
                if target.channel != channel:
                    raise ValueError("Cached Wine channel disagrees with the request")
            except (ValueError, TypeError, KeyError):
                target = None
                entry = {"status": entry.get("status", "unchecked"), "retry": entry.get("retry", 0),
                         "message": entry.get("message", "Wine release information is unavailable")}
            checked, fresh, retry, status = self._freshness(entry)
            revision = self._revisions.get(key, 0)
            needed = (force or (self._force and key not in self._forced) or not fresh
                      or status not in ("ok", "unchecked")) and time.time() >= retry

        if refresh and needed:
            def load():
                try:
                    return releases.fetch_wine_target(channel, _net_get)
                except artifacts.ArtifactError as error:
                    return error

            def publish(received):
                if isinstance(received, artifacts.ArtifactError):
                    previous = self._cache.release_entries.get(key, {})
                    previous = previous if isinstance(previous, dict) else {}
                    self._cache.release_entries[key] = dict(
                        previous, status=getattr(received, "status", "malformed"), message=str(received),
                        retry=time.time() + max(60, getattr(received, "retry_after", 0)),
                    )
                else:
                    self._cache.release_entries[key] = {
                        "target": {"version": received.version, "channel": received.channel, "commit": received.commit},
                        "checked": time.time(), "status": "ok", "retry": 0,
                        "message": f"Released Wine {received.channel} {received.version} from wine-mirror/wine",
                    }

            self._refresh(key, revision, load, publish)
            return self.wine_release_target(channel, refresh=False)
        message = entry.get("message", "Wine release information is unavailable")
        return releases.WineTargetLookup(target, status, message, checked,
                                         checked is not None and (not fresh or status != "ok"))

    def wine_appimage_release(self, *, force=False, refresh=True):
        source = self.wine_release_target(force=force, refresh=refresh)
        if source.status != "ok" or source.stale or source.target is None:
            return releases.ReleaseLookup(status=source.status, message=source.message,
                                          checked=source.checked, stale=source.stale)
        catalog, checked, fresh, status, message = self._catalog(
            releases.WINE_REPOSITORY, force=force, refresh=refresh, wine_target=source.target,
        )
        if catalog.latest is not None and catalog.latest.error:
            status, message = "incompatible", catalog.latest.error
        stale = checked is not None and (not fresh or status not in ("ok", "empty"))
        if status == "empty":
            status = "unavailable"
        return releases.ReleaseLookup(catalog.latest, status, message, checked, stale)

    def _persist(self):
        try:
            with self._write_lock:
                with self._lock:
                    content = deepcopy(self._cache._as_dict())
                self._cache._write(content)
            self.persistence_error = ""
        except (OSError, ValueError) as error:
            self.persistence_error = f"Could not save release metadata ({type(error).__name__})"
            logging.warning(self.persistence_error)
            if self._cache._update_hook:
                self._cache._update_hook()

    def _freshness(self, entry, *, empty=False):
        now = time.time()
        checked = entry.get("checked")
        checked = checked if type(checked) in (int, float) and 0 <= checked <= now else None
        ttl = 300 if empty else constants.CACHE_LIFETIME_HOURS * 3600
        fresh = checked is not None and now - checked < ttl
        retry = entry.get("retry", 0)
        retry = retry if type(retry) in (int, float) and 0 <= retry <= now + 86400 else 0
        return checked, fresh, retry, entry.get("status", "unchecked")

    def _refresh(self, key, revision, load, publish):
        """Coalesce one key without holding snapshot admission during I/O."""
        with self._lock:
            if self._revisions.get(key, 0) != revision:
                return
            event = self._inflight.get(key)
            waiting = event is not None
            if not waiting:
                event = self._inflight[key] = threading.Event()
        if waiting:
            from . import processes

            deadline = time.monotonic() + 240
            while not event.wait(0.1):
                if processes.cancelled():
                    raise artifacts.MetadataError("Release check cancelled")
                if time.monotonic() >= deadline:
                    break
            return
        try:
            received = load()
            with self._lock:
                publish(received)
                self._revisions[key] = revision + 1
                self._forced.add(key)
            self._persist()
        finally:
            with self._lock:
                self._inflight.pop(key, None)
                event.set()

    def _catalog(self, repository, *, force=False, refresh=True, wine_target=None):
        key = releases.policy_key(repository)
        if wine_target is not None:
            wine_target.validate()
            key = f"wine-appimage-2:{wine_target.channel}:{wine_target.version}:{wine_target.commit}"
        with self._lock:
            entry = self._cache.release_entries.get(key, {})
            if not isinstance(entry, dict):
                entry = {}
            try:
                catalog = releases.deserialize(entry.get("catalog"), repository)
                if wine_target is not None and catalog.latest is not None:
                    selected = catalog.latest
                    if (selected.version != wine_target.version
                            or selected.tag not in (f"{wine_target.version}-{wine_target.channel}",
                                                    f"{wine_target.version}-staging")
                            or catalog.pre_release is not None):
                        raise ValueError("Cached Wine provider disagrees with the target")
            except (ValueError, TypeError, KeyError, artifacts.ArtifactError):
                catalog = GithubSoftwareReleasesInfo(None, None)
                entry = {"status": entry.get("status", "unchecked"), "message": entry.get("message", ""),
                         "retry": entry.get("retry", 0)}
            checked, fresh, retry, status = self._freshness(entry, empty=catalog.status == "empty")
            revision = self._revisions.get(key, 0)
            needed = (force or (self._force and key not in self._forced) or not fresh
                      or status not in ("ok", "empty", "unchecked")) and time.time() >= retry
        if refresh and needed:
            def load():
                try:
                    if wine_target is not None:
                        return releases.fetch_wine_appimage(wine_target, _net_get)
                    return _get_release_data(repository)
                except artifacts.ArtifactError as error:
                    return GithubSoftwareReleasesInfo(None, None, "malformed", str(error))

            def publish(received):
                previous = self._cache.release_entries.get(key, {})
                previous = previous if isinstance(previous, dict) else {}
                if received.status in ("ok", "empty"):
                    previous = {"catalog": releases.serialize(received), "checked": time.time(),
                                "status": received.status, "message": received.message, "retry": 0}
                else:
                    previous = dict(previous, status=received.status, message=received.message,
                                    retry=time.time() + max(60, received.retry_after))
                self._cache.release_entries[key] = previous

            self._refresh(key, revision, load, publish)
            return self._catalog(repository, refresh=False, wine_target=wine_target)
        return catalog, checked, fresh, status, entry.get("message", "")

    def _repo_version(self, repository, *, refresh=True):
        catalog, checked, fresh, status, message = self._catalog(repository, refresh=refresh)
        stale = checked is not None and (not fresh or status not in ("ok", "empty"))
        return GithubSoftwareReleasesInfo(catalog.latest, catalog.pre_release, "stale" if stale else status, message)

    def _repo_latest_version(self, repository):
        release = self._repo_version(repository).latest
        if release is None or release.error:
            raise artifacts.ArtifactError(release.error if release else "No stable release is available")
        return release

    def app_latest_version(self, channel):
        if not constants.APP_SELF_UPDATES_ENABLED:
            return None
        lookup = self.controller_release(channel)
        return lookup.release if lookup.status == "ok" and not lookup.stale else None

    def controller_release(self, channel, *, force=False, refresh=True):
        if not constants.APP_SELF_UPDATES_ENABLED:
            return releases.ReleaseLookup(status="disabled", message=constants.APP_SELF_UPDATE_DISABLED_MESSAGE)
        channel = releases.channel_name(channel)
        catalog, checked, fresh, status, message = self._catalog(
            constants.REPOSITORY_SLUG, force=force, refresh=refresh,
        )
        release = releases.choose(catalog, channel)
        if release is not None and release.error:
            status, message = "incompatible", release.error
        elif release is None and status == "ok":
            status, message = "empty", "No release is available for this channel"
        return releases.ReleaseLookup(release, status, message or "No release is available", checked,
                                      checked is not None and (not fresh or status not in ("ok", "empty")))

    def icu_latest_version(self):
        return self._repo_latest_version("FaithLife-Community/icu")


def _release_digest(value):
    return releases.release_digest(value)


def logos_reuse_download(sourceurl, file, targetdir, app, status_messages=True, *, kind, digest=None, size=None,
                         msi_expectation=None):
    candidates = [Path(targetdir) / file, Path(app.conf.download_dir) / file]
    user = app.conf.user_download_dir
    if user:
        candidates.append(Path(user) / file)
    spec = artifacts.ArtifactSpec(sourceurl, file, kind, digest, size, msi_expectation)
    return artifacts.acquire(
        spec, Path(targetdir), candidates, app.status if status_messages else None,
        checkpoint=app.checkpoint,
    )


def _net_get(url):
    return artifacts.get_bytes(url)


def _get_release_data(repository):
    return releases.fetch(repository, _net_get)


def download_recommended_appimage(app, release=None):
    if release is None:
        lookup = app.conf._network.wine_appimage_release()
        if lookup.status != "ok" or lookup.stale or lookup.release is None:
            raise artifacts.ArtifactError(lookup.message)
        release = lookup.release
    release.spec("appimage").validate()
    return logos_reuse_download(
        release.download_url, release.name, app.conf.installer_binary_dir,
        app, kind="appimage", digest=release.digest, size=release.size,
    )


def _get_faithlife_product_releases(product, version, channel):
    try:
        return _fetch_faithlife_product_releases(product, version, channel)
    except artifacts.MetadataError:
        return []


def _fetch_faithlife_product_releases(product, version, channel):
    url = "https://clientservices.logos.com/update/v1/feed/logos10/beta.xml" if channel == "beta" else (
        f"https://clientservices.logos.com/update/v1/feed/logos{version}/stable.xml"
    )
    try:
        root = ET.fromstring(_net_get(url).decode("utf-8-sig"))
    except (ET.ParseError, UnicodeError) as error:
        raise artifacts.ArtifactError("Malformed product feed") from error
    versions = [entry.text for entry in root.findall(".//{http://services.logos.com/update/v1/}version") if entry.text]
    if not all(re.fullmatch(r"\d+(?:\.\d+){1,3}", version) for version in versions):
        raise artifacts.ArtifactError("Product feed contains an invalid version")
    return versions


def update_lli_binary(app):
    if not constants.APP_SELF_UPDATES_ENABLED:
        app.status(constants.APP_SELF_UPDATE_DISABLED_MESSAGE)
        return

    from . import updates

    return updates.prepare(app)
