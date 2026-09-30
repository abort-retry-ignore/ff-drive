"""Ownership of the fixed-name registrations Fast Fruit Drive installs.

The systemd user units and the Nautilus extension live at fixed names in
directories shared with everything else the user runs. These tests pin the
contract the marketplace review asked for:

* install never overwrites a file it cannot identify as its own, and leaves
  the foreign file byte-for-byte intact (and writes nothing else either);
* removal / disarm / uninstall never delete, disable or stop anything that
  is not ours;
* earlier releases' registrations (no marker) are still recognised, so upgrades
  and removals keep working;
* the helper and the independently installed guard agree on every case.

Everything runs against a scratch HOME below the real home (so the no-follow
ancestor checks see normal permissions). systemd, GVFS and rclone are never
contacted: process execution is replaced by recorders.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import signal
import tempfile
import unittest
from unittest import mock

import registration_fixtures as fx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name: str, path: str):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ffd = _load("ffd_ownership_helper", os.path.join(ROOT, "bin", "fast-fruit-drive"))
guard = _load("ffd_ownership_guard", os.path.join(ROOT, "bin", "fast-fruit-drive-guard.py"))


@contextlib.contextmanager
def no_hang(seconds: int = 5):
    """Fail instead of blocking forever (a FIFO opened without O_NONBLOCK)."""

    def on_alarm(_signum, _frame):
        raise AssertionError("operation blocked; a planted FIFO must not hang it")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class Sandbox(unittest.TestCase):
    """Points the helper and the guard at one scratch account home."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ffd-own-test-", dir=os.path.expanduser("~"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, 0o700)

        home = self.home
        patches = {
            "HOME": home,
            "CONFIG_DIR": os.path.join(home, ".config", "fast-fruit-drive"),
            "CACHE_DIR": os.path.join(home, ".cache", "fast-fruit-drive"),
            "UNIT_DIR": os.path.join(home, ".config", "systemd", "user"),
            "WANTS_DIR": os.path.join(home, ".config", "systemd", "user", "default.target.wants"),
            "BOOKMARKS_DIR": os.path.join(home, ".config", "gtk-3.0"),
            "RCLONE_CONF_DIR": os.path.join(home, ".config", "rclone"),
            "EXTENSION_DIR": os.path.join(home, ".local", "share", "nautilus-python", "extensions"),
            "PLUGIN_CHECKOUT": os.path.join(home, ffd.PLUGIN_REL),
            "GUARD_INSTALLED": os.path.join(home, ".config", "fast-fruit-drive", ffd.GUARD_INSTALLED_NAME),
        }
        for attr, value in patches.items():
            self.patch(ffd, attr, value)
        self.patch(guard, "HOME", home)
        self.patch(ffd.Tools, "systemctl", "/usr/bin/systemctl")

        # Record every process the code would have run; capture its diagnostics.
        self.commands: list[list[str]] = []
        self.patch(
            ffd, "run_bounded",
            side_effect=lambda argv, **_kw: self.commands.append(list(argv)) or ffd.BoundedResult(0, b"", b""),
        )
        self.patch(
            guard, "run_tool",
            side_effect=lambda path, args, **_kw: self.commands.append([path, *args]) or guard.Result(0, b"", b""),
        )
        self.stderr = io.StringIO()
        self.patch(ffd.sys, "stderr", self.stderr)

        self.unit_dir = ffd.UNIT_DIR
        self.wants_dir = ffd.WANTS_DIR
        self.ext_dir = ffd.EXTENSION_DIR
        self.guard_path = ffd.GUARD_INSTALLED
        self.checkout = ffd.PLUGIN_CHECKOUT

    def patch(self, target, attr, *args, **kwargs):
        patcher = mock.patch.object(target, attr, *args, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    # -- filesystem helpers -------------------------------------------------
    def put(self, directory: str, name: str, data, mode: int = 0o644) -> str:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        os.chmod(path, mode)
        return path

    def read(self, path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    def enable_link(self, name: str, target: "str | None" = None) -> str:
        """The enablement symlink `systemctl --user enable` creates."""
        os.makedirs(self.wants_dir, mode=0o700, exist_ok=True)
        link = os.path.join(self.wants_dir, name)
        os.symlink(target or os.path.join(self.unit_dir, name), link)
        return link

    def make_checkout(self, extension: bytes = fx.CURRENT_EXTENSION) -> None:
        """The plugin checkout the helper reads its guard/extension assets from."""
        os.makedirs(os.path.join(self.checkout, "bin"), mode=0o700)
        os.makedirs(os.path.join(self.checkout, "nautilus"), mode=0o700)
        self.put(self.checkout, "manifest.json", '{"id": "%s"}' % ffd.PLUGIN_ID, 0o600)
        with open(os.path.join(ROOT, "bin", ffd.GUARD_SOURCE_NAME), "rb") as fh:
            self.put(os.path.join(self.checkout, "bin"), ffd.GUARD_SOURCE_NAME, fh.read(), 0o700)
        self.put(os.path.join(self.checkout, "nautilus"), fx.EXTENSION, extension, 0o600)

    def current_units(self) -> "dict[str, str]":
        return fx.current_units(self.guard_path, self.checkout)

    def unit_paths(self) -> "dict[str, str]":
        return {name: os.path.join(self.unit_dir, name) for name in ffd.UNIT_FILES}

    def commands_naming(self, unit: str) -> "list[list[str]]":
        return [argv for argv in self.commands if unit in argv]


# ---------------------------------------------------------------------------
# Identification: what counts as "ours"

class IdentificationTests(Sandbox):
    def test_every_unit_the_helper_writes_carries_the_marker_and_is_ours(self):
        ffd.write_unit()
        for name, path in self.unit_paths().items():
            data = self.read(path)
            self.assertEqual(data.decode().splitlines()[0], fx.MARKER, name)
            self.assertTrue(ffd.unit_is_ours(name, data), name)
            self.assertTrue(guard.unit_is_ours(name, data), name)

    def test_the_shipped_extension_source_starts_with_the_marker(self):
        with open(os.path.join(ROOT, "nautilus", fx.EXTENSION), "rb") as fh:
            source = fh.read()
        self.assertTrue(source.startswith(fx.MARKER.encode() + b"\n"))
        self.assertTrue(ffd.extension_is_ours(source))
        self.assertTrue(guard.extension_is_ours(source))

    def test_legacy_registrations_are_recognised(self):
        legacy_020 = fx.legacy_020_units(os.path.join(self.checkout, "bin", "fast-fruit-drive"))
        legacy_021 = fx.legacy_021_units(self.guard_path, self.checkout)
        for label, units in (("0.2.0", legacy_020), ("0.2.1", legacy_021)):
            for name, text in units.items():
                self.assertTrue(ffd.unit_is_ours(name, text.encode()), f"{label} {name}")
                self.assertTrue(guard.unit_is_ours(name, text.encode()), f"{label} {name}")
        self.assertTrue(ffd.extension_is_ours(fx.LEGACY_EXTENSION))
        self.assertTrue(guard.extension_is_ours(fx.LEGACY_EXTENSION))

    def test_helper_and_guard_agree_and_reject_hostile_and_near_miss_units(self):
        current = self.current_units()
        service = current[fx.SERVICE]
        foreign = [
            (fx.SERVICE, fx.FOREIGN_UNIT.encode()),
            (fx.SERVICE, b""),
            (fx.SERVICE, b"\xff\xfe\x00 not utf-8"),
            # Same name and description, but running somebody else's binary.
            (fx.SERVICE, service.replace(f'"{self.guard_path}"', '"/tmp/evil"').encode()),
            # Explicitly claimed by another plugin.
            (fx.SERVICE, service.replace(fx.MARKER, "# Managed-By: com.example.other").encode()),
            # Our guard, but under a different account's home.
            (fx.SERVICE, service.replace(self.home, "/home/someone-else").encode()),
            # Legacy shape that runs an unrelated program.
            (fx.SERVICE, fx.legacy_020_units("/usr/bin/other-tool")[fx.SERVICE].encode()),
            # Legacy shape with the right ExecStart but not our log identity.
            (fx.SERVICE, fx.legacy_020_units("/old/bin/fast-fruit-drive")[fx.SERVICE]
             .replace("SyslogIdentifier=fast-fruit-drive\n", "").encode()),
            # A relative ExecStart is never one this plugin generated.
            (fx.SERVICE, fx.legacy_020_units("fast-fruit-drive")[fx.SERVICE].encode()),
            # Right content, wrong file name.
            (fx.LIFECYCLE_PATH, service.encode()),
            (fx.LIFECYCLE_PATH, current[fx.LIFECYCLE_PATH].replace(self.checkout, "/elsewhere").encode()),
            (fx.LIFECYCLE_SERVICE, current[fx.LIFECYCLE_SERVICE].replace("lifecycle-check", "run-anything").encode()),
            ("some-other.service", service.encode()),
        ]
        for name, data in foreign:
            self.assertFalse(ffd.unit_is_ours(name, data), (name, data[:60]))
            self.assertFalse(guard.unit_is_ours(name, data), (name, data[:60]))
        for name, text in current.items():
            self.assertTrue(ffd.unit_is_ours(name, text.encode()), name)
            self.assertTrue(guard.unit_is_ours(name, text.encode()), name)

    def test_helper_and_guard_agree_on_extensions(self):
        foreign = [
            fx.FOREIGN_EXTENSION,
            b"",
            b"\xff\xfe",
            b"# Managed-By: com.example.other\n" + fx.LEGACY_EXTENSION,
            b"import os\n" + fx.CURRENT_EXTENSION,  # marker must be the first line
        ]
        for data in foreign:
            self.assertFalse(ffd.extension_is_ours(data), data[:40])
            self.assertFalse(guard.extension_is_ours(data), data[:40])
        for data in (fx.CURRENT_EXTENSION, fx.LEGACY_EXTENSION):
            self.assertTrue(ffd.extension_is_ours(data), data[:40])
            self.assertTrue(guard.extension_is_ours(data), data[:40])

    def test_bookmark_identity_needs_an_exact_uri_not_a_label(self):
        ours = [
            f"{ffd.DAV_URI} iCloud Drive",
            f"{ffd.DAV_URI} A label the user renamed it to",
            f"{ffd.LEGACY_URI_NO_USER} iCloud Drive",
            f"{ffd.LEGACY_URI_OLD_HOST_AUTH} iCloud WebDAV",
            f"{ffd.LEGACY_URI_OLD_HOST} iCloud Drive",
        ]
        foreign = [
            "dav://nas.local/remote.php/dav iCloud Drive",        # someone else's server, same label
            "davs://example.com/ iCloud Drive",
            f"{ffd.LEGACY_URI_OLD_HOST} My local WebDAV",         # generic loopback URI, other label
            f"{ffd.LEGACY_URI_OLD_HOST}other/path iCloud Drive",   # not the exact URI
            "file:///home/x iCloud Drive",
            "smb://server/share",
            "",
        ]
        for line in ours:
            self.assertTrue(ffd._bookmark_is_ours(line), line)
            self.assertTrue(guard._bookmark_is_ours(line), line)
        for line in foreign:
            self.assertFalse(ffd._bookmark_is_ours(line), line)
            self.assertFalse(guard._bookmark_is_ours(line), line)


# ---------------------------------------------------------------------------
# Install: never overwrite what we did not write

class InstallUnitTests(Sandbox):
    def test_fresh_install_writes_all_three_units_with_the_marker(self):
        ffd.write_unit()
        for name, path in self.unit_paths().items():
            self.assertTrue(self.read(path).startswith(fx.MARKER.encode() + b"\n"), name)

    def test_reinstall_replaces_our_own_units(self):
        for name, text in self.current_units().items():
            self.put(self.unit_dir, name, text + "# stale\n")
        ffd.write_unit()
        for name, path in self.unit_paths().items():
            self.assertNotIn(b"# stale", self.read(path), name)
            self.assertTrue(ffd.unit_is_ours(name, self.read(path)), name)

    def test_upgrade_replaces_0_2_0_and_0_2_1_units_and_adds_the_marker(self):
        for legacy in (
            fx.legacy_020_units("/old/checkout/bin/fast-fruit-drive"),
            fx.legacy_021_units(self.guard_path, self.checkout),
        ):
            shutil.rmtree(self.unit_dir, ignore_errors=True)
            for name, text in legacy.items():
                self.put(self.unit_dir, name, text)
            ffd.write_unit()
            for name, path in self.unit_paths().items():
                self.assertTrue(self.read(path).startswith(fx.MARKER.encode()), name)

    def assert_refused_and_untouched(self, present: "list[str]"):
        """write_unit() must refuse, change nothing, and not contact systemd."""
        before = {name: self.read(os.path.join(self.unit_dir, name)) for name in present}
        self.commands.clear()
        with self.assertRaises(SystemExit):
            ffd.write_unit()
        for name, data in before.items():
            self.assertEqual(self.read(os.path.join(self.unit_dir, name)), data, f"{name} was modified")
        # Refusal is all-or-nothing: units that were absent are not created.
        for name in ffd.UNIT_FILES:
            if name not in before:
                self.assertFalse(os.path.lexists(os.path.join(self.unit_dir, name)), f"{name} was created")
        self.assertEqual(self.commands, [], "systemd must not be contacted after a refusal")
        self.assertIn("not installed by Fast Fruit Drive", self.stderr.getvalue())

    def test_foreign_service_unit_is_refused_and_preserved(self):
        self.put(self.unit_dir, fx.SERVICE, fx.FOREIGN_UNIT)
        self.assert_refused_and_untouched([fx.SERVICE])

    def test_foreign_lifecycle_unit_blocks_the_whole_install(self):
        self.put(self.unit_dir, fx.SERVICE, self.current_units()[fx.SERVICE])
        self.put(self.unit_dir, fx.LIFECYCLE_PATH, fx.FOREIGN_UNIT)
        self.assert_refused_and_untouched([fx.SERVICE, fx.LIFECYCLE_PATH])

    def test_unit_claimed_by_another_plugin_is_refused(self):
        text = self.current_units()[fx.SERVICE].replace(fx.MARKER, "# Managed-By: com.example.other")
        self.put(self.unit_dir, fx.SERVICE, text)
        self.assert_refused_and_untouched([fx.SERVICE])

    def test_symlinked_unit_is_refused_and_the_link_and_target_survive(self):
        target = self.put(self.tmp, "dotfiles-service", self.current_units()[fx.SERVICE])
        os.makedirs(self.unit_dir, mode=0o700)
        link = os.path.join(self.unit_dir, fx.SERVICE)
        os.symlink(target, link)
        before = self.read(target)
        with self.assertRaises(SystemExit):
            ffd.write_unit()
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), target)
        self.assertEqual(self.read(target), before)

    def test_fifo_at_a_unit_name_is_refused_without_hanging(self):
        os.makedirs(self.unit_dir, mode=0o700)
        path = os.path.join(self.unit_dir, fx.SERVICE)
        os.mkfifo(path, 0o600)
        with no_hang(), self.assertRaises(SystemExit):
            ffd.write_unit()
        self.assertTrue(os.path.exists(path) and not os.path.isfile(path))

    def test_world_writable_and_oversized_units_are_refused(self):
        ours = self.current_units()[fx.SERVICE]
        path = self.put(self.unit_dir, fx.SERVICE, ours, mode=0o666)
        with self.assertRaises(SystemExit):
            ffd.write_unit()
        self.assertEqual(self.read(path), ours.encode())
        os.chmod(path, 0o644)
        with open(path, "ab") as fh:
            fh.write(b"# pad\n" * 20_000)  # > 64 KiB
        before = self.read(path)
        with self.assertRaises(SystemExit):
            ffd.write_unit()
        self.assertEqual(self.read(path), before)

    def test_check_registration_ownership_covers_units_and_the_extension(self):
        self.put(self.ext_dir, fx.EXTENSION, fx.FOREIGN_EXTENSION)
        with self.assertRaises(SystemExit):
            ffd.check_registration_ownership()
        self.put(self.ext_dir, fx.EXTENSION, fx.CURRENT_EXTENSION)
        ffd.check_registration_ownership()  # ours: fine
        self.put(self.unit_dir, fx.LIFECYCLE_SERVICE, fx.FOREIGN_UNIT)
        with self.assertRaises(SystemExit):
            ffd.check_registration_ownership()

    def test_ensure_refuses_early_on_a_foreign_extension(self):
        self.put(self.ext_dir, fx.EXTENSION, fx.FOREIGN_EXTENSION)
        with mock.patch.object(ffd, "require_packages"), self.assertRaises(SystemExit):
            ffd.cmd_ensure([])
        # Nothing else was set up before the refusal.
        self.assertFalse(os.path.exists(self.unit_dir))
        self.assertFalse(os.path.exists(ffd.CONFIG_DIR))
        self.assertEqual(self.read(os.path.join(self.ext_dir, fx.EXTENSION)), fx.FOREIGN_EXTENSION)
        self.assertEqual(self.commands, [])

    def test_migration_leaves_a_foreign_unit_alone_and_migrates_a_legacy_one(self):
        self.make_checkout()
        path = self.put(self.unit_dir, fx.SERVICE, fx.FOREIGN_UNIT)
        ffd.cmd_migrate_lifecycle([])
        self.assertEqual(self.read(path), fx.FOREIGN_UNIT.encode())
        self.assertEqual(self.commands, [])
        self.assertFalse(os.path.exists(os.path.join(self.unit_dir, fx.LIFECYCLE_PATH)))

        self.put(self.unit_dir, fx.SERVICE, fx.legacy_020_units("/old/bin/fast-fruit-drive")[fx.SERVICE])
        ffd.cmd_migrate_lifecycle([])
        self.assertTrue(self.read(path).startswith(fx.MARKER.encode()))


