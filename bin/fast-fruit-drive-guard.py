#!/usr/bin/python3 -I
"""Independent lifecycle guard for Fast Fruit Drive.

The enabled user unit executes this copy after ``ensure`` installs it under
``~/.config/fast-fruit-drive/lifecycle-guard``; it never executes the
marketplace checkout directly. Before dispatching the checkout helper the
guard opens the fixed checkout component-by-component with ``O_NOFOLLOW``,
checks its manifest's exact plugin ID, and pins the helper by file descriptor.

It also owns conservative disable/removal cleanup. Omarchy disables an enabled
plugin before deleting its checkout. The manifest service launches ``lifecycle-check``
at that transition; the guard waits for removal to settle, then removes only
Fast Fruit Drive's registrations. Cache, configuration, credentials, and
rclone's own configuration are deliberately preserved: any cache could contain
pending uploads.

This file is intentionally stdlib-only and self-contained. It must still work
after the marketplace checkout has gone away, so it does not import the main
helper or resolve any executable from that checkout.
"""

from __future__ import annotations

import json
import os
import pwd
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Iterable

APP = "fast-fruit-drive"
PLUGIN_ID = "io.github.abort-retry-ignore.ff-drive"
UNIT_NAME = "fast-fruit-drive.service"
LIFECYCLE_SERVICE_NAME = "fast-fruit-drive-lifecycle.service"
LIFECYCLE_PATH_NAME = "fast-fruit-drive-lifecycle.path"

# These names are fixed identities, never settings or arguments.
HELPER_NAME = "fast-fruit-drive"
MANIFEST_NAME = "manifest.json"
GUARD_NAME = "lifecycle-guard"
DAV_URI = "dav://ff-drive@iCloud.localhost:8080/"
LEGACY_DAV_URIS = (
    "dav://iCloud.localhost:8080/",
    "dav://ff-drive@127.0.0.1:8080/",
    "dav://127.0.0.1:8080/",
)
DISPLAY_NAME = "iCloud Drive"
EXTENSION_NAME = "fast_fruit_drive_nautilus.py"

# No PATH lookup is permitted. Each executable is separately resolved through
# root-owned, non-writable /usr ancestry before use.
PYTHON = "/usr/bin/python3"
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
GIO = "/usr/bin/gio"
OMARCHY_SHELL = "/usr/bin/omarchy-shell"

MANIFEST_CAP = 64 * 1024
HELPER_CAP = 2 * 1024 * 1024
OUTPUT_CAP = 64 * 1024
CHECK_DELAY_SECONDS = 2.0


class Unsafe(RuntimeError):
    """A fixed path did not have the ownership/type/mode we require."""


class CheckoutInvalid(Unsafe):
    """The expected checkout is missing, unsafe, or not this plugin."""


def message(text: str) -> None:
    print(f"{APP} guard: {text}", file=sys.stderr)


def account_home() -> str:
    home = pwd.getpwuid(os.getuid()).pw_dir
    if not home.startswith("/") or "\x00" in home:
        raise Unsafe("unexpected account home")
    return home.rstrip("/") or "/"


HOME = account_home()


def config_rel() -> str:
    return ".config/fast-fruit-drive"


def plugin_rel() -> str:
    return f".config/omarchy/plugins/{PLUGIN_ID}"


def unit_rel() -> str:
    return ".config/systemd/user"


def wants_rel() -> str:
    return f"{unit_rel()}/default.target.wants"


def bookmarks_rel() -> str:
    return ".config/gtk-3.0"


def extension_rel() -> str:
    return ".local/share/nautilus-python/extensions"


def _overflow_uid() -> int:
    try:
        with open("/proc/sys/kernel/overflowuid", encoding="ascii") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return 65534


# Within the systemd sandbox the host root UID can be shown as overflowuid.
# The checkout/state side is otherwise current-user owned; executable paths
# under /usr are checked with the stricter root/overflow set below.
def state_uids() -> frozenset[int]:
    return frozenset({os.getuid(), 0, _overflow_uid()})


def root_uids() -> frozenset[int]:
    return frozenset({0, _overflow_uid()})


def valid_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\x00" not in name and all(ord(c) >= 32 for c in name)


