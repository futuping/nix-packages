import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from scripts import update_wordcraft_cli as updater


class Response(io.BytesIO):
    def __init__(self, payload=b"{}", url=updater.API_URL):
        super().__init__(payload)
        self.url = url

    def geturl(self):
        return self.url


class UpdateWordCraftCliTests(unittest.TestCase):
    def release(self, version="0.8.0", **overrides):
        release = {
            "tag_name": f"v{version}",
            "draft": False,
            "prerelease": False,
            "assets": [{
                "name": updater.ASSET_NAME.format(version=version),
                "browser_download_url": updater.DOWNLOAD_URL.format(version=version),
                "size": 100,
                "digest": "sha256:" + "ab" * 32,
            }],
        }
        release.update(overrides)
        return release

    def source(self, version="0.8.0", digest="ab" * 32):
        return {
            "version": version,
            "url": updater.DOWNLOAD_URL.format(version=version),
            "hash": updater.sri_sha256(digest),
        }

    def write_source(self, destination, **arguments):
        destination.write_text(json.dumps(self.source(**arguments)))

    def universal_binary(self):
        return (
            struct.pack(">II", 0xCAFEBABE, 2)
            + struct.pack(">IIIII", 0x01000007, 3, 48, 4, 0)
            + struct.pack(">IIIII", 0x0100000C, 0, 52, 4, 0)
            + b"\xcf\xfa\xed\xfe" * 2
        )

    def archive(self, destination, *, binary=None, mode=None, extra_entries=()):
        directory = "wordcraft-cli-0.8.0-macos-universal"
        with zipfile.ZipFile(destination, "w") as archive:
            executable = zipfile.ZipInfo(directory + "/wordcraft-cli")
            executable.external_attr = (mode or (stat.S_IFREG | 0o755)) << 16
            archive.writestr(executable, self.universal_binary() if binary is None else binary)
            for name in ("README.md", "LICENSE-MIT", "LICENSE-APACHE", "OFL-NotoSans.txt"):
                document = zipfile.ZipInfo(directory + "/" + name)
                document.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(document, b"notice")
            for name in extra_entries:
                if isinstance(name, zipfile.ZipInfo):
                    entry = name
                else:
                    entry = zipfile.ZipInfo(name)
                    entry.external_attr = (stat.S_IFREG | 0o644) << 16
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(entry, b"other")

    def test_exact_universal_asset(self):
        release = self.release()
        release["assets"].append({"name": "wordcraft-0.8.0-macos-universal.dmg"})
        self.assertEqual(
            updater.release_metadata(release),
            ("0.8.0", updater.DOWNLOAD_URL.format(version="0.8.0"), "ab" * 32),
        )

    def test_rejects_unstable_or_malformed_release(self):
        releases = [
            None,
            self.release(draft=True),
            self.release(prerelease=True),
            self.release(tag_name="v0.8.0-rc1"),
            self.release(tag_name="0.8.0"),
            self.release(tag_name="v00.8.0"),
            self.release(assets={}),
            self.release(assets=[None]),
        ]
        for release in releases:
            with self.subTest(release=release), self.assertRaises(updater.UpdateError):
                updater.release_metadata(release)

    def test_rejects_missing_or_duplicate_exact_asset(self):
        for count in (0, 2):
            release = self.release()
            release["assets"] *= count
            with self.subTest(count=count), self.assertRaisesRegex(updater.UpdateError, "exactly one"):
                updater.release_metadata(release)

    def test_rejects_wrong_asset_url_digest_or_size(self):
        mutations = [
            ("browser_download_url", "https://example.com/cli.zip"),
            ("browser_download_url", updater.DOWNLOAD_URL.format(version="0.7.0")),
            ("digest", "sha512:" + "ab" * 64),
            ("digest", 123),
            ("size", 0),
            ("size", True),
            ("size", updater.MAX_DOWNLOAD_BYTES + 1),
        ]
        for key, value in mutations:
            release = self.release()
            release["assets"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(updater.UpdateError):
                updater.release_metadata(release)

    def test_optional_digest_is_supported(self):
        release = self.release()
        del release["assets"][0]["digest"]
        self.assertIsNone(updater.release_metadata(release)[2])

    def test_api_is_bounded_and_checks_final_host(self):
        responses = [
            Response(b" " * (updater.MAX_API_BYTES + 1)),
            Response(url="http://api.github.com/release"),
            Response(url="https://example.com/release"),
            Response(b"not json"),
        ]
        for response in responses:
            with self.subTest(url=response.url), patch.object(
                updater, "open_reviewed", return_value=response
            ), self.assertRaises(updater.UpdateError):
                updater.github_release()

    def test_api_parses_official_response(self):
        release = self.release()
        with patch.object(updater, "open_reviewed", return_value=Response(json.dumps(release).encode())):
            self.assertEqual(updater.github_release(), release)

    def test_redirect_handler_rejects_every_unreviewed_hop(self):
        handler = updater.ReviewedRedirectHandler(updater.ALLOWED_DOWNLOAD_HOSTS, "download")
        for url in (
            "https://example.com/asset",
            "http://release-assets.githubusercontent.com/asset",
            "https://user:password@github.com/asset",
            "https://github.com:444/asset",
            "https://github.com:invalid/asset",
        ):
            with self.subTest(url=url), self.assertRaises(updater.UpdateError):
                handler.redirect_request(None, None, 302, "Found", {}, url)

    def test_download_computes_complete_digest(self):
        payload = b"downloaded bytes"
        url = updater.DOWNLOAD_URL.format(version="0.8.0")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "asset.zip"
            with patch.object(updater, "open_reviewed", return_value=Response(payload, url)):
                digest = updater.download_artifact(url, destination)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            self.assertEqual(destination.read_bytes(), payload)

    def test_download_rejects_empty_oversized_or_unreviewed_response(self):
        url = updater.DOWNLOAD_URL.format(version="0.8.0")
        responses = [Response(b"", url), Response(b"too many bytes", url), Response(b"x", "https://example.com/a")]
        with tempfile.TemporaryDirectory() as temporary:
            for response in responses:
                with self.subTest(response=response), patch.object(
                    updater, "open_reviewed", return_value=response
                ), patch.object(updater, "MAX_DOWNLOAD_BYTES", 5), self.assertRaises(updater.UpdateError):
                    updater.download_artifact(url, Path(temporary) / "asset.zip")

    def test_archive_preserves_expected_documents_and_universal_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "asset.zip"
            self.archive(archive)
            with patch.object(updater.sys, "platform", "linux"):
                updater.inspect_archive(archive, "0.8.0")

    def test_archive_accepts_reviewed_legal_notices(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "asset.zip"
            self.archive(
                archive,
                extra_entries=(
                    "wordcraft-cli-0.8.0-macos-universal/ATTRIBUTION.md",
                    "wordcraft-cli-0.8.0-macos-universal/NOTICE",
                ),
            )
            with patch.object(updater.sys, "platform", "linux"):
                updater.inspect_archive(archive, "0.8.0")

    def test_archive_rejects_unreviewed_notice_paths(self):
        directory = "wordcraft-cli-0.8.0-macos-universal/"
        for name in (
            "ATTRIBUTION.md.exe",
            "ATTRIBUTION.md/../outside",
            "notices/ATTRIBUTION.md",
            "../ATTRIBUTION.md",
            "NOTICE.exe",
            "notices/NOTICE",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "asset.zip"
                self.archive(archive, extra_entries=(directory + name,))
                with self.assertRaisesRegex(updater.UpdateError, "unexpected archive entry"):
                    updater.inspect_archive(archive, "0.8.0")

    def test_archive_rejects_notice_symlinks(self):
        for name in ("ATTRIBUTION.md", "NOTICE"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "asset.zip"
                notice = zipfile.ZipInfo("wordcraft-cli-0.8.0-macos-universal/" + name)
                notice.external_attr = (stat.S_IFLNK | 0o777) << 16
                self.archive(archive, extra_entries=(notice,))
                with self.assertRaisesRegex(updater.UpdateError, "unexpected archive entry"):
                    updater.inspect_archive(archive, "0.8.0")

    def test_archive_rejects_duplicate_notices(self):
        for name in ("ATTRIBUTION.md", "NOTICE"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "asset.zip"
                entry = "wordcraft-cli-0.8.0-macos-universal/" + name
                self.archive(archive, extra_entries=(entry, entry))
                with self.assertRaisesRegex(updater.UpdateError, "one unique"):
                    updater.inspect_archive(archive, "0.8.0")

    def test_archive_rejects_traversal_duplicate_and_unknown_entries(self):
        for entry in (
            "../outside", "/outside", "wordcraft-cli-0.8.0-macos-universal/../outside",
            "wordcraft-cli-0.8.0-macos-universal/unknown",
            "wordcraft-cli-0.8.0-macos-universal/wordcraft-cli",
        ):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "asset.zip"
                self.archive(archive, extra_entries=(entry,))
                with self.assertRaises(updater.UpdateError):
                    updater.inspect_archive(archive, "0.8.0")

    def test_archive_rejects_symlink_nonexecutable_and_wrong_binary(self):
        cases = [
            {"mode": stat.S_IFLNK | 0o755},
            {"mode": stat.S_IFREG | 0o644},
            {"binary": b"not a Mach-O"},
            {"binary": self.universal_binary()[:4] + struct.pack(">I", 3) + self.universal_binary()[8:]},
            {"binary": self.universal_binary()[:28] + b"\0" * 4 + self.universal_binary()[32:]},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "asset.zip"
                self.archive(archive, **arguments)
                with self.assertRaises(updater.UpdateError):
                    updater.inspect_archive(archive, "0.8.0")

    def test_archive_rejects_unpacked_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "asset.zip"
            self.archive(archive)
            with patch.object(updater, "MAX_UNPACKED_BYTES", 10), self.assertRaisesRegex(updater.UpdateError, "unpacked"):
                updater.inspect_archive(archive, "0.8.0")

    def test_archive_rejects_invalid_signature_on_macos(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "asset.zip"
            self.archive(archive)
            with patch.object(updater.sys, "platform", "darwin"), patch.object(
                updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "invalid seal")
            ), self.assertRaisesRegex(updater.UpdateError, "signature verification failed"):
                updater.inspect_archive(archive, "0.8.0")

    def test_archive_checks_every_architecture_and_pinned_identity_on_macos(self):
        identity = f"TeamIdentifier={updater.TEAM_ID}\nAuthority={updater.SIGNING_AUTHORITY}\n"
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "asset.zip"
            self.archive(archive)
            with patch.object(updater.sys, "platform", "darwin"), patch.object(
                updater.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", identity),
                ]
            ) as codesign:
                updater.inspect_archive(archive, "0.8.0")
            self.assertIn("--all-architectures", codesign.call_args_list[0].args[0])
            self.assertEqual(codesign.call_count, 2)

    def test_archive_rejects_changed_or_missing_signing_identity_on_macos(self):
        identities = [
            f"TeamIdentifier=OTHERTEAM0\nAuthority={updater.SIGNING_AUTHORITY}\n",
            f"TeamIdentifier={updater.TEAM_ID}\nAuthority=Unknown Developer\n",
            f"TeamIdentifier={updater.TEAM_ID}\n",
            "",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "asset.zip"
            self.archive(archive)
            for identity in identities:
                with self.subTest(identity=identity), patch.object(updater.sys, "platform", "darwin"), patch.object(
                    updater.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, "", ""),
                        subprocess.CompletedProcess([], 0, "", identity),
                    ]
                ), self.assertRaisesRegex(updater.UpdateError, "unexpected upstream signing identity"):
                    updater.inspect_archive(archive, "0.8.0")

    def test_source_requires_exact_fields_url_and_full_canonical_hash(self):
        sources = [[], {}, self.source() | {"extra": "field"}]
        for key, value in (
            ("version", "0.8.0-rc1"), ("url", "https://example.com/a"),
            ("hash", "sha256-too-short"), ("hash", "sha256-" + "A" * 42 + "B="),
            ("version", 123),
        ):
            source = self.source()
            source[key] = value
            sources.append(source)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.json"
            for source in sources:
                destination.write_text(json.dumps(source))
                with self.subTest(source=source), self.assertRaises(updater.UpdateError):
                    updater.read_source(destination)

    def test_sri_uses_all_256_bits(self):
        digest = "931f2277a357e7d77266ddceea565450b102ca7bf9e0634821162f7bab616dbe"
        self.assertEqual(base64.b64decode(updater.sri_sha256(digest)[7:]).hex(), digest)

    def test_update_rejects_downgrade_before_downloading(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.json"
            self.write_source(destination, version="0.9.0")
            with patch.object(updater, "github_release", return_value=self.release()), patch.object(
                updater, "download_artifact"
            ) as download, self.assertRaisesRegex(updater.UpdateError, "newer than upstream"):
                updater.update(destination)
            download.assert_not_called()

    def test_update_rejects_digest_mismatch_before_archive_inspection(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.json"
            self.write_source(destination)
            with patch.object(updater, "github_release", return_value=self.release()), patch.object(
                updater, "download_artifact", return_value="cd" * 32
            ), patch.object(updater, "inspect_archive") as inspect, self.assertRaisesRegex(updater.UpdateError, "does not match"):
                updater.update(destination)
            inspect.assert_not_called()

    def test_update_rejects_same_version_mutation_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.json"
            self.write_source(destination, digest="aa" * 32)
            before = destination.read_bytes()
            with patch.object(updater, "github_release", return_value=self.release()), patch.object(
                updater, "download_artifact", return_value="ab" * 32
            ), patch.object(updater, "inspect_archive"), self.assertRaisesRegex(updater.UpdateError, "changed"):
                updater.update(destination)
            self.assertEqual(destination.read_bytes(), before)

    def test_update_current_and_check_modes_never_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.json"
            for version, check, expected_changed in (("0.8.0", False, False), ("0.7.0", True, True)):
                self.write_source(destination, version=version)
                before = destination.read_bytes()
                with patch.object(updater, "github_release", return_value=self.release()), patch.object(
                    updater, "download_artifact", return_value="ab" * 32
                ), patch.object(updater, "inspect_archive"), redirect_stdout(io.StringIO()):
                    self.assertEqual(updater.update(destination, check=check), expected_changed)
                self.assertEqual(destination.read_bytes(), before)

    def test_update_writes_only_validated_new_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.json"
            self.write_source(destination, version="0.7.0")
            with patch.object(updater, "github_release", return_value=self.release()), patch.object(
                updater, "download_artifact", return_value="ab" * 32
            ), patch.object(updater, "inspect_archive") as inspect, redirect_stdout(io.StringIO()):
                self.assertTrue(updater.update(destination))
            inspect.assert_called_once()
            self.assertEqual(json.loads(destination.read_text()), self.source())
            self.assertEqual(list(Path(temporary).iterdir()), [destination])


if __name__ == "__main__":
    unittest.main()
