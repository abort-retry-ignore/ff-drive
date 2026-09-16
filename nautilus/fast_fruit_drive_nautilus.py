"""
Fast Fruit Drive — Nautilus emblems for iCloud WebDAV cache/upload state.

Uses InfoProvider.update_file_info_full so Dirty files stay IN_PROGRESS and
the emblem is refreshed when upload finishes (no Ctrl+R).

Monitors on the VFS cache directories invalidate displayed rows when rclone
hydrates or evicts a file, so cloud-only → cached transitions update live.

Dirty (uploading):     emblem-synchronizing
Cached and uploaded:   emblem-default
Cloud-only:            emblem-documents

The cache root, remote name, and DAV host/port are fixed constants that
match the plugin's own helper exactly (bin/fast-fruit-drive). There is no
configuration file read here: a config value has no business steering which
filesystem paths this extension trusts, and previous versions of this file
that read cache_dir/remote/dav_host from config accepted values that let a
crafted URI walk outside the cache root.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import time
from urllib.parse import unquote

from gi import require_version

require_version("Nautilus", "4.1")

from gi.repository import Gio, GLib, GObject, Nautilus  # noqa: E402

HOME = pwd.getpwuid(os.getuid()).pw_dir
CACHE_DIR = os.path.join(HOME, ".cache", "fast-fruit-drive")
REMOTE_NAME = "icloud"
DAV_HOST = "icloud.localhost"
DAV_PORT = 8080
WEBDAV_USER = "ff-drive"
META_ROOT = os.path.join(CACHE_DIR, "vfsMeta", REMOTE_NAME)
VFS_ROOT = os.path.join(CACHE_DIR, "vfs", REMOTE_NAME)

EMBLEM = {
    "dirty": "emblem-synchronizing",
    "cached": "emblem-default",
    "remote": "emblem-documents",
}

MAX_META_FILE = 262_144
_ROWS_CAP = 4096
_MONITORS_CAP = 128
_DEBOUNCE_SECONDS = 0.25

_watched_dirty: set[str] = set()
_dirty_dirs: set[str] = set()
_rows: dict[str, object] = {}
_row_monitors: dict[str, object] = {}
_pending_invalidate: dict[str, float] = {}


def _valid_rel_component(name: str) -> bool:
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\x00" in name:
        return False
    return all(ord(ch) >= 32 for ch in name)


def _valid_rel_path(rel: str) -> bool:
    if rel == "":
        return True
    return all(_valid_rel_component(part) for part in rel.split("/"))


def _rel_from_uri(uri: str) -> "str | None":
    """Return the path relative to the fixed cache root that `uri` names,
    or None if it does not name a path under our own DAV mount at all.
    Every path component is validated: no '..', no empty segments, no
    control characters — a crafted URI cannot walk outside the root."""
    if not uri:
        return None
    if uri.startswith("dav://"):
        rest = uri[len("dav://"):]
        hostport, _, raw_path = rest.partition("/")
        userinfo, _, hostport = hostport.rpartition("@")
        host, _, port_text = hostport.partition(":")
        host = host.lower()
        if host != DAV_HOST:
            return None
        if port_text and port_text != str(DAV_PORT):
            return None
        if userinfo and userinfo != WEBDAV_USER:
            return None
        rel = unquote(raw_path).strip("/")
    else:
        needle = "/gvfs/dav:host="
        idx = uri.find(needle)
        if idx < 0:
            return None
        rest = unquote(uri[idx + len(needle):])
        hostpart, _, rel = rest.partition("/")
        items = hostpart.split(",")
        host = items[0].lower()
        fields = {}
        for item in items[1:]:
            key, _, value = item.partition("=")
            fields[key] = value
        if host != DAV_HOST:
            return None
        if "port" in fields and fields["port"] != str(DAV_PORT):
            return None
        if "user" in fields and fields["user"] != WEBDAV_USER:
            return None
        rel = rel.strip("/")
    if not _valid_rel_path(rel):
        return None
    return rel


def _parents(rel: str) -> list[str]:
    out = []
    while rel:
        rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
        out.append(rel)
    return out


def _rebuild_dirty_dirs() -> None:
    dirs: set[str] = set()
    for rel in _watched_dirty:
        dirs.update(_parents(rel))
    global _dirty_dirs
    _dirty_dirs = dirs


def _open_bounded(root: str, rel: str, *, max_bytes: int):
    """Open root/rel component-by-component with O_NOFOLLOW, refusing
    symlinks and group/world-writable directories at every level, then
    return at most max_bytes of the leaf's content (None if missing, not a
    plain file owned by us, or oversized)."""
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        parts = [p for p in rel.split("/") if p] if rel else []
        for part in parts[:-1]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                os.close(fd)
                return None
            os.close(fd)
            fd = nxt
        leaf = parts[-1] if parts else None
        if leaf is None:
            return None
        try:
            leaf_fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError:
            return None
        try:
            st = os.fstat(leaf_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                return None
            if st.st_size > max_bytes:
                return None
            data = bytearray()
            while True:
                chunk = os.read(leaf_fd, 65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > max_bytes:
                    return None
            return bytes(data)
        finally:
            os.close(leaf_fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _dir_exists_no_follow(root: str, rel: str) -> bool:
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        parts = [p for p in rel.split("/") if p] if rel else []
        for part in parts:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                return False
            os.close(fd)
            fd = nxt
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _leaf_exists_lexists(root: str, rel: str) -> bool:
    if not rel:
        return False
    parent_rel, _, leaf = rel.rpartition("/")
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        for part in ([p for p in parent_rel.split("/") if p] if parent_rel else []):
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                return False
            os.close(fd)
            fd = nxt
        try:
            os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            return True
        except OSError:
            return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_one(rel: str) -> str:
    if rel:
        data = _open_bounded(META_ROOT, rel, max_bytes=MAX_META_FILE)
        if data is not None:
            try:
                obj = json.loads(data)
            except ValueError:
                obj = {}
            if isinstance(obj, dict) and obj.get("Dirty") is True:
                return "dirty"
            return "cached"
    if _dir_exists_no_follow(VFS_ROOT, rel):
        return "cached"
    if rel and _leaf_exists_lexists(VFS_ROOT, rel):
        return "cached"
    return "remote"


def _status(rel: str, is_dir: bool) -> str:
    if is_dir:
        if rel in _watched_dirty or rel in _dirty_dirs:
            return "dirty"
        return _read_one(rel)
    st = _read_one(rel)
    if st == "dirty":
        _watched_dirty.add(rel)
        _rebuild_dirty_dirs()
    else:
        _watched_dirty.discard(rel)
        _rebuild_dirty_dirs()
    return st


def _apply_emblem(file_info, status: str) -> None:
    emblem = EMBLEM.get(status)
    if emblem:
        file_info.add_emblem(emblem)


def _remember_row(rel: str, file_info) -> None:
    if len(_rows) >= _ROWS_CAP:
        _rows.clear()
    _rows[rel] = file_info


def _ensure_monitor(dir_rel: str) -> None:
    if dir_rel in _row_monitors:
        return
    if len(_row_monitors) >= _MONITORS_CAP:
        for monitor in _row_monitors.values():
            monitor.cancel()
        _row_monitors.clear()
    if not _dir_exists_no_follow(VFS_ROOT, dir_rel):
        return
    cache_dir = os.path.join(VFS_ROOT, dir_rel) if dir_rel else VFS_ROOT
    try:
        # Real (post-validation) path: the checks above already proved
        # every component down to dir_rel is a non-symlinked directory
        # under VFS_ROOT that we own; nothing is re-resolved afterwards.
        monitor = Gio.File.new_for_path(cache_dir).monitor_directory(Gio.FileMonitorFlags.NONE)
    except Exception:
        return
    if monitor is None:
        return
    monitor.connect("changed", _on_cache_changed, dir_rel)
    _row_monitors[dir_rel] = monitor


def _invalidate_row(rel: str) -> None:
    file_info = _rows.get(rel)
    if file_info is None:
        return
    try:
        if file_info.is_gone():
            _rows.pop(rel, None)
            return
        file_info.invalidate_extension_info()
    except Exception:
        _rows.pop(rel, None)


def _flush_invalidate(rel: str) -> bool:
    _pending_invalidate.pop(rel, None)
    _invalidate_row(rel)
    return False


def _debounced_invalidate(rel: str) -> None:
    if rel in _pending_invalidate:
        return
    _pending_invalidate[rel] = time.monotonic()
    GLib.timeout_add(int(_DEBOUNCE_SECONDS * 1000), _flush_invalidate, rel)


def _on_cache_changed(monitor, gfile, other_file, event_type, dir_rel) -> None:
    if event_type not in (
        Gio.FileMonitorEvent.CREATED,
        Gio.FileMonitorEvent.CHANGES_DONE_HINT,
        Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
        Gio.FileMonitorEvent.DELETED,
        Gio.FileMonitorEvent.MOVED,
        Gio.FileMonitorEvent.MOVED_IN,
        Gio.FileMonitorEvent.MOVED_OUT,
    ):
        return
    name = gfile.get_basename() if gfile is not None else None
    if name and _valid_rel_component(name):
        child_rel = f"{dir_rel}/{name}" if dir_rel else name
        _debounced_invalidate(child_rel)
        if event_type in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN):
            _ensure_monitor(child_rel)
    if event_type == Gio.FileMonitorEvent.MOVED and other_file is not None:
        other = other_file.get_basename()
        if other and _valid_rel_component(other):
            _debounced_invalidate(f"{dir_rel}/{other}" if dir_rel else other)
    if monitor.is_cancelled():
        _row_monitors.pop(dir_rel, None)


def _register_row(rel: str, file_info) -> None:
    """Track a displayed row and watch the cache dir of the folder listing it."""
    _remember_row(rel, file_info)
    if file_info.is_directory():
        _ensure_monitor(rel)
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    _ensure_monitor(parent)


class FastFruitDriveInfoProvider(GObject.GObject, Nautilus.InfoProvider):
    def __init__(self):
        super().__init__()
        self._timers: dict[int, int] = {}

    def update_file_info(self, file_info):
        uri = file_info.get_uri() or ""
        rel = _rel_from_uri(uri)
        if rel is None:
            return
        _register_row(rel, file_info)
        _apply_emblem(file_info, _status(rel, file_info.is_directory()))

    def update_file_info_full(self, provider, handle, closure, file_info):
        uri = file_info.get_uri() or ""
        rel = _rel_from_uri(uri)
        if rel is None:
            return Nautilus.OperationResult.COMPLETE
        _register_row(rel, file_info)
        st = _status(rel, file_info.is_directory())
        _apply_emblem(file_info, st)
        if st != "dirty" or file_info.is_directory():
            return Nautilus.OperationResult.COMPLETE
        timer = GLib.timeout_add_seconds(
            2, self._poll_dirty, provider, handle, closure, file_info, rel
        )
        self._timers[id(handle)] = timer
        return Nautilus.OperationResult.IN_PROGRESS

    def _poll_dirty(self, provider, handle, closure, file_info, rel):
        try:
            gone = file_info.is_gone()
        except Exception:
            gone = True
        if gone:
            self._finish(provider, handle, closure, Nautilus.OperationResult.FAILED)
            return False
        st = _status(rel, False)
        _apply_emblem(file_info, st)
        if st == "dirty":
            return True
        self._finish(provider, handle, closure, Nautilus.OperationResult.COMPLETE)
        return False

    def _finish(self, provider, handle, closure, result):
        self._timers.pop(id(handle), None)
        try:
            Nautilus.info_provider_update_complete_invoke(closure, provider, handle, result)
        except TypeError:
            Nautilus.info_provider_update_complete_invoke(provider, handle, closure, result)

    def cancel_update(self, provider, handle):
        timer = self._timers.pop(id(handle), None)
        if timer is not None:
            GLib.source_remove(timer)
