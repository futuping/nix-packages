import unittest

from scripts import update_lite_xl


class UpdateLiteXlTests(unittest.TestCase):
    def release(self, **overrides):
        version = overrides.pop("version", "2.1.8")
        asset_name = f"lite-xl-v{version}-macos-arm64.dmg"
        release = {
            "tag_name": f"v{version}",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": asset_name,
                    "browser_download_url": (
                        "https://github.com/lite-xl/lite-xl/releases/download/"
                        f"v{version}/{asset_name}"
                    ),
                    "digest": "sha256:" + "ab" * 32,
                }
            ],
        }
        release.update(overrides)
        return release

    def test_release_metadata_selects_exact_arm_asset(self):
        version, url, digest = update_lite_xl.release_metadata(self.release())

        self.assertEqual(version, "2.1.8")
        self.assertEqual(
            url,
            "https://github.com/lite-xl/lite-xl/releases/download/"
            "v2.1.8/lite-xl-v2.1.8-macos-arm64.dmg",
        )
        self.assertEqual(digest, "ab" * 32)

    def test_release_metadata_rejects_prerelease(self):
        with self.assertRaisesRegex(update_lite_xl.UpdateError, "prerelease"):
            update_lite_xl.release_metadata(self.release(prerelease=True))

    def test_release_metadata_rejects_unexpected_url(self):
        release = self.release()
        release["assets"][0]["browser_download_url"] = "https://example.com/app.dmg"

        with self.assertRaisesRegex(update_lite_xl.UpdateError, "unexpected asset URL"):
            update_lite_xl.release_metadata(release)

    def test_release_metadata_rejects_duplicate_asset(self):
        release = self.release()
        release["assets"].append(dict(release["assets"][0]))

        with self.assertRaisesRegex(update_lite_xl.UpdateError, "exactly one"):
            update_lite_xl.release_metadata(release)

    def test_version_key_rejects_non_stable_version(self):
        with self.assertRaisesRegex(update_lite_xl.UpdateError, "invalid stable"):
            update_lite_xl.version_key("2.2.0-rc1")

    def test_sri_sha256(self):
        self.assertEqual(
            update_lite_xl.sri_sha256(
                "1b8ad02ea575d08d6557daff035d4ac59c069254dd85d01f7bdac839cfdd66e3"
            ),
            "sha256-G4rQLqV10I1lV9r/A11KxZwGklTdhdAfe9rIOc/dZuM=",
        )


if __name__ == "__main__":
    unittest.main()