class InstallExtensionTests(Sandbox):
    def ext_path(self) -> str:
        return os.path.join(self.ext_dir, fx.EXTENSION)

    def test_fresh_install_copies_the_marked_source(self):
        self.make_checkout()
        ffd.install_nautilus_extension()
        self.assertEqual(self.read(self.ext_path()), fx.CURRENT_EXTENSION)

    def test_upgrade_replaces_a_legacy_unmarked_extension(self):
        self.make_checkout()
        self.put(self.ext_dir, fx.EXTENSION, fx.LEGACY_EXTENSION)
        ffd.install_nautilus_extension()
        self.assertEqual(self.read(self.ext_path()), fx.CURRENT_EXTENSION)

    def test_foreign_extension_is_refused_and_preserved(self):
        self.make_checkout()
        self.put(self.ext_dir, fx.EXTENSION, fx.FOREIGN_EXTENSION)
        with self.assertRaises(SystemExit):
            ffd.install_nautilus_extension()
        self.assertEqual(self.read(self.ext_path()), fx.FOREIGN_EXTENSION)
        self.assertIn("not installed by Fast Fruit Drive", self.stderr.getvalue())

    def test_symlinked_extension_is_refused_and_preserved(self):
        self.make_checkout()
        target = self.put(self.tmp, "my-extension.py", fx.FOREIGN_EXTENSION)
        os.makedirs(self.ext_dir, mode=0o700)
        os.symlink(target, self.ext_path())
        with self.assertRaises(SystemExit):
            ffd.install_nautilus_extension()
        self.assertEqual(os.readlink(self.ext_path()), target)
        self.assertEqual(self.read(target), fx.FOREIGN_EXTENSION)

    def test_fifo_at_the_extension_name_is_refused_without_hanging(self):
        self.make_checkout()
        os.makedirs(self.ext_dir, mode=0o700)
        os.mkfifo(self.ext_path(), 0o600)
        with no_hang(), self.assertRaises(SystemExit):
            ffd.install_nautilus_extension()

    def test_a_source_without_the_marker_is_never_installed(self):
        self.make_checkout(extension=fx.LEGACY_EXTENSION)
        with self.assertRaises(SystemExit):
            ffd.install_nautilus_extension()
        self.assertFalse(os.path.exists(self.ext_path()))


