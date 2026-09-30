"""Registration file fixtures shared by the ownership tests.

The "legacy" texts are the units written by earlier releases (0.2.0 ran the
checkout helper directly; 0.2.1 introduced the guard and lifecycle units), so
upgrades from them can be exercised without git history. "Current" texts are
what this release writes: the same units behind an ownership marker.
"""

from __future__ import annotations

PLUGIN_ID = "io.github.abort-retry-ignore.ff-drive"
MARKER = f"# Managed-By: {PLUGIN_ID}"

SERVICE = "fast-fruit-drive.service"
LIFECYCLE_SERVICE = "fast-fruit-drive-lifecycle.service"
LIFECYCLE_PATH = "fast-fruit-drive-lifecycle.path"
EXTENSION = "fast_fruit_drive_nautilus.py"

_SERVICE_TAIL = """\
Restart=on-failure
RestartSec=5s
TimeoutStopSec=15s
KillMode=control-group
Environment=PATH=/usr/bin
NoNewPrivileges=yes
UMask=0077
ProtectSystem=strict
ProtectHome=read-only
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fast-fruit-drive

[Install]
WantedBy=default.target
"""


def _service(exec_line: str, marker: bool) -> str:
    return (
        (MARKER + "\n" if marker else "")
        + "[Unit]\n"
        + "Description=Fast Fruit Drive \u2014 rclone iCloud WebDAV for Nautilus\n"
        + "After=network-online.target\nWants=network-online.target\n\n"
        + "[Service]\nType=simple\n"
        + exec_line + "\n"
        + _SERVICE_TAIL
    )


def _lifecycle_service(guard: str, marker: bool) -> str:
    return (
        (MARKER + "\n" if marker else "")
        + "[Unit]\nDescription=Fast Fruit Drive removal lifecycle check\n\n"
        + "[Service]\nType=oneshot\n"
        + f'ExecStart=/usr/bin/python3 -I "{guard}" lifecycle-check\n'
        + "Environment=PATH=/usr/bin\nNoNewPrivileges=yes\nUMask=0077\n"
    )


def _lifecycle_path(checkout: str, marker: bool) -> str:
    return (
        (MARKER + "\n" if marker else "")
        + "[Unit]\nDescription=Watch Fast Fruit Drive checkout removal\n\n"
        + f"[Path]\nPathChanged={checkout}\nPathChanged={checkout}/manifest.json\n"
        + f"Unit={LIFECYCLE_SERVICE}\n\n[Install]\nWantedBy=default.target\n"
    )


def current_units(guard: str, checkout: str) -> "dict[str, str]":
    return {
        SERVICE: _service(f'ExecStart=/usr/bin/python3 -I "{guard}" serve', True),
        LIFECYCLE_SERVICE: _lifecycle_service(guard, True),
        LIFECYCLE_PATH: _lifecycle_path(checkout, True),
    }


def legacy_020_units(helper_path: str) -> "dict[str, str]":
    """0.2.0 and earlier: only the service, running the checkout helper."""
    return {SERVICE: _service(f"ExecStart={helper_path} serve", False)}


def legacy_021_units(guard: str, checkout: str) -> "dict[str, str]":
    """0.2.1 / 0.2.2: guard-backed units, before the ownership marker."""
    return {
        SERVICE: _service(f'ExecStart=/usr/bin/python3 -I "{guard}" serve', False),
        LIFECYCLE_SERVICE: _lifecycle_service(guard, False),
        LIFECYCLE_PATH: _lifecycle_path(checkout, False),
    }


LEGACY_EXTENSION = (
    '"""\nFast Fruit Drive \u2014 Nautilus emblems for iCloud WebDAV cache/upload state.\n"""\n'
    "import os\n"
).encode("utf-8")
CURRENT_EXTENSION = (MARKER + "\n").encode("utf-8") + LEGACY_EXTENSION

FOREIGN_UNIT = (
    "[Unit]\nDescription=Somebody else's service that happens to share the name\n\n"
    "[Service]\nExecStart=/usr/bin/sleep infinity\n\n[Install]\nWantedBy=default.target\n"
)
FOREIGN_EXTENSION = b"# my own nautilus extension, unrelated to Fast Fruit Drive\nimport os\n"
