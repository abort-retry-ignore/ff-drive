import QtQuick
import qs.Commons

// Whole apple outline — not Apple Inc's bitten-apple mark.
Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Color.foreground

  width: iconSize
  height: iconSize
  implicitWidth: iconSize
  implicitHeight: iconSize

  Canvas {
    id: canvas
    anchors.fill: parent
    antialiasing: true
    renderTarget: Canvas.Image

    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var s = Math.min(width, height)
      if (s < 2)
        return

      var ox = (width - s) / 2
      var oy = (height - s) / 2
      function X(n) { return ox + s * n }
      function Y(n) { return oy + s * n }

      ctx.strokeStyle = Qt.rgba(root.color.r, root.color.g, root.color.b, root.color.a)
      ctx.lineWidth = Math.max(1, s * 0.07)
      ctx.lineCap = "round"
      ctx.lineJoin = "round"

      // Two-lobed crown, round belly. Inset so the stroke stays inside.
      ctx.beginPath()
      ctx.moveTo(X(0.50), Y(0.30))
      ctx.bezierCurveTo(X(0.70), Y(0.14), X(0.94), Y(0.30), X(0.88), Y(0.56))
      ctx.bezierCurveTo(X(0.84), Y(0.80), X(0.66), Y(0.92), X(0.50), Y(0.92))
      ctx.bezierCurveTo(X(0.34), Y(0.92), X(0.16), Y(0.80), X(0.12), Y(0.56))
      ctx.bezierCurveTo(X(0.06), Y(0.30), X(0.30), Y(0.14), X(0.50), Y(0.30))
      ctx.closePath()
      ctx.stroke()

      ctx.beginPath()
      ctx.moveTo(X(0.50), Y(0.28))
      ctx.quadraticCurveTo(X(0.47), Y(0.14), X(0.54), Y(0.07))
      ctx.stroke()

      ctx.beginPath()
      ctx.moveTo(X(0.54), Y(0.20))
      ctx.quadraticCurveTo(X(0.68), Y(0.08), X(0.82), Y(0.14))
      ctx.stroke()
    }
  }

  onColorChanged: canvas.requestPaint()
  onWidthChanged: canvas.requestPaint()
  onHeightChanged: canvas.requestPaint()
  Component.onCompleted: canvas.requestPaint()
}
