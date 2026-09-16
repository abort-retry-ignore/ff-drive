"""Local WebDAV authentication and listener verification for Fast Fruit Drive."""

from __future__ import annotations

import base64
import hashlib
import http.client
import os
import re
import secrets
import stat
import sys
import time

USER = "ff-drive"
PASSWORD_FILE = "webdav-password"
HTPASSWD_FILE = "webdav.htpasswd"
MAX_SECRET = 256
PASSWORD_RE = re.compile(r"[A-Za-z0-9_-]{40,128}\Z")
IPV4_LOOPBACK = "0100007F"
IPV6_LOOPBACK = "00000000000000000000000001000000"


class AuthError(RuntimeError):
    pass


def fail(message: str) -> "None":
    print(f"fast-fruit-drive-auth: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_relative(dirfd: int, name: str, *, missing_ok: bool = False) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except FileNotFoundError:
        if missing_ok:
            return None
        fail(f"missing {name}")
    except OSError:
        fail(f"refusing unsafe {name}")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            fail(f"unsafe owner or mode for {name}")
        if st.st_size > MAX_SECRET:
            fail(f"oversized {name}")
        data = b""
        while len(data) <= MAX_SECRET:
            chunk = os.read(fd, 64)
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_SECRET:
            fail(f"oversized {name}")
        return data
    finally:
        os.close(fd)


def atomic_write(dirfd: int, name: str, data: bytes) -> None:
    tmp = ".ff-auth-" + secrets.token_hex(10)
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=dirfd,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except Exception:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except OSError:
            pass
        raise
    try:
        os.rename(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except Exception:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except OSError:
            pass
        raise


def password_from_fd(dirfd: int, *, create: bool) -> str:
    raw = read_relative(dirfd, PASSWORD_FILE, missing_ok=create)
    if raw is None:
        password = secrets.token_urlsafe(32)
        atomic_write(dirfd, PASSWORD_FILE, (password + "\n").encode())
        return password
    try:
        password = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        fail("invalid local WebDAV password")
    if not PASSWORD_RE.fullmatch(password):
        fail("invalid local WebDAV password")
    return password


def expected_htpasswd(password: str) -> bytes:
    digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
    return f"{USER}:{{SHA}}{digest}\n".encode()


def ensure_auth(dirfd: int) -> None:
    password = password_from_fd(dirfd, create=True)
    atomic_write(dirfd, HTPASSWD_FILE, expected_htpasswd(password))


def validate_auth(dirfd: int) -> str:
    password = password_from_fd(dirfd, create=False)
    actual = read_relative(dirfd, HTPASSWD_FILE)
    if actual != expected_htpasswd(password):
        fail("local WebDAV credentials do not match")
    return password


def socket_inodes(pid: int) -> set[str]:
    result: set[str] = set()
    try:
        for count, entry in enumerate(os.scandir(f"/proc/{pid}/fd")):
            if count >= 8192:
                raise AuthError("service has too many file descriptors")
            try:
                target = os.readlink(entry.path)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                result.add(target[8:-1])
    except OSError:
        fail("cannot inspect service sockets")
    return result


def table_listener_inode(table: str, address: str, port: int) -> str | None:
    expected = f"{address}:{port:04X}"
    try:
        with open(table, encoding="ascii") as fh:
            next(fh, None)
            for line in fh:
                fields = line.split()
                if len(fields) > 9 and fields[1].upper() == expected and fields[3] == "0A":
                    return fields[9]
    except OSError:
        return None
    return None


def listener_owned(pid: int, port: int, rclone_path: str) -> None:
    if pid <= 1 or not 1024 <= port <= 65535:
        raise AuthError("invalid service identity")
    try:
        exe = os.path.realpath(f"/proc/{pid}/exe")
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read(8192).split(b"\0")
    except OSError as exc:
        raise AuthError("service process disappeared") from exc
    if exe != os.path.realpath(rclone_path):
        raise AuthError("listener is not the expected rclone process")
    if b"serve" not in cmdline or b"webdav" not in cmdline or b"--htpasswd" not in cmdline:
        raise AuthError("rclone service is missing authenticated WebDAV arguments")
    index = cmdline.index(b"--htpasswd")
    if index + 1 >= len(cmdline):
        raise AuthError("rclone service has no htpasswd value")
    htpasswd_arg = cmdline[index + 1]
    if not htpasswd_arg.startswith(b"/proc/self/fd/") or not htpasswd_arg.endswith(b"/webdav.htpasswd"):
        raise AuthError("rclone service has an unexpected htpasswd source")
    expected = {
        table_listener_inode("/proc/net/tcp", IPV4_LOOPBACK, port),
        table_listener_inode("/proc/net/tcp6", IPV6_LOOPBACK, port),
    }
    if None in expected or not expected.issubset(socket_inodes(pid)):
        raise AuthError("WebDAV listeners are not owned by the service MainPID")


def authenticated_options(port: int, password: str) -> bool:
    token = base64.b64encode(f"{USER}:{password}".encode()).decode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.4)
    try:
        conn.request("OPTIONS", "/", headers={"Authorization": f"Basic {token}"})
        response = conn.getresponse()
        response.read(1024)
        return response.status == 200
    except OSError:
        return False
    finally:
        conn.close()


def wait_ready(dirfd: int, pid: int, port: int, rclone_path: str) -> None:
    password = validate_auth(dirfd)
    for _ in range(40):
        try:
            listener_owned(pid, port, rclone_path)
        except AuthError:
            pass
        else:
            if authenticated_options(port, password):
                return
        time.sleep(0.15)
    fail("authenticated WebDAV service did not become ready")


def mount_dav(dirfd: int, uri: str, pid: int, port: int, rclone_path: str) -> None:
    password = validate_auth(dirfd)
    try:
        listener_owned(pid, port, rclone_path)
    except AuthError as exc:
        fail(str(exc))
    if not authenticated_options(port, password):
        fail("authenticated WebDAV readiness check failed")

    from gi.repository import Gio, GLib

    loop = GLib.MainLoop()
    result = {"ok": False, "error": None, "timed_out": False}
    operation = Gio.MountOperation()
    cancellable = Gio.Cancellable()
    operation.set_username(USER)
    operation.set_password(password)
    operation.set_password_save(Gio.PasswordSave.NEVER)

    def ask_password(op, message, default_user, default_domain, flags):
        op.set_username(USER)
        op.set_password(password)
        op.set_password_save(Gio.PasswordSave.NEVER)
        op.reply(Gio.MountOperationResult.HANDLED)

    def done(source, async_result):
        try:
            source.mount_enclosing_volume_finish(async_result)
            result["ok"] = True
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                result["ok"] = True
            else:
                result["error"] = str(exc)
        loop.quit()

    def timeout():
        result["timed_out"] = True
        cancellable.cancel()
        loop.quit()
        return False

    operation.connect("ask-password", ask_password)
    file = Gio.File.new_for_uri(uri)
    file.mount_enclosing_volume(Gio.MountMountFlags.NONE, operation, cancellable, done)
    GLib.timeout_add_seconds(10, timeout)
    loop.run()
    try:
        listener_owned(pid, port, rclone_path)
    except AuthError as exc:
        fail(str(exc))
    if not result["ok"]:
        fail(result["error"] or "timed out mounting authenticated WebDAV")


def main() -> None:
    if len(sys.argv) < 4:
        fail("usage: COMMAND DIRFD USER [ARGS]")
    command, dirfd_text, username = sys.argv[1:4]
    if username != USER:
        fail("unexpected WebDAV username")
    dirfd = int(dirfd_text)
    if command == "ensure":
        ensure_auth(dirfd)
    elif command == "validate":
        validate_auth(dirfd)
    elif command == "wait" and len(sys.argv) == 7:
        wait_ready(dirfd, int(sys.argv[4]), int(sys.argv[5]), sys.argv[6])
    elif command == "mount" and len(sys.argv) == 8:
        mount_dav(dirfd, sys.argv[4], int(sys.argv[5]), int(sys.argv[6]), sys.argv[7])
    else:
        fail("invalid command or arguments")


if __name__ == "__main__":
    main()
