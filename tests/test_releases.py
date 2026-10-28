import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

from logos_desktop import artifacts, constants, metadata, network, processes, releases, wine
from logos_desktop.config import Config, PersistentConfiguration
from tests.test_artifacts import response


def release_item(version="1.1.0", *, repository=None, prerelease=False, name=None, identity=1):
    repository = repository or constants.REPOSITORY_SLUG
    tag = "v" + version if repository == constants.REPOSITORY_SLUG else version
    name = name or f"logos-desktop-{version}-linux-x86_64"
    asset = {"name": name, "size": 123, "digest": "sha256:" + hashlib.sha256(b"fixture").hexdigest(),
             "browser_download_url": f"https://github.com/{repository}/releases/download/{quote(tag)}/{quote(name)}"}
    return {"id": identity, "tag_name": tag, "draft": False, "prerelease": prerelease,
            "published_at": f"2026-10-{identity:02d}T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z",
             "assets": [asset]}


def wine_source_responses(channel="devel"):
    data = json.loads((Path(__file__).parent / "data/wine-release-metadata.json").read_text())
    fixture = data[channel]
    commit, version, tag_sha = fixture["commit"], fixture["version"], fixture["tag_sha"]
    api, raw = releases.WINE_SOURCE_API, releases.WINE_SOURCE_RAW
    branch = "master" if channel == "devel" else "stable"
    return {
        f"{api}/commits?sha={branch}&path=ANNOUNCE.md&per_page=100": json.dumps([
            {"sha": commit, "commit": {"message": fixture["message"],
                                       "verification": {"verified": False, "reason": "unsigned"}}},
        ]).encode(),
        f"{raw}/{commit}/VERSION": fixture["VERSION"].encode(),
        f"{raw}/{commit}/ANNOUNCE.md": fixture["ANNOUNCE.md"].encode(),
        f"{api}/git/ref/tags/wine-{version}": json.dumps({
            "ref": f"refs/tags/wine-{version}", "object": {"type": "tag", "sha": tag_sha},
        }).encode(),
        f"{api}/git/tags/{tag_sha}": json.dumps({
            "sha": tag_sha, "tag": f"wine-{version}", "object": {"type": "commit", "sha": commit},
            "verification": {"verified": False, "reason": "unknown_key"},
        }).encode(),
    }


def wine_provider_item(version="11.20"):
    return release_item("continuous-devel", repository=releases.WINE_REPOSITORY,
                        name=f"wine-devel_{version}-x86_64.AppImage")