class SafeDir:
    """A directory identity retained by a no-follow descriptor."""

    __slots__ = ("fd",)

    def __init__(self, fd: int):
        self.fd = fd

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def __enter__(self) -> "SafeDir":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @classmethod
    def root(cls) -> "SafeDir":
        return cls(os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))

    @staticmethod
    def _check_dir_or_file(st: os.stat_result, allowed_uids: Iterable[int], *, directory: bool) -> None:
        expected = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
        if not expected:
            raise Unsafe("unexpected filesystem type")
        if st.st_uid not in allowed_uids:
            raise Unsafe("unexpected owner")
        if st.st_mode & 0o022:
            raise Unsafe("group- or world-writable path")

    def enter(self, name: str, *, allowed_uids: Iterable[int] | None = None) -> "SafeDir":
        if not valid_name(name):
            raise Unsafe("unsafe path component")
        if allowed_uids is None:
            allowed_uids = state_uids()
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        except NotADirectoryError as exc:
            raise Unsafe("path component is not a directory") from exc
        try:
            self._check_dir_or_file(os.fstat(fd), allowed_uids, directory=True)
        except BaseException:
            os.close(fd)
            raise
        return SafeDir(fd)

    def walk(self, rel_path: str, *, allowed_uids: Iterable[int] | None = None) -> "SafeDir":
        if rel_path.startswith("/"):
            raise Unsafe("expected a relative path")
        cur: SafeDir = self
        opened: list[SafeDir] = []
        try:
            for part in rel_path.split("/"):
                if not part:
                    continue
                nxt = cur.enter(part, allowed_uids=allowed_uids)
                opened.append(nxt)
                cur = nxt
        except BaseException:
            for opened_dir in opened:
                opened_dir.close()
            raise
        for opened_dir in opened[:-1]:
            opened_dir.close()
        return opened[-1] if opened else SafeDir(os.dup(self.fd))

    def read_file(self, name: str, cap: int, *, allowed_uids: Iterable[int] | None = None) -> bytes:
        if not valid_name(name):
            raise Unsafe("unsafe filename")
        if allowed_uids is None:
            allowed_uids = state_uids()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            st = os.fstat(fd)
            self._check_dir_or_file(st, allowed_uids, directory=False)
            if st.st_size < 0 or st.st_size > cap:
                raise Unsafe("file is too large")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(fd, min(65536, cap + 1 - total))
                if not block:
                    break
                chunks.append(block)
                total += len(block)
                if total > cap:
                    raise Unsafe("file is too large")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def open_regular(self, name: str, cap: int, *, allowed_uids: Iterable[int] | None = None) -> int:
        """Return a checked, still-open regular file descriptor."""
        if not valid_name(name):
            raise Unsafe("unsafe filename")
        if allowed_uids is None:
            allowed_uids = state_uids()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            st = os.fstat(fd)
            self._check_dir_or_file(st, allowed_uids, directory=False)
            if st.st_size < 0 or st.st_size > cap:
                raise Unsafe("file is too large")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def atomic_write(self, name: str, data: bytes, mode: int = 0o600) -> None:
        if not valid_name(name):
            raise Unsafe("unsafe filename")
        temp = f".ff-{os.getpid()}-{time.monotonic_ns():x}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=self.fd)
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.rename(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            try:
                os.fsync(self.fd)
            except OSError:
                pass
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(temp, dir_fd=self.fd)
            except OSError:
                pass
            raise

    def unlink(self, name: str) -> None:
        if not valid_name(name):
            raise Unsafe("unsafe filename")
        try:
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            pass

    def names(self) -> list[os.DirEntry]:
        with os.scandir(self.fd) as scan:
            return list(scan)


def open_home() -> SafeDir:
    with SafeDir.root() as root:
        return root.walk(HOME.lstrip("/"), allowed_uids=state_uids())


def open_relative(rel_path: str) -> SafeDir | None:
    try:
        with open_home() as home:
            return home.walk(rel_path, allowed_uids=state_uids())
    except (FileNotFoundError, Unsafe, OSError):
        return None


# ---------------------------------------------------------------------------
# Checked /usr executables and bounded process calls

_TRUSTED_LINK_PREFIXES = ("/usr/bin/", "/usr/lib/", "/usr/share/")


def _safe_root_ancestors(directory: str) -> bool:
    if not directory.startswith("/"):
        return False
    try:
        with SafeDir.root() as root:
            checked = root.walk(directory.lstrip("/"), allowed_uids=root_uids())
            checked.close()
        return True
    except (Unsafe, OSError):
        return False


def verified_usr_tool(path: str) -> str | None:
    """Resolve a root-owned executable beneath /usr without PATH lookup."""
    if not path.startswith("/usr/bin/"):
        return None
    candidate = path
    for _hop in range(4):
        try:
            info = os.lstat(candidate)
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):
            # Symlink mode bits are not permissions on Linux (they normally
            # appear as 0777), so only owner + no-follow ancestor checks are
            # meaningful before resolving this root-owned link.
            if info.st_uid not in root_uids() or not _safe_root_ancestors(os.path.dirname(candidate)):
                return None
            try:
                target = os.readlink(candidate)
            except OSError:
                return None
            if not target or ".." in target.split("/"):
                return None
            if not target.startswith("/"):
                target = os.path.normpath(os.path.join(os.path.dirname(candidate), target))
            if not target.startswith(_TRUSTED_LINK_PREFIXES):
                return None
            candidate = target
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in root_uids() or info.st_mode & 0o022 or not info.st_mode & 0o111:
            return None
        return candidate if _safe_root_ancestors(os.path.dirname(candidate)) else None
    return None


