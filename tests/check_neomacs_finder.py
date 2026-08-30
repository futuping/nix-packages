"""Opt-in native check: registers a disposable test app, never changes defaults.

Requires a logged-in macOS GUI session and permission to register this unique
fixture with LaunchServices. Run via the pinned neomacs-finder-check Nix app.
"""

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


def wait_for_records(directory, count, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = [json.loads(path.read_text()) for path in directory.glob("*.json")]
        if len(records) >= count:
            return records
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for %s Finder request(s)" % count)


def run_check(source, event_check):
    with tempfile.TemporaryDirectory(
        prefix="neomacs-finder-", dir=os.environ.get("NEOMACS_FINDER_TEST_ROOT", str(Path.cwd()))
    ) as temporary:
        root = Path(temporary)
        application = root / "Neomacs Finder Test.app"
        shutil.copytree(source, application)
        application.chmod(application.stat().st_mode | 0o200)
        for path in application.rglob("*"):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o200)
        info = application / "Contents/Info.plist"
        metadata = plistlib.loads(info.read_bytes())
        metadata["CFBundleIdentifier"] = "org.neomacs.nix.finder-test." + uuid.uuid4().hex
        executable = "nmf-" + uuid.uuid4().hex[:10]
        (application / "Contents/MacOS" / metadata["CFBundleExecutable"]).rename(
            application / "Contents/MacOS" / executable
        )
        metadata["CFBundleExecutable"] = executable
        metadata["CFBundleName"] = "Neomacs Finder Test"
        metadata["CFBundleDisplayName"] = "Neomacs Finder Test"
        info.write_bytes(plistlib.dumps(metadata))
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(application)],
            check=True,
        )
        records = root / "records"
        records.mkdir()
        environment = {"NEOMACS_FINDER_TEST_DIRECTORY": str(records)}
        for name in (
            "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "TMPDIR", "CFFIXED_USER_HOME"
        ):
            path = root / name.lower()
            path.mkdir()
            environment[name] = str(path)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        # Query/register in the real user's LaunchServices registry. Only the
        # app under test gets an isolated Core Foundation home directory.
        query_environment = dict(os.environ, **environment)
        query_environment.pop("CFFIXED_USER_HOME", None)
        open_command = ["/usr/bin/open", "-g", "-a", str(application)]
        for name, value in environment.items():
            open_command.extend(["--env", name + "=" + value])

        first_files = [root / "space in name.nix", root / "中文.md", root / "-leading.el"]
        later_file = root / "later%20file.toml"
        for path in first_files + [later_file]:
            path.write_text("fixture\n", encoding="utf-8")
        waiter = None
        try:
            # Use the public registration API so a failure has an OSStatus,
            # rather than silently accepting a command-line tool's exit code.
            subprocess.run(
                [str(event_check), "--register", str(application)],
                env=query_environment, check=True, timeout=10,
            )
            waiter = subprocess.Popen(open_command + ["-W"] + [str(path) for path in first_files])
            first = wait_for_records(records, 1)
            assert len(first) == 1, "Cold launch created an extra empty session"
            assert first[0]["arguments"][0] == "--", first
            assert sorted(first[0]["arguments"][1:]) == sorted(str(path) for path in first_files), first

            for path in first_files[:2]:
                subprocess.run(
                    [str(event_check), "--can-open", str(application), str(path)],
                    env=query_environment, check=True, timeout=10,
                )
            print("PASS: .nix and .md appear in macOS Open With candidates")

            subprocess.run(
                ["/usr/bin/open", "-g", "-a", str(application), str(later_file)],
                check=True, timeout=10,
            )
            both = wait_for_records(records, 2)
            assert len(both) == 2, "Unexpected extra session"
            assert len({record["parent"] for record in both}) == 1, "Warm open started a second broker"
            assert ["--", str(later_file)] in [record["arguments"] for record in both], both

            # All fake sessions exit normally; the event receiver must exit too.
            (records / "stop").touch()
            assert waiter.wait(timeout=10) == 0
            print("PASS: cold and warm Finder opens, multi-file argv, Unicode, spaces, %, '-' and broker exit")

            # Also check an ordinary app launch creates exactly one empty session.
            for path in records.glob("*.json"):
                path.unlink()
            (records / "stop").unlink()
            waiter = subprocess.Popen(open_command + ["-W"])
            empty = wait_for_records(records, 1)
            assert len(empty) == 1 and empty[0]["arguments"] == [], empty
            (records / "stop").touch()
            assert waiter.wait(timeout=10) == 0
            print("PASS: ordinary app launch and clean exit")
        finally:
            (records / "stop").touch()
            if waiter is not None:
                try:
                    waiter.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # This UUID-named executable exists only in this test app.
                    # Never target the installed Neomacs or a shared process name.
                    subprocess.run(["/usr/bin/killall", "-TERM", executable], check=False, timeout=5)
                    waiter.wait(timeout=5)
            subprocess.run([LSREGISTER, "-u", str(application)], check=True, timeout=10)


if __name__ == "__main__":
    run_check(Path(sys.argv[1]), Path(sys.argv[2]))