class TestWineSourcePolicy(unittest.TestCase):
    def test_retained_released_channels_require_pinned_documents_and_tag(self):
        for channel, version in (("devel", "11.20"), ("stable", "11.0")):
            responses = wine_source_responses(channel)
            request = Mock(side_effect=responses.__getitem__)
            with self.subTest(channel=channel):
                target = releases.fetch_wine_target(channel, request)
                self.assertEqual(target.version, version)
                self.assertEqual(target.channel, channel)
                self.assertEqual(request.call_count, 5)
                self.assertFalse(any("/master/VERSION" in call.args[0] for call in request.call_args_list))

    def test_rcs_edits_and_later_master_work_do_not_become_releases(self):
        responses = wine_source_responses()
        history_url = next(iter(responses))
        history = json.loads(responses[history_url])
        history[:0] = [{"sha": "a" * 40, "commit": {"message": message}} for message in (
            "Release 12.0-rc1.", "Update ANNOUNCE.md", "Start 12.1 development", "Release 12.0.",
        )]
        responses[history_url] = json.dumps(history).encode()
        self.assertEqual(releases.fetch_wine_target("devel", responses.__getitem__).version, "11.20")

    def test_newest_invalid_release_is_not_replaced_with_an_older_one(self):
        for field in ("VERSION", "ANNOUNCE.md", "tag_commit", "tag_ref", "commit_sha"):
            responses = wine_source_responses()
            history_url = next(iter(responses))
            history = json.loads(responses[history_url])
            history.append({"sha": "b" * 40, "commit": {"message": "Release 11.19."}})
            responses[history_url] = json.dumps(history).encode()
            if field in ("VERSION", "ANNOUNCE.md"):
                url = next(url for url in responses if url.endswith("/" + field))
                responses[url] = b"Wine version 11.21\n" if field == "VERSION" else b"Unreleased development tree"
            elif field == "commit_sha":
                history[0]["sha"] = "master/../../untrusted"
                responses[history_url] = json.dumps(history).encode()
            else:
                url = next(url for url in responses if "/git/tags/" in url or "/git/ref/" in url)
                data = json.loads(responses[url])
                if field == "tag_ref":
                    data["ref"] = "refs/heads/master"
                else:
                    data["object"] = {"type": "commit", "sha": "c" * 40}
                responses[url] = json.dumps(data).encode()
            with self.subTest(field=field), self.assertRaises(artifacts.ArtifactError):
                releases.fetch_wine_target("devel", responses.__getitem__)

    def test_available_development_provider_excludes_unrelated_channels(self):
        devel = wine_provider_item("11.19")
        stable = release_item("continuous-stable", repository=releases.WINE_REPOSITORY,
                              name="wine-stable_11.0-x86_64.AppImage")
        for items in ([devel, stable], [stable, devel]):
            selected = releases.select(items, releases.WINE_REPOSITORY).latest
            self.assertEqual(selected.version, "11.19")
            self.assertEqual(selected.tag, "continuous-devel")
            self.assertIsNotNone(selected.digest)

    def test_broken_development_assets_never_fall_back_to_other_flavors(self):
        for fault in ("missing", "duplicate", "arch", "digest", "url", "rc", "stable", "prerelease"):
            item = wine_provider_item()
            asset = item["assets"][0]
            if fault == "missing":
                item["assets"] = []
            elif fault == "duplicate":
                item["assets"] *= 2
            elif fault == "arch":
                asset["name"] = asset["name"].replace("x86_64", "aarch64")
            elif fault == "digest":
                asset["digest"] = None
            elif fault == "url":
                asset["browser_download_url"] = "https://example.invalid/unrelated.AppImage"
            elif fault in ("rc", "stable"):
                asset["name"] = asset["name"].replace("11.20", "12.0-rc1" if fault == "rc" else "11.0")
            else:
                item["prerelease"] = True
            with self.subTest(fault=fault):
                result = releases.fetch(releases.WINE_REPOSITORY, lambda url: json.dumps([item]).encode())
                self.assertTrue(result.status == "unavailable" or result.latest.error)


