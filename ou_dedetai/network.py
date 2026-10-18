"""Release metadata and application-facing artifact acquisition."""

from base64 import b64encode
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Callable, Optional
from xml.etree import ElementTree as ET

from . import artifacts, constants, paths, utils


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
class SoftwareReleaseInfo:
    version: str
    download_url: str
    digest: artifacts.Digest | None = None


@dataclass
class GithubSoftwareReleasesInfo:
    latest: Optional[SoftwareReleaseInfo]
    pre_release: Optional[SoftwareReleaseInfo]


@dataclass
class CachedRequests:
    faithlife_product_releases: dict = field(default_factory=dict)
    repository_latest_version: dict = field(default_factory=dict)
    repository_latest_url: dict = field(default_factory=dict)
    repository_latest_pre_release_version: dict = field(default_factory=dict)
    repository_latest_pre_release_url: dict = field(default_factory=dict)
    repository_digest: dict = field(default_factory=dict)
    last_updated: Optional[float] = None
    _update_hook: Optional[Callable[[], None]] = None

    @classmethod
    def load(cls):
        paths.ensure_cache_directory(Path(constants.CACHE_DIR), constants.CACHE_BASE)
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
                    for key in values.keys() - {"last_updated", "faithlife_product_releases"}:
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

    def _write(self):
        path = Path(constants.NETWORK_CACHE_PATH)
        cache = paths.ensure_cache_directory(Path(constants.CACHE_DIR), constants.CACHE_BASE)
        if path.parent != cache:
            raise ValueError("Metadata cache must remain in the application cache")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=cache, delete=False) as output:
                temporary = Path(output.name)
                json.dump(self._as_dict(), output, indent=4, sort_keys=True)
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
        if force or not self._is_fresh():
            self = CachedRequests(last_updated=time.time())
            self._write()
        return self


class NetworkRequests:
    def __init__(self, force_clean=None, hook=None):
        self._cache = CachedRequests.load().ensure_fresh(force=force_clean or False)
        self._cache._update_hook = hook

    def faithlife_product_releases(self, product, version, channel):
        channels = self._cache.faithlife_product_releases.setdefault(product, {}).setdefault(version, {})
        if not channels.get(channel):
            channels[channel] = _get_faithlife_product_releases(product, version, channel)
            self._cache._write()
        return channels[channel]

    def wine_appimage_versions(self):
        return self._repo_version("FaithLife-Community/wine-appimages")

    def _repo_version(self, repository):
        cache = self._cache

        def cached(version, url):
            if repository not in version or repository not in url:
                return None
            digest = cache.repository_digest.get(url[repository])
            parsed = _release_digest(digest)
            return SoftwareReleaseInfo(version[repository], url[repository], parsed)

        output = GithubSoftwareReleasesInfo(
            cached(cache.repository_latest_version, cache.repository_latest_url),
            cached(cache.repository_latest_pre_release_version, cache.repository_latest_pre_release_url),
        )
        if output.latest is None and output.pre_release is None:
            output = _get_release_data(repository)
            for release, versions, urls in (
                (output.latest, cache.repository_latest_version, cache.repository_latest_url),
                (output.pre_release, cache.repository_latest_pre_release_version,
                 cache.repository_latest_pre_release_url),
            ):
                if release is not None:
                    versions[repository] = release.version
                    urls[repository] = release.download_url
                    if release.digest is not None:
                        cache.repository_digest[release.download_url] = (
                            f"{release.digest.algorithm}:{release.digest.value}"
                        )
            cache._write()
        return output

    def _repo_latest_version(self, repository):
        release = self._repo_version(repository).latest
        if release is None:
            raise artifacts.ArtifactError("No stable release is available")
        return release

    def app_latest_version(self, channel):
        if not constants.APP_SELF_UPDATES_ENABLED:
            return None
        return self._repo_latest_version(constants.REPOSITORY_SLUG)

    def icu_latest_version(self):
        return self._repo_latest_version("FaithLife-Community/icu")


