from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_shardx_launcher


class UpdateShardXLauncherTests(unittest.TestCase):
    def release(self, **overrides):
        version = overrides.pop("version", "0.1.10")
        product = (
            "ShardBrowser"
            if tuple(map(int, version.split("."))) >= (2, 1, 0)
            else "ShardX"
        )
        asset_name = f"{product}.Launcher_{version}_aarch64.dmg"
        release = {
            "tag_name": f"v{version}",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": asset_name,
                    "browser_download_url": (
                        "https://github.com/ProxyShard/ShardBrowser/releases/download/"
                        f"v{version}/{asset_name}"
                    ),
                    "digest": "sha256:" + "ab" * 32,
                }
            ],
        }
        release.update(overrides)
        return release

    def write_source(self, path: Path, *, version: str, digest: str) -> None:
        path.write_text(
            json.dumps(
                {
                    "version": version,
                    "url": update_shardx_launcher.download_url(version),
                    "hash": update_shardx_launcher.sri_sha256(digest),
                }
            )
        )

    def test_release_metadata_selects_exact_arm_asset(self):
        version, url, digest = update_shardx_launcher.release_metadata(self.release())

        self.assertEqual(version, "0.1.10")
        self.assertEqual(
            url,
            "https://github.com/ProxyShard/ShardBrowser/releases/download/"
            "v0.1.10/ShardX.Launcher_0.1.10_aarch64.dmg",
        )
        self.assertEqual(digest, "ab" * 32)

    def test_product_rename_boundary(self):
        for version, product, executable in (
            ("2.0.3", "ShardX", "shardx-launcher"),
            ("2.0.99", "ShardX", "shardx-launcher"),
            ("2.1.0", "ShardBrowser", "shardbrowser-launcher"),
            ("2.1.1", "ShardBrowser", "shardbrowser-launcher"),
            ("2.10.0", "ShardBrowser", "shardbrowser-launcher"),
        ):
            with self.subTest(version=version):
                name = f"{product}.Launcher_{version}_aarch64.dmg"
                url = (
                    "https://github.com/ProxyShard/ShardBrowser/releases/download/"
                    f"v{version}/{name}"
                )
                self.assertEqual(update_shardx_launcher.asset_name(version), name)
                self.assertEqual(update_shardx_launcher.download_url(version), url)
                self.assertEqual(
                    update_shardx_launcher.application_names(version),
                    (f"{product} Launcher.app", executable),
                )
                self.assertEqual(
                    update_shardx_launcher.release_metadata(self.release(version=version)),
                    (version, url, "ab" * 32),
                )

    def test_source_uses_exact_version_bound_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.json"
            for version in ("2.0.3", "2.1.0", "2.1.1"):
                with self.subTest(version=version):
                    self.write_source(source, version=version, digest="ab" * 32)
                    current = update_shardx_launcher.read_source(source)
                    wrong = "ShardX" if version != "2.0.3" else "ShardBrowser"
                    current["url"] = (
                        "https://github.com/ProxyShard/ShardBrowser/releases/download/"
                        f"v{version}/{wrong}.Launcher_{version}_aarch64.dmg"
                    )
                    source.write_text(json.dumps(current))
                    with self.assertRaisesRegex(
                        update_shardx_launcher.UpdateError, "unexpected configured URL"
                    ):
                        update_shardx_launcher.read_source(source)

    def test_legacy_name_after_rename_is_not_deferred_as_pending_upload(self):
        release = self.release(
            version="2.1.0", published_at="2026-10-22T10:36:05Z"
        )
        release["assets"][0]["name"] = "ShardX.Launcher_2.1.0_aarch64.dmg"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.json"
            self.write_source(source, version="2.0.3", digest="aa" * 32)
            before = source.read_bytes()
            with patch.object(
                update_shardx_launcher, "github_release", return_value=release
            ), patch.object(
                update_shardx_launcher, "datetime", wraps=datetime
            ) as clock, patch.object(
                update_shardx_launcher, "download_artifact"
            ) as download:
                clock.now.return_value = datetime(
                    2026, 10, 22, 11, tzinfo=timezone.utc
                )
                with self.assertRaisesRegex(
                    update_shardx_launcher.UpdateError, "unexpected launcher asset name"
                ):
                    update_shardx_launcher.update(source)
            self.assertEqual(source.read_bytes(), before)
            download.assert_not_called()

    def test_renamed_asset_requires_renamed_url(self):
        release = self.release(version="2.1.1")
        release["assets"][0]["browser_download_url"] = (
            "https://github.com/ProxyShard/ShardBrowser/releases/download/"
            "v2.1.1/ShardX.Launcher_2.1.1_aarch64.dmg"
        )
        with self.assertRaisesRegex(
            update_shardx_launcher.UpdateError, "unexpected asset URL"
        ):
            update_shardx_launcher.release_metadata(release)

    def test_update_adopts_renamed_release_from_legacy_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.json"
            self.write_source(source, version="2.0.3", digest="aa" * 32)
            with patch.object(
                update_shardx_launcher,
                "github_release",
                return_value=self.release(version="2.1.1"),
            ), patch.object(
                update_shardx_launcher, "download_artifact", return_value="ab" * 32
            ) as download, patch.object(
                update_shardx_launcher, "inspect_application"
            ) as inspect:
                self.assertTrue(update_shardx_launcher.update(source))
            self.assertEqual(
                json.loads(source.read_text()),
                {
                    "version": "2.1.1",
                    "url": "https://github.com/ProxyShard/ShardBrowser/releases/download/"
                    "v2.1.1/ShardBrowser.Launcher_2.1.1_aarch64.dmg",
                    "hash": update_shardx_launcher.sri_sha256("ab" * 32),
                },
            )
            self.assertEqual(
                download.call_args.args[1].name,
                "ShardBrowser.Launcher_2.1.1_aarch64.dmg",
            )
            self.assertEqual(inspect.call_args.args[1], "2.1.1")

    def inspect_fixture(
        self,
        version,
        *,
        bundle_name=None,
        plist_overrides=None,
        architectures="arm64",
        extra_app=False,
        expected_error=None,
    ):
        renamed = tuple(map(int, version.split("."))) >= (2, 1, 0)
        app = "ShardBrowser Launcher.app" if renamed else "ShardX Launcher.app"
        executable = "shardbrowser-launcher" if renamed else "shardx-launcher"
        commands = []

        def command(command):
            commands.append(command)
            output = ""
            if command[:2] == ["/usr/bin/hdiutil", "attach"]:
                mount = Path(command[-1])
                application = mount / (bundle_name or app)
                contents = application / "Contents"
                contents.mkdir(parents=True)
                info = {
                    "CFBundleIdentifier": "com.shardx.launcher",
                    "CFBundleShortVersionString": version,
                    "CFBundleExecutable": executable,
                }
                info.update(plist_overrides or {})
                (contents / "Info.plist").write_bytes(plistlib.dumps(info))
                if extra_app:
                    (mount / "Unexpected.app").mkdir()
            elif command[:2] == ["/usr/bin/lipo", "-archs"]:
                self.assertEqual(Path(command[-1]).name, executable)
                output = architectures
            else:
                self.assertEqual(command[:2], ["/usr/bin/hdiutil", "detach"])
            return subprocess.CompletedProcess(command, 0, output, "")

        with patch.object(
            update_shardx_launcher.sys, "platform", "darwin"
        ), patch.object(update_shardx_launcher, "run", side_effect=command):
            if expected_error is None:
                update_shardx_launcher.inspect_application(Path("fixture.dmg"), version)
            else:
                with self.assertRaisesRegex(
                    update_shardx_launcher.UpdateError, expected_error
                ):
                    update_shardx_launcher.inspect_application(Path("fixture.dmg"), version)

        self.assertIn("-readonly", commands[0])
        self.assertEqual(commands[-1][:2], ["/usr/bin/hdiutil", "detach"])
        self.assertEqual(commands[-1][-1], commands[0][-1])
        return commands

    def test_inspect_application_accepts_legacy_and_renamed_bundle(self):
        for version in ("2.0.3", "2.1.0", "2.1.1"):
            with self.subTest(version=version):
                commands = self.inspect_fixture(version)
                self.assertEqual(len(commands), 3)

    def test_inspect_application_rejects_unexpected_bundles_and_detaches(self):
        for options in (
            {"bundle_name": "ShardX Launcher.app"},
            {"bundle_name": "Other.app"},
            {"extra_app": True},
        ):
            with self.subTest(options=options):
                commands = self.inspect_fixture(
                    "2.1.1", expected_error="unexpected application bundles", **options
                )
                self.assertEqual(len(commands), 2)

    def test_inspect_application_rejects_wrong_identity_and_detaches(self):
        for overrides, message in (
            ({"CFBundleIdentifier": "com.other.launcher"}, "unexpected bundle ID"),
            ({"CFBundleShortVersionString": "2.0.3"}, "unexpected bundle version"),
            ({"CFBundleExecutable": "shardx-launcher"}, "unexpected bundle executable"),
            ({"CFBundleExecutable": "../../outside"}, "unexpected bundle executable"),
        ):
            with self.subTest(overrides=overrides):
                commands = self.inspect_fixture(
                    "2.1.1", plist_overrides=overrides, expected_error=message
                )
                self.assertEqual(len(commands), 2)

    def test_inspect_application_rejects_wrong_architectures_and_detaches(self):
        for architectures in ("x86_64", "arm64 x86_64", ""):
            with self.subTest(architectures=architectures):
                commands = self.inspect_fixture(
                    "2.1.1",
                    architectures=architectures,
                    expected_error="unexpected application architectures",
                )
                self.assertEqual(len(commands), 3)

    def test_release_metadata_rejects_prerelease(self):
        with self.assertRaisesRegex(update_shardx_launcher.UpdateError, "prerelease"):
            update_shardx_launcher.release_metadata(self.release(prerelease=True))

    def test_release_metadata_rejects_unexpected_url(self):
        release = self.release()
        release["assets"][0]["browser_download_url"] = "https://example.com/app.dmg"

        with self.assertRaisesRegex(update_shardx_launcher.UpdateError, "unexpected asset URL"):
            update_shardx_launcher.release_metadata(release)

    def test_release_metadata_rejects_duplicate_asset(self):
        release = self.release()
        release["assets"].append(dict(release["assets"][0]))

        with self.assertRaisesRegex(update_shardx_launcher.UpdateError, "exactly one"):
            update_shardx_launcher.release_metadata(release)

    def test_missing_new_asset_during_upload_keeps_source_unchanged(self):
        # The September 12 failure happened before v2.0.2's assets were
        # uploaded at 13:28 UTC, after the release appeared at 10:47 UTC.
        release = self.release(
            version="2.0.2", assets=[], published_at="2026-09-12T10:47:42Z"
        )
        now = datetime(2026, 9, 12, 11, 13, 35, tzinfo=timezone.utc)
        for check in (False, True):
            with self.subTest(check=check), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "source.json"
                self.write_source(source, version="2.0.1", digest="aa" * 32)
                before = source.read_bytes()
                warning = io.StringIO()
                with patch.object(
                    update_shardx_launcher, "github_release", return_value=release
                ), patch.object(
                    update_shardx_launcher, "datetime", wraps=datetime
                ) as clock, patch.object(
                    update_shardx_launcher, "download_artifact"
                ) as download, redirect_stderr(warning):
                    clock.now.return_value = now
                    self.assertFalse(update_shardx_launcher.update(source, check=check))

                self.assertEqual(source.read_bytes(), before)
                self.assertIn("keeping 2.0.1 until the next updater run", warning.getvalue())
                download.assert_not_called()

    def test_missing_asset_outside_upload_window_still_fails(self):
        now = datetime(2026, 9, 12, 17, tzinfo=timezone.utc)
        timestamps = [
            None,
            "invalid",
            "2026-09-12T17:00:01Z",  # Future timestamps cannot extend the window.
            (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "2026-09-11T10:47:42Z",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.json"
            self.write_source(source, version="2.0.1", digest="aa" * 32)
            before = source.read_bytes()
            for published_at in timestamps:
                with self.subTest(published_at=published_at), patch.object(
                    update_shardx_launcher,
                    "github_release",
                    return_value=self.release(
                        version="2.0.2", assets=[], published_at=published_at
                    ),
                ), patch.object(
                    update_shardx_launcher, "datetime", wraps=datetime
                ) as clock, patch.object(
                    update_shardx_launcher, "download_artifact"
                ) as download:
                    clock.now.return_value = now
                    with self.assertRaisesRegex(
                        update_shardx_launcher.UpdateError, "found 0"
                    ):
                        update_shardx_launcher.update(source)
                    self.assertEqual(source.read_bytes(), before)
                    download.assert_not_called()

    def test_missing_current_or_older_asset_is_not_deferred(self):
        now = datetime(2026, 9, 12, 11, tzinfo=timezone.utc)
        for version in ("2.0.1", "2.0.2"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "source.json"
                self.write_source(source, version="2.0.2", digest="aa" * 32)
                with patch.object(
                    update_shardx_launcher,
                    "github_release",
                    return_value=self.release(
                        version=version, assets=[], published_at="2026-09-12T10:47:42Z"
                    ),
                ), patch.object(
                    update_shardx_launcher, "datetime", wraps=datetime
                ) as clock:
                    clock.now.return_value = now
                    with self.assertRaises(update_shardx_launcher.MissingReleaseAsset):
                        update_shardx_launcher.update(source)

    def test_uploaded_new_asset_is_validated_and_adopted(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.json"
            self.write_source(source, version="2.0.1", digest="aa" * 32)
            with patch.object(
                update_shardx_launcher,
                "github_release",
                return_value=self.release(
                    version="2.0.2", published_at="2026-09-12T10:47:42Z"
                ),
            ), patch.object(
                update_shardx_launcher, "download_artifact", return_value="ab" * 32
            ) as download, patch.object(
                update_shardx_launcher, "inspect_application"
            ) as inspect:
                self.assertTrue(update_shardx_launcher.update(source))

            self.assertEqual(json.loads(source.read_text())["version"], "2.0.2")
            download.assert_called_once()
            inspect.assert_called_once()
            self.assertEqual(inspect.call_args.args[1], "2.0.2")

    def test_release_metadata_rejects_unexpected_digest(self):
        release = self.release()
        release["assets"][0]["digest"] = "sha512:" + "ab" * 64

        with self.assertRaisesRegex(update_shardx_launcher.UpdateError, "digest"):
            update_shardx_launcher.release_metadata(release)

    def test_version_key_rejects_non_stable_version(self):
        with self.assertRaisesRegex(update_shardx_launcher.UpdateError, "invalid stable"):
            update_shardx_launcher.version_key("0.2.0-rc1")

    def test_sri_sha256(self):
        self.assertEqual(
            update_shardx_launcher.sri_sha256(
                "d740467d0f914d42c440241495d478974375f84a3166665851547ef323da3fcf"
            ),
            "sha256-10BGfQ+RTULEQCQUldR4l0N1+EoxZmZYUVR+8yPaP88=",
        )

    def test_update_rejects_downgrade_before_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.json"
            self.write_source(source, version="0.1.11", digest="aa" * 32)

            with patch.object(
                update_shardx_launcher,
                "github_release",
                return_value=self.release(version="0.1.10"),
            ), patch.object(update_shardx_launcher, "download_artifact") as download:
                with self.assertRaisesRegex(update_shardx_launcher.UpdateError, "newer"):
                    update_shardx_launcher.update(source)

            download.assert_not_called()

    def test_update_rejects_same_version_asset_mutation(self):
        current_digest = "aa" * 32
        latest_digest = "bb" * 32
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.json"
            self.write_source(source, version="0.1.10", digest=current_digest)
            release = self.release()
            release["assets"][0]["digest"] = "sha256:" + latest_digest

            with patch.object(
                update_shardx_launcher,
                "github_release",
                return_value=release,
            ), patch.object(
                update_shardx_launcher,
                "download_artifact",
                return_value=latest_digest,
            ), patch.object(update_shardx_launcher, "inspect_application"):
                with self.assertRaisesRegex(
                    update_shardx_launcher.UpdateError, "changed"
                ):
                    update_shardx_launcher.update(source)

    def test_update_rejects_digest_mismatch_before_mounting(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.json"
            self.write_source(source, version="0.1.10", digest="aa" * 32)

            with patch.object(
                update_shardx_launcher,
                "github_release",
                return_value=self.release(),
            ), patch.object(
                update_shardx_launcher,
                "download_artifact",
                return_value="bb" * 32,
            ), patch.object(update_shardx_launcher, "inspect_application") as inspect:
                with self.assertRaisesRegex(
                    update_shardx_launcher.UpdateError, "does not match"
                ):
                    update_shardx_launcher.update(source)

            inspect.assert_not_called()

    def test_download_rejects_unallowlisted_redirect_host(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self):
                return "https://example.com/shardx.dmg"

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "artifact.dmg"
            with patch.object(update_shardx_launcher, "urlopen", return_value=Response()):
                with self.assertRaisesRegex(
                    update_shardx_launcher.UpdateError, "download response URL"
                ):
                    update_shardx_launcher.download_artifact(
                        update_shardx_launcher.download_url("0.1.10"),
                        destination,
                    )

    def test_download_rejects_http_redirect(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self):
                return "http://release-assets.githubusercontent.com/shardx.dmg"

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "artifact.dmg"
            with patch.object(update_shardx_launcher, "urlopen", return_value=Response()):
                with self.assertRaisesRegex(
                    update_shardx_launcher.UpdateError, "download response URL"
                ):
                    update_shardx_launcher.download_artifact(
                        update_shardx_launcher.download_url("0.1.10"),
                        destination,
                    )

    def test_api_rejects_http_redirect(self):
        class Response(io.BytesIO):
            def __init__(self):
                super().__init__(b"{}")

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self):
                return "http://api.github.com/repos/ProxyShard/ShardBrowser/releases/latest"

        with patch.object(update_shardx_launcher, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(
                update_shardx_launcher.UpdateError, "API response URL"
            ):
                update_shardx_launcher.github_release()


if __name__ == "__main__":
    unittest.main()
