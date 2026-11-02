import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import manage_todesk as lifecycle


class FakeMacOS:
    def __init__(self):
        self.jobs = set()
        self.calls = []
        self.fail_bootstrap = None
        self.fail_bootout = None
        self.fail_signature = False
        self.gui_running = False

    def __call__(self, command):
        self.calls.append(command)
        result = subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "/bin/ps":
            result.stdout = (
                "501 /System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow\n"
                "502 /System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow\n"
                "0 /System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow\n"
            )
            if self.gui_running:
                result.stdout += "501 /Applications/ToDesk.app/Contents/MacOS/ToDesk\n"
        elif command[0] == "/usr/bin/codesign":
            if self.fail_signature or not Path(command[-1]).exists():
                result.returncode = 1
                result.stderr = "invalid signature"
        elif command[:2] == ["/bin/launchctl", "print"]:
            result.returncode = 0 if command[2] in self.jobs else 113
        elif command[:2] == ["/bin/launchctl", "bootout"]:
            if self.fail_bootout == command[2]:
                self.fail_bootout = None
                result.returncode = 1
                result.stderr = "injected bootout failure"
            else:
                self.jobs.discard(command[2])
        elif command[:2] == ["/bin/launchctl", "bootstrap"]:
            label = plistlib.loads(Path(command[3]).read_bytes())["Label"]
            job = command[2] + "/" + label
            if self.fail_bootstrap == job:
                self.fail_bootstrap = None
                result.returncode = 1
                result.stderr = "injected bootstrap failure"
            else:
                self.jobs.add(job)
        elif command[:2] != ["/bin/launchctl", "enable"]:
            raise AssertionError(f"unexpected platform call: {command}")
        return result


class ManageToDeskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-todesk-lifecycle-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "root"
        self.root.mkdir()
        self.macos = FakeMacOS()
        self.manager = lifecycle.Manager(
            self.root, self.macos, uid=os.geteuid(), gid=os.getegid()
        )
        self.first = self.package("first")
        self.second = self.package("second")

    def package(self, name):
        package = self.directory / name
        app = package / lifecycle.APP
        helper = app / lifecycle.HELPER
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"signed-helper-" + name.encode())
        helper.chmod(0o755)
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"version": name}))
        executable = app / "Contents" / "MacOS" / "ToDesk"
        executable.parent.mkdir()
        executable.write_bytes(b"signed-main-" + name.encode())
        executable.chmod(0o755)
        framework = app / "Contents" / "Frameworks" / "Example.framework" / "Versions"
        (framework / "A").mkdir(parents=True)
        (framework / "A" / "binary").write_bytes(b"signed-framework")
        (framework / "Current").symlink_to("A")
        ai = package / lifecycle.AI
        (ai / "ZAgent.app").mkdir(parents=True)
        for filename in ("libToDesk_AiRecorder.dylib",):
            (ai / filename).write_bytes(b"signed-ai-" + name.encode())
        audio = package / lifecycle.AUDIO
        audio.mkdir(parents=True)
        (audio / "driver").write_bytes(b"signed-audio-" + name.encode())
        for relative, label in lifecycle.LABELS.items():
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps({"Label": label}))
            # The real ToDesk session plist is executable upstream; launchd
            # configuration is deliberately normalized to 0644 on installation.
            if relative == lifecycle.SESSION:
                path.chmod(0o755)
        return package

    def installed(self, relative):
        return self.root / relative

    def contents(self):
        return self.installed(lifecycle.APP) / "Contents"

    def set_user_state(self):
        (self.contents() / "Logs" / "remote.log").write_text("user history")
        (self.contents() / ".SentryNative" / "client" / "pending").write_text("crash data")
        config = self.installed(lifecycle.CONFIG)
        config.write_text("[hkey_local_machine]\nsecret = keep\nbus = official\n")
        config.chmod(0o600)

    def test_unselected_app_creates_no_directories_and_runs_no_commands(self):
        self.assertEqual(self.manager.apply(), "already disabled")
        self.assertEqual(self.manager.apply(), "already disabled")
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.macos.calls, [])

    def test_unselected_app_leaves_unmanaged_installation_and_jobs_alone(self):
        app = self.installed(lifecycle.APP)
        app.mkdir(parents=True)
        (app / "mine").write_text("unmanaged")
        self.macos.jobs.add("system/com.youqu.todesk.service")
        self.assertEqual(self.manager.apply(), "already disabled")
        self.assertEqual((app / "mine").read_text(), "unmanaged")
        self.assertEqual(self.macos.jobs, {"system/com.youqu.todesk.service"})
        self.assertFalse(self.installed("Library").exists())
        self.assertEqual(self.macos.calls, [])

    def test_disable_refuses_dangling_control_symlink_without_creating_target(self):
        control = self.installed(lifecycle.CONTROL)
        control.parent.mkdir(parents=True)
        outside = self.directory / "absent-control"
        control.symlink_to(outside)
        with self.assertRaisesRegex(lifecycle.DeploymentError, "symlink"):
            self.manager.apply()
        self.assertTrue(control.is_symlink())
        self.assertFalse(outside.exists())
        self.assertEqual(self.macos.calls, [])

    def test_install_native_paths_permissions_services_and_no_vendor_execution(self):
        self.assertEqual(self.manager.apply(self.first), "installed")
        self.assertFalse(self.installed(lifecycle.STARTUP).exists())
        self.assertEqual(self.installed(lifecycle.APP).stat().st_mode & 0o7777, 0o775)
        self.assertEqual((self.installed(lifecycle.APP) / lifecycle.HELPER).stat().st_mode & 0o7777, 0o4755)
        for relative in lifecycle.REQUIRED_DIRECTORIES:
            self.assertEqual((self.contents() / relative).stat().st_mode & 0o7777, 0o777)
        self.assertEqual(self.installed(lifecycle.STATE + "/dumps").stat().st_mode & 0o7777, 0o1777)
        self.assertEqual(self.manager.manifest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.macos.jobs, {
            "system/com.youqu.todesk.service", "gui/501/com.youqu.todesk.desktop",
            "gui/502/com.youqu.todesk.desktop", "loginwindow/com.youqu.todesk.desktop",
        })
        self.assertEqual({call[0] for call in self.macos.calls}, {"/bin/ps", "/bin/launchctl", "/usr/bin/codesign"})

    def test_identical_activation_preserves_inode_and_runtime_and_repairs_missing_job(self):
        self.manager.apply(self.first)
        self.set_user_state()
        inode = self.installed(lifecycle.APP).stat().st_ino
        self.macos.jobs.remove("gui/501/com.youqu.todesk.desktop")
        self.macos.calls.clear()
        self.assertEqual(self.manager.apply(self.first), "already current")
        self.assertEqual(self.installed(lifecycle.APP).stat().st_ino, inode)
        self.assertEqual((self.contents() / "Logs" / "remote.log").read_text(), "user history")
        self.assertIn("gui/501/com.youqu.todesk.desktop", self.macos.jobs)
        self.assertFalse(any(call[1] == "bootout" for call in self.macos.calls if len(call) > 1))

    def test_upgrade_preserves_runtime_config_and_permissions(self):
        self.manager.apply(self.first)
        self.set_user_state()
        self.manager.apply(self.second)
        self.assertEqual((self.contents() / "MacOS" / "ToDesk").read_bytes(), b"signed-main-second")
        self.assertEqual((self.contents() / "Logs" / "remote.log").read_text(), "user history")
        self.assertEqual((self.contents() / ".SentryNative" / "client" / "pending").read_text(), "crash data")
        config = self.installed(lifecycle.CONFIG)
        self.assertIn("secret = keep", config.read_text())
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.manager.manifest.read_text())["package"], str(self.second))

    def test_runtime_snapshot_preserves_file_ownership_and_does_not_follow_links(self):
        source = self.directory / "runtime"
        source.mkdir()
        log = source / "session.log"
        log.write_text("private user history")
        log.chmod(0o600)
        outside = self.directory / "outside-secret"
        outside.write_text("must not copy this content")
        (source / "symbolic").symlink_to(outside)
        destination = self.directory / "snapshot"
        inode = log.stat().st_ino
        original_fstat = os.fstat
        ownership = []

        def user_file_metadata(descriptor):
            metadata = original_fstat(descriptor)
            if metadata.st_ino == inode:
                return SimpleNamespace(
                    st_uid=12345, st_gid=23456, st_mode=metadata.st_mode,
                    st_atime_ns=metadata.st_atime_ns, st_mtime_ns=metadata.st_mtime_ns,
                )
            return metadata

        with patch.object(lifecycle.os, "fstat", user_file_metadata), patch.object(
            lifecycle.os, "chown", side_effect=lambda *args, **kwargs: ownership.append((args, kwargs))
        ):
            self.manager.copy_runtime(source, destination)
        self.assertIn(((destination / "session.log", 12345, 23456), {"follow_symlinks": False}), ownership)
        self.assertEqual((destination / "session.log").stat().st_mode & 0o777, 0o600)
        self.assertEqual((destination / "session.log").read_text(), "private user history")
        self.assertTrue((destination / "symbolic").is_symlink())
        self.assertEqual(os.readlink(destination / "symbolic"), str(outside))

    def test_disable_removes_owned_code_and_jobs_but_preserves_user_data(self):
        self.manager.apply(self.first, ui_auto_start=True)
        self.set_user_state()
        self.manager.apply()
        for relative in lifecycle.TARGETS:
            self.assertFalse(self.installed(relative).exists())
        self.assertFalse(self.manager.manifest.exists())
        self.assertFalse(self.macos.jobs)
        archives = list(self.installed(lifecycle.STATE).glob("nix-preserved-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / "Logs" / "remote.log").read_text(), "user history")
        self.assertIn("secret = keep", self.installed(lifecycle.CONFIG).read_text())
        self.assertEqual(self.manager.apply(), "already disabled")
        self.assertEqual(len(list(self.installed(lifecycle.STATE).glob("nix-preserved-*"))), 1)

    def test_autostart_changes_converge_without_loginwindow_ui(self):
        self.manager.apply(self.first, ui_auto_start=True)
        self.assertIn("gui/501/com.youqu.todesk.client.startup", self.macos.jobs)
        self.assertNotIn("loginwindow/com.youqu.todesk.client.startup", self.macos.jobs)
        self.manager.apply(self.first, ui_auto_start=False)
        self.assertFalse(self.installed(lifecycle.STARTUP).exists())
        self.assertFalse(any("client.startup" in job for job in self.macos.jobs))

    def test_refuses_unmanaged_app_before_touching_it_or_config(self):
        app = self.installed(lifecycle.APP)
        app.mkdir(parents=True)
        (app / "mine").write_text("unmanaged")
        with self.assertRaisesRegex(lifecycle.DeploymentError, "unmanaged target"):
            self.manager.apply(self.first)
        self.assertEqual((app / "mine").read_text(), "unmanaged")
        self.assertFalse(self.installed(lifecycle.CONFIG).exists())
        self.assertFalse(any("bootout" in call or "bootstrap" in call for call in self.macos.calls))

    def test_refuses_unmanaged_plist_even_when_app_target_absent(self):
        target = self.installed(lifecycle.SESSION)
        target.parent.mkdir(parents=True)
        target.write_text("user plist")
        with self.assertRaisesRegex(lifecycle.DeploymentError, "unmanaged target"):
            self.manager.apply(self.first)
        self.assertFalse(self.installed(lifecycle.APP).exists())
        self.assertEqual(target.read_text(), "user plist")

    def test_refuses_preexisting_unmanaged_service(self):
        self.macos.jobs.add("system/com.youqu.todesk.service")
        with self.assertRaisesRegex(lifecycle.DeploymentError, "unmanaged launchd"):
            self.manager.apply(self.first)
        self.assertEqual(self.macos.jobs, {"system/com.youqu.todesk.service"})

    def test_refuses_symlink_parent_and_runtime_without_following(self):
        destination = self.directory / "outside"
        destination.mkdir()
        self.installed("Applications").symlink_to(destination)
        with self.assertRaisesRegex(lifecycle.DeploymentError, "symlink"):
            self.manager.apply(self.first)
        self.assertFalse(list(destination.iterdir()))
        self.installed("Applications").unlink()
        self.manager.apply(self.first)
        logs = self.contents() / "Logs"
        (logs / "cache").rmdir()
        logs.rmdir()
        logs.symlink_to(destination)
        with self.assertRaisesRegex(lifecycle.DeploymentError, "redirected runtime"):
            self.manager.apply(self.second)
        self.assertTrue(logs.is_symlink())
        self.assertFalse(list(destination.iterdir()))

    def test_bad_signature_prevents_any_service_or_target_change(self):
        self.manager.apply(self.first)
        before = set(self.macos.jobs)
        self.macos.calls.clear()
        self.macos.fail_signature = True
        with self.assertRaisesRegex(lifecycle.DeploymentError, "invalid signature"):
            self.manager.apply(self.second)
        self.assertEqual(self.macos.jobs, before)
        self.assertEqual((self.contents() / "MacOS" / "ToDesk").read_bytes(), b"signed-main-first")
        self.assertFalse(any("bootout" in call for call in self.macos.calls))

    def test_failed_service_start_rolls_back_files_config_manifest_and_old_jobs(self):
        self.manager.apply(self.first)
        self.set_user_state()
        before_manifest = self.manager.manifest.read_bytes()
        before_jobs = set(self.macos.jobs)
        dumps = self.installed(lifecycle.STATE + "/dumps")
        dumps.chmod(0o755)
        self.macos.fail_bootstrap = "gui/501/com.youqu.todesk.desktop"
        with self.assertRaisesRegex(lifecycle.DeploymentError, "previous files restored"):
            self.manager.apply(self.second)
        self.assertEqual((self.contents() / "MacOS" / "ToDesk").read_bytes(), b"signed-main-first")
        self.assertEqual(self.manager.manifest.read_bytes(), before_manifest)
        self.assertEqual(self.macos.jobs, before_jobs)
        self.assertEqual(dumps.stat().st_mode & 0o7777, 0o755)
        self.assertIn("secret = keep", self.installed(lifecycle.CONFIG).read_text())
        self.assertFalse(list(self.root.rglob(".nix-todesk-*")))

    def test_partial_stop_failure_restores_jobs_before_any_replacement(self):
        self.manager.apply(self.first)
        before = set(self.macos.jobs)
        self.macos.fail_bootout = "gui/501/com.youqu.todesk.desktop"
        with self.assertRaisesRegex(lifecycle.DeploymentError, "bootout failure"):
            self.manager.apply(self.second)
        self.assertEqual(self.macos.jobs, before)
        self.assertEqual((self.contents() / "MacOS" / "ToDesk").read_bytes(), b"signed-main-first")

    def test_failed_first_install_leaves_no_owned_code_or_manifest(self):
        self.macos.fail_bootstrap = "system/com.youqu.todesk.service"
        with self.assertRaisesRegex(lifecycle.DeploymentError, "bootstrap failure"):
            self.manager.apply(self.first)
        self.assertFalse(self.manager.manifest.exists())
        self.assertFalse(self.installed(lifecycle.CONFIG).exists())
        self.assertFalse(self.macos.jobs)
        self.assertFalse(any(self.installed(relative).exists() for relative in lifecycle.TARGETS))

    def test_repairs_helper_mode_and_static_code_drift(self):
        self.manager.apply(self.first)
        self.set_user_state()
        (self.installed(lifecycle.APP) / lifecycle.HELPER).chmod(0o755)
        (self.contents() / "MacOS" / "ToDesk").write_bytes(b"changed-code")
        self.assertEqual(self.manager.apply(self.first), "installed")
        self.assertEqual((self.installed(lifecycle.APP) / lifecycle.HELPER).stat().st_mode & 0o7777, 0o4755)
        self.assertEqual((self.contents() / "MacOS" / "ToDesk").read_bytes(), b"signed-main-first")
        self.assertEqual((self.contents() / "Logs" / "remote.log").read_text(), "user history")

    def test_repairs_required_state_without_losing_unrelated_configuration(self):
        self.manager.apply(self.first)
        self.set_user_state()
        self.installed(lifecycle.STATE + "/dumps").chmod(0o755)
        config = self.installed(lifecycle.CONFIG)
        config.write_text("[hkey_local_machine]\nsecret = keep\nbus = old-channel\n")
        self.assertEqual(self.manager.apply(self.first), "installed")
        self.assertEqual(self.installed(lifecycle.STATE + "/dumps").stat().st_mode & 0o7777, 0o1777)
        self.assertIn("secret = keep", config.read_text())
        self.assertIn("bus = official", config.read_text())
        self.assertEqual(self.manager.apply(self.first), "already current")

    def test_refuses_upgrade_with_open_gui_but_allows_identical_activation(self):
        self.manager.apply(self.first)
        self.macos.gui_running = True
        self.assertEqual(self.manager.apply(self.first), "already current")
        with self.assertRaisesRegex(lifecycle.DeploymentError, "quit ToDesk"):
            self.manager.apply(self.second)
        with self.assertRaisesRegex(lifecycle.DeploymentError, "quit ToDesk"):
            self.manager.apply()

    def test_refuses_writable_manifest_and_untrusted_parent(self):
        self.manager.apply(self.first)
        self.manager.manifest.chmod(0o644)
        with self.assertRaisesRegex(lifecycle.DeploymentError, "root-only"):
            self.manager.apply(self.first)
        self.manager.manifest.chmod(0o600)
        self.installed("Library/LaunchAgents").chmod(0o777)
        with self.assertRaisesRegex(lifecycle.DeploymentError, "writable parent"):
            self.manager.apply(self.first)

    def test_only_root_admin_applications_group_write_is_accepted(self):
        applications = self.installed("Applications")
        applications.mkdir()
        applications.chmod(0o775)
        original_stat = Path.stat

        def administrator_stat(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == applications:
                fields = list(result)
                fields[5] = 80
                return os.stat_result(fields)
            return result

        with patch.object(Path, "stat", administrator_stat):
            self.manager.safe_parents(applications)
        applications.chmod(0o777)
        with patch.object(Path, "stat", administrator_stat), self.assertRaisesRegex(lifecycle.DeploymentError, "writable parent"):
            self.manager.safe_parents(applications)


if __name__ == "__main__":
    unittest.main()