class TestReleasePolicy(unittest.TestCase):
    def test_asset_permutations_keep_role_architecture_and_digest_together(self):
        item = release_item()
        intended = item["assets"][0]
        others = [dict(intended, name="controller-aarch64"), dict(intended, name="SHA256SUMS"),
                  dict(intended, name="source.tar.gz")]
        for permutation in itertools.permutations([intended, *others]):
            item["assets"] = list(permutation)
            selected = releases.select([item], constants.REPOSITORY_SLUG).latest
            self.assertEqual(selected.name, intended["name"])
            self.assertEqual(selected.download_url, intended["browser_download_url"])
            self.assertEqual(selected.spec("elf").digest.value, intended["digest"][7:])

    def test_channels_and_old_edits_do_not_hide_newer_versions(self):
        old, stable, beta = release_item("1.0.0"), release_item("1.1.0"), release_item("1.2.0-beta.1", prerelease=True)
        old["updated_at"] = "2030-01-01T00:00:00Z"
        for permutation in itertools.permutations([old, stable, beta]):
            catalog = releases.select(list(permutation), constants.REPOSITORY_SLUG)
            self.assertEqual(releases.choose(catalog, "stable").version, "1.1.0")
            for channel in ("beta", "dev"):
                self.assertEqual(releases.choose(catalog, channel).version, "1.2.0-beta.1")
        catalog = releases.select([beta], constants.REPOSITORY_SLUG)
        self.assertIsNone(releases.choose(catalog, "stable"))
        self.assertEqual(releases.choose(catalog, "beta").version, "1.2.0-beta.1")
        self.assertEqual(releases.choose(releases.select([stable], constants.REPOSITORY_SLUG), "beta").version, "1.1.0")
        with self.assertRaises(artifacts.ArtifactError):
            releases.choose(catalog, "nightly")

    def test_bad_newest_asset_is_not_hidden_by_an_older_release(self):
        for failure in ("empty", "duplicate", "digest", "url", "size"):
            newest = release_item("1.2.0")
            if failure == "empty":
                newest["assets"] = []
            elif failure == "duplicate":
                newest["assets"] *= 2
            else:
                field = {"digest": "digest", "url": "browser_download_url", "size": "size"}[failure]
                newest["assets"][0][field] = None
            selected = releases.select([release_item(), newest], constants.REPOSITORY_SLUG).latest
            self.assertEqual(selected.version, "1.2.0")
            self.assertTrue(selected.error)
            with self.assertRaises(artifacts.ArtifactError):
                selected.spec("elf")

    def test_drafts_invalid_tags_flags_duplicate_versions_and_hosts(self):
        invalid = release_item("continuous")
        draft = release_item("9.0.0")
        draft["draft"] = True
        self.assertEqual(releases.select([invalid, draft], constants.REPOSITORY_SLUG).status, "empty")
        mismatch = release_item("1.2.0-beta.1")
        with self.assertRaises(artifacts.ArtifactError):
            releases.select([mismatch], constants.REPOSITORY_SLUG)
        self.assertTrue(releases.select([release_item(), release_item()], constants.REPOSITORY_SLUG).latest.error)
        with patch.object(releases.platform, "machine", return_value="aarch64"):
            self.assertEqual(releases.select([release_item()], constants.REPOSITORY_SLUG).status, "incompatible")

    def test_dependency_policies_keep_custom_tags_and_optional_digests(self):
        self.assertEqual(releases.select([wine_provider_item()], releases.WINE_REPOSITORY).latest.version, "11.20")
        icu = [release_item(tag, repository=releases.ICU_REPOSITORY, name="icu-win.tar.gz", identity=index)
               for index, tag in enumerate(("75.1", "72.1-custom+4"), 1)]
        icu[0]["updated_at"] = "2030-01-01T00:00:00Z"
        self.assertEqual(releases.select(icu, releases.ICU_REPOSITORY).latest.version, "72.1-custom+4")

    def test_pagination_is_complete_or_explicitly_unavailable(self):
        batch = [release_item("continuous") for _ in range(100)]
        request = Mock(side_effect=[json.dumps(batch).encode(), json.dumps([release_item()]).encode()])
        self.assertEqual(releases.fetch(constants.REPOSITORY_SLUG, request).latest.version, "1.1.0")
        self.assertIn("page=2", request.call_args.args[0])
        request = Mock(return_value=json.dumps(batch).encode())
        result = releases.fetch(constants.REPOSITORY_SLUG, request)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(request.call_count, 5)

    def test_http_outcomes_are_not_successfully_empty_feeds(self):
        for status, expected in ((403, "rate-limited"), (429, "rate-limited"), (404, "unavailable")):
            reply = response()
            reply.status_code = status
            with (patch.object(artifacts.requests, "get", return_value=reply),
                  patch.object(metadata, "request", side_effect=metadata.read)):
                self.assertEqual(network._get_release_data(constants.REPOSITORY_SLUG).status, expected)
        self.assertEqual(releases.fetch(constants.REPOSITORY_SLUG, lambda url: b"[]").status, "empty")


