import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "push_neomacs_cache.sh"
BASH = os.environ.get("NIX_PACKAGES_BASH", "/bin/bash")


class PushNeomacsCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="neomacs cache ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repository = self.root / "checkout with spaces"
        (self.repository / "scripts").mkdir(parents=True)
        (self.repository / "packages").mkdir()
        for name in (
            "flake.nix",
            "flake.lock",
            "scripts/push_neomacs_cache.sh",
            "packages/neomacs.nix",
        ):
            (self.repository / name).write_text("sentinel")
        self.log = self.root / "commands.jsonl"
        self.mock = self.root / "mock tool"
        self.mock.write_text(
            f"#!{sys.executable}\n" + textwrap.dedent(
                """\
                import json
                import os
                import sys

                arguments = sys.argv[1:]
                record = {"arguments": arguments}
                if arguments[0] == "push":
                    record["paths"] = sys.stdin.read().splitlines()
                with open(os.environ["MOCK_LOG"], "a") as log:
                    log.write(json.dumps(record) + "\\n")
                mode = os.environ.get("MOCK_MODE", "success")
                if arguments[0] == "build":
                    if mode == "build-failure":
                        print("/nix/store/mock-partial-result")
                        sys.exit(31)
                    print("/nix/store/mock-neomacs")
                    print("/nix/store/mock-dummy-source")
                elif mode == "push-failure":
                    sys.exit(32)
                """
            )
        )
        self.mock.chmod(0o755)
        self.environment = {
            **os.environ,
            "NIX_PACKAGES_NIX": str(self.mock),
            "NIX_PACKAGES_CACHIX": str(self.mock),
            "NIX_PACKAGES_REPOSITORY": str(self.repository),
            "CACHIX_AUTH_TOKEN": "test-only-cache-token",
            "MOCK_LOG": str(self.log),
        }

    def run_publisher(self, *arguments, mode="success"):
        return subprocess.run(
            [BASH, str(SCRIPT), *arguments],
            env={**self.environment, "MOCK_MODE": mode},
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def commands(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_only_runtime_and_ifd_source_are_published(self):
        result = self.run_publisher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.commands(),
            [
                {
                    "arguments": [
                        "build",
                        "--no-link",
                        "--no-update-lock-file",
                        "--print-out-paths",
                        ".#packages.aarch64-darwin.neomacs",
                        ".#packages.aarch64-darwin.neomacs.upstream.cargoArtifacts.src",
                    ]
                },
                {
                    "arguments": ["push", "utitsoga"],
                    "paths": [
                        "/nix/store/mock-neomacs",
                        "/nix/store/mock-dummy-source",
                    ],
                },
            ],
        )
        self.assertNotIn("test-only-cache-token", result.stdout + result.stderr)
        self.assertNotIn("test-only-cache-token", self.log.read_text())
        self.assertEqual((self.repository / "flake.lock").read_text(), "sentinel")
        self.assertFalse((self.repository / "result").exists())

    def test_build_failure_does_not_publish_partial_output(self):
        result = self.run_publisher(mode="build-failure")
        self.assertEqual(result.returncode, 31, result.stderr)
        self.assertEqual(len(self.commands()), 1)

    def test_upload_failure_is_reported(self):
        result = self.run_publisher(mode="push-failure")
        self.assertEqual(result.returncode, 32, result.stderr)

    def test_missing_token_fails_before_building(self):
        self.environment.pop("CACHIX_AUTH_TOKEN")
        result = self.run_publisher()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.commands(), [])

    def test_wrong_checkout_does_not_build_or_upload(self):
        self.environment["NIX_PACKAGES_REPOSITORY"] = str(self.root)
        result = self.run_publisher()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.commands(), [])

    def test_help_does_not_require_a_token_or_checkout(self):
        self.environment.pop("CACHIX_AUTH_TOKEN")
        self.environment["NIX_PACKAGES_REPOSITORY"] = str(self.root / "absent")
        result = self.run_publisher("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), [])

    def test_unknown_arguments_do_not_publish(self):
        result = self.run_publisher("--unknown")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.commands(), [])


if __name__ == "__main__":
    unittest.main()
