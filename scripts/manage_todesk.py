#!/usr/bin/env python3
"""Own ToDesk's documented macOS installation state without running its installer."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile


APP = "Applications/ToDesk.app"
AI = "Library/Application Support/ToDesk/ai"
AUDIO = "Library/Audio/Plug-Ins/HAL/ToDeskOutputDriver.driver"
DAEMON = "Library/LaunchDaemons/com.youqu.todesk.service.plist"
SESSION = "Library/LaunchAgents/com.youqu.todesk.session.plist"
STARTUP = "Library/LaunchAgents/com.youqu.todesk.startup.plist"
TARGETS = (APP, AI, AUDIO, DAEMON, SESSION, STARTUP)
STATE = "Library/Application Support/ToDesk"
CONTROL = "Library/Application Support/.nix-todesk"
CONFIG = "private/etc/todesk/reg.conf"
HELPER = "Contents/XPCServices/ToDesk_XPCHelper.xpc/Contents/MacOS/ToDesk_XPCHelper"
RUNTIME = ("Logs", ".SentryNative", "ImageResources")
REQUIRED_DIRECTORIES = (
    "Logs", "Logs/cache", ".SentryNative", ".SentryNative/client",
    ".SentryNative/service", ".SentryNative/session", ".SentryNative/render",
    "ImageResources",
)
LABELS = {
    DAEMON: "com.youqu.todesk.service",
    SESSION: "com.youqu.todesk.desktop",
    STARTUP: "com.youqu.todesk.client.startup",
}
OWNER = "futuping/nix-packages:todesk:v1"


class DeploymentError(RuntimeError):
    pass


def run(command):
    return subprocess.run(command, text=True, capture_output=True, check=False)


class Manager:
    """The root and runner arguments exist for isolated tests, never as CLI options."""

    def __init__(self, root=Path("/"), runner=run, uid=0, gid=0):
        self.root = Path(root)
        self.runner = runner
        self.uid = uid
        self.gid = gid
        self.manifest = self.path(CONTROL) / "manifest.json"
        self.client_running = False

    def path(self, relative):
        return self.root / relative

    def safe_parents(self, path, *, create=False):
        """Do not let mutable system state redirect a privileged filesystem write."""
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise DeploymentError(f"refusing symlink in managed path: {current}")
            if current.exists():
                if not current.is_dir():
                    raise DeploymentError(f"expected directory: {current}")
                if current.stat().st_uid != self.uid:
                    raise DeploymentError(f"refusing directory owned by another user: {current}")
                # /Applications is normally root:admin 0775 on macOS. Permit
                # trusted administrator groups, never a world-writable parent.
                trusted_applications = (
                    current == self.path("Applications") and current.stat().st_gid == 80
                )
                if current.stat().st_mode & 0o002 or (
                    current.stat().st_mode & 0o020 and not trusted_applications
                ):
                    raise DeploymentError(f"refusing writable parent directory: {current}")
            elif create:
                current.mkdir(mode=0o755)
                os.chown(current, self.uid, self.gid)

    @contextmanager
    def lock(self):
        control = self.path(CONTROL)
        self.safe_parents(control, create=True)
        os.chmod(control, 0o700)
        lock_path = control / "lock"
        if lock_path.is_symlink():
            raise DeploymentError("refusing symlink for deployment lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(descriptor).st_uid != self.uid:
                raise DeploymentError("deployment lock has unexpected ownership")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def read_manifest(self):
        if not self.manifest.exists() and not self.manifest.is_symlink():
            return None
        metadata = self.manifest.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.uid
            or metadata.st_mode & 0o077
        ):
            raise DeploymentError("ownership manifest must be a root-only regular file")
        try:
            value = json.loads(self.manifest.read_text())
        except (OSError, ValueError) as error:
            raise DeploymentError("invalid ownership manifest") from error
        if (
            not isinstance(value, dict)
            or set(value) != {"owner", "package", "targets", "uiAutoStart"}
            or value["owner"] != OWNER
            or not isinstance(value["package"], str)
            or not isinstance(value["targets"], list)
            or not all(isinstance(target, str) for target in value["targets"])
            or len(set(value["targets"])) != len(value["targets"])
            or not set(value["targets"]) <= set(TARGETS)
            or type(value["uiAutoStart"]) is not bool
        ):
            raise DeploymentError("invalid ownership manifest contract")
        return value

    def command(self, *arguments):
        result = self.runner(list(arguments))
        if result.returncode:
            raise DeploymentError(
                f"command failed: {' '.join(arguments)}: {(result.stderr or result.stdout).strip()}"
            )
        return result.stdout

    def domains(self):
        output = self.command("/bin/ps", "-axo", "uid=,comm=")
        domains = set()
        self.client_running = False
        for line in output.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and fields[1] == "/Applications/ToDesk.app/Contents/MacOS/ToDesk":
                self.client_running = True
            if len(fields) != 2 or fields[1] != "/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow":
                continue
            if not fields[0].isdigit():
                raise DeploymentError("invalid loginwindow process owner")
            uid = int(fields[0])
            domains.add(f"gui/{uid}" if uid else "loginwindow")
        return sorted(domains)

    def services(self, targets, domains):
        for relative in targets:
            if relative == DAEMON:
                yield "system", relative
            elif relative == SESSION:
                for domain in domains:
                    yield domain, relative
            elif relative == STARTUP:
                for domain in domains:
                    if domain.startswith("gui/"):
                        yield domain, relative

    def loaded(self, domain, relative):
        return self.runner(["/bin/launchctl", "print", f"{domain}/{LABELS[relative]}"]).returncode == 0

    def stop(self, targets, domains):
        for domain, relative in self.services(targets, domains):
            if self.loaded(domain, relative):
                self.command("/bin/launchctl", "bootout", f"{domain}/{LABELS[relative]}")

    def start(self, targets, domains):
        for domain, relative in self.services(targets, domains):
            self.command("/bin/launchctl", "enable", f"{domain}/{LABELS[relative]}")
            if not self.loaded(domain, relative):
                self.command("/bin/launchctl", "bootstrap", domain, str(self.path(relative)))
            if not self.loaded(domain, relative):
                raise DeploymentError(f"required service did not load: {domain}/{LABELS[relative]}")

    def preflight(self, old, domains):
        owned = set(old["targets"]) if old else set()
        for relative in TARGETS:
            path = self.path(relative)
            self.safe_parents(path.parent)
            if path.exists() or path.is_symlink():
                if relative not in owned:
                    raise DeploymentError(f"refusing unmanaged target: {path}")
                if path.is_symlink() or path.lstat().st_uid != self.uid:
                    raise DeploymentError(f"refusing replaced managed target: {path}")
        if not old:
            for domain, relative in self.services(TARGETS, domains):
                if self.loaded(domain, relative):
                    raise DeploymentError(f"refusing unmanaged launchd job: {domain}/{LABELS[relative]}")
        config = self.path(CONFIG)
        self.safe_parents(config.parent)
        if config.exists() and (not config.is_file() or config.lstat().st_uid != self.uid):
            raise DeploymentError("refusing unowned ToDesk configuration")
        if config.is_symlink():
            raise DeploymentError("refusing symlink for ToDesk configuration")

    def own_tree(self, path):
        for current, directories, files in os.walk(path, followlinks=False):
            for entry in [Path(current)] + [Path(current) / name for name in directories + files]:
                os.chown(entry, self.uid, self.gid, follow_symlinks=False)
                if not entry.is_symlink():
                    mode = entry.stat().st_mode
                    os.chmod(entry, (stat.S_IMODE(mode) & 0o777) | 0o200)

    def verify(self, path):
        self.command("/usr/bin/codesign", "--verify", "--deep", "--strict", str(path))

    def matches(self, package, relative):
        """Compare code with the pinned source without trusting mutable app state."""
        source = package / relative
        target = self.path(relative)

        def digest(path):
            result = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    result.update(chunk)
            return result.digest()

        def same(original, installed, local=Path(".")):
            if not installed.exists() and not installed.is_symlink():
                return False
            metadata = installed.lstat()
            if metadata.st_uid != self.uid or metadata.st_gid != self.gid:
                return False
            if original.is_symlink():
                return installed.is_symlink() and os.readlink(original) == os.readlink(installed)
            if installed.is_symlink():
                return False
            mode = (stat.S_IMODE(original.stat().st_mode) & 0o777) | 0o200
            if relative in LABELS:
                mode = 0o644
            elif relative == APP:
                if str(local) in {".", "Contents"}:
                    mode = 0o775
                elif str(local) == HELPER:
                    mode = 0o4755
            if stat.S_IMODE(metadata.st_mode) != mode:
                return False
            if original.is_dir():
                if not installed.is_dir():
                    return False
                ignored = set(RUNTIME) if relative == APP and str(local) == "Contents" else set()
                expected = {entry.name for entry in original.iterdir()} - ignored
                actual = {entry.name for entry in installed.iterdir()} - ignored
                return expected == actual and all(
                    same(original / name, installed / name, local / name) for name in expected
                )
            return (
                original.is_file() and installed.is_file()
                and original.stat().st_size == metadata.st_size
                and digest(original) == digest(installed)
            )

        if not source.exists() or not same(source, target):
            return False
        if relative == APP:
            for name in REQUIRED_DIRECTORIES:
                directory = target / "Contents" / name
                if directory.is_symlink() or not directory.is_dir():
                    return False
                metadata = directory.stat()
                if (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) != (self.uid, self.gid, 0o777):
                    return False
        return True

    def runtime(self, staged_app, previous_app=None):
        if previous_app:
            for name in RUNTIME:
                source = previous_app / "Contents" / name
                if source.is_symlink():
                    raise DeploymentError(f"refusing redirected runtime directory: {source}")
                if source.exists():
                    if not source.is_dir():
                        raise DeploymentError(f"expected runtime directory: {source}")
                    destination = staged_app / "Contents" / name
                    if destination.exists():
                        shutil.rmtree(destination)
                    self.copy_runtime(source, destination)
        for relative in REQUIRED_DIRECTORIES:
            directory = staged_app / "Contents" / relative
            # A user-writable parent may contain attacker-controlled symlinks.
            for part in [directory] + list(directory.parents):
                if part == staged_app:
                    break
                if part.is_symlink():
                    raise DeploymentError(f"refusing redirected runtime directory: {part}")
            directory.mkdir(parents=True, exist_ok=True)
            os.chown(directory, self.uid, self.gid)
            os.chmod(directory, 0o777)
        os.chmod(staged_app, 0o775)
        os.chmod(staged_app / "Contents", 0o775)
        helper = staged_app / HELPER
        if helper.is_symlink() or not helper.is_file():
            raise DeploymentError("missing regular ToDesk privileged helper")
        os.chown(helper, self.uid, self.gid)
        os.chmod(helper, 0o4755)

    def copy_runtime(self, source, destination):
        """Snapshot user data through directory FDs; never follow a swapped symlink."""
        def copy_directory(descriptor, target):
            metadata = os.fstat(descriptor)
            target.mkdir(mode=0o700)
            for entry in os.listdir(descriptor):
                child = target / entry
                information = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(information.st_mode):
                    child.symlink_to(os.readlink(entry, dir_fd=descriptor))
                elif stat.S_ISDIR(information.st_mode):
                    nested = os.open(entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    try:
                        copy_directory(nested, child)
                    finally:
                        os.close(nested)
                    continue
                elif stat.S_ISREG(information.st_mode):
                    file_descriptor = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                    with os.fdopen(file_descriptor, "rb") as stream:
                        information = os.fstat(stream.fileno())
                        if not stat.S_ISREG(information.st_mode):
                            raise DeploymentError("runtime file changed type while copying")
                        with child.open("xb") as output:
                            shutil.copyfileobj(stream, output)
                    os.chmod(child, stat.S_IMODE(information.st_mode) & 0o777)
                    os.utime(child, ns=(information.st_atime_ns, information.st_mtime_ns))
                else:
                    raise DeploymentError(f"unsupported special file in runtime data: {entry}")
                os.chown(child, information.st_uid, information.st_gid, follow_symlinks=False)
            os.chown(target, metadata.st_uid, metadata.st_gid)
            os.chmod(target, stat.S_IMODE(metadata.st_mode) & 0o777)
            os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

        descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            copy_directory(descriptor, destination)
        finally:
            os.close(descriptor)

    def stage_target(self, package, relative, stage, previous):
        source = package / relative
        if source.is_symlink() or not source.exists():
            raise DeploymentError(f"missing original package payload: {source}")
        if source.is_dir():
            shutil.copytree(source, stage, symlinks=True)
            self.own_tree(stage)
        else:
            shutil.copy2(source, stage)
            os.chown(stage, self.uid, self.gid)
            os.chmod(stage, 0o644)
        if relative == APP:
            self.verify(stage)
            self.runtime(stage, previous)
        elif relative == AI:
            for name in ("ZAgent.app", "libToDesk_AiRecorder.dylib"):
                self.verify(stage / name)
            shortcut = stage / "libToDesk_AiShortCut.dylib"
            if shortcut.exists():
                self.verify(shortcut)
        elif relative == AUDIO:
            self.verify(stage)
        else:
            try:
                metadata = plistlib.loads(stage.read_bytes())
            except (OSError, ValueError) as error:
                raise DeploymentError(f"invalid launchd plist: {source}") from error
            if metadata.get("Label") != LABELS[relative]:
                raise DeploymentError(f"unexpected launchd label in {source}")

    def archive_runtime(self):
        app = self.path(APP)
        if not app.exists():
            return None
        directory = self.path(STATE)
        self.safe_parents(directory, create=True)
        archive = Path(tempfile.mkdtemp(prefix="nix-preserved-", dir=directory))
        try:
            for name in RUNTIME:
                source = app / "Contents" / name
                if source.is_symlink():
                    raise DeploymentError(f"refusing redirected runtime directory: {source}")
                if source.exists():
                    self.copy_runtime(source, archive / name)
            return archive
        except Exception:
            shutil.rmtree(archive)
            raise

    def atomic_manifest(self, value):
        descriptor, name = tempfile.mkstemp(prefix="manifest-", dir=self.manifest.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(value, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chown(temporary, self.uid, self.gid)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.manifest)
        finally:
            temporary.unlink(missing_ok=True)

    def apply(self, package=None, ui_auto_start=False):
        # The shared package-source module is imported even on machines that
        # have never selected ToDesk. Do not create app-specific state there.
        if package is None:
            try:
                self.path(CONTROL).lstat()
            except FileNotFoundError:
                return "already disabled"
        with self.lock():
            return self._apply(Path(package) if package is not None else None, ui_auto_start)

    def state_matches(self):
        state = self.path(STATE)
        if state.is_symlink() or not state.is_dir():
            return False
        metadata = state.stat()
        if (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) != (self.uid, self.gid, 0o755):
            return False
        dumps = self.path(STATE) / "dumps"
        if dumps.is_symlink() or not dumps.is_dir():
            return False
        metadata = dumps.stat()
        if (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) != (self.uid, self.gid, 0o1777):
            return False
        config = self.path(CONFIG)
        if not config.is_file():
            return False
        channels = re.findall(r"^\s*bus\s*=\s*(.*?)\s*$", config.read_text(), re.M)
        return channels == ["official"]

    def _apply(self, package, ui_auto_start):
        old = self.read_manifest()
        if package is None and old is None:
            return "already disabled"
        domains = self.domains()
        self.preflight(old, domains)
        wanted = list(TARGETS if ui_auto_start else TARGETS[:-1]) if package else []
        if old and package and old["package"] == str(package) and old["targets"] == wanted:
            if self.state_matches() and all(self.matches(package, relative) for relative in wanted):
                self.start(wanted, domains)
                return "already current"
        if self.client_running:
            raise DeploymentError("quit ToDesk before upgrading or disabling its managed installation")

        staged = []
        archive = None
        replaced = []
        directory_metadata = []
        services_touched = False
        old_targets = old["targets"] if old else []
        old_manifest_bytes = self.manifest.read_bytes() if old else None
        try:
            # Prepare every replacement before stopping or replacing live state.
            for relative in dict.fromkeys(old_targets + wanted):
                target = self.path(relative)
                self.safe_parents(target.parent, create=True)
                directory = Path(tempfile.mkdtemp(prefix=".nix-todesk-", dir=target.parent))
                entry = (target, directory, relative in wanted)
                staged.append(entry)
                if relative in wanted:
                    previous = self.path(APP) if old and self.path(APP).exists() else None
                    self.stage_target(package, relative, directory / "new", previous)
            if package:
                config = self.path(CONFIG)
                self.safe_parents(config.parent, create=True)
                directory = Path(tempfile.mkdtemp(prefix=".nix-todesk-", dir=config.parent))
                staged.append((config, directory, True))
                original = config.read_text() if config.exists() else "[hkey_local_machine]\n"
                lines = [line for line in original.splitlines() if not re.match(r"^\s*bus\s*=", line)]
                (directory / "new").write_text("\n".join(lines + ["bus = official"]) + "\n")
                config_mode = stat.S_IMODE(config.stat().st_mode) & 0o777 if config.exists() else 0o600
                os.chmod(directory / "new", config_mode)
                state = self.path(STATE)
                self.safe_parents(state, create=True)
                directory_metadata.append((state, state.stat()))
                os.chown(state, self.uid, self.gid)
                os.chmod(state, 0o755)
                dumps = state / "dumps"
                if dumps.is_symlink() or (dumps.exists() and not dumps.is_dir()):
                    raise DeploymentError("refusing redirected ToDesk dumps directory")
                directory_metadata.append((dumps, dumps.stat() if dumps.exists() else None))
                dumps.mkdir(exist_ok=True)
                os.chown(dumps, self.uid, self.gid)
                os.chmod(dumps, 0o1777)
            else:
                archive = self.archive_runtime()

            services_touched = True
            self.stop(old_targets, domains)
            for target, directory, enabled in staged:
                existed = target.exists()
                if existed:
                    os.replace(target, directory / "old")
                replaced.append((target, directory, existed))
                if enabled:
                    os.replace(directory / "new", target)
            if package:
                self.start(wanted, domains)
                self.atomic_manifest({
                    "owner": OWNER, "package": str(package), "targets": wanted,
                    "uiAutoStart": bool(ui_auto_start),
                })
            else:
                self.manifest.unlink()
        except Exception as error:
            failures = []
            if services_touched:
                try:
                    self.stop(wanted, domains)
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
                for target, directory, existed in reversed(replaced):
                    try:
                        if target.exists():
                            if target.is_dir():
                                shutil.rmtree(target)
                            else:
                                target.unlink()
                        if existed:
                            os.replace(directory / "old", target)
                    except Exception as rollback_error:
                        failures.append(str(rollback_error))
                try:
                    if old_manifest_bytes is not None:
                        self.atomic_manifest(json.loads(old_manifest_bytes))
                    else:
                        self.manifest.unlink(missing_ok=True)
                    self.start(old_targets, domains)
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
            if archive:
                shutil.rmtree(archive)
            for directory, metadata in reversed(directory_metadata):
                try:
                    if metadata is None:
                        # A started service may already have written a dump;
                        # retain that data even when a new installation fails.
                        if not any(directory.iterdir()):
                            directory.rmdir()
                    else:
                        os.chown(directory, metadata.st_uid, metadata.st_gid)
                        os.chmod(directory, stat.S_IMODE(metadata.st_mode))
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
            if failures:
                raise DeploymentError(
                    f"deployment failed: {error}; rollback incomplete: {'; '.join(failures)}; "
                    "preserving .nix-todesk-* backups for recovery"
                ) from error
            for _, directory, _ in staged:
                shutil.rmtree(directory)
            raise DeploymentError(f"deployment failed and previous files restored: {error}") from error
        for _, directory, _ in staged:
            shutil.rmtree(directory)
        return "installed" if package else "disabled; user configuration and runtime archives retained"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--package", type=Path)
    action.add_argument("--disable", action="store_true")
    parser.add_argument("--ui-auto-start", action="store_true")
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("ToDesk activation requires root")
    if arguments.disable and arguments.ui_auto_start:
        parser.error("--ui-auto-start requires --package")
    try:
        print("ToDesk: " + Manager().apply(arguments.package, arguments.ui_auto_start))
    except (DeploymentError, OSError) as error:
        print(f"ToDesk: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
