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
  outside its own cache directory;
- marketplace removal leaving an enabled user service which later executes
  arbitrary content placed at the deleted plugin checkout path.

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
  (its `ExecStart` invokes the independent guard, never the plugin checkout)
- `~/.config/systemd/user/fast-fruit-drive-lifecycle.path` and
  `fast-fruit-drive-lifecycle.service` — an enabled checkout-change watcher
  and its on-demand guard invocation
- `~/.config/fast-fruit-drive/lifecycle-guard` — a mode-700 copy of the
  independently executable lifecycle guard
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

GVFS mounts are per desktop session. When the enabled widget observes an
already-running server without a mount, it restores that mount through the
same supervised, listener-verified helper path, with at most three automatic
attempts until the server stops or a mount succeeds. It does not start a
stopped server, persist the password in a keyring, or create any additional
systemd registrations. An explicit stop takes priority over automatic mounting.

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

### Marketplace removal lifecycle

Omarchy has no declarative manifest uninstall hook, but its current removal
flow deletes the checkout after disabling an enabled plugin. Fast Fruit Drive
does not rely on a QML destruction callback for security: an enabled systemd
path unit watches the fixed checkout and manifest path, so deletion or a
foreign replacement invokes the guard even if QML is reloading or the plugin
was already disabled. The bar widget still launches a delayed `lifecycle-check`
on destruction so a plain disable can stop/disarm the drive while the checkout
remains. The guard waits briefly, then distinguishes ordinary checkout updates,
plain disable, and removal by checking the fixed checkout and fresh shell
registry.

The generated executable registrations run only:

```text
/usr/bin/python3 -I ~/.config/fast-fruit-drive/lifecycle-guard serve
/usr/bin/python3 -I ~/.config/fast-fruit-drive/lifecycle-guard lifecycle-check
```

Before executing anything from the checkout, the guard derives the account
home from `passwd(5)`, opens the exact fixed
`~/.config/omarchy/plugins/io.github.abort-retry-ignore.ff-drive` path
component-by-component with `O_NOFOLLOW`, checks ownership/mode and the exact
manifest ID, opens `bin/fast-fruit-drive` through that retained directory FD,
and executes `/proc/self/fd/N` rather than reopening a checkout pathname.

If the checkout is missing, symlinked, unsafe, or has another manifest ID, the
guard executes none of its code. It disables the main user unit and lifecycle
path unit first, schedules idempotent registration cleanup through a transient
user service, then exits successfully so `Restart=on-failure` cannot retry a
removed checkout. This is
a fallback as well as removal protection: even if desktop lifecycle cleanup is
interrupted, the next service invocation fails closed.

Disable/removal cleanup stops/disables the units it wrote, removes those unit
files and their wants links, unmounts the fixed DAV URI, removes only this
plugin's GTK bookmark and the Nautilus extension/bytecode it installed (each
subject to the ownership rules below). It deliberately does **not** delete
configuration, including the now-inert guard, local WebDAV credentials, cache,
or `rclone.conf`; preserving a possible `Dirty: true` cache is safer than
losing an upload. The explicit `uninstall` command remains the only full state
purge and retains its pending-upload refusal.

### Ownership of shared registrations

The three systemd user units (`fast-fruit-drive.service`,
`fast-fruit-drive-lifecycle.service`, `fast-fruit-drive-lifecycle.path`), their
`default.target.wants` links, and the Nautilus extension
(`fast_fruit_drive_nautilus.py`) live at fixed names in directories shared with
everything else the user runs. The plugin only overwrites, disables, stops or
deletes a registration it can positively identify as its own; the same rules
are implemented independently by the helper and by the guard (which must keep
working after the checkout is gone), and a test asserts both classify every
fixture identically.

**Identity.** Every file the plugin writes begins with
`# Managed-By: io.github.abort-retry-ignore.ff-drive`. A unit only counts as
ours if that marker is its first line **and** it is bound to this account's
installation: the service units must execute the guard copy under this
account's `~/.config/fast-fruit-drive/`, and the path unit must watch this
account's fixed plugin checkout and trigger our lifecycle service. The
extension counts as ours if the marker is its first line. Files written by
0.2.x and earlier have no marker; they are recognised only by their exact
generated shape (the unit's `Description=`, a `SyslogIdentifier=` and an
`ExecStart=` that runs this plugin, or the extension's original header) and
are rewritten with the marker on first write. A marker naming any other plugin
is treated as foreign.

**Install** (`ensure`, `start`, `restart`, lifecycle migration). All three unit
files and the extension are checked through the same retained directory
descriptors used to write them, before anything is written. If any is foreign
the command fails with a message naming the file and changes nothing: no unit,
config, bookmark or extension is written, systemd is not contacted, and the
foreign file is left byte-for-byte intact. Anything that is not a plain,
bounded regular file owned by you or root and not group/world-writable
(symlink, FIFO, other owner, oversized) is foreign; symlinked dotfiles are
never replaced. Reads use `O_NONBLOCK`, so a planted FIFO cannot hang the
check. The extension source is also required to carry the marker, so the plugin
never installs a file it could not later recognise.

**Runtime control.** Status, `start`, `stop` and `restart` only address
`fast-fruit-drive.service` when the file at that name is ours; otherwise the
unit is never queried as if it were ours and never stopped or restarted.

**Removal** (`uninstall`, and the guard's disable/remove cleanup and
fail-closed disarm). Each unit is classified individually. Only units that are
ours are passed to `systemctl disable --now`, have `reset-failed` issued, and
are deleted (re-checked through the same descriptor immediately before the
unlink). A foreign unit of the same name keeps its file, its enablement and
its runtime state, and is reported on stderr. A `default.target.wants` entry is
removed only if it is a symlink whose target is our own unit file. The
extension and its bytecode are removed only if the extension is ours.

**Bookmarks.** The GTK bookmark is matched by exact URI, never by label. The
current URI (`dav://ff-drive@iCloud.localhost:8080/`) is unique to this plugin;
the generic loopback URIs earlier releases used only count when they also carry
a label this plugin wrote, so another tool's bookmark that merely says "iCloud
Drive" is preserved.

**Not covered.** The ownership check and the atomic rename are two steps within
one pinned directory; a process running as you that swaps the file between them
is outside the threat model (it could equally edit the unit directly). The
early-release `gio mount -u` of the generic loopback legacy URIs during
cleanup is unchanged.

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

`clear-cache` and explicit full `uninstall` both delete the local on-demand cache.
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