# ---------------------------------------------------------------------------
# Runtime control: never query or stop a unit that is not ours

class RuntimeControlTests(Sandbox):
    def test_foreign_unit_is_never_queried_stopped_or_restarted(self):
        self.put(self.unit_dir, fx.SERVICE, fx.FOREIGN_UNIT)
        self.assertFalse(ffd.service_unit_owned())
        self.assertFalse(ffd.service_running())
        self.assertIsNone(ffd.service_main_pid())
        for verb in ("start", "stop", "restart"):
            self.assertFalse(ffd.systemctl(verb, ffd.UNIT_NAME).ok, verb)
        self.assertEqual(self.commands, [], "no systemctl call may reach a foreign unit")

    def test_our_unit_is_controlled_normally(self):
        self.put(self.unit_dir, fx.SERVICE, self.current_units()[fx.SERVICE])
        self.assertTrue(ffd.service_unit_owned())
        ffd.service_running()
        ffd.systemctl("stop", ffd.UNIT_NAME)
        self.assertEqual(len(self.commands), 2)
        self.assertIn("is-active", self.commands[0])
        self.assertEqual(self.commands[1][-2:], ["stop", ffd.UNIT_NAME])

    def test_a_missing_unit_is_not_ours_either(self):
        self.assertFalse(ffd.service_running())
        self.assertEqual(self.commands, [])

    def test_uninstall_never_stops_or_disables_a_foreign_service(self):
        foreign = self.put(self.unit_dir, fx.SERVICE, fx.FOREIGN_UNIT)
        self.enable_link(fx.SERVICE)
        with mock.patch.object(ffd, "unmount_dav"), mock.patch.object(ffd, "cache_has_dirty_uploads", return_value=False):
            ffd.cmd_uninstall([])
        self.assertEqual(self.read(foreign), fx.FOREIGN_UNIT.encode())
        self.assertTrue(os.path.islink(os.path.join(self.wants_dir, fx.SERVICE)))
        self.assertEqual(self.commands_naming(fx.SERVICE), [])


