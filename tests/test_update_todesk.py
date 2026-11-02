from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_todesk as updater


class Response(io.BytesIO):
    def __init__(self, payload=b"{}", url=updater.API_URL, content_type="application/json; charset=utf-8"):
        super().__init__(payload)
        self.url = url
        self.headers = {"Content-Type": content_type}

    def geturl(self):
        return self.url


class UpdateToDeskTests(unittest.TestCase):
    def metadata(self, version="5.2.3.0", **overrides):
        fields = {
            "mac_version": version,
            "mac_link": updater.DOWNLOAD_URL.format(version=version),
            "mac_link_new": updater.DOWNLOAD_URL.format(version=version),
            "new_percent": "100",
            "mac_version_gray": "5.3.1.0",
            "mac_link_gray": updater.DOWNLOAD_URL.format(version="5.3.1.0"),
            "mac_gray_percent": "10",
            "mac_downloader_link": "https://dl.todesk.com/official/ToDesk_Installer.dmg",
        }
        fields.update(overrides)
        return {
            "code": 200000,
            "data": {"list": [
                {"id": index, "type": 1, "name": name, "value": value}
                for index, (name, value) in enumerate(fields.items(), 1)
            ]},
        }

    def source(self, version="5.2.3.0", artifact=b"xar!example package"):
        return {
            "version": version,
            "url": updater.DOWNLOAD_URL.format(version=version),
            "hash": updater.sri_sha256(hashlib.sha256(artifact).digest()),
        }

    def write_source(self, path, **arguments):
        path.write_text(json.dumps(self.source(**arguments)) + "\n")

    def installer_signature(self):
        return (
            'Package "ToDesk.pkg":\n'
            "   Status: signed by a developer certificate issued by Apple for distribution\n"
            "   Notarization: trusted by the Apple notary service\n"
            "   Certificate Chain:\n"
            f"    1. {updater.INSTALLER_AUTHORITY}\n"
            "    2. Developer ID Certification Authority\n"
        )

    def application_signature(self):
        return (
            f"Authority={updater.APPLICATION_AUTHORITY}\n"
            "Authority=Developer ID Certification Authority\n"
            f"TeamIdentifier={updater.TEAM_ID}\n"
        )

    def bundle(self, directory, **overrides):
        application = Path(directory) / "ToDesk.app"
        contents = application / "Contents"
        contents.mkdir(parents=True)
        metadata = {
            "CFBundleIdentifier": updater.BUNDLE_ID,
            "CFBundleShortVersionString": "5.2.3.0",
            "CFBundleVersion": "2943",
            "CFBundleExecutable": updater.EXECUTABLE_NAME,
        }
        metadata.update(overrides)
        (contents / "Info.plist").write_bytes(plistlib.dumps(metadata))
        return application

    def test_official_stable_is_distinct_from_gray_and_downloader(self):
        self.assertEqual(
            updater.stable_metadata(self.metadata()),
            ("5.2.3.0", "https://dl.todesk.com/macos/ToDesk_5.2.3.0.pkg"),
        )

    def test_new_link_is_selected_only_after_full_rollout(self):
        for percent in ("-1", "0", "10", "99"):
            with self.subTest(percent=percent):
                payload = self.metadata(new_percent=percent, mac_link_new="https://example.com/unreleased.pkg")
                self.assertEqual(updater.stable_metadata(payload)[0], "5.2.3.0")
        # The primary link may lag when the official *_new rollout reaches 100%.
        payload = self.metadata(mac_link="https://dl.todesk.com/macos/ToDesk_5.2.2.0.pkg")
        self.assertEqual(updater.stable_metadata(payload)[1], updater.DOWNLOAD_URL.format(version="5.2.3.0"))

    def test_rejects_invalid_or_ambiguous_stable_metadata(self):
        payloads = [None, {}, {"code": 500000}, {"code": 200000, "data": {"list": {}}}]
        for version in ("5.2.3", "v5.2.3.0", "5.2.3.0-beta", "05.2.3.0", "5.2.3.0\n", 123):
            payloads.append(self.metadata(mac_version=version))
        for percent in ("", "101", "-2", "1.5", "01", 100):
            payloads.append(self.metadata(new_percent=percent))
        missing = self.metadata()
        missing["data"]["list"] = missing["data"]["list"][1:]
        payloads.append(missing)
        duplicate = self.metadata()
        duplicate["data"]["list"].append(dict(duplicate["data"]["list"][0]))
        payloads.append(duplicate)
        wrong_type = self.metadata()
        wrong_type["data"]["list"][0]["type"] = True
        payloads.append(wrong_type)
        malformed = self.metadata()
        malformed["data"]["list"].append(None)
        payloads.append(malformed)
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(updater.UpdateError):
                updater.stable_metadata(payload)

    def test_requires_exact_versioned_official_pkg_url(self):
        for url in (
            "http://dl.todesk.com/macos/ToDesk_5.2.3.0.pkg",
            "https://example.com/macos/ToDesk_5.2.3.0.pkg",
            "https://dl.todesk.com/macos/ToDesk_5.3.1.0.pkg",
            "https://dl.todesk.com/macos/ToDesk_5.2.3.0.pkg?token=value",
            "https://dl.todesk.com/official/ToDesk_Installer.dmg",
            "https://user:password@dl.todesk.com/macos/ToDesk_5.2.3.0.pkg",
        ):
            with self.subTest(url=url), self.assertRaises(updater.UpdateError):
                updater.stable_metadata(self.metadata(mac_link_new=url))

    def test_fetches_bounded_official_json(self):
        metadata = self.metadata()
        with patch.object(updater, "open_metadata", return_value=Response(json.dumps(metadata).encode())):
            self.assertEqual(updater.official_metadata(), metadata)

    def test_rejects_api_html_oversize_invalid_json_and_changed_url(self):
        responses = [
            Response(b"challenge", content_type="text/html"),
            Response(b" " * (updater.MAX_API_BYTES + 1)),
            Response(b"not JSON"),
            Response(url="https://example.com/config"),
            Response(url="http://www.todesk.com/api/config/getConfig?type=1"),
        ]
        for response in responses:
            with self.subTest(response=response), patch.object(updater, "open_metadata", return_value=response), self.assertRaises(updater.UpdateError):
                updater.official_metadata()

    def test_rejects_redirect_before_contacting_unreviewed_destination(self):
        handler = updater.ReviewedRedirectHandler()
        for url in ("https://example.com/config", "http://www.todesk.com/api/config/getConfig?type=1", "https://www.todesk.com/other"):
            with self.subTest(url=url), self.assertRaises(updater.UpdateError):
                handler.redirect_request(None, None, 302, "Found", {}, url)

    def test_hashes_full_private_installer_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "download.pkg"
            snapshot = Path(temporary) / "snapshot.pkg"
            payload = b"xar!" + b"content" * 200000
            source.write_bytes(payload)
            digest = updater.copy_artifact(source, snapshot)
            self.assertEqual(digest, updater.sri_sha256(hashlib.sha256(payload).digest()))
            self.assertEqual(snapshot.read_bytes(), payload)

    def test_rejects_empty_html_and_oversized_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "download.pkg"
            for payload in (b"", b"<html>browser verification</html>", b"xar!" + b"x" * 50):
                source.write_bytes(payload)
                with self.subTest(payload=payload), patch.object(updater, "MAX_ARTIFACT_BYTES", 40), self.assertRaises(updater.UpdateError):
                    updater.copy_artifact(source, Path(temporary) / "snapshot.pkg")

    def test_bundle_checks_short_version_identity_signer_and_universal_architectures(self):
        with tempfile.TemporaryDirectory() as temporary:
            application = self.bundle(temporary)
            with patch.object(updater, "command_output", side_effect=["", self.application_signature(), "x86_64 arm64\n"]) as run:
                updater.inspect_bundle(application, "5.2.3.0")
            self.assertEqual(run.call_args_list[0].args[0][:5], ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--all-architectures"])
            self.assertEqual(run.call_args_list[-1].args[0][-1], str(application / "Contents/MacOS/ToDesk"))

    def test_bundle_rejects_wrong_version_id_or_executable_before_native_checks(self):
        for overrides in (
            {"CFBundleIdentifier": "example.other"},
            {"CFBundleShortVersionString": "5.3.1.0"},
            {"CFBundleExecutable": "../../outside"},
        ):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as temporary:
                application = self.bundle(temporary, **overrides)
                with patch.object(updater, "command_output") as run, self.assertRaises(updater.UpdateError):
                    updater.inspect_bundle(application, "5.2.3.0")
                run.assert_not_called()

    def test_bundle_rejects_unreviewed_signer_or_missing_arm64(self):
        for identity, architectures in (
            (self.application_signature().replace(updater.TEAM_ID, "OTHERTEAM1"), "x86_64 arm64"),
            (self.application_signature().replace("Hainan Youqu", "Other"), "x86_64 arm64"),
            (self.application_signature(), "x86_64"),
        ):
            with self.subTest(identity=identity, architectures=architectures), tempfile.TemporaryDirectory() as temporary:
                application = self.bundle(temporary)
                with patch.object(updater, "command_output", side_effect=["", identity, architectures]), self.assertRaises(updater.UpdateError):
                    updater.inspect_bundle(application, "5.2.3.0")

    def test_installer_only_checks_signature_and_expands_without_executing_scripts(self):
        calls = []

        def command(arguments, description):
            calls.append(arguments)
            if arguments[1] == "--check-signature":
                return self.installer_signature()
            self.assertEqual(arguments[:2], ["/usr/sbin/pkgutil", "--expand-full"])
            (Path(arguments[-1]) / updater.APPLICATION_PATH).mkdir(parents=True)
            return ""

        with patch.object(updater.sys, "platform", "darwin"), patch.object(updater, "command_output", side_effect=command), patch.object(updater, "inspect_bundle") as inspect:
            updater.inspect_installer(Path("/tmp/ToDesk.pkg"), "5.2.3.0")
        self.assertEqual(len(calls), 2)
        self.assertEqual(inspect.call_args.args[1], "5.2.3.0")
        self.assertTrue(str(inspect.call_args.args[0]).endswith(str(updater.APPLICATION_PATH)))

    def test_installer_rejects_untrusted_unsigned_or_changed_signer(self):
        for signature in (
            self.installer_signature().replace(updater.SIGNER, "Other (OTHERTEAM1)"),
            self.installer_signature().replace("signed by a developer certificate issued by Apple for distribution", "no signature"),
            self.installer_signature().replace("Notarization: trusted by the Apple notary service", "Notarization: not trusted"),
        ):
            with self.subTest(signature=signature), patch.object(updater.sys, "platform", "darwin"), patch.object(updater, "command_output", return_value=signature) as run, self.assertRaises(updater.UpdateError):
                updater.inspect_installer(Path("/tmp/ToDesk.pkg"), "5.2.3.0")
            self.assertEqual(run.call_count, 1)

    def test_installer_import_requires_native_macos(self):
        with patch.object(updater.sys, "platform", "linux"), patch.object(updater, "command_output") as run, self.assertRaises(updater.UpdateError):
            updater.inspect_installer(Path("/tmp/ToDesk.pkg"), "5.2.3.0")
        run.assert_not_called()

    def test_failed_or_timed_out_native_check_is_an_update_error(self):
        results = [
            subprocess.CompletedProcess(["false"], 1, "", "bad signature"),
            subprocess.TimeoutExpired(["pkgutil"], 180),
        ]
        for result in results:
            arguments = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with self.subTest(result=result), patch.object(updater.subprocess, "run", **arguments), self.assertRaises(updater.UpdateError):
                updater.command_output(["/usr/sbin/pkgutil"], "inspect package")

    def test_monitor_and_check_never_rewrite_source_or_inspect_an_installer(self):
        for check in (False, True):
            with self.subTest(check=check), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "source.json"
                self.write_source(path, version="5.2.2.0")
                before = path.read_bytes()
                with patch.object(updater, "official_metadata", return_value=self.metadata()), patch.object(updater, "inspect_installer") as inspect, redirect_stdout(io.StringIO()):
                    self.assertTrue(updater.update(path, check=check))
                self.assertEqual(path.read_bytes(), before)
                inspect.assert_not_called()

    def test_current_monitor_reports_metadata_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.json"
            self.write_source(path)
            output = io.StringIO()
            with patch.object(updater, "official_metadata", return_value=self.metadata()), redirect_stdout(output):
                self.assertFalse(updater.update(path))
            self.assertIn("hash was not reverified", output.getvalue())

    def test_rejects_downgrade_and_malformed_source_before_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.json"
            self.write_source(path, version="5.3.1.0")
            with patch.object(updater, "official_metadata", return_value=self.metadata()), patch.object(updater, "copy_artifact") as copy, self.assertRaisesRegex(updater.UpdateError, "newer than upstream"):
                updater.update(path, artifact=Path("unused.pkg"))
            copy.assert_not_called()
            path.write_text('{"version":"broken"}')
            with patch.object(updater, "official_metadata") as fetch, self.assertRaises(updater.UpdateError):
                updater.update(path)
            fetch.assert_not_called()

    def test_rejects_invalid_source_url_hash_and_noncanonical_sri(self):
        mutations = [
            ("url", "https://example.com/package.pkg"),
            ("hash", "sha256-invalid"),
            ("hash", "sha256-" + "A" * 42 + "B="),
            ("version", 123),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.json"
            for key, value in mutations:
                source = self.source()
                source[key] = value
                path.write_text(json.dumps(source))
                with self.subTest(key=key, value=value), self.assertRaises(updater.UpdateError):
                    updater.read_source(path)

    def test_import_same_version_preserves_source_and_rejects_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.json"
            artifact = Path(temporary) / "download.pkg"
            self.write_source(path)
            before = path.read_bytes()
            with patch.object(updater, "official_metadata", return_value=self.metadata()), patch.object(updater, "inspect_installer"), redirect_stdout(io.StringIO()):
                artifact.write_bytes(b"xar!example package")
                self.assertFalse(updater.update(path, artifact=artifact))
                artifact.write_bytes(b"xar!different package")
                with self.assertRaisesRegex(updater.UpdateError, "manual review"):
                    updater.update(path, artifact=artifact)
            self.assertEqual(path.read_bytes(), before)

    def test_new_artifact_is_pinned_only_after_successful_inspection(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.json"
            artifact = Path(temporary) / "download.pkg"
            self.write_source(path, version="5.2.2.0")
            before = path.read_bytes()
            artifact.write_bytes(b"xar!example package")
            with patch.object(updater, "official_metadata", return_value=self.metadata()), patch.object(updater, "inspect_installer", side_effect=updater.UpdateError("bad signature")), self.assertRaises(updater.UpdateError):
                updater.update(path, artifact=artifact)
            self.assertEqual(path.read_bytes(), before)
            with patch.object(updater, "official_metadata", return_value=self.metadata()), patch.object(updater, "inspect_installer"), redirect_stdout(io.StringIO()):
                self.assertTrue(updater.update(path, artifact=artifact, check=True))
                self.assertEqual(path.read_bytes(), before)
                self.assertTrue(updater.update(path, artifact=artifact))
            self.assertEqual(json.loads(path.read_text()), self.source())
            self.assertEqual(list(Path(temporary).glob(".source.json.*")), [])

    def test_monitor_exit_status_signals_required_manual_update(self):
        for changed, arguments, expected in (
            (False, [], 0), (True, [], 1), (True, ["--check"], 1),
            (True, ["--artifact", "download.pkg"], 0),
            (True, ["--artifact", "download.pkg", "--check"], 1),
        ):
            with self.subTest(arguments=arguments), patch.object(updater.sys, "argv", ["update_todesk", *arguments]), patch.object(updater, "update", return_value=changed):
                self.assertEqual(updater.main(), expected)


if __name__ == "__main__":
    unittest.main()
