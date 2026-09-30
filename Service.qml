import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

Item {
  id: root

  property var settings: ({})
  // Panel.qml owns this controller. Its short migration command is idempotent
  // and rewrites only a pre-0.2.1 unit that already exists; it never creates a
  // drive service merely because the widget was loaded.

  property bool ready: false
  property bool running: false
  property bool mounted: false
  property bool rcloneFound: false
  property bool remoteConfigured: false
  property bool hasSession: false
  property bool needsLogin: false
  property bool gvfsDav: false
  property bool refreshing: false
  property string statusText: "Checking…"
  property string remote: "icloud:"
  property string uri: "dav://ff-drive@iCloud.localhost:8080/"
  property string displayName: "iCloud Drive"
  property string cacheMaxSize: "4G"
  property string cacheMaxAge: "24h"
  property double cacheUsedBytes: 0
  property string lastError: ""
  property string actionStatus: ""

  property int _desired: -1
  readonly property bool active: _desired === -1 ? running : (_desired === 1)

  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 15, 5, 3600)
  readonly property string settingCacheMaxSize: String(setting("cacheMaxSize", "4G"))
  readonly property int settingCacheMaxAgeHours: intSetting("cacheMaxAgeHours", 24, 1, 168)
  readonly property bool busy: statusProcess.running || controlProcess.running
  readonly property string helperPath: helperFromUrl(Qt.resolvedUrl("bin/fast-fruit-drive"))

  property string _statusOutput: ""
  property string _statusError: ""
  property string _controlOutput: ""
  property string _controlError: ""
  property string _appliedKey: ""
  property bool _statusOverflow: false
  property bool _controlOverflow: false
  property bool _lifecycleMigrationLaunched: false
  property bool _lifecycleCleanupLaunched: false
  // Bounded auto-remount attempts after a reboot (GVFS mounts do not
  // survive the session; see cmd_mount()). Reset when a mount exists.
  property int _mountTries: 0

  // The helper's own __supervise command caps stdout/stderr at this many
  // raw bytes each *before* any line parsing happens (see run_bounded() in
  // bin/fast-fruit-drive), so this is a belt-and-suspenders duplicate of a
  // guarantee that already holds by the time SplitParser ever sees a byte.
  readonly property int outputCap: 65536
  readonly property int statusSuperviseSec: 12
  readonly property int controlSuperviseSec: 40

  readonly property string pythonPath: "/usr/bin/python3"

  // Every helper invocation is python3 -I (isolated mode: ignores all
  // PYTHON* environment variables and the user site directory) running
  // __supervise, which itself launches the real command in its own
  // session with a hand-picked environment, a raw-byte output cap, and a
  // wall-clock deadline — see run_bounded()/cmd_supervise() in
  // bin/fast-fruit-drive. Quickshell's own Process.environment merges
  // with the inherited environment rather than replacing it (there is no
  // API to read the inherited values, e.g. DBUS_SESSION_BUS_ADDRESS, to
  // reconstruct a full allow-list here), so loader/interpreter injection
  // vectors are neutralized by explicit override instead: LD_PRELOAD and
  // friends are blanked before the interpreter that will run __supervise
  // is even exec'd, and __supervise's own child gets the strict allow-list.
  readonly property var baseEnvironment: ({
    "PATH": "/usr/bin",
    "BASH_ENV": "", "ENV": "",
    "PYTHONPATH": "", "PYTHONHOME": "", "PYTHONSTARTUP": "", "PYTHONINSPECT": "",
    "LD_PRELOAD": "", "LD_LIBRARY_PATH": "", "LD_AUDIT": "",
    "GIO_EXTRA_MODULES": "", "GI_TYPELIB_PATH": "", "GSETTINGS_BACKEND": "", "GIO_USE_VFS": ""
  })

  function wrappedCommand(args, timeoutSeconds) {
    return [pythonPath, "-I", helperPath, "__supervise", String(timeoutSeconds)].concat(args)
  }

  function collectStatusOut(line) {
    if (_statusOverflow) return
    if (_statusOutput.length + line.length + 1 > outputCap) {
      _statusOverflow = true
      statusProcess.running = false
      return
    }
    _statusOutput += line + "\n"
  }

  function collectStatusErr(line) {
    if (_statusOverflow) return
    if (_statusError.length + line.length + 1 > outputCap) {
      _statusOverflow = true
      statusProcess.running = false
      return
    }
    _statusError += line + "\n"
  }

  function collectControlOut(line) {
    if (_controlOverflow) return
    if (_controlOutput.length + line.length + 1 > outputCap) {
      _controlOverflow = true
      controlProcess.running = false
      return
    }
    _controlOutput += line + "\n"
  }

  function collectControlErr(line) {
    if (_controlOverflow) return
    if (_controlError.length + line.length + 1 > outputCap) {
      _controlOverflow = true
      controlProcess.running = false
      return
    }
    _controlError += line + "\n"
  }

  function helperFromUrl(url) {
    var value = String(url || "")
    if (value.indexOf("file://") === 0) value = value.substring(7)
    try { return decodeURIComponent(value) } catch (e) { return "" }
  }

  // Derive the independent guard from this component's own helper URL, not
  // inherited HOME/XDG. The guard still re-derives passwd(5) home and the
  // fixed checkout before it acts. Removal is owned by the systemd path
  // watcher; this is only the disable/reload classifier.
  function lifecycleGuardPath() {
    var suffix = "/.config/omarchy/plugins/io.github.abort-retry-ignore.ff-drive/bin/fast-fruit-drive"
    if (helperPath.length <= suffix.length || helperPath.slice(-suffix.length) !== suffix) return ""
    var home = helperPath.slice(0, -suffix.length)
    if (home === "" || home.charAt(0) !== "/" || home.indexOf("\u0000") !== -1) return ""
    return home + "/.config/fast-fruit-drive/lifecycle-guard"
  }

  function migrateLifecycleGuard() {
    if (_lifecycleMigrationLaunched || helperPath === "") return
    _lifecycleMigrationLaunched = true
    Quickshell.execDetached({
      command: [pythonPath, "-I", helperPath, "__migrate-lifecycle"],
      clearEnvironment: false,
      environment: baseEnvironment,
      unbindStdout: true
    })
  }

  function checkRemovalLifecycle() {
    if (_lifecycleCleanupLaunched) return
    _lifecycleCleanupLaunched = true
    var guard = lifecycleGuardPath()
    if (guard === "") return
    Quickshell.execDetached({
      command: [pythonPath, "-I", guard, "lifecycle-check"],
      clearEnvironment: false,
      environment: baseEnvironment,
      unbindStdout: true
    })
  }

  Component.onCompleted: Qt.callLater(root.migrateLifecycleGuard)
  Component.onDestruction: root.checkRemovalLifecycle()

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    if (n < min) n = min
    if (n > max) n = max
    return n
  }

  function configKey() {
    return settingCacheMaxSize + "|" + settingCacheMaxAgeHours
  }

  function refresh() {
    if (statusProcess.running) return
    _statusOutput = ""
    _statusError = ""
    _statusOverflow = false
    refreshing = true
    statusProcess.command = root.wrappedCommand(["status"], statusSuperviseSec)
    statusProcess.running = true
  }

  function applyStatus(raw) {
    var parsed = Model.parseStatus(raw)
    ready = parsed.ready === true
    running = parsed.running === true
    mounted = parsed.mounted === true
    rcloneFound = parsed.rcloneFound === true
    remoteConfigured = parsed.remoteConfigured === true
    hasSession = parsed.hasSession === true
    needsLogin = parsed.needsLogin === true
    gvfsDav = parsed.gvfsDav === true
    statusText = String(parsed.statusText || (running ? "Connected" : "Stopped"))
    remote = String(parsed.remote || "icloud:")
    uri = String(parsed.uri || "dav://ff-drive@iCloud.localhost:8080/")
    displayName = String(parsed.displayName || "iCloud Drive")
    cacheMaxSize = String(parsed.cacheMaxSize || settingCacheMaxSize)
    cacheMaxAge = String(parsed.cacheMaxAge || (settingCacheMaxAgeHours + "h"))
    cacheUsedBytes = Number(parsed.cacheUsedBytes || 0)
    lastError = String(parsed.lastError || "")
    if (_desired !== -1 && running === (_desired === 1)) _desired = -1
    // After a reboot the running server has no GVFS mount until something
    // mounts it with the local credential; remount from here so the
    // sidebar bookmark opens without a password prompt. The helper only
    // sends that credential after verifying the listener belongs to the
    // sandboxed service. Bounded retries cover GVFS not being ready yet
    // when the shell loads; a successful mount resets the counter.
    if (root.mounted) root._mountTries = 0
    else if (root.running && !root.busy && root._mountTries < 3) {
      root._mountTries += 1
      root.mountDrive()
    }
  }

  function elideStatus(text) {
    var value = String(text || "").replace(/\s+/g, " ").trim()
    return value.length > 160 ? value.substring(0, 157) + "…" : value
  }

  function runControl(args, desired) {
    if (controlProcess.running) return
    if (desired !== undefined) _desired = desired
    _controlOutput = ""
    _controlError = ""
    _controlOverflow = false
    controlProcess.command = root.wrappedCommand(args, controlSuperviseSec)
    controlProcess.running = true
  }

  function start() { runControl(["start"], 1) }
  function stop() { runControl(["stop"], 0) }
  function toggleRunning() { active ? stop() : start() }
  function openDrive() { runControl(["open"]) }
  function mountDrive() { runControl(["mount"]) }
  function login() { runControl(["login"]) }
  function clearCache() { runControl(["clear-cache"]) }

  function applySettings() {
    var key = configKey()
    if (key === _appliedKey) return
    _appliedKey = key
    runControl([
      "configure",
      "cache_max_size=" + settingCacheMaxSize,
      "cache_max_age_hours=" + settingCacheMaxAgeHours,
      "restart=1"
    ], undefined)
  }

  onSettingCacheMaxSizeChanged: applySettings()
  onSettingCacheMaxAgeHoursChanged: applySettings()

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    id: delayedRefresh
    interval: 800
    repeat: false
    onTriggered: root.refresh()
  }

  Timer {
    id: actionStatusTimer
    interval: 2200
    repeat: false
    onTriggered: root.actionStatus = ""
  }

  // __supervise itself always returns within statusSuperviseSec/
  // controlSuperviseSec plus a couple of seconds of its own cleanup grace;
  // these watchdogs are a backstop for the case the supervisor process
  // itself wedges (a bug, not normal operation), and kill the exact PID
  // Quickshell handed us — no process-group guessing, no PID parsed out
  // of stderr: Quickshell's Process.processId is the real pid, and
  // Process.signal() delivers a real signal to it.
  Timer {
    id: statusWatchdog
    interval: (root.statusSuperviseSec + 6) * 1000
    repeat: false
    running: statusProcess.running
    onTriggered: {
      if (statusProcess.processId) statusProcess.signal(9)
      root.refreshing = false
      root.lastError = "Fast Fruit Drive status timed out"
    }
  }

  Timer {
    id: controlWatchdog
    interval: (root.controlSuperviseSec + 6) * 1000
    repeat: false
    running: controlProcess.running
    onTriggered: {
      if (controlProcess.processId) controlProcess.signal(9)
      root._desired = -1
      root.lastError = "Fast Fruit Drive command timed out"
      root.actionStatus = root.lastError
      actionStatusTimer.restart()
      delayedRefresh.restart()
    }
  }

  Timer {
    id: settleTimer
    property int ticks: 0
    interval: 700
    repeat: true
    running: false
    onTriggered: {
      settleTimer.ticks += 1
      root.refresh()
      if (settleTimer.ticks >= 6) {
        settleTimer.ticks = 0
        settleTimer.running = false
        root._desired = -1
      }
    }
  }

  Process {
    id: statusProcess
    running: false
    command: []
    clearEnvironment: false
    environment: root.baseEnvironment
    stdout: SplitParser { onRead: function(line) { root.collectStatusOut(line) } }
    stderr: SplitParser { onRead: function(line) { root.collectStatusErr(line) } }
    onExited: function(exitCode) {
      root.refreshing = false
      if (root._statusOverflow) {
        root._statusOverflow = false
        root.lastError = "Fast Fruit Drive status output exceeded safety limit"
        return
      }
      if (exitCode === 0) root.applyStatus(_statusOutput)
      else root.lastError = root.elideStatus(_statusError || _statusOutput || "Could not read Fast Fruit Drive status")
    }
  }

  Process {
    id: controlProcess
    running: false
    command: []
    clearEnvironment: false
    environment: root.baseEnvironment
    stdout: SplitParser { onRead: function(line) { root.collectControlOut(line) } }
    stderr: SplitParser { onRead: function(line) { root.collectControlErr(line) } }
    onExited: function(exitCode) {
      if (root._controlOverflow) {
        root._controlOverflow = false
        root._desired = -1
        root.lastError = "Fast Fruit Drive command output exceeded safety limit"
        root.actionStatus = root.lastError
        actionStatusTimer.restart()
        settleTimer.ticks = 0
        settleTimer.restart()
        delayedRefresh.restart()
        return
      }
      var stdout = root._controlOutput
      var stderr = root._controlError
      if (exitCode !== 0) {
        root._desired = -1
        root.lastError = root.elideStatus(stderr || stdout || "Fast Fruit Drive command failed")
        root.actionStatus = root.lastError
        actionStatusTimer.restart()
      } else {
        root.lastError = ""
        var note = root.elideStatus(stdout)
        root.actionStatus = note
        if (note !== "") actionStatusTimer.restart()
      }
      settleTimer.ticks = 0
      settleTimer.restart()
      delayedRefresh.restart()
    }
  }
}
