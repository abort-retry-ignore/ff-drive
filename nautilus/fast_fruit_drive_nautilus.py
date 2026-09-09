"""
Fast Fruit Drive — Nautilus emblems for iCloud WebDAV cache/upload state.

Keeps update_file_info cheap (no directory walks). A 2s GLib timer re-reads
only Dirty files and invalidates their emblems when upload finishes.

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

from gi.repository import GLib, GObject, Nautilus  # noqa: E402

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
_cached: set[str] = set()
_uri_for_rel: dict[str, str] = {}
_dirty_dirs: set[str] = set()


def _load_config() -> dict:
    global _cfg, _cfg_at
    now = time.monotonic()
    if _cfg is not None and now - _cfg_at < 5:
        return _cfg
    out = {"cache": DEFAULT_CACHE, "remote": DEFAULT_REMOTE, "dav_host": "icloud.localhost"}
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            for line in fh:
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
            with open(meta_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
        if data.get("Dirty") is True:
            return "dirty"
        return "cached"
    if os.path.isdir(vfs_path) or os.path.isdir(meta_path):
        return "cached" if os.path.isdir(vfs_path) else "remote"
    if rel and os.path.lexists(vfs_path):
        return "cached"
    return "remote"


def _status(rel: str, is_dir: bool) -> str:
    if is_dir:
        if rel in _watched_dirty or rel in _dirty_dirs:
            return "dirty"
        return _read_one(rel)
    if rel in _watched_dirty:
        return "dirty"
    if rel in _cached:
        return "cached"
    st = _read_one(rel)
    if st == "dirty":
        _watched_dirty.add(rel)
        _rebuild_dirty_dirs()
    elif st == "cached":
        _cached.add(rel)
    return st


def _invalidate(rel: str) -> None:
    uri = _uri_for_rel.get(rel)
    if not uri:
        return
    info = Nautilus.FileInfo.lookup_for_uri(uri)
    if info is not None:
        info.invalidate_extension_info()


def _poll() -> bool:
    finished = []
    for rel in list(_watched_dirty):
        if _read_one(rel) != "dirty":
            finished.append(rel)
    if not finished:
        return True
    for rel in finished:
        _watched_dirty.discard(rel)
        _cached.add(rel)
    _rebuild_dirty_dirs()
    for rel in finished:
        _invalidate(rel)
        for parent in _parents(rel):
            _invalidate(parent)
    return True


GLib.timeout_add(2000, _poll)


class FastFruitDriveInfoProvider(GObject.GObject, Nautilus.InfoProvider):
    def update_file_info(self, file_info):
        uri = file_info.get_uri() or ""
        rel = _rel_from_uri(uri)
        if rel is None:
            return
        _uri_for_rel[rel] = uri
        st = _status(rel, file_info.is_directory())
        emblem = EMBLEM.get(st)
        if emblem:
            file_info.add_emblem(emblem)
        if st == "dirty":
            for parent in _parents(rel):
                if parent not in _uri_for_rel:
                    parent_uri = uri.rsplit("/", 1)[0] + ("/" if parent == "" else "")
                    # best-effort; lookup uses stored uris
                    pass
