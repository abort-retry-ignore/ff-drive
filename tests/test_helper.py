"""Regression tests for bin/fast-fruit-drive.

These tests sandbox all filesystem state under a temporary directory (never
the real account home) and never touch systemd, rclone, or GVFS. Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

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
                "BOOKMARKS_DIR", "RCLONE_CONF_DIR", "EXTENSION_DIR",
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


class SuperviseGuardTests(unittest.TestCase):
    def test_serve_and_login_tui_are_not_supervisable(self):
        self.assertNotIn("serve", ffd.SUPERVISABLE_COMMANDS)
        self.assertNotIn("login-tui", ffd.SUPERVISABLE_COMMANDS)
        self.assertNotIn("help", ffd.SUPERVISABLE_COMMANDS)
        self.assertIn("status", ffd.SUPERVISABLE_COMMANDS)
        self.assertIn("start", ffd.SUPERVISABLE_COMMANDS)


if __name__ == "__main__":
    unittest.main()