_ENV_KEEP = frozenset({
    "LANG", "TZ", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
    "DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE",
})


def child_env() -> dict[str, str]:
    env = {"PATH": "/usr/bin", "HOME": HOME}
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = ""
    if user:
        env["USER"] = user
        env["LOGNAME"] = user
    for key, value in os.environ.items():
        if key in _ENV_KEEP or key.startswith("LC_"):
            env[key] = value
    return env


class Result:
    def __init__(self, code: int, stdout: bytes, stderr: bytes, *, timed_out: bool = False, overflowed: bool = False):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.overflowed = overflowed

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out and not self.overflowed


def _kill_group(proc: subprocess.Popen[bytes], force: bool) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ProcessLookupError, PermissionError):
        pass


def run_bounded(argv: list[str], *, timeout: float, cap: int = OUTPUT_CAP) -> Result:
    """Run a verified fixed command with raw-byte caps and group cleanup."""
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env(), close_fds=True, start_new_session=True,
        )
    except OSError as exc:
        return Result(127, b"", str(exc).encode("utf-8", "replace"))
    assert proc.stdout is not None and proc.stderr is not None
    out, err = bytearray(), bytearray()
    pipes = {proc.stdout.fileno(): (proc.stdout, out), proc.stderr.fileno(): (proc.stderr, err)}
    for stream in (proc.stdout, proc.stderr):
        os.set_blocking(stream.fileno(), False)
    open_fds = set(pipes)
    deadline = time.monotonic() + timeout
    timed_out = overflowed = False
    try:
        while open_fds:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select(list(open_fds), [], [], min(remaining, 0.25))
            for fd in ready:
                _stream, target = pipes[fd]
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    open_fds.discard(fd)
                    continue
                if len(target) < cap:
                    target.extend(chunk[: cap - len(target)])
                if len(target) >= cap:
                    overflowed = True
            if overflowed:
                break
        if timed_out or overflowed:
            _kill_group(proc, False)
    finally:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_group(proc, True)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    return Result(proc.returncode or 0, bytes(out), bytes(err), timed_out=timed_out, overflowed=overflowed)


def run_tool(path: str, args: list[str], *, timeout: float = 10, cap: int = OUTPUT_CAP) -> Result:
    tool = verified_usr_tool(path)
    if not tool:
        return Result(127, b"", b"verified system tool unavailable")
    return run_bounded([tool, *args], timeout=timeout, cap=cap)


# ---------------------------------------------------------------------------
# Fixed checkout identity and pinned helper execution

def _manifest_is_ours(data: bytes) -> bool:
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(manifest, dict) and manifest.get("id") == PLUGIN_ID


