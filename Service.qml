import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

Item {
  id: root

  property var settings: ({})

  property bool ready: false
  property bool running: false
  property bool mounted: false
  property bool readOnly: true
  property bool rcloneFound: false
  property bool remoteConfigured: false
  property bool hasSession: false
  property bool needsLogin: false
  property bool gvfsDav: false
  property bool refreshing: false
  property string statusText: "Checking…"
  property string remote: "icloud:"
  property string uri: "dav://127.0.0.1:8080/"
  property string displayName: "iCloud Drive"
  property string cacheMaxSize: "4G"
  property string cacheMaxAge: "24h"
  property double cacheUsedBytes: 0
  property string lastError: ""
  property string actionStatus: ""

  property int _desired: -1
  readonly property bool active: _desired === -1 ? running : (_desired === 1)

  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 15, 5, 3600)
  readonly property string settingReadOnly: String(setting("readOnly", "On"))
  readonly property string settingCacheMaxSize: String(setting("cacheMaxSize", "4G"))
  readonly property int settingCacheMaxAgeHours: intSetting("cacheMaxAgeHours", 24, 1, 168)
  readonly property bool busy: statusProcess.running || controlProcess.running
  readonly property string helperPath: helperFromUrl(Qt.resolvedUrl("bin/fast-fruit-drive"))

  property string _statusOutput: ""
  property string _statusError: ""
  property string _controlOutput: ""
  property string _controlError: ""
  property string _appliedKey: ""

  function helperFromUrl(url) {
    var value = String(url || "")
    if (value.indexOf("file://") === 0) value = value.substring(7)
    return decodeURIComponent(value)
  }

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
    return settingReadOnly + "|" + settingCacheMaxSize + "|" + settingCacheMaxAgeHours
  }

  function refresh() {
    if (statusProcess.running) return
    _statusOutput = ""
    _statusError = ""
    refreshing = true
    statusProcess.command = [helperPath, "status"]
    statusProcess.running = true
  }

  function applyStatus(raw) {
    var parsed = Model.parseStatus(raw)
    ready = parsed.ready === true
    running = parsed.running === true
    mounted = parsed.mounted === true
    readOnly = parsed.readOnly !== false
    rcloneFound = parsed.rcloneFound === true
    remoteConfigured = parsed.remoteConfigured === true
    hasSession = parsed.hasSession === true
    needsLogin = parsed.needsLogin === true
    gvfsDav = parsed.gvfsDav === true
    statusText = String(parsed.statusText || (running ? "Connected" : "Stopped"))
    remote = String(parsed.remote || "icloud:")
    uri = String(parsed.uri || "dav://127.0.0.1:8080/")
    displayName = String(parsed.displayName || "iCloud Drive")
    cacheMaxSize = String(parsed.cacheMaxSize || settingCacheMaxSize)
    cacheMaxAge = String(parsed.cacheMaxAge || (settingCacheMaxAgeHours + "h"))
    cacheUsedBytes = Number(parsed.cacheUsedBytes || 0)
    lastError = String(parsed.lastError || "")
    if (_desired !== -1 && running === (_desired === 1)) _desired = -1
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
    controlProcess.command = [helperPath].concat(args)
    controlProcess.running = true
  }

  function start() { runControl(["start"], 1) }
  function stop() { runControl(["stop"], 0) }
  function toggleRunning() { active ? stop() : start() }
  function openDrive() { runControl(["open"]) }
  function login() { runControl(["login"]) }
  function setReadOnly(on) {
    runControl(["configure", "read_only=" + (on ? "true" : "false"), "restart=1"])
  }

  function applySettings() {
    var key = configKey()
    if (key === _appliedKey) return
    _appliedKey = key
    runControl([
      "configure",
      "cache_max_size=" + settingCacheMaxSize,
      "cache_max_age=" + settingCacheMaxAgeHours + "h",
      "restart=1"
    ])
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
    stdout: StdioCollector { id: statusStdout; waitForEnd: true; onStreamFinished: root._statusOutput = text }
    stderr: StdioCollector { id: statusStderr; waitForEnd: true; onStreamFinished: root._statusError = text }
    onExited: function(exitCode) {
      root.refreshing = false
      var stdout = String(statusStdout.text || root._statusOutput || "")
      var stderr = String(statusStderr.text || root._statusError || "")
      if (exitCode === 0) root.applyStatus(stdout)
      else root.lastError = root.elideStatus(stderr || stdout || "Could not read Fruit Drive status")
    }
  }

  Process {
    id: controlProcess
    running: false
    command: []
    stdout: StdioCollector { id: controlStdout; waitForEnd: true; onStreamFinished: root._controlOutput = text }
    stderr: StdioCollector { id: controlStderr; waitForEnd: true; onStreamFinished: root._controlError = text }
    onExited: function(exitCode) {
      var stdout = String(controlStdout.text || root._controlOutput || "")
      var stderr = String(controlStderr.text || root._controlError || "")
      if (exitCode !== 0) {
        root._desired = -1
        root.lastError = root.elideStatus(stderr || stdout || "Fruit Drive command failed")
        root.actionStatus = root.lastError
        actionStatusTimer.restart()
      } else {
        root.lastError = ""
        root.actionStatus = ""
      }
      settleTimer.ticks = 0
      settleTimer.restart()
      delayedRefresh.restart()
    }
  }
}
