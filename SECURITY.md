# Security model

Fast Fruit Drive runs a local, loopback-only WebDAV server (rclone) in
front of your iCloud Drive and serves it to Nautilus over GVFS. This
document describes the trust boundaries, what each control actually
defends against, and what is explicitly out of scope.

## Threat model

In scope — this plugin defends against:

- another local user or process on the same machine reaching the local
  WebDAV listener;
- a hostile inherited environment (`PATH`, `BASH_ENV`, `PYTHONPATH`,
  `LD_PRELOAD`, and similar loader/interpreter injection vectors);
- a symlink or path swap between validating a filesystem path and using it
  (TOCTOU);
- malformed or oversized config and cache-metadata files;
- a helper command that hangs, forks children, or produces unbounded
  output;
- another process squatting on the fixed loopback port before rclone
  starts;
- `clear-cache` or `uninstall` destroying a file that has not finished
  uploading to iCloud yet;
- a crafted DAV URI making the Nautilus extension read or watch a path
  outside its own cache directory.

Out of scope:

- a compromised root account, or an attacker who already has arbitrary
  code execution as your own user with full access to your home directory
  and rclone credentials;
- compromise of signed Arch/AUR packages, or of Apple's iCloud service
  itself;
- physical access or an already-compromised desktop session.

## What this plugin can read and write

Fast Fruit Drive is **read/write**. Nautilus operations against the
**iCloud Drive** bookmark — delete, move, rename, overwrite — apply to your
real iCloud Drive through rclone, the same as any other Nautilus location.

State this plugin writes, all under your own home directory:

- `~/.config/systemd/user/fast-fruit-drive.service` — the user service unit
- `~/.config/gtk-3.0/bookmarks` — one line, the **iCloud Drive** entry
- `~/.local/share/nautilus-python/extensions/fast_fruit_drive_nautilus.py`
  — the emblem extension
- `~/.config/fast-fruit-drive/config` — cache size/age only
- `~/.config/fast-fruit-drive/webdav-password`,
  `~/.config/fast-fruit-drive/webdav.htpasswd` — the local WebDAV
  credential (below)
- `~/.cache/fast-fruit-drive/` — the on-demand file cache

`~/.config/rclone/rclone.conf` is rclone's own file. This plugin only reads
it to check whether a remote and session exist; it never writes to it.

## Local WebDAV authentication

The rclone WebDAV server only listens on `127.0.0.1` and `[::1]`, but
loopback is shared by every local user and process, and an unauthenticated
WebDAV server there would let any of them read or write your iCloud Drive
undetected. So the server requires a per-install credential:

- A random 256-bit password is generated once per install and stored at
  `~/.config/fast-fruit-drive/webdav-password`, mode `600`.
- rclone is only ever given a SHA-hashed `htpasswd` file
  (`webdav.htpasswd`, also mode `600`), passed as
  `/proc/self/fd/N/webdav.htpasswd` — a path that resolves through an
  already-open, already-validated directory descriptor, not a path that
  could be swapped out from under it later.
- GVFS is handed the plaintext password in memory, through
  `Gio.MountOperation`, with `PasswordSave.NEVER`. It is never placed in
  the DAV URI, the GTK bookmark, argv, or the systemd journal.
- Before the password is ever supplied, the helper proves that both the
  IPv4 and IPv6 loopback listeners on the fixed port belong to the
  current systemd `MainPID`, that that PID's executable is the verified
  `rclone` binary, and that its arguments actually request authenticated
  WebDAV — not merely that *some* process is listening on the port.

An unauthenticated request gets HTTP 401:

```console
$ curl -X PROPFIND -H 'Depth: 0' http://127.0.0.1:8080/
HTTP/1.1 401 Unauthorized
```

This password is unrelated to your Apple ID password.

## Filesystem safety

Every read, write, and delete this plugin performs is descriptor-relative:
each path component is opened with `O_NOFOLLOW` relative to an
already-open parent directory descriptor, and the resulting descriptor is
what gets used — never a path re-resolved later. Concretely:

- validating a directory and opening it happen in the same `os.open()`
  call, so there is no window between "checked" and "used" for a symlink
  swap to exploit;
- writes (config, unit, bookmark, extension copy, WebDAV credentials) are
  atomic: a `O_CREAT|O_EXCL` temp file relative to the same retained
  descriptor, then `rename()` within that descriptor;
- deletion (cache clearing, uninstall) walks the tree through retained
  directory descriptors with bounded depth and entry counts, and only
  ever targets this plugin's own fixed config/cache directories — never an
  arbitrary caller-supplied path.

Tool resolution is similarly strict: `rclone`, `python3`, `systemctl`,
`gio`, `nautilus`, and related binaries are only accepted from
`/usr/bin` or `/usr/local/bin`, must be regular, root-owned,
non-group/world-writable files (root-owned symlinks are followed at most
three hops, only within `/usr`), and the service's own `PATH` is closed to
`/usr/bin`.

## Process isolation

Every command the widget can trigger runs through a small supervisor
(`fast-fruit-drive __supervise TIMEOUT COMMAND ARGS...`) that:

- launches the real command as an isolated Python process (`python3 -I`,
  which ignores every `PYTHON*` environment variable and the user site
  directory) in its own session, with an explicit allow-listed
  environment — not the caller's;
- reads its stdout/stderr as raw bytes and caps each stream at 64 KiB
  **before** any line-oriented parsing happens, so an unterminated or
  malicious producer cannot buffer unbounded data upstream;
- enforces a monotonic wall-clock deadline, and on timeout or overflow
  sends the whole process group `SIGTERM`, then `SIGKILL` if it is still
  alive after a short grace period, and always reaps it — including any
  grandchildren it may have forked.

The widget itself only ever sees this already-bounded output. Its own
watchdog timers are a backstop for the case the supervisor process itself
hangs (a bug, not normal operation): they signal the exact PID Quickshell
reports for that process, not a guessed process-group ID.

## Pending uploads are protected

`clear-cache` and `uninstall` both delete the local on-demand cache.
Before doing so, the service is stopped and rclone's own VFS metadata is
scanned (bounded, descriptor-relative, same as everything else) for any
entry marked `Dirty: true` — a write that has not finished uploading to
iCloud yet. If any are found, the service is restarted and the command is
refused with a clear message; nothing is deleted.

## Service sandboxing

The systemd user unit runs with `NoNewPrivileges`, a closed `PATH`, a
stripped environment (`UnsetEnvironment=` covers the same
loader/interpreter injection variables described above), and systemd
sandboxing directives appropriate to a network-facing, single-purpose
service: `ProtectSystem=strict`, `ProtectHome=read-only` (with the cache
directory carved out via `ReadWritePaths=`), `PrivateTmp`,
`PrivateDevices`, `ProtectKernelTunables`/`Modules`/`Logs`,
`ProtectControlGroups`, `RestrictSUIDSGID`, `LockPersonality`,
`RestrictRealtime`, an address-family restriction that still allows the
outbound HTTPS connection to Apple and the loopback WebDAV listener, an
empty capability bounding set, and native syscall architectures only.

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository (Security
→ Advisories → Report a vulnerability) rather than a public issue.