def open_checkout_helper() -> int:
    """Validate the only allowed checkout and return a pinned helper FD."""
    try:
        with open_home() as home:
            checkout = home.walk(plugin_rel(), allowed_uids=state_uids())
        try:
            manifest = checkout.read_file(MANIFEST_NAME, MANIFEST_CAP, allowed_uids=state_uids())
            if not _manifest_is_ours(manifest):
                raise CheckoutInvalid("unexpected plugin manifest")
            bin_dir = checkout.enter("bin", allowed_uids=state_uids())
            try:
                return bin_dir.open_regular(HELPER_NAME, HELPER_CAP, allowed_uids=state_uids())
            finally:
                bin_dir.close()
        finally:
            checkout.close()
    except (FileNotFoundError, OSError, Unsafe) as exc:
        if isinstance(exc, CheckoutInvalid):
            raise
        raise CheckoutInvalid("expected plugin checkout is missing or unsafe") from exc


def checkout_is_ours() -> bool:
    try:
        fd = open_checkout_helper()
    except CheckoutInvalid:
        return False
    try:
        return True
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Persistent registration cleanup

def _bookmark_is_ours(line: str) -> bool:
    if not line.startswith("dav://"):
        return False
    return line.startswith((DAV_URI, *LEGACY_DAV_URIS)) or "iCloud Drive" in line or "iCloud WebDAV" in line


def remove_bookmark() -> None:
    directory = open_relative(bookmarks_rel())
    if directory is None:
        return
    try:
        try:
            old = directory.read_file("bookmarks", 1024 * 1024)
        except FileNotFoundError:
            return
        lines = old.decode("utf-8", "replace").splitlines()
        kept = [line for line in lines if not _bookmark_is_ours(line)]
        payload = ("\n".join(kept) + "\n").encode("utf-8", "replace") if kept else b""
        directory.atomic_write("bookmarks", payload, 0o600)
    except (Unsafe, OSError) as exc:
        message(f"could not safely remove bookmark: {exc}")
    finally:
        directory.close()


def remove_extension() -> None:
    directory = open_relative(extension_rel())
    if directory is None:
        return
    try:
        directory.unlink(EXTENSION_NAME)
        try:
            pycache = directory.enter("__pycache__")
        except (FileNotFoundError, Unsafe, OSError):
            return
        try:
            for entry in pycache.names():
                if entry.name.startswith("fast_fruit_drive_nautilus") and entry.name.endswith(".pyc"):
                    pycache.unlink(entry.name)
        finally:
            pycache.close()
    except (Unsafe, OSError) as exc:
        message(f"could not safely remove Nautilus extension: {exc}")
    finally:
        directory.close()


def remove_unit_files() -> None:
    for rel, names in (
        (unit_rel(), (UNIT_NAME, LIFECYCLE_SERVICE_NAME, LIFECYCLE_PATH_NAME)),
        (wants_rel(), (UNIT_NAME, LIFECYCLE_PATH_NAME)),
    ):
        directory = open_relative(rel)
        if directory is None:
            continue
        try:
            for name in names:
                directory.unlink(name)
        except (Unsafe, OSError) as exc:
            message(f"could not safely remove unit registration: {exc}")
        finally:
            directory.close()


def disarm_unit() -> None:
    # The user manager, not this possibly sandboxed process, removes the
    # wants symlink. This is the mandatory safety action when validation
    # fails; filesystem cleanup below is best effort and idempotent.
    run_tool(SYSTEMCTL, ["--user", "disable", UNIT_NAME, LIFECYCLE_PATH_NAME], timeout=12)


def cleanup(mode: str) -> None:
    if mode not in ("disable", "remove"):
        raise Unsafe("invalid cleanup mode")
    for uri in (DAV_URI, *LEGACY_DAV_URIS):
        run_tool(GIO, ["mount", "-u", uri], timeout=8)
    # Do not stop the lifecycle service itself here: this invocation may be
    # that service. Disabling/stopping the path unit is sufficient, and both
    # unit files are removed below after this guard has finished using them.
    run_tool(SYSTEMCTL, ["--user", "disable", "--now", UNIT_NAME, LIFECYCLE_PATH_NAME], timeout=20)
    remove_unit_files()
    run_tool(SYSTEMCTL, ["--user", "reset-failed", UNIT_NAME], timeout=8)
    run_tool(SYSTEMCTL, ["--user", "daemon-reload"], timeout=12)
    remove_bookmark()
    remove_extension()
    # No data path is removed. Keep the independent guard as inert config:
    # concurrent path events can complete their idempotent cleanup instead of
    # failing because an earlier event unlinked their executable. It has no
    # persistent registration after the unit/path files above are gone.
    message(f"{mode} cleanup disarmed Fast Fruit Drive registrations; cache and config were preserved")