# ---------------------------------------------------------------------------
# Removal: only what we wrote

class HelperRemovalTests(Sandbox):
    def install_ours(self, *, with_links: bool = True):
        for name, text in self.current_units().items():
            self.put(self.unit_dir, name, text)
        if with_links:
            for name in ffd.ENABLED_UNITS:
                self.enable_link(name)

    def test_removes_our_units_enablement_links_and_state(self):
        self.install_ours()
        ffd.remove_unit_registrations()
        for path in self.unit_paths().values():
            self.assertFalse(os.path.lexists(path), path)
        for name in ffd.ENABLED_UNITS:
            self.assertFalse(os.path.lexists(os.path.join(self.wants_dir, name)), name)
        self.assertIn(["/usr/bin/systemctl", "--user", "disable", "--now", *ffd.ENABLED_UNITS], self.commands)
        self.assertIn(["/usr/bin/systemctl", "--user", "reset-failed", ffd.UNIT_NAME], self.commands)
        self.assertIn(["/usr/bin/systemctl", "--user", "daemon-reload"], self.commands)

    def test_removes_legacy_units_too(self):
        for name, text in fx.legacy_021_units(self.guard_path, self.checkout).items():
            self.put(self.unit_dir, name, text)
        ffd.remove_unit_registrations()
        for path in self.unit_paths().values():
            self.assertFalse(os.path.lexists(path), path)

    def test_a_foreign_service_unit_is_kept_and_its_systemd_state_untouched(self):
        self.install_ours()
        foreign = self.put(self.unit_dir, fx.SERVICE, fx.FOREIGN_UNIT)
        ffd.remove_unit_registrations()
        # Our other two units are gone; the foreign one is intact and still enabled.
        self.assertFalse(os.path.lexists(os.path.join(self.unit_dir, fx.LIFECYCLE_SERVICE)))
        self.assertFalse(os.path.lexists(os.path.join(self.unit_dir, fx.LIFECYCLE_PATH)))
        self.assertEqual(self.read(foreign), fx.FOREIGN_UNIT.encode())
        self.assertTrue(os.path.islink(os.path.join(self.wants_dir, fx.SERVICE)))
        self.assertEqual(self.commands_naming(fx.SERVICE), [], "the foreign unit must not be disabled, stopped or reset")
        self.assertIn(["/usr/bin/systemctl", "--user", "disable", "--now", fx.LIFECYCLE_PATH], self.commands)
        self.assertIn("not installed by Fast Fruit Drive", self.stderr.getvalue())

    def test_nothing_is_touched_when_nothing_is_ours(self):
        for name in ffd.UNIT_FILES:
            self.put(self.unit_dir, name, fx.FOREIGN_UNIT)
        for name in ffd.ENABLED_UNITS:
            self.enable_link(name)
        ffd.remove_unit_registrations()
        for path in self.unit_paths().values():
            self.assertEqual(self.read(path), fx.FOREIGN_UNIT.encode())
        for name in ffd.ENABLED_UNITS:
            self.assertTrue(os.path.islink(os.path.join(self.wants_dir, name)))
        self.assertEqual(self.commands, [])

    def test_only_symlinks_that_point_at_our_unit_file_are_removed(self):
        self.install_ours(with_links=False)
        elsewhere = self.enable_link(fx.SERVICE, target="/somewhere/else/fast-fruit-drive.service")
        relative = self.enable_link(fx.LIFECYCLE_PATH, target="../" + fx.LIFECYCLE_PATH)
        ffd.remove_unit_registrations()
        self.assertTrue(os.path.islink(elsewhere), "a link to another target is not ours to remove")
        self.assertFalse(os.path.lexists(relative))

    def test_a_planted_regular_file_in_wants_is_not_deleted(self):
        self.install_ours(with_links=False)
        planted = self.put(self.wants_dir, fx.SERVICE, "user data")
        ffd.remove_unit_registrations()
        self.assertEqual(self.read(planted), b"user data")

    def test_symlinked_and_fifo_units_are_left_alone(self):
        target = self.put(self.tmp, "real-unit", self.current_units()[fx.LIFECYCLE_SERVICE])
        os.makedirs(self.unit_dir, mode=0o700)
        os.symlink(target, os.path.join(self.unit_dir, fx.LIFECYCLE_SERVICE))
        os.mkfifo(os.path.join(self.unit_dir, fx.LIFECYCLE_PATH), 0o600)
        with no_hang():
            ffd.remove_unit_registrations()
        self.assertTrue(os.path.islink(os.path.join(self.unit_dir, fx.LIFECYCLE_SERVICE)))
        self.assertTrue(os.path.exists(target))
        self.assertTrue(os.path.exists(os.path.join(self.unit_dir, fx.LIFECYCLE_PATH)))

    def test_extension_removal_deletes_ours_and_its_bytecode(self):
        path = self.put(self.ext_dir, fx.EXTENSION, fx.CURRENT_EXTENSION)
        cache = os.path.join(self.ext_dir, "__pycache__")
        pyc = self.put(cache, "fast_fruit_drive_nautilus.cpython-313.pyc", b"pyc")
        other = self.put(cache, "someone_else.cpython-313.pyc", b"keep")
        ffd.remove_nautilus_extension()
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(pyc))
        self.assertTrue(os.path.exists(other))

    def test_extension_removal_keeps_a_foreign_file_and_its_bytecode(self):
        path = self.put(self.ext_dir, fx.EXTENSION, fx.FOREIGN_EXTENSION)
        pyc = self.put(os.path.join(self.ext_dir, "__pycache__"), "fast_fruit_drive_nautilus.cpython-313.pyc", b"pyc")
        ffd.remove_nautilus_extension()
        self.assertEqual(self.read(path), fx.FOREIGN_EXTENSION)
        self.assertTrue(os.path.exists(pyc))

    def test_bookmark_rewrite_only_drops_our_lines(self):
        keep = [
            "file:///home/user/Documents Documents",
            "dav://nas.local/remote.php/dav iCloud Drive",
            f"{ffd.LEGACY_URI_OLD_HOST} My local WebDAV",
        ]
        bookmarks = os.path.join(ffd.BOOKMARKS_DIR, "bookmarks")
        self.put(ffd.BOOKMARKS_DIR, "bookmarks",
                 "\n".join([keep[0], f"{ffd.DAV_URI} iCloud Drive", keep[1],
                            f"{ffd.LEGACY_URI_NO_USER} iCloud WebDAV", keep[2]]) + "\n", 0o600)
        ffd.remove_bookmark()
        self.assertEqual(self.read(bookmarks).decode().splitlines(), keep)
        ffd.ensure_bookmark()
        self.assertEqual(self.read(bookmarks).decode().splitlines(), keep + [f"{ffd.DAV_URI} {ffd.DISPLAY_NAME}"])


