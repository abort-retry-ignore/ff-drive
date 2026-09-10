import QtQuick
import QtQuick.Shapes
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

  readonly property real w: width
  readonly property real h: height
  readonly property real stroke: Math.max(1.4, iconSize * 0.09)

  Shape {
    anchors.fill: parent
    antialiasing: true
    layer.enabled: true
    layer.samples: 4
    preferredRendererType: Shape.CurveRenderer

    // Two-lobed crown, round belly, no bite. Inset so the stroke stays inside.
    ShapePath {
      fillColor: "transparent"
      strokeColor: root.color
      strokeWidth: root.stroke
      capStyle: ShapePath.RoundCap
      joinStyle: ShapePath.RoundJoin
      startX: root.w * 0.50
      startY: root.h * 0.34
      PathCubic {
        x: root.w * 0.88; y: root.h * 0.50
        control1X: root.w * 0.72; control1Y: root.h * 0.16
        control2X: root.w * 0.94; control2Y: root.h * 0.30
      }
      PathCubic {
        x: root.w * 0.50; y: root.h * 0.90
        control1X: root.w * 0.84; control1Y: root.h * 0.74
        control2X: root.w * 0.68; control2Y: root.h * 0.90
      }
      PathCubic {
        x: root.w * 0.12; y: root.h * 0.50
        control1X: root.w * 0.32; control1Y: root.h * 0.90
        control2X: root.w * 0.16; control2Y: root.h * 0.74
      }
      PathCubic {
        x: root.w * 0.50; y: root.h * 0.34
        control1X: root.w * 0.06; control1Y: root.h * 0.30
        control2X: root.w * 0.28; control2Y: root.h * 0.16
      }
    }

    // Stem
    ShapePath {
      fillColor: "transparent"
      strokeColor: root.color
      strokeWidth: root.stroke
      capStyle: ShapePath.RoundCap
      startX: root.w * 0.50
      startY: root.h * 0.10
      PathLine {
        x: root.w * 0.47
        y: root.h * 0.32
      }
    }

    // Leaf as a single stroke so it stays hollow at bar size.
    ShapePath {
      fillColor: "transparent"
      strokeColor: root.color
      strokeWidth: root.stroke
      capStyle: ShapePath.RoundCap
      startX: root.w * 0.54
      startY: root.h * 0.22
      PathCubic {
        x: root.w * 0.84; y: root.h * 0.08
        control1X: root.w * 0.62; control1Y: root.h * 0.06
        control2X: root.w * 0.74; control2Y: root.h * 0.00
      }
    }
  }
}
