"""
Fast Fruit Drive — Nautilus emblems for iCloud WebDAV cache/upload state.

Dirty (uploading / not yet on iCloud):  emblem-synchronizing
Present in the local VFS cache:         emblem-default
Cloud-only (not hydrated):              emblem-documents
"""

from __future__ import annotations

import json
import os
from urllib.parse import unquote, urlparse

from gi import require_version

require_version("Nautilus", "4.1")

from gi.repository import GObject, Nautilus  # noqa: E402

CONFIG = os.path.expanduser("~/.config/fast-fruit-drive/config")
DEFAULT_CACHE = os.path.expanduser("~/.cache/fast-fruit-drive")
DEFAULT_REMOTE = "icloud"
DAV_HOSTS = {"icloud.localhost", "127.0.0.1", "localhost", "::1"}

EMBLEM = {
    "dirty": "emblem-synchronizing",
    "cached": "emblem-default",
    "remote": "emblem-documents",
}


def _load_config() -> dict:
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
    return out


def _rel_from_file_info(file_info) -> str | None:
    uri = file_info.get_uri() or ""
    parsed = urlparse(uri)
    cfg = _load_config()
    hosts = set(DAV_HOSTS)
    hosts.add(cfg.get("dav_host", "").lower())

    if parsed.scheme == "dav":
        host = (parsed.hostname or "").lower()
        if host in hosts:
            return unquote(parsed.path or "/").lstrip("/")
        return None

    location = file_info.get_location()
    path = location.get_path() if location is not None else None
    if not path:
        return None
    needle = "/gvfs/dav:host="
    idx = path.find(needle)
    if idx < 0:
        return None
    rest = path[idx + len(needle) :]
    hostpart, _, rel = rest.partition("/")
    host = hostpart.split(",", 1)[0].lower()
    if host not in hosts:
        return None
    return unquote(rel)


def _any_dirty(meta_dir: str, limit: int = 80) -> bool:
    if not os.path.isdir(meta_dir):
        return False
    seen = 0
    for dirpath, dirnames, filenames in os.walk(meta_dir):
        dirnames.sort()
        for name in filenames:
            seen += 1
            if seen > limit:
                return False
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("Dirty") is True:
                return True
    return False


def status_for(rel: str) -> str:
    cfg = _load_config()
    remote = cfg["remote"]
    cache = cfg["cache"]
    meta_root = os.path.join(cache, "vfsMeta", remote)
    vfs_root = os.path.join(cache, "vfs", remote)
    meta_path = os.path.join(meta_root, rel) if rel else meta_root
    vfs_path = os.path.join(vfs_root, rel) if rel else vfs_root

    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
        if data.get("Dirty") is True:
            return "dirty"
        return "cached"

    if os.path.isdir(meta_path) or os.path.isdir(vfs_path):
        if _any_dirty(meta_path if os.path.isdir(meta_path) else os.path.join(meta_root, rel)):
            return "dirty"
        if os.path.isdir(vfs_path):
            return "cached"
        return "remote"

    return "remote"


class FastFruitDriveInfoProvider(GObject.GObject, Nautilus.InfoProvider):
    def update_file_info(self, file_info):
        rel = _rel_from_file_info(file_info)
        if rel is None:
            return
        emblem = EMBLEM.get(status_for(rel))
        if emblem:
            file_info.add_emblem(emblem)