def _release_digest(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise artifacts.ArtifactError("Unsupported release digest metadata")
    digest = artifacts.Digest("sha256", value[7:], "selected GitHub release asset digest")
    digest.validate()
    return digest


def logos_reuse_download(sourceurl, file, targetdir, app, status_messages=True, *, kind, digest=None):
    candidates = [Path(targetdir) / file, Path(app.conf.download_dir) / file]
    user = app.conf.user_download_dir
    if user:
        candidates.append(Path(user) / file)
    spec = artifacts.ArtifactSpec(sourceurl, file, kind, digest)
    return artifacts.acquire(spec, Path(targetdir), candidates, app.status if status_messages else None)


def _net_get(url):
    return artifacts.get_bytes(url)


def _get_first_asset_url(json_data):
    assets = json_data.get("assets") or []
    if not assets or not assets[0].get("browser_download_url"):
        raise artifacts.ArtifactError("Release has no download asset")
    return assets[0]["browser_download_url"]


def _get_version_name(json_data):
    return json_data["tag_name"].lstrip("v")


def _get_release_data(repository):
    try:
        data = json.loads(_net_get(f"https://api.github.com/repos/{repository}/releases"))
        data = sorted(data, key=lambda release: datetime.fromisoformat(release["updated_at"]), reverse=True)

        def release(item):
            return SoftwareReleaseInfo(
                _get_version_name(item), _get_first_asset_url(item), _release_digest(item["assets"][0].get("digest")),
            )

        prerelease = release(data[0]) if data and data[0]["prerelease"] else None
        stable = next((item for item in data if item["prerelease"] is False), None)
        return GithubSoftwareReleasesInfo(release(stable) if stable else None, prerelease)
    except artifacts.ArtifactError:
        logging.warning("Could not obtain release metadata")
        return GithubSoftwareReleasesInfo(None, None)
    except (ValueError, KeyError, TypeError) as error:
        raise artifacts.ArtifactError("Malformed release metadata") from error


def download_recommended_appimage(app):
    release = app.conf._network.wine_appimage_versions().latest
    if release is None:
        raise artifacts.ArtifactError("No recommended AppImage release is available")
    return logos_reuse_download(
        release.download_url, app.conf.wine_appimage_recommended_file_name, app.conf.installer_binary_dir,
        app, kind="appimage", digest=release.digest,
    )


def _get_faithlife_product_releases(product, version, channel):
    url = "https://clientservices.logos.com/update/v1/feed/logos10/beta.xml" if channel == "beta" else (
        f"https://clientservices.logos.com/update/v1/feed/logos{version}/stable.xml"
    )
    try:
        root = ET.fromstring(_net_get(url).decode("utf-8-sig"))
    except artifacts.ArtifactError:
        logging.warning("Could not obtain product releases")
        return []
    except (ET.ParseError, UnicodeError) as error:
        raise artifacts.ArtifactError("Malformed product feed") from error
    return [entry.text for entry in root.findall(".//{http://services.logos.com/update/v1/}version") if entry.text]


def update_lli_binary(app):
    if not constants.APP_SELF_UPDATES_ENABLED:
        app.status(constants.APP_SELF_UPDATE_DISABLED_MESSAGE)
        return

    # Final controller replacement remains M05 work while self-update is disabled.
    accepted = logos_reuse_download(
        app.conf.app_latest_version_url, constants.BINARY_NAME, app.conf.download_dir, app, kind="elf",
    )
    accepted.check()
    version = utils.get_lli_release_version(accepted.path)
    if version != app.conf.app_latest_version:
        raise artifacts.ArtifactError("Controller artifact has the wrong version")
    target = os.path.realpath(sys.argv[0])
    temporary = Path(app.conf.download_dir) / f"{constants.BINARY_NAME}.tmp"
    shutil.copy(accepted.path, temporary)
    shutil.move(temporary, target)
    utils.restart_lli()
