"""Exercise Service.qml with real Quickshell process completion semantics.

The controller and model are copied to a private scratch configuration. Its
helper is replaced with a fake that only reads/writes scratch JSON; no test
contacts systemd, GVFS, rclone, Apple, or the live desktop shell. Runtime tests
are skipped when Quickshell is not installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
QUICKSHELL = shutil.which("quickshell")

FAKE_HELPER = '''import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
args = sys.argv[1:]
if args and args[0] == "__supervise":
    args = args[2:]
command = args[0] if args else "help"
if command == "__migrate-lifecycle":
    raise SystemExit(0)
with (root / "commands.jsonl").open("a") as out:
    out.write(json.dumps(command) + "\\n")
state = json.loads((root / "state.json").read_text())
if command == "status":
    print(json.dumps({
        "ok": True, "ready": state["ready"],
        "running": state["running"], "mounted": state["mounted"],
        "statusText": "Connected" if state["mounted"] else
                      "Serving" if state["running"] else "Stopped",
    }))
elif command == "mount":
    if not state["mountSucceeds"]:
        print("fake mount failure", file=sys.stderr)
        raise SystemExit(1)
    state["mounted"] = True
    (root / "state.json").write_text(json.dumps(state))
elif command != "configure":
    raise SystemExit("unexpected command: " + command)
'''


@unittest.skipUnless(QUICKSHELL, "Quickshell is required for QML runtime tests")
class ServiceRuntimeTests(unittest.TestCase):
    def run_controller(self, *, running=True, mounted=False, ready=True,
                       mount_succeeds=True, stop_requested=False,
                       condition, hold_ms=250):
        with tempfile.TemporaryDirectory(prefix="ff-drive-qml-test-") as tmp:
            root = Path(tmp)
            for name in ("Service.qml", "Model.js"):
                shutil.copyfile(ROOT / name, root / name)
            # Service.qml imports Commons but uses no types from it. Provide
            # a scratch module, not any installed Omarchy singleton services.
            (root / "Commons").mkdir()
            (root / "Commons/qmldir").write_text(
                "module qs.Commons\nMarker 1.0 Marker.qml\n")
            (root / "Commons/Marker.qml").write_text("import QtQuick\nQtObject {}\n")
            (root / "bin").mkdir()
            (root / "bin/fast-fruit-drive").write_text(FAKE_HELPER)
            (root / "state.json").write_text(json.dumps({
                "running": running, "mounted": mounted, "ready": ready,
                "mountSucceeds": mount_succeeds,
            }))
            (root / "shell.qml").write_text('''import QtQuick
import Quickshell

ShellRoot {
  id: harness
  property double satisfiedAt: 0
  Service {
    id: drive
    Component.onCompleted: %s
  }
  Timer {
    interval: 50
    running: true
    repeat: true
    onTriggered: {
      if (!(%s)) { harness.satisfiedAt = 0; return }
      if (harness.satisfiedAt === 0) harness.satisfiedAt = Date.now()
      if (Date.now() - harness.satisfiedAt >= %d) {
        console.log("FF_SERVICE_TEST_PASS")
        Qt.quit()
      }
    }
  }
  Timer {
    interval: 10000
    running: true
    onTriggered: { console.log("FF_SERVICE_TEST_TIMEOUT"); Qt.quit() }
  }
}
''' % ("drive._desired = 0" if stop_requested else "{}", condition, hold_ms))
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", NO_COLOR="1")
            result = subprocess.run(
                [QUICKSHELL, "--path", str(root), "--no-color"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("FF_SERVICE_TEST_PASS", result.stdout)
            self.assertNotIn("FF_SERVICE_TEST_TIMEOUT", result.stdout)
            events = root / "commands.jsonl"
            return [json.loads(line) for line in events.read_text().splitlines()]

    def test_remount_after_status_process_exit(self):
        # busy is still bound to true inside Process.onExited, despite
        # Process.running already reading false. This failed when restore
        # ran synchronously; Qt.callLater must allow the binding to settle.
        commands = self.run_controller(
            condition='drive.mounted && drive.statusText === "Connected" && !drive.busy')
        self.assertEqual(commands.count("mount"), 1)
        self.assertNotIn("start", commands)

    def test_stopped_server_is_not_started_or_mounted(self):
        commands = self.run_controller(
            running=False,
            condition='drive.ready && drive.statusText === "Stopped" && !drive.busy')
        self.assertNotIn("mount", commands)
        self.assertNotIn("start", commands)

    def test_existing_mount_is_left_alone(self):
        commands = self.run_controller(
            mounted=True,
            condition='drive.mounted && !drive.busy && drive._mountTries === 0')
        self.assertNotIn("mount", commands)

    def test_remount_does_not_race_requested_stop(self):
        commands = self.run_controller(
            stop_requested=True,
            condition='drive.running && drive._desired === 0 && !drive.busy')
        self.assertNotIn("mount", commands)

    def test_not_ready_server_is_not_mounted(self):
        commands = self.run_controller(
            ready=False,
            condition='drive.running && drive.statusText === "Serving" && !drive.busy')
        self.assertNotIn("mount", commands)

    def test_mount_failures_have_a_retry_limit(self):
        commands = self.run_controller(
            mount_succeeds=False, hold_ms=4500,
            condition='drive._mountTries === 3')
        self.assertEqual(commands.count("mount"), 3)
        self.assertNotIn("start", commands)


if __name__ == "__main__":
    unittest.main()
