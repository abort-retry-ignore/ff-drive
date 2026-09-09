# Fast Fruit Drive

Omarchy bar widget that serves **iCloud Drive** to Nautilus over local rclone
WebDAV. Listings stay in the cloud; only files you open are cached on disk.

Nautilus shows it as **iCloud Drive**, not `127.0.0.1:8080`.

## Why

A FUSE mount of the whole drive (rclone mount / StratoSync) walks iCloud as if
it were local. Nautilus WebDAV lists folders on demand, skips thumbnails when
`show-image-thumbnails` is `local-only`, and is much snappier for browsing.

iCloud cannot stream/seek, so opened files are hydrated into a capped local
cache (`--vfs-cache-mode full`). Copying in Nautilus also works.

## Requirements

- [Omarchy](https://omarchy.org/)
- `rclone` with an `icloud` remote (`type = iclouddrive`)
- `gvfs-dnssd` (Nautilus WebDAV backend)

```bash
omarchy pkg add gvfs-dnssd
rclone config   # create remote named "icloud", backend iclouddrive
```

## Install

```bash
omarchy plugin add /path/to/fast-fruit-drive --enable
# later, from git:
# omarchy plugin add https://example.com/you/fast-fruit-drive.git --enable
```

Then click the fruit icon on the bar and toggle it on. Nautilus gets a sidebar
bookmark named **iCloud Drive**.

## Settings (bar widget)

| Setting | Default | Meaning |
|---|---|---|
| Read-only | On | Nautilus cannot delete/move/overwrite iCloud files |
| Local cache size | 4G | Cap for files you actually open |
| Cache max age | 24h | Unused cache entries are dropped |
| Refresh interval | 15s | How often the widget polls status |

Changing cache or read-only restarts the local WebDAV server. It never deletes
or moves objects on iCloud.

## Widget actions

- Left click: panel
- Right click: refresh
- Middle click: open in Nautilus
- In the panel: toggle the server, open Nautilus
- Keys: `r` refresh, `o` open, `p` / Enter on the switch to toggle

## CLI

The widget shells out to `bin/fast-fruit-drive`:

```bash
fast-fruit-drive status
fast-fruit-drive start
fast-fruit-drive stop
fast-fruit-drive toggle
fast-fruit-drive open
fast-fruit-drive configure read_only=true cache_max_size=4G cache_max_age=24h
```

Config lives in `~/.config/fast-fruit-drive/config`. Cache lives in
`~/.cache/fast-fruit-drive`. The user systemd unit is `fast-fruit-drive.service`.

## Safety

- Read-only by default
- No rclone purge/delete flags
- Cache eviction is local only
- Binds to `127.0.0.1` only
