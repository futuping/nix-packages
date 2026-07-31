import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import update_ego_lite


class UpdateEgoLiteTests(unittest.TestCase):
    digest = "ab" * 32
    version_id = "test-S3_version.id"

    def source(self, **overrides):
        source = {
            "version": "0.4.5.5",
            "bundleVersion": "4.5.5",
            "url": update_ego_lite.DOWNLOAD_URL,
            "hash": update_ego_lite.sri_sha256(self.digest),
            "upstreamHash": f"sha256:{self.digest}",
            "s3VersionId": self.version_id,
        }
        source.update(overrides)
        return source

    def write_source(self, directory: str, source=None) -> Path:
        path = Path(directory) / "ego-lite-source.json"
        path.write_text(json.dumps(source or self.source(), indent=2) + "\n")
        return path

    def mocked_upstream(self, *, version="0.4.5.5", bundle_version="4.5.5"):
        return (
            mock.patch.object(
                update_ego_lite,
                "download_artifact",
                return_value=(self.digest, self.digest, self.version_id),
            ),
            mock.patch.object(
                update_ego_lite,
                "inspect_application",
                return_value=(version, bundle_version),
            ),
        )

    def test_version_key_rejects_non_numeric_version(self):
        with self.assertRaisesRegex(update_ego_lite.UpdateError, "invalid application"):
            update_ego_lite.version_key("0.4.6-beta.1")

    def test_validate_download_url_rejects_unreviewed_host(self):
        with self.assertRaisesRegex(update_ego_lite.UpdateError, "unexpected download"):
            update_ego_lite.validate_download_url(
                "https://example.com/setup/macos/arm64/egolite.dmg"
            )

    def test_response_validators_accept_expected_metadata(self):
        headers = {
            "Content-Type": "application/x-apple-diskimage",
            "Content-Length": "129152669",
            "X-Amz-Meta-Sha256": self.digest,
            "X-Amz-Version-Id": self.version_id,
        }

        self.assertEqual(
            update_ego_lite.response_validators(headers),
            (129152669, self.digest, self.version_id),
        )

    def test_response_validators_reject_oversized_download(self):
        headers = {
            "Content-Type": "application/x-apple-diskimage",
            "Content-Length": str(update_ego_lite.MAX_DOWNLOAD_BYTES + 1),
            "X-Amz-Meta-Sha256": self.digest,
            "X-Amz-Version-Id": self.version_id,
        }

        with self.assertRaisesRegex(update_ego_lite.UpdateError, "content length"):
            update_ego_lite.response_validators(headers)

    def test_read_source_rejects_unexpected_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(directory, {**self.source(), "extra": "value"})

            with self.assertRaisesRegex(update_ego_lite.UpdateError, "source keys"):
                update_ego_lite.read_source(path)

    def test_read_source_rejects_mismatched_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(
                directory,
                self.source(hash=update_ego_lite.sri_sha256("cd" * 32)),
            )

            with self.assertRaisesRegex(update_ego_lite.UpdateError, "does not match"):
                update_ego_lite.read_source(path)

    def test_update_leaves_current_source_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(directory)
            before = path.read_bytes()
            download, inspect = self.mocked_upstream()

            with download, inspect:
                changed = update_ego_lite.update(path)

            self.assertFalse(changed)
            self.assertEqual(path.read_bytes(), before)

    def test_update_rejects_same_version_artifact_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(directory, self.source(s3VersionId="old-version"))
            download, inspect = self.mocked_upstream()

            with download, inspect, self.assertRaisesRegex(
                update_ego_lite.UpdateError, "changed; manual review"
            ):
                update_ego_lite.update(path)

    def test_update_rejects_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(
                directory,
                self.source(version="0.4.6.0", bundleVersion="4.6.0"),
            )
            download, inspect = self.mocked_upstream()

            with download, inspect, self.assertRaisesRegex(
                update_ego_lite.UpdateError, "newer than upstream"
            ):
                update_ego_lite.update(path)

    def test_update_writes_newer_version_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(directory)
            download, inspect = self.mocked_upstream(
                version="0.4.6.0", bundle_version="4.6.0"
            )

            with download, inspect:
                changed = update_ego_lite.update(path)

            self.assertTrue(changed)
            self.assertEqual(
                json.loads(path.read_text()),
                self.source(version="0.4.6.0", bundleVersion="4.6.0"),
            )

    def test_check_reports_newer_version_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(directory)
            before = path.read_bytes()
            download, inspect = self.mocked_upstream(
                version="0.4.6.0", bundle_version="4.6.0"
            )

            with download, inspect:
                changed = update_ego_lite.update(path, check=True)

            self.assertTrue(changed)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
