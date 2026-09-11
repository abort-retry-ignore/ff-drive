import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "io.github.abort-retry-ignore.ff-drive"
  ipcTarget: "io.github.abort-retry-ignore.ff-drive"
  manageIpc: false

  property string focusSection: "toggle"
  property bool cursorActive: false
  property bool clearArmed: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color iconColor: drive.active ? foreground : dim
  readonly property color barIconColor: drive.active ? barForeground : Qt.darker(barForeground, 1.55)
  readonly property string toggleHint: drive.active ? "Stop iCloud Drive" : "Start iCloud Drive"
  readonly property bool headerHasCursor: cursorActive && focusSection === "header" && drive.ready

  function ensureCursor() {
    if (!drive.ready) {
      focusSection = "setup"
      return
    }
    if (focusSection !== "header" && focusSection !== "open" && focusSection !== "clear") focusSection = "header"
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    ensureCursor()
    if (dy === 0) return
    if (!drive.ready) {
      focusSection = "setup"
      return
    }
    if (focusSection === "header" && dy > 0) focusSection = "open"
    else if (focusSection === "open" && dy < 0) focusSection = "header"
    else if (focusSection === "open" && dy > 0) focusSection = "clear"
    else if (focusSection === "clear" && dy < 0) focusSection = "open"
  }

  function activateCursor() {
    ensureCursor()
    if (focusSection === "setup") return
    if (focusSection === "header") toggleRunning()
    else if (focusSection === "open") drive.openDrive()
    else if (focusSection === "clear") root.clearPressed()
  }

  function toggleRunning() {
    if (drive.ready && !drive.busy) drive.toggleRunning()
  }

  function clearPressed() {
    if (!drive.ready || drive.busy) return
    if (!clearArmed) {
      clearArmed = true
      clearArmTimer.restart()
      return
    }
    clearArmed = false
    clearArmTimer.stop()
    drive.clearCache()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    if (!opened) {
      clearArmed = false
      clearArmTimer.stop()
      return
    }
    cursorActive = false
    drive.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Timer {
    id: clearArmTimer
    interval: 5000
    repeat: false
    onTriggered: root.clearArmed = false
  }

  Service {
    id: drive
    settings: root.settings
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { drive.refresh(); return "ok" }
    function start(): string { drive.start(); return "ok" }
    function stop(): string { drive.stop(); return "ok" }
    function files(): string { drive.openDrive(); return "ok" }
    function login(): string { drive.login(); return "ok" }
    function clearcache(): string { drive.clearCache(); return "ok" }
    function status(): string { return drive.statusText }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    iconComponent: Component {
      Item {
        FruitIcon {
          anchors.centerIn: parent
          iconSize: parent.height
          color: root.barIconColor
          opacity: drive.active ? 1.0 : 0.6
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) drive.refresh()
      else if (buttonCode === Qt.MiddleButton) drive.openDrive()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(420))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") drive.refresh()
        else if (t === "o" || t === "O") drive.openDrive()
        else if (t === "p" || t === "P") root.toggleRunning()
        else if (t === "i" || t === "I") drive.login()
        else if (t === "c" || t === "C") root.clearPressed()
      }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(12)

        Item {
          id: header
          width: parent.width
          implicitHeight: hero.implicitHeight
          readonly property bool ringVisible: root.headerHasCursor
          function focusHero() {
            root.cursorActive = true
            root.focusSection = "header"
          }

          PanelHero {
            id: hero
            width: parent.width
            title: "Fruit Drive"
            meta: drive.statusText
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: drive.active ? 1.0 : 0.5
            iconComponent: Component {
              FruitIcon {
                iconSize: Style.font.display
                color: root.iconColor
              }
            }
            trailingControl: Component {
              ToggleSwitch {
                id: powerSwitch
                visible: drive.ready
                checked: drive.active
                busy: drive.busy
                hasCursor: header.ringVisible
                foreground: hero.foreground
                onHovered: function(on) { if (on) header.focusHero() }
                onToggled: root.toggleRunning()

                PanelToolTip {
                  visible: powerSwitch.containsMouse
                  text: root.toggleHint
                  fontFamily: hero.fontFamily
                }
              }
            }
          }
        }

        Text {
          textFormat: Text.PlainText
          visible: drive.actionStatus !== "" || drive.lastError !== ""
          width: parent.width
          text: drive.actionStatus !== "" ? drive.actionStatus : drive.lastError
          color: drive.lastError !== "" && drive.actionStatus === "" ? root.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }

        Column {
          visible: !drive.ready
          width: parent.width
          spacing: Style.space(8)

          Text {
            width: parent.width
            text: setupText()
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
            function setupText() {
              if (!drive.rcloneFound) return "Install rclone and put it on PATH."
              if (drive.needsLogin) return "Sign in with your Apple ID password and 2FA. rclone keeps a trust token for about 30 days, then you sign in again. App-specific passwords are not accepted."
              if (!drive.gvfsDav) return "Install gvfs-dnssd so Nautilus can speak WebDAV."
              return "Fruit Drive is not ready yet."
            }
          }
        }

        CursorSurface {
          id: loginRow
          visible: drive.needsLogin
          width: parent.width
          hasCursor: root.cursorActive && root.focusSection === "login"
          foreground: root.foreground
          implicitHeight: loginInner.implicitHeight + Style.spacing.rowPaddingX

          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onEntered: { root.cursorActive = true; root.focusSection = "login" }
            onClicked: drive.login()
          }

          RowLayout {
            id: loginInner
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.space(10)
            anchors.rightMargin: Style.space(10)
            spacing: Style.space(8)
            Text {
              text: "󰌆"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.icon
            }
            ColumnLayout {
              Layout.fillWidth: true
              spacing: Style.space(1)
              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: drive.remoteConfigured ? "Reconnect iCloud" : "Sign in to iCloud"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }
              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: "Apple ID + 2FA in a terminal"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
            PanelActionButton {
              iconText: "󰌋"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: drive.login()
            }
          }
        }

        Column {
          visible: drive.ready
          width: parent.width
          spacing: Style.spacing.labelGap
          InfoPair { label: "Shown as"; value: drive.displayName }
          InfoPair { label: "Cache"; value: Model.cacheText(drive.cacheUsedBytes, drive.cacheMaxSize) }
          InfoPair { label: "Remote"; value: drive.remote }
        }

        Item {
          visible: drive.ready
          width: parent.width
          implicitHeight: roRow.implicitHeight

          RowLayout {
            id: roRow
            width: parent.width
            spacing: Style.space(8)
            Text {
              textFormat: Text.PlainText
              text: "Read-only"
              color: root.foreground
              opacity: 0.6
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              Layout.fillWidth: true
            }
            ToggleSwitch {
              id: roSwitch
              checked: drive.readOnly
              busy: drive.busy
              foreground: root.foreground
              onToggled: drive.setReadOnly(!drive.readOnly)
            }
          }
        }

        PanelSeparator {
          visible: drive.ready
          foreground: root.foreground
        }

        CursorSurface {
          id: openRow
          visible: drive.ready
          width: parent.width
          hasCursor: root.cursorActive && root.focusSection === "open"
          foreground: root.foreground
          implicitHeight: openInner.implicitHeight + Style.spacing.rowPaddingX

          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onEntered: {
              root.cursorActive = true
              root.focusSection = "open"
            }
            onClicked: drive.openDrive()
          }

          RowLayout {
            id: openInner
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.space(10)
            anchors.rightMargin: Style.space(10)
            spacing: Style.space(8)

            Text {
              text: "󰉋"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.icon
              Layout.alignment: Qt.AlignVCenter
            }

            ColumnLayout {
              Layout.fillWidth: true
              spacing: Style.space(1)
              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: "Open in Nautilus"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }
              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: drive.displayName
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            PanelActionButton {
              iconText: "󰁔"
              foreground: root.foreground
              fontFamily: root.fontFamily
              Layout.alignment: Qt.AlignVCenter
              onClicked: drive.openDrive()
            }
          }
        }

        CursorSurface {
          id: clearRow
          visible: drive.ready
          width: parent.width
          hasCursor: root.cursorActive && root.focusSection === "clear"
          foreground: root.foreground
          implicitHeight: clearInner.implicitHeight + Style.spacing.rowPaddingX

          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onEntered: {
              root.cursorActive = true
              root.focusSection = "clear"
            }
            onClicked: root.clearPressed()
          }

          RowLayout {
            id: clearInner
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.space(10)
            anchors.rightMargin: Style.space(10)
            spacing: Style.space(8)

            Text {
              text: "󰩺"
              color: root.clearArmed ? root.urgent : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.icon
              Layout.alignment: Qt.AlignVCenter
            }

            ColumnLayout {
              Layout.fillWidth: true
              spacing: Style.space(1)
              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: root.clearArmed ? "Click again to clear" : "Clear local cache"
                color: root.clearArmed ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }
              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: root.clearArmed
                  ? "Interrupts transfers, discards pending uploads"
                  : Model.cacheText(drive.cacheUsedBytes, drive.cacheMaxSize)
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            PanelActionButton {
              iconText: "󰩺"
              foreground: root.clearArmed ? root.urgent : root.foreground
              fontFamily: root.fontFamily
              Layout.alignment: Qt.AlignVCenter
              onClicked: root.clearPressed()
            }
          }
        }
      }
    }
  }

  component InfoPair: Row {
    property string label: ""
    property string value: ""
    width: parent.width
    spacing: Style.space(8)
    Text {
      textFormat: Text.PlainText
      text: label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
    Item {
      width: Math.max(0, parent.width - parent.children[0].implicitWidth - parent.children[2].implicitWidth - parent.spacing * 2)
      height: 1
    }
    Text {
      textFormat: Text.PlainText
      text: value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
    }
  }
}
