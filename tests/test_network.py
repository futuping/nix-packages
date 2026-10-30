import errno
import hashlib
from http.client import IncompleteRead, RemoteDisconnected
import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from scripts import network, update_ego_lite, update_shardx_launcher, update_wordcraft_cli


class Response(io.BytesIO):
    def __init__(self, data, url, *, interrupt=False, headers=None):
        super().__init__(data)
        self.url = url
        self.headers = headers or {}
        self.interrupt = interrupt
        self.read_once = False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        if self.interrupt and self.read_once:
            raise ConnectionResetError(errno.ECONNRESET, "connection reset")
        self.read_once = True
        return super().read(size)


class NetworkRetryTests(unittest.TestCase):
    def setUp(self):
        self.sleep = self.enter_patch(mock.patch.object(network.time, "sleep"))
        self.enter_patch(mock.patch.object(network.sys, "stderr", io.StringIO()))

    def enter_patch(self, patch):
        value = patch.start()
        self.addCleanup(patch.stop)
        return value

    def assert_retried(self, error):
        operation = mock.Mock(side_effect=[error, "success"])
        self.sleep.reset_mock()
        self.assertEqual(network.with_network_retries(operation, "test request"), "success")
        self.assertEqual(operation.call_count, 2)
        self.sleep.assert_called_once_with(1)

    def assert_not_retried(self, error):
        operation = mock.Mock(side_effect=error)
        self.sleep.reset_mock()
        with self.assertRaises(type(error)) as caught:
            network.with_network_retries(operation, "test request")
        self.assertIs(caught.exception, error)
        operation.assert_called_once_with()
        self.sleep.assert_not_called()

    def http_error(self, url, status):
        error = HTTPError(url, status, "unavailable", {}, io.BytesIO())
        self.addCleanup(error.close)
        return error

    def test_retries_only_reviewed_transient_http_statuses(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                error = self.http_error("https://example.test", status)
                self.assert_retried(error)
                self.assertTrue(error.fp.closed)
        for status in (400, 401, 403, 404, 409, 422, 501):
            with self.subTest(status=status):
                self.assert_not_retried(self.http_error("https://example.test", status))

    def test_retries_timeouts_resets_incomplete_reads_and_temporary_dns(self):
        for error in (
            TimeoutError("timed out"),
            URLError(TimeoutError("timed out")),
            ConnectionResetError(errno.ECONNRESET, "reset"),
            ConnectionAbortedError(errno.ECONNABORTED, "aborted"),
            OSError(errno.ENETRESET, "network reset"),
            IncompleteRead(b"partial", 100),
            RemoteDisconnected("remote disconnected"),
            socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure"),
            URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")),
        ):
            with self.subTest(error=error):
                self.assert_retried(error)

    def test_does_not_retry_tls_permanent_dns_or_filesystem_errors(self):
        for error in (
            ssl.SSLCertVerificationError("invalid certificate"),
            ssl.SSLError("TLS protocol failure"),
            URLError(ssl.SSLCertVerificationError("invalid certificate")),
            socket.gaierror(socket.EAI_NONAME, "unknown host"),
            PermissionError(errno.EACCES, "permission denied"),
            FileNotFoundError(errno.ENOENT, "missing file"),
            OSError(errno.ENOSPC, "disk full"),
            URLError("unclassified error"),
        ):
            with self.subTest(error=error):
                self.assert_not_retried(error)

    def test_does_not_retry_validation_errors_or_interrupts(self):
        for error in (
            ValueError("invalid metadata"),
            json.JSONDecodeError("invalid JSON", "x", 0),
            update_ego_lite.UpdateError("hash mismatch"),
            update_shardx_launcher.UpdateError("invalid signature"),
            update_wordcraft_cli.UpdateError("unexpected archive entry"),
            KeyboardInterrupt(),
        ):
            with self.subTest(error=error):
                self.assert_not_retried(error)

    def test_exhaustion_preserves_failure_after_three_attempts(self):
        error = TimeoutError("still unavailable")
        operation = mock.Mock(side_effect=error)
        with self.assertRaises(TimeoutError) as caught:
            network.with_network_retries(operation, "test request")
        self.assertIs(caught.exception, error)
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])

    def download_cases(self):
        return (
            (update_ego_lite, "urlopen", update_ego_lite.DOWNLOAD_URL),
            (update_shardx_launcher, "urlopen", update_shardx_launcher.download_url("2.1.1")),
            (update_wordcraft_cli, "open_reviewed", update_wordcraft_cli.DOWNLOAD_URL.format(version="0.12.0")),
        )

    def headers(self, data):
        return {
            "Content-Type": "application/x-apple-diskimage",
            "Content-Length": str(len(data)),
            "X-Amz-Meta-Sha256": hashlib.sha256(data).hexdigest(),
            "X-Amz-Version-Id": "test-version",
        }

    def test_every_downloader_restarts_file_and_hash_after_interrupted_body(self):
        payload = b"complete verified artifact"
        digest = hashlib.sha256(payload).hexdigest()
        for updater, opener, url in self.download_cases():
            with self.subTest(updater=updater.__name__), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "artifact"
                responses = [
                    Response(b"partial bytes that must not survive" * 10, url,
                             interrupt=True, headers=self.headers(payload)),
                    Response(payload, url, headers=self.headers(payload)),
                ]
                with mock.patch.object(updater, opener, side_effect=responses) as opened:
                    result = updater.download_artifact(url, destination)
                self.assertEqual(destination.read_bytes(), payload)
                self.assertEqual(result[0] if updater is update_ego_lite else result, digest)
                self.assertEqual(opened.call_count, 2)

    def test_every_downloader_fails_after_exhausted_transport_retries(self):
        for updater, opener, url in self.download_cases():
            with self.subTest(updater=updater.__name__), tempfile.TemporaryDirectory() as temporary:
                with mock.patch.object(updater, opener, side_effect=IncompleteRead(b"partial", 10)) as opened:
                    with self.assertRaisesRegex(updater.UpdateError, "unable to download"):
                        updater.download_artifact(url, Path(temporary) / "artifact")
                self.assertEqual(opened.call_count, 3)

    def test_every_downloader_rejects_unreviewed_redirect_without_retry(self):
        for updater, opener, url in self.download_cases():
            with self.subTest(updater=updater.__name__), tempfile.TemporaryDirectory() as temporary:
                response = Response(b"artifact", "https://example.test/asset", headers=self.headers(b"artifact"))
                with mock.patch.object(updater, opener, return_value=response) as opened:
                    with self.assertRaises(updater.UpdateError):
                        updater.download_artifact(url, Path(temporary) / "artifact")
                opened.assert_called_once()

    def test_ego_digest_mismatch_is_not_retried(self):
        payload = b"modified artifact"
        response = Response(payload, update_ego_lite.DOWNLOAD_URL,
                            headers={**self.headers(payload), "X-Amz-Meta-Sha256": "ab" * 32})
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(update_ego_lite, "urlopen", return_value=response) as opened:
                with self.assertRaisesRegex(update_ego_lite.UpdateError, "SHA-256 does not match"):
                    update_ego_lite.download_artifact(update_ego_lite.DOWNLOAD_URL, Path(temporary) / "artifact")
            opened.assert_called_once()

    def test_release_queries_retry_transport_failures(self):
        for updater, opener in ((update_shardx_launcher, "urlopen"), (update_wordcraft_cli, "open_reviewed")):
            with self.subTest(updater=updater.__name__):
                response = Response(b'{"tag_name": "v2.1.1"}', updater.API_URL)
                error = self.http_error(updater.API_URL, 503)
                with mock.patch.object(updater, opener, side_effect=[error, response]) as opened:
                    self.assertEqual(updater.github_release(), {"tag_name": "v2.1.1"})
                self.assertEqual(opened.call_count, 2)

    def test_release_queries_do_not_retry_invalid_json(self):
        for updater, opener in ((update_shardx_launcher, "urlopen"), (update_wordcraft_cli, "open_reviewed")):
            with self.subTest(updater=updater.__name__):
                with mock.patch.object(updater, opener, return_value=Response(b"invalid", updater.API_URL)) as opened:
                    with self.assertRaises(updater.UpdateError):
                        updater.github_release()
                opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
