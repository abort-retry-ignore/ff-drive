"""
Fast Fruit Drive — Nautilus emblems for iCloud WebDAV cache/upload state.

Uses InfoProvider.update_file_info_full so Dirty files stay IN_PROGRESS and
the emblem is refreshed when upload finishes (no Ctrl+R).

Monitors on the VFS cache directories invalidate displayed rows when rclone
hydrates or evicts a file, so cloud-only → cached transitions update live.

Dirty (uploading):     emblem-synchronizing
Cached and uploaded:   emblem-default
Cloud-only:            emblem-documents
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import unquote

from gi import require_version

require_version("Nautilus", "4.1")

from gi.repository import Gio, GLib, GObject, Nautilus  # noqa: E402

CONFIG = os.path.expanduser("~/.config/fast-fruit-drive/config")
DEFAULT_CACHE = os.path.expanduser("~/.cache/fast-fruit-drive")
DEFAULT_REMOTE = "icloud"
DAV_HOSTS = {"icloud.localhost", "127.0.0.1", "localhost", "::1"}

EMBLEM = {
    "dirty": "emblem-synchronizing",
    "cached": "emblem-default",
    "remote": "emblem-documents",
}

_cfg = None
_cfg_at = 0.0
_watched_dirty: set[str] = set()
_dirty_dirs: set[str] = set()

# Live emblem refresh: rows we have displayed, keyed by their path relative to
# the remote root, and one cache-directory monitor per displayed folder. When
# rclone hydrates or evicts a file, the monitor invalidates that row's
# extension info and Nautilus re-asks for the emblem — no manual reload.
_rows: dict[str, object] = {}
_row_monitors: dict[str, object] = {}
_ROWS_CAP = 4096
_MONITORS_CAP = 128


def _load_config() -> dict:
    global _cfg, _cfg_at
    now = time.monotonic()
    if _cfg is not None and now - _cfg_at < 5:
        return _cfg
    out = {"cache": DEFAULT_CACHE, "remote": DEFAULT_REMOTE, "dav_host": "icloud.localhost"}
    try:
        with open(CONFIG, encoding="utf-8", errors="replace") as fh:
            blob = fh.read(65537)
        if len(blob) > 65536:
            blob = ""
        for line in blob.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key == "cache_dir":
                out["cache"] = os.path.expanduser(value)
            elif key == "remote":
                out["remote"] = value.rstrip(":").split("/")[0]
            elif key == "dav_host":
                out["dav_host"] = value.lower()
    except OSError:
        pass
    hosts = set(DAV_HOSTS)
    hosts.add(out["dav_host"])
    out["hosts"] = hosts
    remote = out["remote"]
    cache = out["cache"]
    out["meta_root"] = os.path.join(cache, "vfsMeta", remote)
    out["vfs_root"] = os.path.join(cache, "vfs", remote)
    _cfg, _cfg_at = out, now
    return out


def _rel_from_uri(uri: str) -> str | None:
    if not uri:
        return None
    cfg = _load_config()
    if uri.startswith("dav://"):
        rest = uri[6:]
        hostpart, _, path = rest.partition("/")
        host = hostpart.rsplit("@", 1)[-1].split(":")[0].lower()
        if host not in cfg["hosts"]:
            return None
        return unquote(path).strip("/")
    needle = "/gvfs/dav:host="
    idx = uri.find(needle)
    if idx < 0:
        return None
    rest = unquote(uri[idx + len(needle) :])
    hostpart, _, rel = rest.partition("/")
    host = hostpart.split(",", 1)[0].lower()
    if host not in cfg["hosts"]:
        return None
    return rel.strip("/")


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


def _read_one(rel: str) -> str:
    cfg = _load_config()
    meta_path = os.path.join(cfg["meta_root"], rel) if rel else cfg["meta_root"]
    vfs_path = os.path.join(cfg["vfs_root"], rel) if rel else cfg["vfs_root"]
    if rel and os.path.isfile(meta_path):
        try:
            if os.path.getsize(meta_path) > 262144:
                # Bounded reads: oversized metadata is never parsed.
                return "cached"
            with open(meta_path, encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
        if data.get("Dirty") is True:
            return "dirty"
        return "cached"
    if os.path.isdir(vfs_path):
        return "cached"
    if rel and os.path.lexists(vfs_path):
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


def _ensure_monitor(cfg: dict, dir_rel: str) -> None:
    if dir_rel in _row_monitors:
        return
    if len(_row_monitors) >= _MONITORS_CAP:
        for monitor in _row_monitors.values():
            monitor.cancel()
        _row_monitors.clear()
    cache_dir = os.path.join(cfg["vfs_root"], dir_rel) if dir_rel else cfg["vfs_root"]
    if not os.path.isdir(cache_dir):
        return
    try:
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


def _on_cache_changed(monitor, gfile, other_file, event_type, dir_rel) -> None:
    if event_type not in (
        Gio.FileMonitorEvent.CREATED,
        Gio.FileMonitorEvent.CHANGES_DONE_HINT,
        Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
        Gio.FileMonitorEvent.DELETED,
        Gio.FileMonitorEvent.MOVED,
    ):
        return
    name = gfile.get_basename() if gfile is not None else None
    if name:
        child_rel = f"{dir_rel}/{name}" if dir_rel else name
        _invalidate_row(child_rel)
        # A directory appearing in the cache means its contents just started
        # hydrating: attach its monitor now so the files inside it are seen.
        if event_type == Gio.FileMonitorEvent.CREATED:
            _ensure_monitor(_load_config(), child_rel)
    if event_type == Gio.FileMonitorEvent.MOVED and other_file is not None:
        other = other_file.get_basename()
        if other:
            _invalidate_row(f"{dir_rel}/{other}" if dir_rel else other)
    if monitor.is_cancelled():
        _row_monitors.pop(dir_rel, None)


def _register_row(cfg: dict, rel: str, file_info) -> None:
    """Track a displayed row and watch the cache dir of the folder listing it."""
    _remember_row(rel, file_info)
    if file_info.is_directory():
        _ensure_monitor(cfg, rel)
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    _ensure_monitor(cfg, parent)


class FastFruitDriveInfoProvider(GObject.GObject, Nautilus.InfoProvider):
    def __init__(self):
        super().__init__()
        self._timers: dict[int, int] = {}

    def update_file_info(self, file_info):
        uri = file_info.get_uri() or ""
        rel = _rel_from_uri(uri)
        if rel is None:
            return
        _register_row(_load_config(), rel, file_info)
        _apply_emblem(file_info, _status(rel, file_info.is_directory()))

    def update_file_info_full(self, provider, handle, closure, file_info):
        uri = file_info.get_uri() or ""
        rel = _rel_from_uri(uri)
        if rel is None:
            return Nautilus.OperationResult.COMPLETE
        _register_row(_load_config(), rel, file_info)
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
