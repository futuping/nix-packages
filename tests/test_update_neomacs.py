import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_neomacs.sh"
BASH = os.environ.get("NIX_PACKAGES_BASH", "/bin/bash")


class UpdateNeomacsTests(unittest.TestCase):
    original = b'{"original": "including uncommitted lock state"}\n'
    candidate = b'{"candidate": "main"}\n'

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="neomacs updater ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repository = self.root / "checkout with spaces"
        self.repository.mkdir()
        (self.repository / "scripts").mkdir()
        (self.repository / "scripts" / "update_neomacs.sh").write_text("sentinel")
        (self.repository / "flake.nix").write_text("{}")
        self.lock = self.repository / "flake.lock"
        self.lock.write_bytes(self.original)
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.log = self.root / "commands.jsonl"
        self.nix = self.root / "mock nix"
        self.nix.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                """\
                import json
                import os
                from pathlib import Path
                import sys

                arguments = sys.argv[1:]
                with open(os.environ['MOCK_NIX_LOG'], 'a') as log:
                    log.write(json.dumps(arguments) + '\\n')
                mode = os.environ.get('MOCK_NIX_MODE', 'success')
                if arguments == ['flake', 'update', 'neomacs']:
                    if mode != 'unchanged':
                        Path('flake.lock').write_bytes(b'{"candidate": "main"}\\n')
                    if mode == 'update-failure':
                        sys.exit(31)
                elif arguments == ['run', '--no-update-lock-file', '.#neomacs-package-check']:
                    if mode == 'check-failure':
                        sys.exit(32)
                else:
                    sys.exit(99)
                """
            )
        )
        self.nix.chmod(0o755)
        self.environment = {
            **os.environ,
            "NIX_PACKAGES_NIX": str(self.nix),
            "NIX_PACKAGES_REPOSITORY": str(self.repository),
            "MOCK_NIX_LOG": str(self.log),
            "TMPDIR": str(self.tmp),
        }

    def run_updater(self, *arguments, mode="success"):
        return subprocess.run(
            [BASH, str(SCRIPT), *arguments],
            cwd=self.root,
            env={**self.environment, "MOCK_NIX_MODE": mode},
            capture_output=True,
            text=True,
            timeout=10,
        )

    def commands(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_temporary_files_removed(self):
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_success_keeps_candidate_after_native_check(self):
        result = self.run_updater()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lock.read_bytes(), self.candidate)
        self.assertEqual(
            self.commands(),
            [
                ["flake", "update", "neomacs"],
                ["run", "--no-update-lock-file", ".#neomacs-package-check"],
            ],
        )
        self.assert_temporary_files_removed()

    def test_update_failure_restores_original_bytes(self):
        result = self.run_updater(mode="update-failure")
        self.assertEqual(result.returncode, 31, result.stderr)
        self.assertEqual(self.lock.read_bytes(), self.original)
        self.assertEqual(self.commands(), [["flake", "update", "neomacs"]])
        self.assert_temporary_files_removed()

    def test_package_failure_restores_original_bytes(self):
        result = self.run_updater(mode="check-failure")
        self.assertEqual(result.returncode, 32, result.stderr)
        self.assertEqual(self.lock.read_bytes(), self.original)
        self.assert_temporary_files_removed()

    def test_unchanged_lock_skips_package_build(self):
        result = self.run_updater(mode="unchanged")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lock.read_bytes(), self.original)
        self.assertEqual(self.commands(), [["flake", "update", "neomacs"]])
        self.assert_temporary_files_removed()

    def test_wrong_repository_fails_before_running_nix(self):
        self.environment["NIX_PACKAGES_REPOSITORY"] = str(self.root)
        result = self.run_updater()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.lock.read_bytes(), self.original)
        self.assertEqual(self.commands(), [])

    def test_symlink_lock_is_rejected_without_changing_target(self):
        target = self.repository / "original.lock"
        self.lock.rename(target)
        self.lock.symlink_to(target)
        result = self.run_updater()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(target.read_bytes(), self.original)
        self.assertEqual(self.commands(), [])

    def test_unknown_arguments_do_not_change_lock(self):
        result = self.run_updater("--unknown")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.lock.read_bytes(), self.original)
        self.assertEqual(self.commands(), [])

    def test_help_does_not_require_a_checkout_or_run_nix(self):
        self.environment["NIX_PACKAGES_REPOSITORY"] = str(self.root / "absent")
        result = self.run_updater("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands(), [])


if __name__ == "__main__":
    unittest.main()