def schedule_cleanup(mode: str) -> bool:
    if mode not in ("disable", "remove"):
        return False
    tool = verified_usr_tool(SYSTEMD_RUN)
    python = verified_usr_tool(PYTHON)
    if not tool or not python:
        return False
    # This guard path is derived from passwd(5), never HOME/XDG. The unit name
    # contains only a numeric PID generated by this process.
    guard_path = os.path.join(HOME, config_rel(), GUARD_NAME)
    unit = f"fast-fruit-drive-cleanup-{os.getpid()}.service"
    result = run_bounded(
        [tool, "--user", "--quiet", "--collect", f"--unit={unit}", "--service-type=exec", python, "-I", guard_path, "cleanup", mode],
        timeout=10,
    )
    return result.ok


# ---------------------------------------------------------------------------
# Commands

def cmd_serve() -> int:
    python = verified_usr_tool(PYTHON)
    if not python:
        message("verified python3 is unavailable; disarming unit")
        disarm_unit()
        return 0
    try:
        helper_fd = open_checkout_helper()
    except CheckoutInvalid:
        message("expected plugin checkout is missing or foreign; disarming unit")
        disarm_unit()
        if not schedule_cleanup("remove"):
            message("could not schedule cleanup; the unit has still been disabled")
        # Exit success: Restart=on-failure must never loop on a removed plugin.
        return 0
    try:
        os.set_inheritable(helper_fd, True)
        os.execve(python, [python, "-I", f"/proc/self/fd/{helper_fd}", "serve"], child_env())
    except OSError as exc:
        message(f"could not start pinned helper ({exc}); disarming unit")
        disarm_unit()
        schedule_cleanup("remove")
        return 0
    finally:
        # Only reached if execve fails.
        try:
            os.close(helper_fd)
        except OSError:
            pass


def query_plugin_enabled() -> bool | None:
    result = run_tool(OMARCHY_SHELL, ["shell", "listPlugins"], timeout=8)
    if not result.ok:
        return None
    try:
        listing = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(listing, list):
        return None
    for item in listing:
        if isinstance(item, dict) and item.get("id") == PLUGIN_ID:
            return item.get("enabled") is True
    return None


def lifecycle_action(*, delay: bool = True) -> str:
    if delay:
        time.sleep(CHECK_DELAY_SECONDS)
    if not checkout_is_ours():
        return "remove"
    enabled = query_plugin_enabled()
    if enabled is False:
        return "disable"
    # An enabled plugin means ordinary shell reload/restart. An unavailable or
    # malformed shell response is also no-op: deleting registrations at logout
    # would be worse than leaving a guard that independently fails closed.
    return "none"


def cmd_lifecycle_check() -> int:
    mode = lifecycle_action()
    if mode == "none":
        return 0
    if not schedule_cleanup(mode):
        # Missing/foreign checkout is the one state where an enabled unit could
        # otherwise be dangerous. Disable it even if transient cleanup failed.
        if mode == "remove":
            disarm_unit()
        message("could not schedule lifecycle cleanup")
        return 0
    return 0


def cmd_cleanup(args: list[str]) -> int:
    if len(args) != 1 or args[0] not in ("disable", "remove"):
        message("usage: cleanup disable|remove")
        return 2
    cleanup(args[0])
    return 0


USAGE = "Usage: lifecycle-guard serve|lifecycle-check|cleanup disable|remove\n"


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(USAGE)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "serve" and not rest:
        return cmd_serve()
    if command == "lifecycle-check" and not rest:
        return cmd_lifecycle_check()
    if command == "cleanup":
        return cmd_cleanup(rest)
    sys.stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
