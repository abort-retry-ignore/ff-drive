"""Regression tests for bin/fast-fruit-drive.

These tests sandbox all filesystem state under a temporary directory (never
the real account home) and never touch systemd, rclone, or GVFS. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import json
import importlib.machinery
import io
import importlib.util
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

import registration_fixtures as fx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER_PATH = os.path.join(ROOT, "bin", "fast-fruit-drive")


def load_helper():
    loader = importlib.machinery.SourceFileLoader("ffd_under_test", HELPER_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ffd = load_helper()


class SandboxedHome(unittest.TestCase):
    """Redirects every module-level state path at a scratch directory."""

    def setUp(self):
        # Every real ancestor from '/' down is walked and ownership-checked
        # (see open_home()), so the scratch tree must live somewhere with
        # normal, non-group/world-writable permissions all the way up —
        # unlike /tmp (mode 1777), which the safety checks correctly refuse.
        # A prefixed, cleaned-up subdirectory of the real home is the
        # closest sandbox that still exercises the real code path.
        self.tmp = tempfile.mkdtemp(prefix="ffd-test-", dir=os.path.expanduser("~"))
        home = os.path.join(self.tmp, "home")
        os.makedirs(home, 0o700)
        self._orig = {
            name: getattr(ffd, name)
            for name in (
                "HOME", "CONFIG_DIR", "CACHE_DIR", "UNIT_DIR", "WANTS_DIR",
                "BOOKMARKS_DIR", "RCLONE_CONF_DIR", "EXTENSION_DIR", "PLUGIN_CHECKOUT", "GUARD_INSTALLED",
                "_EXTRA_TRUSTED_UIDS",
            )
        }
        ffd.HOME = home
        ffd.CONFIG_DIR = os.path.join(home, ".config", "fast-fruit-drive")
        ffd.CACHE_DIR = os.path.join(home, ".cache", "fast-fruit-drive")
        ffd.UNIT_DIR = os.path.join(home, ".config", "systemd", "user")
        ffd.WANTS_DIR = os.path.join(ffd.UNIT_DIR, "default.target.wants")
        ffd.BOOKMARKS_DIR = os.path.join(home, ".config", "gtk-3.0")
        ffd.RCLONE_CONF_DIR = os.path.join(home, ".config", "rclone")
        ffd.EXTENSION_DIR = os.path.join(home, ".local", "share", "nautilus-python", "extensions")
        ffd.PLUGIN_CHECKOUT = os.path.join(home, ffd.PLUGIN_REL)
        ffd.GUARD_INSTALLED = os.path.join(ffd.CONFIG_DIR, ffd.GUARD_INSTALLED_NAME)

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(ffd, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)


class ValidComponentTests(unittest.TestCase):
    def test_rejects_traversal_and_control_chars(self):
        for bad in ("..", ".", "", "a/b", "a\0b", "a\nb"):
            self.assertFalse(ffd._valid_component(bad), repr(bad))

    def test_accepts_normal_names(self):
        for good in ("config", "webdav.htpasswd", "a.b-c_9"):
            self.assertTrue(ffd._valid_component(good), repr(good))


class SafeDirTests(SandboxedHome):
    def test_atomic_write_and_read_roundtrip(self):
        d = ffd.ensure_state_dir(ffd.rel_to_home(ffd.CONFIG_DIR))
        try:
            d.atomic_write("thing", b"hello world", 0o600)
            data = d.read_bounded("thing", 1024)
            self.assertEqual(data, b"hello world")
            st = os.stat(os.path.join(ffd.CONFIG_DIR, "thing"))
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        finally:
            d.close()

    def test_read_bounded_rejects_oversized(self):
        d = ffd.ensure_state_dir(ffd.rel_to_home(ffd.CONFIG_DIR))
        try:
            d.atomic_write("big", b"x" * 100, 0o600)
            with self.assertRaises(ffd.Unsafe):
                d.read_bounded("big", 10)
        finally:
            d.close()

    def test_enter_refuses_symlinked_component(self):
        real_target = os.path.join(self.tmp, "elsewhere")
        os.makedirs(real_target, 0o700)
        parent = ffd.ensure_state_dir(ffd.rel_to_home(ffd.CONFIG_DIR))
        try:
            link_path = os.path.join(ffd.CONFIG_DIR, "evil-link")
            os.symlink(real_target, link_path)
            # However the kernel classifies it (ELOOP vs ENOTDIR for the
            # O_DIRECTORY|O_NOFOLLOW combination), a symlinked component
            # must never be silently followed.
            with self.assertRaises((OSError, ffd.Unsafe)):
                parent.enter("evil-link")
        finally:
            parent.close()

    def test_enter_refuses_group_writable_directory(self):
        parent = ffd.ensure_state_dir(ffd.rel_to_home(ffd.CONFIG_DIR))
        try:
            bad = os.path.join(ffd.CONFIG_DIR, "loose")
            os.mkdir(bad, 0o777)
            os.chmod(bad, 0o777)  # mkdir's mode is subject to umask; force it
            with self.assertRaises(ffd.Unsafe):
                parent.enter("loose")
        finally:
            parent.close()

    def test_remove_tree_deletes_nested_structure(self):
        home = ffd.open_home()
        try:
            cache = ffd.ensure_state_dir(ffd.rel_to_home(ffd.CACHE_DIR))
            try:
                sub = cache.enter("vfsMeta", create=True)
                sub.atomic_write("leaf", b"{}", 0o600)
                sub.close()
            finally:
                cache.close()
            home.remove_tree(ffd.rel_to_home(ffd.CACHE_DIR))
            self.assertFalse(os.path.exists(ffd.CACHE_DIR))
        finally:
            home.close()

    def test_remove_tree_enforces_entry_cap(self):
        cache = ffd.ensure_state_dir(ffd.rel_to_home(ffd.CACHE_DIR))
        try:
            sub = cache.enter("many", create=True)
            for i in range(5):
                sub.atomic_write(f"f{i}", b"x", 0o600)
            sub.close()
        finally:
            cache.close()
        home = ffd.open_home()
        try:
            with self.assertRaises(ffd.Unsafe):
                home.remove_tree(ffd.rel_to_home(ffd.CACHE_DIR), max_entries=2)
        finally:
            home.close()


class GuardInstallTests(SandboxedHome):
    def _write_expected_plugin(self, plugin_id=None):
        plugin = os.path.join(ffd.HOME, ".config", "omarchy", "plugins", ffd.PLUGIN_ID)
        os.makedirs(os.path.join(plugin, "bin"), mode=0o700)
        with open(os.path.join(plugin, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": plugin_id or ffd.PLUGIN_ID}, fh)
        with open(os.path.join(ROOT, "bin", ffd.GUARD_SOURCE_NAME), "rb") as src:
            data = src.read()
        with open(os.path.join(plugin, "bin", ffd.GUARD_SOURCE_NAME), "wb") as dst:
            dst.write(data)
        os.chmod(os.path.join(plugin, "bin", ffd.GUARD_SOURCE_NAME), 0o700)

    def test_installs_guard_only_from_expected_manifest(self):
        self._write_expected_plugin()
        ffd.install_lifecycle_guard()
        with open(ffd.GUARD_INSTALLED, "rb") as fh:
            installed = fh.read()
        with open(os.path.join(ROOT, "bin", ffd.GUARD_SOURCE_NAME), "rb") as fh:
            self.assertEqual(installed, fh.read())
        self.assertEqual(stat.S_IMODE(os.stat(ffd.GUARD_INSTALLED).st_mode), 0o700)

    def test_refuses_guard_from_foreign_checkout(self):
        self._write_expected_plugin("example.foreign")
        with self.assertRaises(SystemExit):
            ffd.install_lifecycle_guard()
        self.assertFalse(os.path.exists(ffd.GUARD_INSTALLED))

    def test_unit_execstart_references_installed_guard_not_checkout(self):
        old_systemctl, old_run = ffd.Tools.systemctl, ffd.run_bounded
        ffd.Tools.systemctl = "/usr/bin/systemctl"
        ffd.run_bounded = lambda *_args, **_kwargs: ffd.BoundedResult(0, b"", b"")
        try:
            ffd.write_unit()
        finally:
            ffd.Tools.systemctl, ffd.run_bounded = old_systemctl, old_run
        with open(os.path.join(ffd.UNIT_DIR, ffd.UNIT_NAME), encoding="utf-8") as fh:
            unit = fh.read()
        self.assertIn(f'ExecStart=/usr/bin/python3 -I "{ffd.GUARD_INSTALLED}" serve', unit)
        self.assertNotIn(".config/omarchy/plugins/", unit)
        with open(os.path.join(ffd.UNIT_DIR, ffd.LIFECYCLE_SERVICE_NAME), encoding="utf-8") as fh:
            lifecycle_service = fh.read()
        self.assertIn(f'ExecStart=/usr/bin/python3 -I "{ffd.GUARD_INSTALLED}" lifecycle-check', lifecycle_service)
        self.assertNotIn(".config/omarchy/plugins/", lifecycle_service)
        with open(os.path.join(ffd.UNIT_DIR, ffd.LIFECYCLE_PATH_NAME), encoding="utf-8") as fh:
            self.assertIn(ffd.PLUGIN_CHECKOUT, fh.read())

    def test_lifecycle_migration_rewrites_only_an_existing_unit(self):
        self._write_expected_plugin()
        os.makedirs(ffd.UNIT_DIR, mode=0o700)
        old_unit = os.path.join(ffd.UNIT_DIR, ffd.UNIT_NAME)
        with open(old_unit, "w", encoding="utf-8") as fh:
            # A real 0.2.0 unit: it ran the checkout helper directly.
            fh.write(fx.legacy_020_units("/old/checkout/bin/fast-fruit-drive")[fx.SERVICE])
        commands = []
        old_systemctl, old_run = ffd.Tools.systemctl, ffd.run_bounded
        ffd.Tools.systemctl = "/usr/bin/systemctl"
        ffd.run_bounded = lambda argv, **_kwargs: commands.append(argv) or ffd.BoundedResult(0, b"", b"")
        try:
            ffd.cmd_migrate_lifecycle([])
        finally:
            ffd.Tools.systemctl, ffd.run_bounded = old_systemctl, old_run
        with open(old_unit, encoding="utf-8") as fh:
            self.assertIn(f'ExecStart=/usr/bin/python3 -I "{ffd.GUARD_INSTALLED}" serve', fh.read())
        self.assertTrue(os.path.exists(ffd.GUARD_INSTALLED))
        self.assertTrue(any(command[-1:] == ["daemon-reload"] for command in commands))
        self.assertFalse(any(command[-2:] == ["enable", ffd.UNIT_NAME] for command in commands))
        self.assertTrue(any(command[-2:] == ["enable", ffd.LIFECYCLE_PATH_NAME] for command in commands))


class ConfigTests(SandboxedHome):
    def test_defaults_when_missing(self):
        cfg = ffd.load_config()
        self.assertEqual(cfg.cache_max_size, ffd.DEFAULT_CACHE_MAX_SIZE)
        self.assertEqual(cfg.cache_max_age_hours, ffd.DEFAULT_CACHE_MAX_AGE_HOURS)

    def test_save_and_reload_roundtrip(self):
        cfg = ffd.Config()
        cfg.cache_max_size = "1G"
        cfg.cache_max_age_hours = 48
        ffd.save_config(cfg)
        reloaded = ffd.load_config()
        self.assertEqual(reloaded.cache_max_size, "1G")
        self.assertEqual(reloaded.cache_max_age_hours, 48)

    def test_legacy_keys_are_dropped_not_trusted(self):
        d = ffd.ensure_state_dir(ffd.rel_to_home(ffd.CONFIG_DIR))
        try:
            d.atomic_write(
                "config",
                b"remote=attacker:\nbind=0.0.0.0:9999\ndav_host=attacker.example\n"
                b"cache_max_size=2G\ncache_max_age_hours=12\n",
                0o600,
            )
        finally:
            d.close()
        cfg = ffd.load_config()
        # Only the two supported keys are honored; the rest are silently
        # ignored, and the plugin's remote/bind/host stay fixed constants.
        self.assertEqual(cfg.cache_max_size, "2G")
        self.assertEqual(cfg.cache_max_age_hours, 12)
        self.assertEqual(ffd.REMOTE, "icloud:")
        self.assertEqual(ffd.DAV_HOST, "iCloud.localhost")

    def test_invalid_values_rejected(self):
        self.assertFalse(ffd.valid_cache_size("999Z"))
        self.assertFalse(ffd.valid_cache_size("4g"))
        self.assertTrue(ffd.valid_cache_size("4G"))
        self.assertFalse(ffd.valid_cache_age_hours("0"))
        self.assertFalse(ffd.valid_cache_age_hours("169"))
        self.assertFalse(ffd.valid_cache_age_hours("abc"))
        self.assertTrue(ffd.valid_cache_age_hours("168"))
        self.assertTrue(ffd.valid_cache_age_hours("1"))


class DirtyScanTests(SandboxedHome):
    def _write_meta(self, rel_path: str, body: bytes):
        cache = ffd.ensure_state_dir(ffd.rel_to_home(ffd.CACHE_DIR))
        try:
            d = cache.enter("vfsMeta", create=True)
            try:
                parts = rel_path.split("/")
                cur = d
                for part in parts[:-1]:
                    nxt = cur.enter(part, create=True)
                    if cur is not d:
                        cur.close()
                    cur = nxt
                cur.atomic_write(parts[-1], body, 0o600)
                if cur is not d:
                    cur.close()
            finally:
                d.close()
        finally:
            cache.close()

    def test_no_cache_means_not_dirty(self):
        self.assertFalse(ffd.cache_has_dirty_uploads())

    def test_clean_metadata_is_not_dirty(self):
        self._write_meta("icloud/Documents/a.txt", b'{"Dirty": false}')
        self.assertFalse(ffd.cache_has_dirty_uploads())

    def test_dirty_metadata_is_detected(self):
        self._write_meta("icloud/Documents/a.txt", b'{"Dirty": true}')
        self.assertTrue(ffd.cache_has_dirty_uploads())

    def test_nested_dirty_metadata_is_detected(self):
        self._write_meta("icloud/a/b/c/d.txt", b'{"Dirty": true}')
        self.assertTrue(ffd.cache_has_dirty_uploads())

    def test_malformed_json_is_ignored_not_fatal(self):
        self._write_meta("icloud/broken.txt", b"{not json")
        self.assertFalse(ffd.cache_has_dirty_uploads())

    def test_oversized_metadata_is_skipped_not_fatal(self):
        big = b'{"Dirty": true, "pad": "' + b"x" * ffd.MAX_META_FILE + b'"}'
        self._write_meta("icloud/huge.txt", big)
        self.assertFalse(ffd.cache_has_dirty_uploads())


class SanitizedEnvTests(unittest.TestCase):
    def test_dangerous_vars_never_forwarded(self):
        old = dict(os.environ)
        try:
            os.environ["BASH_ENV"] = "/tmp/hostile"
            os.environ["PYTHONPATH"] = "/tmp/hostile"
            os.environ["LD_PRELOAD"] = "/tmp/hostile.so"
            env = ffd.sanitized_child_env()
        finally:
            os.environ.clear()
            os.environ.update(old)
        for bad in ("BASH_ENV", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"):
            self.assertNotIn(bad, env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_extra_overrides_win(self):
        env = ffd.sanitized_child_env({"HOME": "/nonexistent"})
        self.assertEqual(env["HOME"], "/nonexistent")


class RunBoundedTests(unittest.TestCase):
    def test_output_capped_before_delimiter(self):
        result = ffd.run_bounded(
            ["/usr/bin/python3", "-I", "-c", "import sys; sys.stdout.write('x' * 5_000_000)"],
            timeout=5, cap=4096,
        )
        self.assertTrue(result.overflowed)
        self.assertLessEqual(len(result.stdout), 4096)

    def test_timeout_kills_even_a_term_trapping_child(self):
        start = time.monotonic()
        result = ffd.run_bounded(
            ["/usr/bin/bash", "-c", 'trap "" TERM; /usr/bin/sleep 30'],
            timeout=1,
        )
        elapsed = time.monotonic() - start
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 6)

    def test_group_kill_reaps_grandchildren(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "gc.pid")
            ffd.run_bounded(
                ["/usr/bin/bash", "-c", f"/usr/bin/sleep 30 & echo $! > {pidfile}; wait"],
                timeout=1,
            )
            time.sleep(0.3)
            with open(pidfile) as fh:
                gc_pid = int(fh.read().strip())
            with self.assertRaises(OSError):
                os.kill(gc_pid, 0)

    def test_normal_command_returns_promptly(self):
        result = ffd.run_bounded(["/usr/bin/true"], timeout=5)
        self.assertTrue(result.ok)


class VerifiedExecTests(unittest.TestCase):
    def test_rejects_untrusted_prefix(self):
        self.assertIsNone(ffd.verified_exec("/tmp/whatever"))
        self.assertIsNone(ffd.verified_exec("/home/anyone/bin/rclone"))

    def test_resolves_a_real_system_tool(self):
        resolved = ffd.verified_exec("/usr/bin/python3")
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.startswith("/usr/"))


class MountCommandTests(unittest.TestCase):
    def test_mount_is_public_and_supervisable(self):
        self.assertIn("mount", ffd.PUBLIC_COMMANDS)
        self.assertIn("mount", ffd.SUPERVISABLE_COMMANDS)
        self.assertIn("mount", ffd.COMMANDS)

    def test_usage_lists_mount(self):
        self.assertIn("mount", ffd.USAGE)

    def test_mount_refuses_stopped_service_without_side_effects(self):
        with mock.patch.object(ffd, "service_running", return_value=False), \
             mock.patch.object(ffd, "wait_for_server") as ready, \
             mock.patch.object(ffd, "do_mount") as mount, \
             mock.patch.object(ffd, "ensure_bookmark") as bookmark, \
             mock.patch.object(ffd, "systemctl") as systemctl:
            with self.assertRaises(SystemExit):
                ffd.cmd_mount([])
            ready.assert_not_called()
            mount.assert_not_called()
            bookmark.assert_not_called()
            systemctl.assert_not_called()

    def test_mount_checks_readiness_before_mounting_without_reinstall(self):
        calls = []
        with mock.patch.object(ffd, "service_running", return_value=True), \
             mock.patch.object(ffd, "require_valid_session", side_effect=lambda: calls.append("session")), \
             mock.patch.object(ffd, "wait_for_server", side_effect=lambda: calls.append("ready")), \
             mock.patch.object(ffd, "do_mount", side_effect=lambda: calls.append("mount")), \
             mock.patch.object(ffd, "ensure_bookmark", side_effect=lambda: calls.append("bookmark")), \
             mock.patch.object(ffd, "cmd_ensure") as ensure, \
             mock.patch.object(ffd, "systemctl") as systemctl:
            ffd.cmd_mount([])
            self.assertEqual(calls, ["session", "ready", "mount", "bookmark"])
            ensure.assert_not_called()
            systemctl.assert_not_called()

    def test_failed_readiness_does_not_attempt_mount(self):
        with mock.patch.object(ffd, "service_running", return_value=True), \
             mock.patch.object(ffd, "require_valid_session"), \
             mock.patch.object(ffd, "wait_for_server", side_effect=SystemExit(1)), \
             mock.patch.object(ffd, "do_mount") as mount, \
             mock.patch.object(ffd, "ensure_bookmark") as bookmark:
            with self.assertRaises(SystemExit):
                ffd.cmd_mount([])
            mount.assert_not_called()
            bookmark.assert_not_called()


class SessionValidityTests(SandboxedHome):
    """An rclone.conf that still holds a trust token is not a valid session:
    iCloud rejects an expired one, and the failure used to surface as a
    Nautilus password prompt. These pin the detection and its handling."""

    def setUp(self):
        super().setUp()
        self.conf_dir = ffd.RCLONE_CONF_DIR
        os.makedirs(self.conf_dir, 0o700)
        os.makedirs(ffd.CONFIG_DIR, 0o700)
        self.write_conf("trust-one")
        self._tools = (ffd.Tools.rclone, ffd.Tools.python3)
        ffd.Tools.rclone = "/usr/bin/rclone"
        ffd.Tools.python3 = "/usr/bin/python3"

    def tearDown(self):
        ffd.Tools.rclone, ffd.Tools.python3 = self._tools
        super().tearDown()

    def write_conf(self, token):
        path = os.path.join(self.conf_dir, "rclone.conf")
        with open(path, "w") as fh:
            fh.write(f"[icloud]\ntype = iclouddrive\ntrust_token = {token}\ncookies = c\n")
        os.chmod(path, 0o600)

    def result(self, rc=0, err=b"", out=b"", timed_out=False):
        return ffd.BoundedResult(rc, out, err, timed_out=timed_out)

    def test_probe_ok(self):
        with mock.patch.object(ffd, "run_bounded", return_value=self.result()):
            self.assertEqual(ffd.probe_session(), "ok")

    def test_probe_recognises_the_real_expiry_error(self):
        err = (b"ERROR : error listing: HTTP error 421 (421 Misdirected Request) returned body: "
               b"\"{\\\"reason\\\":\\\"Invalid global session\\\",\\\"error\\\":2}\"")
        with mock.patch.object(ffd, "run_bounded", return_value=self.result(rc=1, err=err)):
            self.assertEqual(ffd.probe_session(), "expired")

    def test_probe_does_not_mistake_network_trouble_for_expiry(self):
        for res in (self.result(rc=1, err=b"dial tcp: lookup p63-drivews.icloud.com: no such host"),
                    self.result(rc=1, err=b"2026/10/01 22:17:421 NOTICE: something else"),
                    self.result(timed_out=True, rc=-9)):
            with mock.patch.object(ffd, "run_bounded", return_value=res):
                self.assertEqual(ffd.probe_session(), "unknown")

    def test_fingerprint_follows_trust_token_not_secret_text(self):
        first = ffd.session_fingerprint()
        self.assertNotIn("trust-one", first)
        self.assertEqual(first, ffd.session_fingerprint())
        self.write_conf("trust-two")
        self.assertNotEqual(first, ffd.session_fingerprint())

    def test_refresh_records_verdict_and_drops_entry_on_expiry(self):
        with mock.patch.object(ffd, "probe_session", return_value="expired"), \
             mock.patch.object(ffd, "drop_dav_entry") as drop:
            self.assertEqual(ffd.refresh_session(), "expired")
            drop.assert_called_once()
        with mock.patch.object(ffd, "spawn_detached") as spawn:
            self.assertEqual(ffd.cached_session_state(allow_probe=True), "expired")
            spawn.assert_not_called()  # fresh verdict, nothing to re-probe

    def test_good_refresh_leaves_the_entry_alone(self):
        with mock.patch.object(ffd, "probe_session", return_value="ok"), \
             mock.patch.object(ffd, "drop_dav_entry") as drop:
            self.assertEqual(ffd.refresh_session(), "ok")
            drop.assert_not_called()

    def test_new_sign_in_invalidates_a_cached_expired_verdict(self):
        with mock.patch.object(ffd, "probe_session", return_value="expired"), \
             mock.patch.object(ffd, "drop_dav_entry"):
            ffd.refresh_session()
        self.write_conf("trust-two")
        with mock.patch.object(ffd, "spawn_detached"):
            self.assertEqual(ffd.cached_session_state(allow_probe=False), "unknown")

    def test_status_never_probes_while_stopped(self):
        with mock.patch.object(ffd, "spawn_detached") as spawn:
            self.assertEqual(ffd.cached_session_state(allow_probe=False), "unknown")
            spawn.assert_not_called()

    def test_stale_verdict_spawns_exactly_one_background_probe(self):
        with mock.patch.object(ffd, "spawn_detached") as spawn:
            self.assertEqual(ffd.cached_session_state(allow_probe=True), "unknown")
            self.assertEqual(ffd.cached_session_state(allow_probe=True), "unknown")
            self.assertEqual(spawn.call_count, 1)
            argv = spawn.call_args[0][0]
            self.assertEqual(argv[-1], "__check-session")

    def test_dead_background_probe_is_retried(self):
        with mock.patch.object(ffd, "spawn_detached") as spawn:
            ffd.cached_session_state(allow_probe=True)
            record = ffd._load_session_check()
            record["pending"] = int(time.time()) - int(ffd.SESSION_PENDING_TTL) - 5
            ffd._store_session_check(record)
            ffd.cached_session_state(allow_probe=True)
            self.assertEqual(spawn.call_count, 2)

    def status_payload(self, verdict):
        import io
        out = io.StringIO()
        with mock.patch.object(ffd, "remote_configured", return_value=True), \
             mock.patch.object(ffd, "service_running", return_value=True), \
             mock.patch.object(ffd, "gio_mounted", return_value=False), \
             mock.patch.object(ffd, "cache_bytes", return_value=0), \
             mock.patch.object(ffd, "cached_session_state", return_value=verdict), \
             contextlib.redirect_stdout(out):
            ffd.cmd_status([])
        return json.loads(out.getvalue())

    def test_status_reports_expiry_as_needs_login_and_not_ready(self):
        payload = self.status_payload("expired")
        self.assertTrue(payload["hasSession"])  # token is still there...
        self.assertTrue(payload["sessionExpired"])  # ...but iCloud rejects it
        self.assertTrue(payload["needsLogin"])
        self.assertFalse(payload["ready"])  # widget must stop retrying the mount
        self.assertEqual(payload["statusText"], "iCloud session expired")

    def test_status_with_a_good_session_is_unchanged(self):
        payload = self.status_payload("ok")
        self.assertFalse(payload["sessionExpired"])
        self.assertFalse(payload["needsLogin"])

    def test_start_refuses_before_starting_the_server(self):
        with mock.patch.object(ffd, "cmd_ensure"), \
             mock.patch.object(ffd, "remote_configured", return_value=True), \
             mock.patch.object(ffd, "refresh_session", return_value="expired"), \
             mock.patch.object(ffd, "systemctl") as systemctl, \
             mock.patch.object(ffd, "do_mount") as mount:
            with self.assertRaises(SystemExit):
                ffd.cmd_start([])
            systemctl.assert_not_called()
            mount.assert_not_called()

    def test_offline_unknown_does_not_block_start(self):
        with mock.patch.object(ffd, "refresh_session", return_value="unknown"):
            ffd.require_valid_session()  # must not raise

    def test_mount_refuses_expired_session_without_mounting(self):
        with mock.patch.object(ffd, "service_running", return_value=True), \
             mock.patch.object(ffd, "refresh_session", return_value="expired"), \
             mock.patch.object(ffd, "wait_for_server") as ready, \
             mock.patch.object(ffd, "do_mount") as mount, \
             mock.patch.object(ffd, "ensure_bookmark") as bookmark:
            with self.assertRaises(SystemExit):
                ffd.cmd_mount([])
            ready.assert_not_called()
            mount.assert_not_called()
            bookmark.assert_not_called()  # never re-add the prompting bookmark

    def test_open_with_expired_session_goes_to_sign_in_not_nautilus(self):
        with mock.patch.object(ffd, "refresh_session", return_value="expired"), \
             mock.patch.object(ffd, "cmd_login") as login, \
             mock.patch.object(ffd, "spawn_detached") as spawn, \
             mock.patch.object(ffd, "cmd_start") as start, \
             mock.patch.object(ffd, "do_mount") as mount:
            ffd.cmd_open([])
            login.assert_called_once()
            spawn.assert_not_called()
            start.assert_not_called()
            mount.assert_not_called()

    PCS_ERR = (b"CRITICAL: Failed to create file system for \"icloud:\": requestPCS(iclouddrive): "
               b"HTTP error 500 (500 Internal Server Error) returned body: "
               b"\"{\\\"success\\\":false,\\\"error\\\":\\\"Missing X-APPLE-WEBAUTH-TOKEN cookie\\\"}\"")

    def test_probe_recognises_signed_in_but_no_web_access(self):
        # Real failure after a fresh `rclone config reconnect` on an ADP
        # account (rclone issue #9658): not an expiry, needs different advice.
        with mock.patch.object(ffd, "run_bounded", return_value=self.result(rc=1, err=self.PCS_ERR)):
            self.assertEqual(ffd.probe_session(), "pcs")

    def test_pcs_verdict_is_treated_like_expiry_everywhere(self):
        with mock.patch.object(ffd, "probe_session", return_value="pcs"), \
             mock.patch.object(ffd, "drop_dav_entry") as drop:
            self.assertEqual(ffd.refresh_session(), "pcs")
            drop.assert_called_once()
        with mock.patch.object(ffd, "spawn_detached"):
            self.assertEqual(ffd.cached_session_state(allow_probe=False), "pcs")
        with mock.patch.object(ffd, "refresh_session", return_value="pcs"):
            with self.assertRaises(SystemExit):
                ffd.require_valid_session()

    def test_status_reports_pcs_problem_distinctly(self):
        payload = self.status_payload("pcs")
        self.assertTrue(payload["needsLogin"])
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["sessionProblem"], "pcs")
        self.assertEqual(payload["statusText"], "iCloud sign-in incomplete")
        self.assertEqual(self.status_payload("expired")["sessionProblem"], "expired")
        self.assertEqual(self.status_payload("ok")["sessionProblem"], "")

    def test_failed_start_does_not_leave_a_dead_sidebar_bookmark(self):
        # cmd_ensure runs before the session check in `start`; the bookmark
        # it writes must not appear while the verdict is bad.
        with mock.patch.object(ffd, "probe_session", return_value="pcs"), \
             mock.patch.object(ffd, "drop_dav_entry"):
            ffd.refresh_session()
        with mock.patch.object(ffd, "rewrite_bookmarks") as rewrite:
            ffd.ensure_bookmark()
            rewrite.assert_not_called()

    def test_bookmark_is_written_with_a_good_or_unknown_verdict(self):
        with mock.patch.object(ffd, "rewrite_bookmarks") as rewrite:
            ffd.ensure_bookmark()  # nothing cached yet -> unknown
            rewrite.assert_called_once()
        with mock.patch.object(ffd, "probe_session", return_value="ok"):
            ffd.refresh_session()
        with mock.patch.object(ffd, "rewrite_bookmarks") as rewrite:
            ffd.ensure_bookmark()
            rewrite.assert_called_once()

    def test_new_sign_in_brings_the_bookmark_back(self):
        with mock.patch.object(ffd, "probe_session", return_value="pcs"), \
             mock.patch.object(ffd, "drop_dav_entry"):
            ffd.refresh_session()
        self.write_conf("trust-two")
        with mock.patch.object(ffd, "rewrite_bookmarks") as rewrite:
            ffd.ensure_bookmark()
            rewrite.assert_called_once()

    def test_login_with_no_web_access_explains_and_restarts_nothing(self):
        out = io.StringIO()
        with mock.patch.object(ffd, "remote_configured", return_value=True), \
             mock.patch.object(ffd.subprocess, "run"), \
             mock.patch.object(ffd, "fresh_session_state", return_value="pcs"), \
             mock.patch.object(ffd, "service_running", return_value=True), \
             mock.patch.object(ffd, "systemctl") as systemctl, \
             contextlib.redirect_stdout(out):
            ffd.cmd_login_tui([])
        systemctl.assert_not_called()
        self.assertIn("Access iCloud Data on the Web", out.getvalue())
        self.assertIn("icloud.com", out.getvalue())
        self.assertIn("Terms", out.getvalue())

    # ---- desktop notification on a bad verdict -------------------------

    def refresh(self, verdict, **kw):
        with mock.patch.object(ffd, "probe_session", return_value=verdict), \
             mock.patch.object(ffd, "drop_dav_entry"), \
             mock.patch.object(ffd, "notify_session_problem") as notify:
            ffd.refresh_session(**kw)
        return notify

    def test_bad_verdict_notifies_once_per_credential_and_verdict(self):
        self.assertEqual(self.refresh("pcs").call_count, 1)
        self.assertEqual(self.refresh("pcs").call_count, 0)  # same credential, same problem
        self.assertEqual(self.refresh("expired").call_count, 1)  # different problem

    def test_flaky_probe_does_not_rearm_the_notification(self):
        self.assertEqual(self.refresh("pcs").call_count, 1)
        self.refresh("unknown")
        self.assertEqual(self.refresh("pcs").call_count, 0)

    def test_recovery_rearms_the_notification(self):
        self.refresh("pcs")
        self.refresh("ok")
        self.assertEqual(self.refresh("pcs").call_count, 1)

    def test_new_sign_in_rearms_the_notification(self):
        self.refresh("pcs")
        self.write_conf("trust-two")
        self.assertEqual(self.refresh("pcs").call_count, 1)

    def test_good_and_unknown_verdicts_never_notify(self):
        self.assertEqual(self.refresh("ok").call_count, 0)
        self.assertEqual(self.refresh("unknown").call_count, 0)

    def test_notify_false_stays_quiet_but_counts_as_told(self):
        self.assertEqual(self.refresh("pcs", notify=False).call_count, 0)
        self.assertEqual(self.refresh("pcs").call_count, 0)  # user already saw it in the terminal

    def test_notification_is_fixed_text_run_without_a_shell(self):
        with mock.patch.object(ffd.Tools, "notify_send", "/usr/bin/notify-send"), \
             mock.patch.object(ffd, "run_bounded") as run:
            ffd.notify_session_problem("pcs")
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "/usr/bin/notify-send")
        self.assertIn("--", argv)  # nothing after it can be parsed as an option
        body = " ".join(argv)
        self.assertIn("Terms & Conditions", body)
        self.assertIn("icloud.com", body)
        self.assertLess(run.call_args[1]["timeout"], 10)

    def test_notification_tells_expiry_apart_from_the_terms_problem(self):
        self.assertNotEqual(ffd._SESSION_NOTICES["expired"], ffd._SESSION_NOTICES["pcs"])
        self.assertNotIn("Terms", " ".join(ffd._SESSION_NOTICES["expired"]))

    def test_missing_notify_send_is_harmless(self):
        with mock.patch.object(ffd.Tools, "notify_send", ""), \
             mock.patch.object(ffd, "run_bounded") as run:
            ffd.notify_session_problem("pcs")
            run.assert_not_called()

    def test_unknown_state_has_no_notice(self):
        with mock.patch.object(ffd.Tools, "notify_send", "/usr/bin/notify-send"), \
             mock.patch.object(ffd, "run_bounded") as run:
            ffd.notify_session_problem("unknown")
            ffd.notify_session_problem("ok")
            run.assert_not_called()

    def test_background_probe_notifies(self):
        with mock.patch.object(ffd, "probe_session", return_value="expired"), \
             mock.patch.object(ffd, "drop_dav_entry"), \
             mock.patch.object(ffd, "notify_session_problem") as notify:
            ffd.cmd_check_session([])
        notify.assert_called_once_with("expired")

    def test_start_failure_on_a_bad_session_notifies(self):
        with mock.patch.object(ffd, "cmd_ensure"), \
             mock.patch.object(ffd, "probe_session", return_value="pcs"), \
             mock.patch.object(ffd, "drop_dav_entry"), \
             mock.patch.object(ffd, "notify_session_problem") as notify, \
             mock.patch.object(ffd, "systemctl") as systemctl:
            with self.assertRaises(SystemExit):
                ffd.cmd_start([])
        notify.assert_called_once_with("pcs")
        systemctl.assert_not_called()

    # ---- sign-in terminal ----------------------------------------------

    def run_login(self, states, running, start_side_effect=None):
        out = io.StringIO()
        seq = iter(states)
        with mock.patch.object(ffd, "remote_configured", return_value=True), \
             mock.patch.object(ffd.subprocess, "run"), \
             mock.patch.object(ffd.time, "sleep") as sleep, \
             mock.patch.object(ffd, "fresh_session_state", side_effect=lambda **kw: next(seq)) as probe, \
             mock.patch.object(ffd, "service_running", return_value=running), \
             mock.patch.object(ffd, "systemctl"), \
             mock.patch.object(ffd, "wait_for_server"), \
             mock.patch.object(ffd, "do_mount"), \
             mock.patch.object(ffd, "ensure_bookmark"), \
             mock.patch.object(ffd, "cmd_start", side_effect=start_side_effect) as start, \
             contextlib.redirect_stdout(out):
            ffd.cmd_login_tui([])
        return out.getvalue(), start, probe, sleep

    def test_login_starts_the_drive_when_it_was_off(self):
        text, start, _p, _s = self.run_login(["ok"], running=False)
        start.assert_called_once()
        self.assertIn("is on", text)

    def test_login_reports_when_it_cannot_start_the_drive(self):
        text, _start, _p, _s = self.run_login(["ok"], running=False, start_side_effect=SystemExit(1))
        self.assertIn("could not start automatically", text)

    def test_login_retries_an_inconclusive_check_then_proceeds(self):
        text, start, probe, sleep = self.run_login(["unknown", "unknown", "ok"], running=False)
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        start.assert_called_once()

    def test_login_gives_up_retrying_after_a_few_tries(self):
        _t, start, probe, _s = self.run_login(["unknown"] * 4, running=False)
        self.assertEqual(probe.call_count, 4)
        start.assert_called_once()  # offline is not expiry: still try to start

    def test_login_retry_stops_on_a_definite_bad_answer(self):
        text, start, probe, _s = self.run_login(["unknown", "pcs"], running=False)
        self.assertEqual(probe.call_count, 2)
        start.assert_not_called()
        self.assertIn("Terms", text)

    def test_login_never_notifies_desktop_because_the_terminal_explains(self):
        _t, _start, probe, _s = self.run_login(["pcs"], running=False)
        for call in probe.call_args_list:
            self.assertIs(call.kwargs.get("notify"), False)

    def test_dropping_the_entry_only_unmounts_our_own_uri(self):
        # The reviewer objected to unmounting the generic loopback WebDAV
        # URIs (possibly another app's mount). Expiry must not do that.
        with mock.patch.object(ffd.Tools, "gio", "/usr/bin/gio"), \
             mock.patch.object(ffd, "run_bounded") as run, \
             mock.patch.object(ffd, "remove_bookmark") as bookmark, \
             mock.patch.object(ffd, "unmount_legacy_dav") as legacy, \
             mock.patch.object(ffd, "unmount_dav") as sweep:
            ffd.drop_dav_entry()
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ["/usr/bin/gio", "mount", "-u", ffd.DAV_URI])
        bookmark.assert_called_once()
        legacy.assert_not_called()
        sweep.assert_not_called()

    def test_check_session_command_is_hidden_and_not_supervisable(self):
        self.assertIn("__check-session", ffd.COMMANDS)
        self.assertNotIn("__check-session", ffd.PUBLIC_COMMANDS)
        self.assertNotIn("__check-session", ffd.SUPERVISABLE_COMMANDS)

    def test_login_restarts_a_running_server_with_the_new_session(self):
        calls = []
        with mock.patch.object(ffd, "remote_configured", return_value=True), \
             mock.patch.object(ffd.subprocess, "run"), \
             mock.patch.object(ffd, "fresh_session_state", return_value="ok"), \
             mock.patch.object(ffd, "service_running", return_value=True), \
             mock.patch.object(ffd, "systemctl", side_effect=lambda *a, **k: calls.append(a[0])), \
             mock.patch.object(ffd, "wait_for_server", side_effect=lambda: calls.append("ready")), \
             mock.patch.object(ffd, "do_mount", side_effect=lambda: calls.append("mount")), \
             mock.patch.object(ffd, "ensure_bookmark", side_effect=lambda: calls.append("bookmark")), \
             contextlib.redirect_stdout(io.StringIO()):
            ffd.cmd_login_tui([])
        self.assertEqual(calls, ["restart", "ready", "mount", "bookmark"])

    def test_login_that_iCloud_still_rejects_does_not_restart_anything(self):
        with mock.patch.object(ffd, "remote_configured", return_value=True), \
             mock.patch.object(ffd.subprocess, "run"), \
             mock.patch.object(ffd, "fresh_session_state", return_value="expired"), \
             mock.patch.object(ffd, "service_running", return_value=True), \
             mock.patch.object(ffd, "systemctl") as systemctl, \
             contextlib.redirect_stdout(io.StringIO()):
            ffd.cmd_login_tui([])
        systemctl.assert_not_called()


class ServeArgumentsTests(SandboxedHome):
    def serve_argv(self):
        captured = {}
        saved = ffd.Tools.rclone
        ffd.Tools.rclone = "/usr/bin/rclone"
        try:
            with mock.patch.object(ffd, "remote_configured", return_value=True), \
                 mock.patch.object(ffd, "validate_webdav_auth", return_value="pw"), \
                 mock.patch.object(ffd.os, "execve", side_effect=lambda path, args, env: captured.update(args=args)):
                ffd.cmd_serve([])
        finally:
            ffd.Tools.rclone = saved
        return captured["args"]

    def test_unknown_folder_dates_are_shown_as_the_epoch_not_year_2000(self):
        # iCloud gives folders no modified time; rclone's placeholder is
        # 2000-01-01, which reads as a real (and absurd) date. Nautilus treats
        # exactly 0 as "unknown".
        args = self.serve_argv()
        self.assertEqual(args[args.index("--default-time") + 1], "1970-01-01T00:00:00Z")

    def test_serve_stays_loopback_only_and_authenticated(self):
        args = self.serve_argv()
        self.assertIn("--htpasswd", args)
        addrs = [args[i + 1] for i, a in enumerate(args) if a == "--addr"]
        self.assertEqual(addrs, ["127.0.0.1:8080", "[::1]:8080"])


class ConfigureBookmarkTests(SandboxedHome):
    """The widget runs `configure` every time it loads, to apply its settings.
    That must not put an "iCloud Drive" bookmark in Nautilus for a drive that
    is not running (found by uninstalling and reinstalling the plugin)."""

    ARGS = ["cache_max_size=4G", "cache_max_age_hours=24", "restart=1"]

    def run_configure(self, running, args=None):
        with mock.patch.object(ffd, "service_running", return_value=running), \
             mock.patch.object(ffd, "ensure_bookmark") as bookmark, \
             mock.patch.object(ffd, "cmd_restart") as restart:
            ffd.cmd_configure(args or self.ARGS)
        return bookmark, restart

    def test_stopped_drive_gets_no_bookmark_at_widget_load(self):
        bookmark, restart = self.run_configure(running=False)
        bookmark.assert_not_called()
        restart.assert_not_called()

    def test_running_drive_keeps_its_bookmark(self):
        bookmark, restart = self.run_configure(running=True)  # defaults: nothing changed
        bookmark.assert_called_once()
        restart.assert_not_called()

    def test_changed_setting_on_a_running_drive_restarts_it(self):
        bookmark, restart = self.run_configure(running=True, args=["cache_max_size=8G", "restart=1"])
        restart.assert_called_once()
        bookmark.assert_not_called()  # cmd_restart owns the bookmark

    def test_changed_setting_on_a_stopped_drive_is_saved_without_side_effects(self):
        bookmark, restart = self.run_configure(running=False, args=["cache_max_size=8G", "restart=1"])
        bookmark.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(ffd.load_config().cache_max_size, "8G")


class SuperviseGuardTests(unittest.TestCase):
    def test_serve_and_login_tui_are_not_supervisable(self):
        self.assertNotIn("serve", ffd.SUPERVISABLE_COMMANDS)
        self.assertNotIn("login-tui", ffd.SUPERVISABLE_COMMANDS)
        self.assertNotIn("help", ffd.SUPERVISABLE_COMMANDS)
        self.assertIn("status", ffd.SUPERVISABLE_COMMANDS)
        self.assertIn("start", ffd.SUPERVISABLE_COMMANDS)


if __name__ == "__main__":
    unittest.main()
