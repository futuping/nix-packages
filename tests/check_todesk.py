"""Inspect the packaged payload without starting ToDesk or modifying macOS."""

import plistlib
from pathlib import Path
import os
import subprocess
import sys
import tempfile

from scripts.manage_todesk import APP, Manager


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def check_staging(package):
    """Use real payload/signature checks, with all service operations simulated."""
    loaded = set()

    def runner(arguments):
        if arguments[0] == "/bin/ps":
            output = f"{os.getuid()} /System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow\n"
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if arguments[0] == "/bin/launchctl":
            action = arguments[1]
            if action == "print":
                return subprocess.CompletedProcess(arguments, 0 if arguments[2] in loaded else 113, "", "")
            if action == "bootstrap":
                with Path(arguments[3]).open("rb") as stream:
                    label = plistlib.load(stream)["Label"]
                loaded.add(arguments[2] + "/" + label)
            elif action == "bootout":
                loaded.discard(arguments[2])
            elif action != "enable":
                raise AssertionError(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        assert arguments[0] == "/usr/bin/codesign", arguments
        return subprocess.run(arguments, capture_output=True, text=True)

    with tempfile.TemporaryDirectory(prefix="todesk-package-check-") as directory:
        root = Path(directory)
        manager = Manager(root=root, runner=runner, uid=os.getuid(), gid=os.getgid())
        assert manager.apply(package) == "installed"
        assert len(loaded) == 2, loaded
        log = root / APP / "Contents/Logs/package-check.log"
        log.write_text("retain this runtime fixture\n")
        assert manager.apply(package) == "already current"
        assert manager.apply().startswith("disabled;")
        archives = list((root / "Library/Application Support/ToDesk").glob("nix-preserved-*/Logs/package-check.log"))
        assert len(archives) == 1 and archives[0].read_text() == "retain this runtime fixture\n"
        assert not loaded and not (root / APP).exists()
    print("ToDesk native payload staging, repeated activation and removal verified in isolation; launchctl simulated")


def check(package, version):
    package = Path(package)
    app = package / "Applications/ToDesk.app"
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    assert info["CFBundleIdentifier"] == "com.youqu.todesk.mac", info
    assert info["CFBundleShortVersionString"] == version, info
    assert info["CFBundleExecutable"] == "ToDesk", info
    bundles = [
        app,
        package / "Library/Application Support/ToDesk/ai/ZAgent.app",
        package / "Library/Audio/Plug-Ins/HAL/ToDeskOutputDriver.driver",
    ]
    for bundle in bundles:
        command("/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle))
        signature = command("/usr/bin/codesign", "-d", "--verbose=4", str(bundle))
        assert "TeamIdentifier=KM56KD59W4" in signature.splitlines(), signature
        assert (
            "Authority=Developer ID Application: Hainan Youqu Technology Co., Ltd. (KM56KD59W4)"
            in signature.splitlines()
        ), signature
    for binary in ("ToDesk", "ToDesk_Service", "ToDesk_Session_Proxy"):
        executable = app / "Contents/MacOS" / binary
        assert executable.is_file(), executable
        architectures = command("/usr/bin/lipo", "-archs", str(executable)).split()
        assert "arm64" in architectures, (executable, architectures)
    for path, label, binary in (
        ("LaunchDaemons/com.youqu.todesk.service.plist", "com.youqu.todesk.service", "ToDesk_Service"),
        ("LaunchAgents/com.youqu.todesk.session.plist", "com.youqu.todesk.desktop", "ToDesk_Session_Proxy"),
    ):
        with (package / "Library" / path).open("rb") as stream:
            job = plistlib.load(stream)
        assert job["Label"] == label, job
        assert job["ProgramArguments"][0] == "/Applications/ToDesk.app/Contents/MacOS/" + binary, job
    assert (app / "Contents/XPCServices/ToDesk_XPCHelper.xpc/Contents/MacOS/ToDesk_XPCHelper").is_file()
    print("ToDesk " + version + ": payload, signatures, arm64 executables, and service contracts verified")
    check_staging(package)


if __name__ == "__main__":
    check(sys.argv[1], sys.argv[2])
