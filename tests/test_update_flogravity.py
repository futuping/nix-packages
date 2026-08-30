import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import update_flogravity


class UpdateFloGravityTests(unittest.TestCase):
    digest = "ab" * 32
    signature = base64.b64encode(b"s" * 64).decode("ascii")

    def source(self, **overrides):
        version = overrides.get("version", "4.12.0")
        source = {
            "version": version,
            "bundleVersion": "157",
            "minimumSystemVersion": "14.0",
            "url": update_flogravity.expected_download_url(version),
            "hash": update_flogravity.sri_sha256(self.digest),
            "size": 100,
            "sparkleSignature": self.signature,
        }
        source.update(overrides)
        return source

    def release(self, **overrides):
        version = overrides.get("version", "4.12.0")
        release = {
            "version": version,
            "bundleVersion": "157",
            "minimumSystemVersion": "14.0",
            "url": update_flogravity.expected_download_url(version),
            "size": 100,
            "sparkleSignature": self.signature,
        }
        release.update(overrides)
        return release

    def appcast_item(
        self,
        *,
        version="4.12.0",
        bundle_version="157",
        url=None,
        extra_enclosure="",
    ):
        url = url or update_flogravity.expected_download_url(version)
        return f"""
        <item>
          <title>{version}</title>
          <sparkle:version>{bundle_version}</sparkle:version>
          <sparkle:shortVersionString>{version}</sparkle:shortVersionString>
          <sparkle:minimumSystemVersion>14.0</sparkle:minimumSystemVersion>
          <enclosure url="{url}" length="100" type="application/octet-stream"
            sparkle:edSignature="{self.signature}" />
          {extra_enclosure}
        </item>
        """

    def appcast(self, *items):
        return (
            "<?xml version=\"1.0\"?>"
            '<rss xmlns:sparkle="'
            + update_flogravity.SPARKLE_NAMESPACE
            + '"><channel>'
            + "".join(items)
            + "</channel></rss>"
        ).encode()

    def write_source(self, directory, source=None):
        path = Path(directory) / "flogravity-source.json"
        path.write_text(json.dumps(source or self.source(), indent=2) + "\n")
        return path

    def mocked_upstream(self, release=None, digest=None):
        return (
            mock.patch.object(update_flogravity, "fetch_appcast", return_value=b"xml"),
            mock.patch.object(
                update_flogravity,
                "parse_appcast",
                return_value=release or self.release(),
            ),
            mock.patch.object(
                update_flogravity,
                "download_artifact",
                return_value=digest or self.digest,
            ),
            mock.patch.object(update_flogravity, "verify_sparkle_signature"),
            mock.patch.object(update_flogravity, "inspect_application"),
        )

    def test_version_key_rejects_prerelease(self):
        with self.assertRaisesRegex(update_flogravity.UpdateError, "invalid stable"):
            update_flogravity.version_key("4.13.0-beta.1")

    def test_ed25519_verifies_rfc_8032_vector(self):
        public_key = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a"
            "0ee172f3daa62325af021a68f707511a"
        )
        signature = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a"
            "84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd"
            "25bf5f0595bbe24655141438e7a100b"
        )

        self.assertTrue(update_flogravity.verify_ed25519(public_key, signature, b""))
        self.assertFalse(
            update_flogravity.verify_ed25519(public_key, signature, b"changed")
        )

    def test_parse_appcast_selects_latest_stable_release(self):
        metadata = update_flogravity.parse_appcast(
            self.appcast(
                self.appcast_item(version="4.11.0", bundle_version="156"),
                self.appcast_item(version="4.12.0", bundle_version="157"),
            )
        )

        self.assertEqual(metadata["version"], "4.12.0")
        self.assertEqual(metadata["bundleVersion"], "157")
        self.assertEqual(metadata["size"], 100)

    def test_parse_appcast_rejects_unexpected_download_host(self):
        payload = self.appcast(
            self.appcast_item(url="https://example.com/FloGravity-4.12.0.dmg")
        )
        with self.assertRaisesRegex(update_flogravity.UpdateError, "download URL"):
            update_flogravity.parse_appcast(payload)

    def test_parse_appcast_rejects_multiple_enclosures(self):
        extra = (
            '<enclosure url="https://d.vkr.me/fg/mac/releases/4.12.0/'
            'FloGravity-4.12.0.dmg" length="100" '
            'type="application/octet-stream" sparkle:edSignature="'
            + self.signature
            + '" />'
        )
        payload = self.appcast(self.appcast_item(extra_enclosure=extra))
        with self.assertRaisesRegex(update_flogravity.UpdateError, "exactly one"):
            update_flogravity.parse_appcast(payload)

    def test_parse_appcast_rejects_document_type(self):
        payload = b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss />'
        with self.assertRaisesRegex(update_flogravity.UpdateError, "DTD or entities"):
            update_flogravity.parse_appcast(payload)

    def test_read_source_rejects_unexpected_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(directory, {**self.source(), "extra": "value"})
            with self.assertRaisesRegex(update_flogravity.UpdateError, "source keys"):
                update_flogravity.read_source(path)

    def test_signature_rejects_invalid_current_version(self):
        details = "\n".join(
            [
                "Identifier=me.vkr.fg",
                "Authority=(unavailable)",
                "TeamIdentifier=3MFNWTLLFG",
                "CodeDirectory v=20500 flags=0x10000(runtime)",
            ]
        )
        invalid = mock.Mock(
            returncode=1,
            stdout="",
            stderr="invalid signature",
        )
        with mock.patch.object(
            update_flogravity,
            "command_output",
            return_value=details,
        ), mock.patch.object(
            update_flogravity,
            "run",
            return_value=invalid,
        ), self.assertRaisesRegex(
            update_flogravity.UpdateError,
            "invalid signature",
        ):
            update_flogravity.validate_application_signature(Path("FloGravity.app"))

    def test_signature_rejects_invalid_future_version(self):
        details = "\n".join(
            [
                "Identifier=me.vkr.fg",
                "Authority=(unavailable)",
                "TeamIdentifier=3MFNWTLLFG",
                "CodeDirectory v=20500 flags=0x10000(runtime)",
            ]
        )
        invalid = mock.Mock(
            returncode=1,
            stdout="",
            stderr="invalid signature",
        )
        with mock.patch.object(
            update_flogravity,
            "command_output",
            return_value=details,
        ), mock.patch.object(
            update_flogravity,
            "run",
            return_value=invalid,
        ), self.assertRaisesRegex(
            update_flogravity.UpdateError,
            "invalid signature",
        ):
            update_flogravity.validate_application_signature(Path("FloGravity.app"))

    def test_signature_requires_expected_authority_when_valid(self):
        details = "\n".join(
            [
                "Identifier=me.vkr.fg",
                "Authority=(unavailable)",
                "TeamIdentifier=3MFNWTLLFG",
                "CodeDirectory v=20500 flags=0x10000(runtime)",
            ]
        )
        valid = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            update_flogravity,
            "command_output",
            return_value=details,
        ), mock.patch.object(
            update_flogravity,
            "run",
            return_value=valid,
        ), self.assertRaisesRegex(
            update_flogravity.UpdateError,
            "signing authorities",
        ):
            update_flogravity.validate_application_signature(Path("FloGravity.app"))

    def test_signature_accepts_expected_notarized_developer_id(self):
        details = "\n".join(
            [
                "Identifier=me.vkr.fg",
                "Authority=Developer ID Application: JUN LIU (3MFNWTLLFG)",
                "Authority=Developer ID Certification Authority",
                "Authority=Apple Root CA",
                "TeamIdentifier=3MFNWTLLFG",
                "CodeDirectory v=20500 flags=0x10000(runtime)",
            ]
        )
        valid = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            update_flogravity,
            "command_output",
            side_effect=[details, "source=Notarized Developer ID"],
        ), mock.patch.object(
            update_flogravity,
            "run",
            return_value=valid,
        ):
            update_flogravity.validate_application_signature(Path("FloGravity.app"))

    def test_signature_requires_hardened_runtime(self):
        details = "\n".join(
            [
                "Identifier=me.vkr.fg",
                "Authority=Developer ID Application: JUN LIU (3MFNWTLLFG)",
                "TeamIdentifier=3MFNWTLLFG",
                "CodeDirectory v=20500 flags=0x0(none)",
            ]
        )
        with mock.patch.object(
            update_flogravity,
            "command_output",
            return_value=details,
        ), self.assertRaisesRegex(
            update_flogravity.UpdateError,
            "hardened runtime",
        ):
            update_flogravity.validate_application_signature(Path("FloGravity.app"))

    def test_signature_requires_notarized_gatekeeper_result(self):
        details = "\n".join(
            [
                "Identifier=me.vkr.fg",
                "Authority=Developer ID Application: JUN LIU (3MFNWTLLFG)",
                "Authority=Developer ID Certification Authority",
                "Authority=Apple Root CA",
                "TeamIdentifier=3MFNWTLLFG",
                "CodeDirectory v=20500 flags=0x10000(runtime)",
            ]
        )
        valid = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            update_flogravity,
            "command_output",
            side_effect=[details, "source=Developer ID"],
        ), mock.patch.object(
            update_flogravity,
            "run",
            return_value=valid,
        ), self.assertRaisesRegex(
            update_flogravity.UpdateError,
            "Gatekeeper assessment",
        ):
            update_flogravity.validate_application_signature(Path("FloGravity.app"))

    def test_download_rejects_unreviewed_redirect_host(self):
        class Response(io.BytesIO):
            headers = {
                "Content-Type": "application/x-apple-diskimage",
                "Content-Length": "100",
            }

            def __init__(self):
                super().__init__(b"x" * 100)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self):
                return "https://example.com/FloGravity-4.12.0.dmg"

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "FloGravity.dmg"
            with mock.patch.object(
                update_flogravity,
                "open_url",
                return_value=Response(),
            ), self.assertRaisesRegex(update_flogravity.UpdateError, "download URL"):
                update_flogravity.download_artifact(
                    update_flogravity.expected_download_url("4.12.0"),
                    "4.12.0",
                    100,
                    destination,
                )

    def test_redirect_handler_rejects_before_creating_next_request(self):
        handler = update_flogravity.ValidatedRedirectHandler(
            lambda url: update_flogravity.validate_download_url(url, "4.12.0")
        )
        with mock.patch.object(
            update_flogravity.HTTPRedirectHandler,
            "redirect_request",
        ) as parent_redirect:
            with self.assertRaisesRegex(update_flogravity.UpdateError, "download URL"):
                handler.redirect_request(
                    mock.sentinel.request,
                    mock.sentinel.response,
                    302,
                    "Found",
                    {},
                    "https://example.com/FloGravity-4.12.0.dmg",
                )
            parent_redirect.assert_not_called()

    def test_update_leaves_current_source_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(directory)
            before = path.read_bytes()
            fetch, parse, download, verify, inspect = self.mocked_upstream()

            with fetch, parse, download, verify, inspect:
                changed = update_flogravity.update(path)

            self.assertFalse(changed)
            self.assertEqual(path.read_bytes(), before)

    def test_update_rejects_same_version_artifact_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(directory)
            fetch, parse, download, verify, inspect = self.mocked_upstream(
                digest="cd" * 32
            )

            with fetch, parse, download, verify, inspect, self.assertRaisesRegex(
                update_flogravity.UpdateError,
                "changed; manual review",
            ):
                update_flogravity.update(path)

    def test_update_rejects_downgrade_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(
                directory,
                self.source(
                    version="4.13.0",
                    bundleVersion="158",
                    url=update_flogravity.expected_download_url("4.13.0"),
                ),
            )
            fetch, parse, download, verify, inspect = self.mocked_upstream()

            with fetch, parse, download as download_mock, verify as verify_mock, inspect as inspect_mock:
                with self.assertRaisesRegex(
                    update_flogravity.UpdateError,
                    "newer than upstream",
                ):
                    update_flogravity.update(path)
                download_mock.assert_not_called()
                verify_mock.assert_not_called()
                inspect_mock.assert_not_called()

    def test_update_writes_newer_version_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(directory)
            latest = self.release(
                version="4.13.0",
                bundleVersion="158",
                url=update_flogravity.expected_download_url("4.13.0"),
                size=200,
            )
            fetch, parse, download, verify, inspect = self.mocked_upstream(release=latest)

            with fetch, parse, download, verify, inspect:
                changed = update_flogravity.update(path)

            self.assertTrue(changed)
            self.assertEqual(
                json.loads(path.read_text()),
                self.source(
                    version="4.13.0",
                    bundleVersion="158",
                    url=update_flogravity.expected_download_url("4.13.0"),
                    size=200,
                ),
            )

    def test_check_reports_newer_version_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_source(directory)
            before = path.read_bytes()
            latest = self.release(
                version="4.13.0",
                bundleVersion="158",
                url=update_flogravity.expected_download_url("4.13.0"),
            )
            fetch, parse, download, verify, inspect = self.mocked_upstream(release=latest)

            with fetch, parse, download, verify, inspect:
                changed = update_flogravity.update(path, check=True)

            self.assertTrue(changed)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