class GuardRemovalTests(Sandbox):
    def guard_units(self):
        return fx.current_units(guard.guard_path(), guard.plugin_checkout())

    def install_ours(self, units=None, *, with_links: bool = True):
        for name, text in (units or self.guard_units()).items():
            self.put(self.unit_dir, name, text)
        if with_links:
            for name in guard.ENABLED_UNITS:
                self.enable_link(name)

    def test_guard_paths_match_the_helpers(self):
        self.assertEqual(guard.guard_path(), self.guard_path)
        self.assertEqual(guard.plugin_checkout(), self.checkout)

    def test_cleanup_removes_our_registrations(self):
        self.install_ours()
        self.put(self.ext_dir, fx.EXTENSION, fx.CURRENT_EXTENSION)
        guard.cleanup("remove")
        for path in self.unit_paths().values():
            self.assertFalse(os.path.lexists(path), path)
        for name in guard.ENABLED_UNITS:
            self.assertFalse(os.path.lexists(os.path.join(self.wants_dir, name)))
        self.assertFalse(os.path.exists(os.path.join(self.ext_dir, fx.EXTENSION)))
        self.assertIn([guard.SYSTEMCTL, "--user", "disable", "--now", *guard.ENABLED_UNITS], self.commands)

    def test_cleanup_removes_units_written_by_0_2_1(self):
        self.install_ours(fx.legacy_021_units(guard.guard_path(), guard.plugin_checkout()))
        self.put(self.ext_dir, fx.EXTENSION, fx.LEGACY_EXTENSION)
        guard.cleanup("remove")
        for path in self.unit_paths().values():
            self.assertFalse(os.path.lexists(path), path)
        self.assertFalse(os.path.exists(os.path.join(self.ext_dir, fx.EXTENSION)))

    def test_cleanup_leaves_foreign_registrations_and_their_systemd_state_alone(self):
        self.install_ours()
        foreign_unit = self.put(self.unit_dir, fx.SERVICE, fx.FOREIGN_UNIT)
        foreign_ext = self.put(self.ext_dir, fx.EXTENSION, fx.FOREIGN_EXTENSION)
        guard.cleanup("remove")
        self.assertEqual(self.read(foreign_unit), fx.FOREIGN_UNIT.encode())
        self.assertEqual(self.read(foreign_ext), fx.FOREIGN_EXTENSION)
        self.assertTrue(os.path.islink(os.path.join(self.wants_dir, fx.SERVICE)))
        self.assertEqual(self.commands_naming(fx.SERVICE), [])
        # Our own lifecycle units were still cleaned up.
        self.assertFalse(os.path.lexists(os.path.join(self.unit_dir, fx.LIFECYCLE_PATH)))
        self.assertFalse(os.path.lexists(os.path.join(self.unit_dir, fx.LIFECYCLE_SERVICE)))

    def test_cleanup_with_nothing_of_ours_makes_no_systemctl_calls(self):
        for name in guard.UNIT_FILES:
            self.put(self.unit_dir, name, fx.FOREIGN_UNIT)
        guard.cleanup("disable")
        for path in self.unit_paths().values():
            self.assertEqual(self.read(path), fx.FOREIGN_UNIT.encode())
        self.assertEqual([argv for argv in self.commands if argv[0] == guard.SYSTEMCTL], [])

    def test_only_symlinks_that_point_at_our_unit_file_are_removed(self):
        self.install_ours(with_links=False)
        elsewhere = self.enable_link(fx.SERVICE, target="/somewhere/else/fast-fruit-drive.service")
        relative = self.enable_link(fx.LIFECYCLE_PATH, target="../" + fx.LIFECYCLE_PATH)
        guard.cleanup("remove")
        self.assertTrue(os.path.islink(elsewhere))
        self.assertFalse(os.path.lexists(relative))

    def test_symlink_and_fifo_registrations_are_left_alone_without_hanging(self):
        target = self.put(self.tmp, "real-unit", self.guard_units()[fx.LIFECYCLE_SERVICE])
        os.makedirs(self.unit_dir, mode=0o700)
        os.symlink(target, os.path.join(self.unit_dir, fx.LIFECYCLE_SERVICE))
        os.makedirs(self.ext_dir, mode=0o700)
        os.mkfifo(os.path.join(self.ext_dir, fx.EXTENSION), 0o600)
        with no_hang():
            guard.cleanup("remove")
        self.assertTrue(os.path.islink(os.path.join(self.unit_dir, fx.LIFECYCLE_SERVICE)))
        self.assertTrue(os.path.exists(target))
        self.assertTrue(os.path.exists(os.path.join(self.ext_dir, fx.EXTENSION)))

    def test_extension_bytecode_is_kept_when_the_extension_is_foreign(self):
        self.put(self.ext_dir, fx.EXTENSION, fx.FOREIGN_EXTENSION)
        pyc = self.put(os.path.join(self.ext_dir, "__pycache__"), "fast_fruit_drive_nautilus.cpython-313.pyc", b"pyc")
        guard.remove_extension()
        self.assertTrue(os.path.exists(pyc))

    def test_disarm_only_disables_units_that_are_ours(self):
        guard.disarm_unit()
        self.assertEqual(self.commands, [], "no unit directory: nothing to disarm")

        self.put(self.unit_dir, fx.SERVICE, fx.FOREIGN_UNIT)
        guard.disarm_unit()
        self.assertEqual(self.commands, [], "a foreign unit must not be disabled")

        for name, text in self.guard_units().items():
            self.put(self.unit_dir, name, text)
        self.put(self.unit_dir, fx.SERVICE, fx.FOREIGN_UNIT)
        guard.disarm_unit()
        self.assertEqual(self.commands, [[guard.SYSTEMCTL, "--user", "disable", fx.LIFECYCLE_PATH]])

        self.commands.clear()
        self.put(self.unit_dir, fx.SERVICE, self.guard_units()[fx.SERVICE])
        guard.disarm_unit()
        self.assertEqual(self.commands, [[guard.SYSTEMCTL, "--user", "disable", *guard.ENABLED_UNITS]])

    def test_bookmark_cleanup_keeps_foreign_bookmarks(self):
        keep = ["dav://nas.local/dav iCloud Drive", f"{guard.LEGACY_DAV_URIS[2]} My local WebDAV"]
        self.put(ffd.BOOKMARKS_DIR, "bookmarks",
                 "\n".join([keep[0], f"{guard.DAV_URI} iCloud Drive", keep[1],
                            f"{guard.LEGACY_DAV_URIS[0]} iCloud Drive"]) + "\n", 0o600)
        guard.remove_bookmark()
        self.assertEqual(self.read(os.path.join(ffd.BOOKMARKS_DIR, "bookmarks")).decode().splitlines(), keep)


if __name__ == "__main__":
    unittest.main()
