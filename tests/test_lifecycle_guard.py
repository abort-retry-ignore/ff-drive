"""Regression tests for the independently installed removal lifecycle guard.

All tests redirect HOME to an owner-only scratch tree below the real home. They
never contact the user systemd manager, GVFS, rclone, or the live plugin.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import tempfile
import unittest

import registration_fixtures as fx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD_PATH = os.path.join(ROOT, "bin", "fast-fruit-drive-guard.py")


def load_guard():
    loader = importlib.machinery.SourceFileLoader("ffd_guard_under_test", GUARD_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


guard = load_guard()


class SandboxedGuardHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ffd-guard-test-", dir=os.path.expanduser("~"))
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home, 0o700)
        self.old_home = guard.HOME
        guard.HOME = self.home

    def tearDown(self):
        guard.HOME = self.old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def checkout(self, plugin_id=None):
        root = os.path.join(self.home, ".config", "omarchy", "plugins", guard.PLUGIN_ID)
        os.makedirs(os.path.join(root, "bin"), mode=0o700)
        with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": plugin_id or guard.PLUGIN_ID}, fh)
        helper = os.path.join(root, "bin", guard.HELPER_NAME)
        with open(helper, "wb") as fh:
            fh.write(b"#!/usr/bin/python3 -I\nprint('pinned helper')\n")
        os.chmod(helper, 0o700)
        return root, helper


class CheckoutIdentityTests(SandboxedGuardHome):
    def test_opens_only_expected_manifest_and_helper(self):
        _root, helper = self.checkout()
        fd = guard.open_checkout_helper()
        try:
            # Replacing the checkout leaf after validation cannot change the
            # bytes the retained descriptor supplies to Python.
            replacement = helper + ".new"
            with open(replacement, "wb") as fh:
                fh.write(b"attacker replacement")
            os.rename(replacement, helper)
            self.assertEqual(os.read(fd, 32), b"#!/usr/bin/python3 -I\nprint('pin")
        finally:
            os.close(fd)

    def test_rejects_foreign_manifest(self):
        self.checkout("example.foreign")
        with self.assertRaises(guard.CheckoutInvalid):
            guard.open_checkout_helper()

    def test_rejects_symlinked_helper(self):
        root, helper = self.checkout()
        replacement = os.path.join(self.home, "replacement")
        with open(replacement, "wb") as fh:
            fh.write(b"x")
        os.unlink(helper)
        os.symlink(replacement, helper)
        with self.assertRaises(guard.CheckoutInvalid):
            guard.open_checkout_helper()
        self.assertTrue(os.path.isfile(os.path.join(root, "manifest.json")))

    def test_rejects_group_writable_checkout_component(self):
        root, _helper = self.checkout()
        os.chmod(root, 0o770)
        with self.assertRaises(guard.CheckoutInvalid):
            guard.open_checkout_helper()


class LifecycleDecisionTests(SandboxedGuardHome):
    def test_enabled_plugin_is_noop_after_shell_restart(self):
        self.checkout()
        old_query = guard.query_plugin_enabled
        guard.query_plugin_enabled = lambda: True
        try:
            self.assertEqual(guard.lifecycle_action(delay=False), "none")
        finally:
            guard.query_plugin_enabled = old_query

    def test_disabled_plugin_requests_disable_cleanup(self):
        self.checkout()
        old_query = guard.query_plugin_enabled
        guard.query_plugin_enabled = lambda: False
        try:
            self.assertEqual(guard.lifecycle_action(delay=False), "disable")
        finally:
            guard.query_plugin_enabled = old_query

    def test_missing_checkout_requests_removal_cleanup_without_shell(self):
        old_query = guard.query_plugin_enabled
        guard.query_plugin_enabled = lambda: None
        try:
            self.assertEqual(guard.lifecycle_action(delay=False), "remove")
        finally:
            guard.query_plugin_enabled = old_query

    # The query itself, not just the decision made from its answer. The tests
    # above replace query_plugin_enabled wholesale, which hid that the real
    # call ran with a closed environment and made omarchy-shell exit with
    # "OMARCHY_PATH is not set": a plain disable then never disarmed anything.

    def test_query_passes_omarchy_path_to_the_shell_command(self):
        seen = {}

        def fake_run_tool(path, args, **kw):
            seen.update(path=path, args=args, env_extra=kw.get("env_extra"))
            return guard.Result(0, b'[{"id": "%s", "enabled": false}]' % guard.PLUGIN_ID.encode(), b"")

        old_tool, old_root = guard.run_tool, guard.omarchy_path
        guard.run_tool, guard.omarchy_path = fake_run_tool, lambda: "/usr/share/omarchy"
        try:
            self.assertIs(guard.query_plugin_enabled(), False)
        finally:
            guard.run_tool, guard.omarchy_path = old_tool, old_root
        self.assertEqual(seen["path"], guard.OMARCHY_SHELL)
        self.assertEqual(seen["env_extra"], {"OMARCHY_PATH": "/usr/share/omarchy"})

    def test_environment_stays_closed_apart_from_that_one_call(self):
        os.environ["OMARCHY_PATH"] = "/usr/share/omarchy"
        try:
            self.assertNotIn("OMARCHY_PATH", guard.child_env())
        finally:
            del os.environ["OMARCHY_PATH"]

    def test_run_bounded_merges_only_the_requested_extra_environment(self):
        result = guard.run_bounded(["/usr/bin/env"], timeout=5, env_extra={"OMARCHY_PATH": "/x"})
        lines = result.stdout.decode().splitlines()
        self.assertIn("OMARCHY_PATH=/x", lines)
        self.assertIn("PATH=/usr/bin", lines)
        plain = guard.run_bounded(["/usr/bin/env"], timeout=5).stdout.decode().splitlines()
        self.assertFalse(any(line.startswith("OMARCHY_PATH=") for line in plain))

    def test_no_trustworthy_omarchy_root_means_do_nothing(self):
        old_root = guard.omarchy_path
        guard.omarchy_path = lambda: None
        try:
            self.assertIsNone(guard.query_plugin_enabled())  # -> lifecycle "none": never disarm on doubt
        finally:
            guard.omarchy_path = old_root

    def test_omarchy_path_rejects_an_inherited_value_that_is_not_root_owned(self):
        evil = os.path.join(self.tmp, "fake-omarchy")
        os.makedirs(os.path.join(evil, "shell"))
        with open(os.path.join(evil, "shell", "shell.qml"), "w") as fh:
            fh.write("// planted\n")
        os.environ["OMARCHY_PATH"] = evil
        try:
            chosen = guard.omarchy_path()
        finally:
            del os.environ["OMARCHY_PATH"]
        self.assertNotEqual(chosen, evil)  # user-owned tree: never handed to omarchy-shell
        self.assertIn(chosen, (None, guard.OMARCHY_ROOT_DEFAULT))

    def test_omarchy_path_rejects_traversal_and_relative_values(self):
        for bad in ("relative/path", "/usr/share/../../tmp"):
            os.environ["OMARCHY_PATH"] = bad
            try:
                self.assertNotEqual(guard.omarchy_path(), bad)
            finally:
                del os.environ["OMARCHY_PATH"]

    def test_omarchy_path_falls_back_to_the_packaged_default_when_present(self):
        if not os.path.isfile(os.path.join(guard.OMARCHY_ROOT_DEFAULT, "shell", "shell.qml")):
            self.skipTest("Omarchy is not installed in the packaged location")
        os.environ.pop("OMARCHY_PATH", None)
        self.assertEqual(guard.omarchy_path(), guard.OMARCHY_ROOT_DEFAULT)

    def test_invalid_serve_disarms_and_exits_successfully(self):
        calls = []
        old_disarm, old_schedule = guard.disarm_unit, guard.schedule_cleanup
        guard.disarm_unit = lambda: calls.append("disarm")
        guard.schedule_cleanup = lambda mode: calls.append(mode) or True
        try:
            self.assertEqual(guard.cmd_serve(), 0)
        finally:
            guard.disarm_unit, guard.schedule_cleanup = old_disarm, old_schedule
        self.assertEqual(calls, ["disarm", "remove"])

    def test_valid_serve_executes_the_pinned_proc_fd(self):
        self.checkout()
        calls = []
        old_tool, old_execve = guard.verified_usr_tool, guard.os.execve
        old_disarm, old_schedule = guard.disarm_unit, guard.schedule_cleanup
        guard.verified_usr_tool = lambda path: path if path == guard.PYTHON else None
        guard.disarm_unit = lambda: calls.append("disarm")
        guard.schedule_cleanup = lambda mode: calls.append(mode) or True

        def fake_execve(path, argv, env):
            calls.append((path, argv, env))
            raise OSError("test stop")

        guard.os.execve = fake_execve
        try:
            self.assertEqual(guard.cmd_serve(), 0)
        finally:
            guard.verified_usr_tool, guard.os.execve = old_tool, old_execve
            guard.disarm_unit, guard.schedule_cleanup = old_disarm, old_schedule
        path, argv, env = calls[0]
        self.assertEqual(path, guard.PYTHON)
        self.assertEqual(argv[:2], [guard.PYTHON, "-I"])
        self.assertRegex(argv[2], r"^/proc/self/fd/[0-9]+$")
        self.assertEqual(argv[3], "serve")
        self.assertEqual(env["HOME"], self.home)
        self.assertIn("disarm", calls)


class CleanupTests(SandboxedGuardHome):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.home, ".config", "systemd", "user", "default.target.wants"), mode=0o700)
        os.makedirs(os.path.join(self.home, ".config", "gtk-3.0"), mode=0o700)
        os.makedirs(os.path.join(self.home, ".local", "share", "nautilus-python", "extensions", "__pycache__"), mode=0o700)
        os.makedirs(os.path.join(self.home, ".config", "fast-fruit-drive"), mode=0o700)
        os.makedirs(os.path.join(self.home, ".cache", "fast-fruit-drive"), mode=0o700)
        unit_dir = os.path.join(self.home, ".config", "systemd", "user")
        units = fx.current_units(guard.guard_path(), guard.plugin_checkout())
        for name in (guard.UNIT_NAME, guard.LIFECYCLE_SERVICE_NAME, guard.LIFECYCLE_PATH_NAME):
            with open(os.path.join(unit_dir, name), "w") as fh:
                fh.write(units[name])
        for name in (guard.UNIT_NAME, guard.LIFECYCLE_PATH_NAME):
            # `systemctl enable` links to the absolute unit path.
            os.symlink(os.path.join(unit_dir, name), os.path.join(unit_dir, "default.target.wants", name))
        with open(os.path.join(self.home, ".config", "gtk-3.0", "bookmarks"), "w") as fh:
            fh.write("file:///home/igor Keep me\n")
            fh.write("dav://ff-drive@iCloud.localhost:8080/ iCloud Drive\n")
        with open(os.path.join(self.home, ".local", "share", "nautilus-python", "extensions", guard.EXTENSION_NAME), "wb") as fh:
            fh.write(fx.CURRENT_EXTENSION)
        with open(os.path.join(self.home, ".local", "share", "nautilus-python", "extensions", "__pycache__", "fast_fruit_drive_nautilus.cpython-313.pyc"), "wb") as fh:
            fh.write(b"bytecode")
        with open(os.path.join(self.home, ".config", "fast-fruit-drive", guard.GUARD_NAME), "w") as fh:
            fh.write("guard")
        with open(os.path.join(self.home, ".config", "fast-fruit-drive", "config"), "w") as fh:
            fh.write("cache_max_size=4G\n")
        with open(os.path.join(self.home, ".cache", "fast-fruit-drive", "dirty"), "w") as fh:
            fh.write("must survive")

    def test_cleanup_removes_registrations_and_preserves_data(self):
        old_run_tool = guard.run_tool
        guard.run_tool = lambda *_args, **_kwargs: guard.Result(0, b"", b"")
        try:
            guard.cleanup("remove")
            # Repeating a partially completed cleanup must be harmless.
            guard.cleanup("remove")
        finally:
            guard.run_tool = old_run_tool

        for name in (guard.UNIT_NAME, guard.LIFECYCLE_SERVICE_NAME, guard.LIFECYCLE_PATH_NAME):
            self.assertFalse(os.path.lexists(os.path.join(self.home, ".config", "systemd", "user", name)))
        for name in (guard.UNIT_NAME, guard.LIFECYCLE_PATH_NAME):
            self.assertFalse(os.path.lexists(os.path.join(self.home, ".config", "systemd", "user", "default.target.wants", name)))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "share", "nautilus-python", "extensions", guard.EXTENSION_NAME)))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "share", "nautilus-python", "extensions", "__pycache__", "fast_fruit_drive_nautilus.cpython-313.pyc")))
        # The guard is inert configuration once its unit/path registrations
        # are gone; retaining it makes concurrent path events idempotent.
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config", "fast-fruit-drive", guard.GUARD_NAME)))
        with open(os.path.join(self.home, ".config", "gtk-3.0", "bookmarks"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "file:///home/igor Keep me\n")
        with open(os.path.join(self.home, ".config", "fast-fruit-drive", "config"), encoding="utf-8") as fh:
            self.assertIn("cache_max_size", fh.read())
        with open(os.path.join(self.home, ".cache", "fast-fruit-drive", "dirty"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "must survive")


class LifecycleQmlTests(unittest.TestCase):
    def test_widget_bootstraps_migration_and_disable_classification(self):
        with open(os.path.join(ROOT, "Service.qml"), encoding="utf-8") as fh:
            qml = fh.read()
        self.assertIn("Component.onCompleted", qml)
        self.assertIn("migrateLifecycleGuard", qml)
        self.assertIn('"-I", helperPath, "__migrate-lifecycle"', qml)
        self.assertIn("Component.onDestruction", qml)
        self.assertIn('"-I", guard, "lifecycle-check"', qml)
        self.assertNotIn("rm -rf", qml)

    def test_widget_remounts_running_server_without_mount(self):
        with open(os.path.join(ROOT, "Service.qml"), encoding="utf-8") as fh:
            qml = fh.read()
        self.assertIn('runControl(["mount"])', qml)
        self.assertIn("mountDrive", qml)
        # Retries must be bounded, not a busy loop while the mount fails.
        self.assertIn("_mountTries >= 3", qml)
        self.assertIn("Qt.callLater(root.restoreMount)", qml)
        self.assertIn("_desired === 0", qml)


if __name__ == "__main__":
    unittest.main()
