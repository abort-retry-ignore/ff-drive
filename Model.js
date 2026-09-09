function parseStatus(raw) {
  var text = String(raw || "").trim()
  if (text === "") return defaultStatus("No status yet")
  try {
    var parsed = JSON.parse(text)
    if (!parsed || typeof parsed !== "object") return defaultStatus("Invalid status")
    parsed.ok = parsed.ok !== false
    return parsed
  } catch (e) {
    return defaultStatus("Failed to parse status")
  }
}

function defaultStatus(error) {
  return {
    ok: false,
    ready: false,
    running: false,
    mounted: false,
    readOnly: true,
    rcloneFound: false,
    remoteConfigured: false,
    hasSession: false,
    needsLogin: false,
    gvfsDav: false,
    cacheUsedBytes: 0,
    statusText: error || "Unavailable",
    remote: "icloud:",
    addr: "127.0.0.1:8080",
    uri: "dav://127.0.0.1:8080/",
    displayName: "iCloud Drive",
    cacheMaxSize: "4G",
    cacheMaxAge: "24h",
    cachePath: "",
    lastError: error || ""
  }
}

function formatBytes(bytes) {
  var value = Number(bytes || 0)
  if (!isFinite(value) || value <= 0) return "0 B"
  var units = ["B", "KB", "MB", "GB", "TB"]
  var index = 0
  while (value >= 1000 && index < units.length - 1) {
    value = value / 1000
    index++
  }
  var digits = value >= 10 || index === 0 ? 0 : 1
  return value.toFixed(digits) + " " + units[index]
}

function cacheText(usedBytes, maxSize) {
  return formatBytes(usedBytes) + " / " + String(maxSize || "?")
}

function modeText(readOnly) {
  return readOnly ? "Read-only" : "Read & write"
}
