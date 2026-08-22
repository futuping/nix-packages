import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_shardx_launcher


class UpdateShardXLauncherTests(unittest.TestCase):
    def release(self, **overrides):
        version = overrides.pop("version", "0.1.10")
        asset_name = f"ShardX.Launcher_{version}_aarch64.dmg"
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
                    "url": update_shardx_launcher.DOWNLOAD_URL.format(
                        version=version
                    ),
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
                        update_shardx_launcher.DOWNLOAD_URL.format(version="0.1.10"),
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
                        update_shardx_launcher.DOWNLOAD_URL.format(version="0.1.10"),
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