class TestReleaseRefresh(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        values = patch.multiple(constants, CACHE_BASE=root, CACHE_DIR=str(root / "logos-desktop"),
                                NETWORK_CACHE_PATH=str(root / "logos-desktop/network.json"),
                                APP_SELF_UPDATES_ENABLED=True, APP_RELEASE_VERSION="1.0.0")
        values.start()
        self.addCleanup(values.stop)
        self.hook = Mock()
        self.client = network.NetworkRequests(hook=self.hook)
        self.catalog = releases.select([release_item()], constants.REPOSITORY_SLUG)

    def test_wine_source_cache_channels_force_failure_and_reload(self):
        responses = wine_source_responses() | wine_source_responses("stable")
        with patch.object(network, "_net_get", side_effect=responses.__getitem__) as fetch:
            with patch.object(network.time, "time", return_value=1000):
                self.assertEqual(self.client.wine_release_target().target.version, "11.20")
                self.assertEqual(self.client.wine_release_target("stable").target.version, "11.0")
                self.client.wine_release_target()
                self.assertEqual(fetch.call_count, 10)
                self.client.wine_release_target(force=True)
                self.assertEqual(fetch.call_count, 15)

        loaded = network.NetworkRequests()
        with patch.object(network.time, "time", return_value=1001), patch.object(network, "_net_get") as fetch:
            self.assertEqual(loaded.wine_release_target().target.version, "11.20")
            fetch.assert_not_called()
        with patch.object(network.time, "time", return_value=50000), patch.object(
            network, "_net_get", side_effect=artifacts.MetadataError("offline"),
        ) as fetch:
            result = loaded.wine_release_target()
            self.assertTrue(result.stale)
            self.assertEqual(result.target.version, "11.20")
            self.assertEqual(result.checked, 1000)
            unavailable = loaded.wine_appimage_release()
            self.assertIsNone(unavailable.release)
            self.assertEqual(unavailable.status, "offline")
            self.assertEqual(fetch.call_count, 2)

    def test_wine_exact_provider_cache_isolated_from_old_catalog(self):
        old_repository = "FaithLife-Community/wine-appimages"
        old = release_item("10.8-staging", repository=old_repository,
                            name="wine-staging_10.8-x86_64.AppImage")
        self.client._cache.release_entries[releases.policy_key(old_repository)] = {
            "catalog": {"latest": old},
            "checked": 1000, "status": "ok",
        }
        item = wine_provider_item("11.19")
        responses = {f"https://api.github.com/repos/{releases.WINE_REPOSITORY}/releases?per_page=100&page=1":
                     json.dumps([item]).encode()}
        with patch.object(network.time, "time", return_value=1000), patch.object(
            network, "_net_get", side_effect=responses.__getitem__,
        ) as fetch:
            self.assertEqual(self.client.wine_appimage_release().release.version, "11.19")
            self.client.wine_appimage_release()
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue(all(releases.WINE_REPOSITORY in call.args[0] for call in fetch.call_args_list))
        loaded = network.NetworkRequests()
        with patch.object(network.time, "time", return_value=1001), patch.object(network, "_net_get") as fetch:
            result = loaded.wine_appimage_release()
            self.assertEqual(result.release.tag, "continuous-devel")
            self.assertFalse(result.stale)
            fetch.assert_not_called()

    def test_missing_current_provider_is_fresh_unavailable_not_older_recommendation(self):
        responses = wine_source_responses()
        fixture = json.loads((Path(__file__).parent / "data/wine-release-metadata.json").read_text())
        responses[f"https://api.github.com/repos/{releases.WINE_REPOSITORY}/releases?per_page=100&page=1"] = (
            json.dumps(fixture["provider"]).encode()
        )
        with patch.object(network.time, "time", return_value=1000), patch.object(
            network, "_net_get", side_effect=responses.__getitem__,
        ):
            result = self.client.wine_appimage_release()
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.stale)
        self.assertIsNone(result.release)
        self.assertIn(releases.WINE_REPOSITORY, result.message)

    def test_same_instance_expires_and_force_refresh_preserves_hooks(self):
        with patch.object(network, "_get_release_data", return_value=self.catalog) as fetch:
            with patch.object(network.time, "time", return_value=1000):
                self.assertEqual(self.client.controller_release("stable").release.version, "1.1.0")
                self.client.controller_release("stable")
                self.assertEqual(fetch.call_count, 1)
                self.client.controller_release("stable", force=True)
                self.assertEqual(fetch.call_count, 2)
            with patch.object(network.time, "time", return_value=44201):
                self.client.controller_release("beta")
            self.assertEqual(fetch.call_count, 3)
        self.assertEqual(self.hook.call_count, 3)
        loaded = network.NetworkRequests()
        with (patch.object(network.time, "time", return_value=44202),
              patch.object(network, "_get_release_data") as fetch):
            self.assertEqual(loaded.controller_release("stable").release.version, "1.1.0")
            fetch.assert_not_called()

    def test_offline_stale_and_rate_limit_cooldown_are_truthful(self):
        with patch.object(network.time, "time", return_value=1000), patch.object(
            network, "_get_release_data", return_value=self.catalog,
        ):
            self.client.controller_release("stable")
        failure = releases.GithubSoftwareReleasesInfo(None, None, "rate-limited", "rate limit", 300)
        with patch.object(network, "_get_release_data", return_value=failure) as fetch:
            with patch.object(network.time, "time", return_value=50000):
                result = self.client.controller_release("stable")
                self.assertTrue(result.stale)
                self.assertEqual(result.release.version, "1.1.0")
                self.assertEqual(releases.controller_state(result), "stale")
                self.client.controller_release("beta", force=True)
                self.assertEqual(fetch.call_count, 1)
            with patch.object(network.time, "time", return_value=50301):
                self.client.controller_release("beta", force=True)
            self.assertEqual(fetch.call_count, 2)

    def test_failed_forced_check_retries_after_cooldown_even_with_recent_success(self):
        with patch.object(network.time, "time", return_value=1000), patch.object(
            network, "_get_release_data", return_value=self.catalog,
        ):
            self.client.controller_release("stable")
        with patch.object(network.time, "time", return_value=1001), patch.object(
            network, "_get_release_data", return_value=releases.GithubSoftwareReleasesInfo(None, None, "offline"),
        ):
            self.assertTrue(self.client.controller_release("stable", force=True).stale)
        with patch.object(network.time, "time", return_value=1062), patch.object(
            network, "_get_release_data", return_value=self.catalog,
        ) as fetch:
            self.assertFalse(self.client.controller_release("stable").stale)
            fetch.assert_called_once()

    def test_failed_product_feed_keeps_success_time_and_cannot_report_current(self):
        with patch.object(network.time, "time", return_value=1000), patch.object(
            network, "_fetch_faithlife_product_releases", return_value=["1.0.0.0"],
        ):
            self.client.product_release("Logos", "10", "stable")
        with patch.object(network.time, "time", return_value=1001), patch.object(
            network, "_fetch_faithlife_product_releases", side_effect=artifacts.MetadataError("offline"),
        ):
            stale = self.client.product_release("Logos", "10", "stable", force=True)
        self.assertEqual(stale.checked, 1000)
        self.assertEqual(stale.versions, ("1.0.0.0",))
        self.assertTrue(stale.stale)
        config = object.__new__(Config)
        config._network = self.client
        config._raw = PersistentConfiguration(faithlife_product="Logos", faithlife_product_version="10")
        with patch.object(network.time, "time", return_value=1002):
            self.assertIsNone(config.is_installed_faithlife_product_release_latest)
        with patch.object(network.time, "time", return_value=1062), patch.object(
            network, "_fetch_faithlife_product_releases", return_value=[],
        ) as fetch:
            result = self.client.product_release("Logos", "10", "stable")
            self.assertEqual(result.status, "empty")
            fetch.assert_called_once()

    def test_expired_wine_failure_cannot_publish_a_current_comparison(self):
        responses = wine_source_responses()
        item = release_item("11.20-devel", repository=releases.WINE_REPOSITORY,
                            name="wine-devel_11.20-x86_64.AppImage")
        responses[f"https://api.github.com/repos/{releases.WINE_REPOSITORY}/releases?per_page=100&page=1"] = (
            json.dumps([item]).encode()
        )
        with patch.object(network.time, "time", return_value=1000), patch.object(
            network, "_net_get", side_effect=responses.__getitem__,
        ):
            self.client.wine_appimage_release()
        config = object.__new__(Config)
        config._raw = PersistentConfiguration(wine_binary="fixture.AppImage", wine_binary_code="AppImage")
        config._network = self.client
        with patch.object(network.time, "time", return_value=50000), patch.object(
            network, "_net_get", side_effect=artifacts.MetadataError("offline"),
        ), patch("logos_desktop.utils.compare_recommended_appimage_version") as compare:
            config.refresh_release_snapshots()
            self.assertTrue(self.client.wine_appimage_release(refresh=False).stale)
        compare.assert_not_called()
        self.assertEqual(config._recommended_wine_status[0], -1)
        self.assertIn("stale", config._recommended_wine_status[1])

    def test_empty_cache_persistence_failure_and_old_lineages(self):
        self.client._cache.repository_latest_version[constants.REPOSITORY_SLUG] = "99.0.0"
        self.client._cache.repository_latest_url[constants.REPOSITORY_SLUG] = "https://example.invalid/old"
        empty = releases.GithubSoftwareReleasesInfo(None, None, "empty")
        with patch.object(network.time, "time", return_value=1000), patch.object(
            network, "_get_release_data", return_value=empty,
        ) as fetch, patch.object(self.client._cache, "_write", side_effect=OSError("disk full")):
            self.assertEqual(self.client.controller_release("stable").status, "empty")
            self.client.controller_release("stable")
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue(self.client.persistence_error)
        self.assertEqual(releases.controller_state(releases.ReleaseLookup(self.catalog.latest, "ok")), "available")
        with patch.object(constants, "APP_RELEASE_VERSION", None):
            state = releases.controller_state(releases.ReleaseLookup(self.catalog.latest, "ok"))
            self.assertEqual(state, "unversioned")

    def test_simultaneous_force_checks_are_coalesced(self):
        entered, finish = Event(), Event()

        def load(repository):
            entered.set()
            self.assertTrue(finish.wait(3))
            return self.catalog

        results = []
        with patch.object(network, "_get_release_data", side_effect=load) as fetch:
            first = Thread(target=lambda: results.append(self.client.controller_release("stable", force=True)))
            first.start()
            self.assertTrue(entered.wait(3))
            second = Thread(target=lambda: results.append(self.client.controller_release("beta", force=True)))
            second.start()
            finish.set()
            first.join(3)
            second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(fetch.call_count, 1)

    def test_closed_monitor_and_channel_switch_discard_late_results(self):
        entered, finish = Event(), Event()

        def load(channel, force=False):
            entered.set()
            finish.wait(3)
            return releases.ReleaseLookup(self.catalog.latest, "ok")

        client = Mock(controller_release=load)
        monitor = releases.ControllerChecks(client)
        monitor.request("stable")
        self.assertTrue(entered.wait(3))
        monitor.request("beta")
        monitor.close()
        finish.set()
        self.assertNotEqual(monitor.result.status, "ok")

    def test_force_during_admission_gets_a_followup_fetch(self):
        entered, finish = Event(), Event()

        def admit():
            entered.set()
            finish.wait(3)

        with patch.object(network.time, "time", return_value=1000), patch.object(
            network, "_get_release_data", return_value=self.catalog,
        ) as fetch:
            self.client.controller_release("stable")
            fetch.reset_mock()
            monitor = releases.ControllerChecks(self.client, admit)
            monitor.request("stable")
            self.assertTrue(entered.wait(3))
            monitor.request("stable", force=True)
            finish.set()
            monitor._worker.join(3)
            monitor.close()
            self.assertFalse(monitor._worker.is_alive())
            fetch.assert_called_once()

    def test_monitor_close_cancels_and_reaps_a_real_slow_wine_probe(self):
        from tests.test_controller_update import controller_elf

        root = Path(constants.CACHE_BASE)
        binary = root / "slow-wine-probe"
        original = processes.subprocess.Popen
        original_read = processes.os.read

        for close_output in (False, True):
            binary.write_bytes(controller_elf(loop=True, close_output=close_output))
            binary.chmod(0o700)
            entered, eof = Event(), Event()
            children = []

            def launch(*arguments, **keywords):
                child = original(*arguments, **keywords)
                children.append(child)
                entered.set()
                return child

            def read(descriptor, count):
                content = original_read(descriptor, count)
                if not content and any(not child.stdout.closed and descriptor == child.stdout.fileno()
                                       for child in children):
                    eof.set()
                return content

            def callback(force=False):
                return wine.get_wine_release(str(binary))

            with (patch.object(processes.subprocess, "Popen", side_effect=launch),
                  patch.object(processes.os, "read", side_effect=read),
                  patch.object(wine.runtime, "executable", return_value=str(binary)),
                  patch.object(self.client, "controller_release", return_value=releases.ReleaseLookup(status="empty"))):
                monitor = releases.ControllerChecks(self.client, refresh_product=callback)
                monitor.request("stable")
                self.assertTrue(entered.wait(3))
                if close_output:
                    self.assertTrue(eof.wait(3), "The probe must reach actual EOF before cancellation")
                    self.assertIsNone(children[0].poll())
                monitor.close()
            self.assertFalse(monitor._worker.is_alive())
            self.assertTrue(children)
            self.assertTrue(all(child.poll() is not None for child in children))

    def test_saved_channels_survive_reload_and_explicit_environment_wins(self):
        path = Path(constants.CACHE_BASE) / "selected.json"
        path.write_text(json.dumps({"app_release_channel": "dev", "faithlife_product_release_channel": "beta"}))
        with patch.dict(os.environ, {}, clear=True):
            loaded = PersistentConfiguration.load_from_path(str(path))
            self.assertEqual(loaded.app_release_channel, "dev")
            self.assertEqual(loaded.faithlife_product_release_channel, "beta")
        with patch.dict(os.environ, {"lli_release_channel": "stable"}, clear=True):
            self.assertEqual(PersistentConfiguration.load_from_path(str(path)).app_release_channel, "stable")
        config = object.__new__(Config)
        config._raw = loaded
        with patch.object(config, "_write") as write:
            config.toggle_installer_release_channel()
            write.assert_called_once_with(checked=True)
            self.assertEqual(loaded.app_release_channel, "stable")

    def test_unreadable_cache_does_not_prevent_constructing_an_offline_client(self):
        with patch.object(network.CachedRequests, "load", side_effect=PermissionError("fixture cache denied")):
            client = network.NetworkRequests()
        self.assertTrue(client.persistence_error)
        self.assertEqual(client.controller_release("stable", refresh=False).status, "unchecked")
